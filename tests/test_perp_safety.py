"""The futures desk's safety net, offline: sizing by risk, the re-check at the open, the brakes, a feed that
stops answering, time without watching (sleep) and one position failing without taking the others with it."""

import time

import pytest

SLIP = 0.0002
BOOK = {"levels": [[{"px": "100.00", "sz": "500"}], [{"px": "100.02", "sz": "500"}]]}   # mid 100.01


@pytest.fixture
def desk(monkeypatch, tmp_path):
    from deskbot import perpdesk
    monkeypatch.setattr(perpdesk, "OUT", tmp_path)
    monkeypatch.setattr(perpdesk, "STOP_FILE", tmp_path / "stop")     # never the real desk's stop file
    monkeypatch.setattr(perpdesk.PerpDesk, "_eur_usd", lambda self: 1.0)
    d = perpdesk.PerpDesk(30, 100, lev=5)
    d.live.mode = "off"
    d.meta = {c: {"max_lev": 10, "funding": 0.0, "mark": 100.0} for c in ("AAA", "BBB", "CCC")}
    d.mids, d.book, d.bars, d.calls = {}, BOOK, None, []

    def info(body, session=None, **kw):
        d.calls.append((body["type"], kw))
        return {"allMids": d.mids if d.mids is None else dict(d.mids), "l2Book": d.book,
                "candleSnapshot": d.bars}.get(body["type"])
    monkeypatch.setattr(d, "info", info)
    return d


def put(desk, side, coin="AAA", entry=100.0, margin=20.0, lev=5, atr=1.0):
    from deskbot import perpdesk
    qty = margin * lev / entry
    fee = qty * entry * 0.00045
    q = perpdesk.Perp(coin.lower(), coin, side, lev, "2026-01-01T00:00:00+00:00", entry, qty, margin, atr,
                      entry - side * 1.5 * atr, entry * (1 - side * (1 / lev - 0.05)), entry, entry, fees=fee)
    desk.d.cash -= margin + fee
    desk.d.positions.append(q)
    return q


def tick(desk, **mids):
    desk.mids = {k: str(v) for k, v in mids.items()}
    desk.risk()


def alerts(desk):
    return [e["reason"] for e in desk.d.events if e["verdict"] == "alert"]


def minute_bars(lows, ago_s=600):
    """One-minute candles as Hyperliquid sends them, the first one `ago_s` seconds ago."""
    t0 = int((time.time() - ago_s) * 1000)
    return [{"t": t0 + i * 60_000, "o": "100", "h": str(max(100.5, lo + 1)), "l": str(lo), "c": "100", "v": "1"}
            for i, lo in enumerate(lows)]


# ---------------------------------------------------------------- sizing by risk
def test_a_stop_out_costs_about_one_percent_and_nervous_coins_get_smaller_positions(desk):
    calm = desk.size("AAA", 1, 1.0, 100.0)                # stop 1.5% away
    assert calm["ok"] and calm["lev"] == 2 and abs(calm["risk"] - 1.0) < 1e-9
    assert abs(calm["notional"] - 1.0 / (0.015 + 0.0013)) < 1e-6
    nervous = desk.size("AAA", 1, 2.0, 100.0)             # stop 3% away: half the size
    assert nervous["lev"] == 2 and nervous["notional"] < calm["notional"] * 0.55
    assert abs(nervous["risk"] - 1.0) < 1e-9
    tight = desk.size("AAA", 1, 0.2, 100.0)               # stop 0.3% away: the cash ceiling decides, not the risk
    assert tight["lev"] == 5 and abs(tight["margin"] - 40.0) < 1e-9 and tight["risk"] < 1.0


def test_leverage_that_puts_liquidation_near_the_stop_is_never_used(desk):
    wide = desk.size("AAA", 1, 6.0, 100.0)                # stop 9% away: 5x would liquidate at 15%
    assert wide["ok"] and wide["lev"] == 2
    assert desk.size("AAA", 1, 16.0, 100.0) == {"ok": False, "why": "stop demasiado perto da liquidação"}
    assert desk.size("AAA", 1, 8.0, 100.0)["why"] == "aposta abaixo do mínimo"
    assert desk.size("AAA", 1, 1.0, 0.0)["why"] == "sem preço para medir o risco"


def test_same_side_is_halved_and_open_risk_is_capped_at_two_percent(desk):
    put(desk, 1, "BBB")                                   # risks 1.5 at its stop
    again = desk.size("AAA", 1, 1.0, 100.0)
    assert again["halved"] and again["risk"] <= 0.5 + 1e-9
    other = desk.size("AAA", -1, 1.0, 100.0)
    assert not other["halved"] and other["risk"] <= 0.5 + 1e-9        # 2% of the equity less the 1.5 already open
    put(desk, -1, "CCC")
    assert desk.size("AAA", 1, 1.0, 100.0)["why"] == "risco aberto no máximo"


def test_a_stop_at_the_entry_frees_the_risk_budget(desk):
    q = put(desk, 1, "BBB", margin=30.0)                  # risks 2.25: nothing else fits
    assert desk.size("AAA", -1, 1.0, 100.0)["ok"] is False
    q.stop = q.entry
    assert desk.open_risk() == 0.0 and desk.size("AAA", -1, 1.0, 100.0)["ok"] is True


# ---------------------------------------------------------------- the re-check at the open
def test_open_uses_the_risk_size_and_says_what_it_risks(desk):
    desk.open("AAA", 1, 1.0, 88, 100.0)
    q = desk.d.positions[0]
    assert q.lev == 2 and q.liq < q.stop < q.entry
    lost_at_stop = q.qty * (q.entry - q.stop)
    assert 0.9 < lost_at_stop < 1.0                       # about 1% of 100, costs included in the budget
    buy = desk.d.events[-1]
    assert buy["verdict"] == "buy" and "arrisca" in buy["reason"] and "40%" not in buy["reason"]


@pytest.mark.parametrize("px0, book, why", [
    (99.4, BOOK, "preço fugiu"),                          # the book is 0.6 ATR above the signal
    (101.2, BOOK, "sinal quebrou"),                       # 1.2 ATR back
    (100.0, {"levels": [[{"px": "99.5", "sz": "500"}], [{"px": "100.5", "sz": "500"}]]}, "livro piorou"),
    (100.0, None, "sem preço no livro"),
])
def test_no_entry_when_the_price_left_the_signal_or_the_book_got_worse(desk, px0, book, why):
    desk.book = book
    desk.open("AAA", 1, 1.0, 88, px0)
    assert not desk.d.positions and desk.d.cash == 100.0
    assert desk.d.events[-1]["verdict"] == "skip" and why in desk.d.events[-1]["reason"]


def test_short_side_measures_the_move_the_other_way(desk):
    desk.open("AAA", -1, 1.0, 88, 100.7)                  # fell 0.69 ATR since the signal: chasing
    assert not desk.d.positions and "preço fugiu" in desk.d.events[-1]["reason"]
    desk.open("AAA", -1, 1.0, 88, 100.2)
    assert len(desk.d.positions) == 1 and desk.d.positions[0].side == -1


# ---------------------------------------------------------------- brakes
def ready(desk, coin="AAA"):
    desk.ready = [{"coin": coin, "side": 1, "score": 90, "since": time.time(), "atr": 1.0, "px": 100.0}]


def test_session_loss_stops_new_entries_but_not_the_exits(desk):
    q = put(desk, 1, "BBB")
    desk.d.cash -= 11.0                                   # the session is 11% down
    ready(desk)
    desk.execute_best()
    assert len(desk.d.positions) == 1 and not desk.guard.ok
    assert sum("perda da sessão" in a for a in alerts(desk)) == 1
    desk.execute_best()
    assert sum("perda da sessão" in a for a in alerts(desk)) == 1     # said once
    tick(desk, BBB=98.0)
    assert q not in desk.d.positions and desk.d.trades[-1]["kind"] == "stop"


def test_three_stops_in_a_row_pause_entries_and_the_losing_coin_rests(desk):
    for coin in ("AAA", "BBB", "CCC"):
        put(desk, 1, coin, margin=10.0)                   # each stop costs about 1%: under the day brake
        tick(desk, **{coin: 98.0})
        assert desk.cool[coin] > time.time() + 7000       # two hours, not the 30 minutes of a normal look
    assert desk.guard.streak == 3 and desk.equity() > 96
    ready(desk)
    desk.execute_best()
    assert not desk.d.positions and any("3 perdas seguidas" in a for a in alerts(desk))
    desk.save()
    assert desk.d.guard["on"] is True and desk.d.guard["kind"] == "streak"


def test_a_winner_does_not_put_the_coin_to_rest(desk):
    q = put(desk, 1)
    tick(desk, AAA=103.1)                                 # half at 2R
    tick(desk, AAA=100.0)                                 # the rest stopped at the entry: the position won
    assert q not in desk.d.positions and desk.guard.streak == 0 and "AAA" not in desk.cool


# ---------------------------------------------------------------- feed health
def test_three_readings_without_prices_raise_the_alarm_once_and_recovery_is_said(desk):
    q = put(desk, 1)
    for n in range(4):
        desk.mids = None
        desk.risk()
        assert desk.d.feed_stale is (n >= 2)
    assert sum("sem preços da Hyperliquid" in a for a in alerts(desk)) == 1
    assert desk.calls[-1] == ("allMids", {"timeout": 4, "tries": 1})  # a slow answer cannot hold the stops
    tick(desk, AAA=100.5)
    assert desk.d.feed_stale is False and "de volta" in alerts(desk)[-1] and q in desk.d.positions
    tick(desk, AAA=100.5)
    assert len(alerts(desk)) == 2


def test_the_real_info_gives_up_at_once_when_asked_to(desk, monkeypatch):
    from deskbot import perpdesk

    class Busy:
        status_code, n = 429, 0

        def post(self, url, json=None, timeout=None):
            Busy.n += 1
            Busy.timeout = timeout
            return self
    naps = []
    monkeypatch.setattr(perpdesk.time, "sleep", naps.append)
    assert perpdesk.PerpDesk.info(desk, {"type": "allMids"}, Busy(), timeout=4, tries=1) is None
    assert Busy.n == 1 and Busy.timeout == 4 and naps == [] and desk.d.rate_limited == 1


# ---------------------------------------------------------------- time without watching
def test_a_stop_hit_while_nobody_watched_is_booked_at_the_stop_not_at_todays_better_price(desk):
    q = put(desk, 1)
    q._tick = time.time() - 600
    desk.bars = minute_bars([99.5, 98.0, 99.0, 100.0])
    tick(desk, AAA=101.0)                                 # the price is back above the entry now
    t = desk.d.trades[-1]
    assert q not in desk.d.positions and t["kind"] == "stop" and t["blind"] is True
    assert abs(t["exit"] - 98.5 * (1 - SLIP)) < 1e-9 and t["pnl_usd"] < 0
    assert "sem vigilância" in t["reason"] and "10 min" in t["reason"]


def test_a_liquidation_inside_the_gap_wins_over_the_stop(desk):
    q = put(desk, 1)                                      # liquidation at 85
    q._tick = time.time() - 600
    desk.bars = minute_bars([99.0, 84.0, 99.0])
    tick(desk, AAA=101.0)
    t = desk.d.trades[-1]
    assert t["kind"] == "liq" and abs(t["pnl_usd"] + 20.0 + q.fees) < 1e-9 and "liquidada durante" in t["reason"]


def test_short_side_reads_the_highs_and_old_candles_do_not_count(desk):
    q = put(desk, -1)                                     # stop at 101.5
    q._tick = time.time() - 300
    old = minute_bars([100.0], ago_s=3600)
    old[0]["h"] = "150"                                   # an hour ago: before the gap
    desk.bars = old + minute_bars([100.0, 100.0], ago_s=240)
    tick(desk, AAA=100.0)
    assert q in desk.d.positions and q.blind is True
    q._tick = time.time() - 300
    spike = minute_bars([100.0, 100.0], ago_s=240)
    spike[1]["h"] = "101.6"
    desk.bars = spike
    tick(desk, AAA=100.0)
    assert q not in desk.d.positions and abs(desk.d.trades[-1]["exit"] - 101.5 * (1 + SLIP)) < 1e-9


def test_without_candles_the_gap_falls_back_to_the_price_seen_now(desk):
    q = put(desk, 1)
    q._tick = time.time() - 600
    tick(desk, AAA=98.0)                                  # no candles answered: the normal check still stops it
    assert desk.d.trades[-1]["kind"] == "stop" and desk.d.trades[-1]["blind"] is True
    assert desk.d.trades[-1]["reason"] == "stop 1.5 ATR"


def test_a_jump_of_the_clock_is_said_pauses_entries_and_drops_old_signals(desk):
    put(desk, 1, "BBB")
    t0 = time.time() - 1000
    desk._gap(t0)
    desk._gap(t0 + 5)
    assert not alerts(desk) and desk.d.blind_s == 0
    desk._gap(t0 + 905)                                   # 15 minutes asleep
    assert desk.d.blind_s == 900 and desk.d.max_gap_s == 900 and desk.stale.is_set()
    assert "15 min" in alerts(desk)[0] and "1 posição esteve sem stop" in alerts(desk)[0]
    ready(desk)
    desk.queue.append(("CCC", 1, {}))
    desk.execute_best()
    assert not desk.ready and not desk.queue and len(desk.d.positions) == 1
    desk.no_new_until = time.time() + 300
    ready(desk)
    desk.execute_best()
    assert len(desk.d.positions) == 1 and len(desk.ready) == 1        # still paused, the signal waits
    desk.no_new_until = 0.0
    desk.execute_best()
    assert len(desk.d.positions) == 2


# ---------------------------------------------------------------- one position failing
def test_a_position_whose_check_fails_does_not_leave_the_next_one_without_its_stop(desk):
    a, b = put(desk, 1, "AAA"), put(desk, 1, "BBB")
    a.series = None                                       # its check will blow up
    tick(desk, AAA=98.0, BBB=98.0)
    assert b not in desk.d.positions and desk.d.trades[-1]["coin"] == "BBB"
    assert a in desk.d.positions and desk.d.risk_errors == 1 and not alerts(desk)
    tick(desk, AAA=98.0)
    tick(desk, AAA=98.0)
    assert desk.d.risk_errors == 3 and "falhou 3 vezes" in alerts(desk)[-1]
    a.series = []
    tick(desk, AAA=98.0)
    assert a not in desk.d.positions


def test_a_broken_dry_run_bridge_does_not_stop_a_close(desk, monkeypatch):
    q = put(desk, 1)
    monkeypatch.setattr(desk.live, "on_close", lambda *a: 1 / 0)
    tick(desk, AAA=98.0)
    assert q not in desk.d.positions and desk.d.trades[-1]["kind"] == "stop"
    desk.save()                                           # and the state still gets written


def test_the_stop_button_is_seen_by_the_price_thread(desk, tmp_path, monkeypatch):
    (tmp_path / "stop").write_text("x", encoding="utf-8")
    monkeypatch.setattr(desk, "save", lambda final=False: desk.stop.set())   # one turn of the loop
    desk.risk_loop()
    assert desk.quit.is_set()
