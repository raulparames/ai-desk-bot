"""The futures desk's safety net, offline: sizing by risk, the re-check at the open, the brakes, a feed that
stops answering, time without watching (sleep) and one position failing without taking the others with it."""

import json
import time
from datetime import datetime, timezone

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


# ---------------------------------------------------------------- what the second review found
def test_no_entry_while_the_price_thread_is_not_turning(desk):
    ready(desk)
    desk._loop_at = time.time() - 600                     # it froze ten minutes ago and has not come back to say so
    desk.execute_best()
    assert not desk.d.positions and len(desk.ready) == 1 and not desk.calls       # the signal waits, no book is read
    desk._loop_at = time.time()
    desk.execute_best()
    assert len(desk.d.positions) == 1


def test_no_entry_while_prices_are_not_arriving(desk):
    put(desk, 1, "BBB")
    for _ in range(3):
        desk.mids = None
        desk.risk()
    assert desk.d.feed_stale
    ready(desk)
    desk.execute_best()
    assert len(desk.d.positions) == 1 and len(desk.ready) == 1        # nobody could watch its stop
    tick(desk, BBB=100.5)
    desk.execute_best()
    assert len(desk.d.positions) == 2


def test_no_entry_once_the_session_is_asked_to_stop(desk):
    ready(desk)
    desk.quit.set()
    desk.execute_best()
    assert not desk.d.positions and len(desk.ready) == 1 and not desk.calls


@pytest.mark.parametrize("meanwhile, why", [
    (lambda d: setattr(d.d, "cash", d.d.cash - 11.0), "travão ligado enquanto se preparava a entrada"),
    (lambda d: setattr(d, "_loop_at", time.time() - 600), "computador esteve parado: entradas em pausa"),
    (lambda d: setattr(d, "no_new_until", time.time() + 300), "computador esteve parado: entradas em pausa"),
    (lambda d: setattr(d.d, "feed_stale", True), "sem preços da Hyperliquid: entradas em pausa"),
    (lambda d: d.quit.set(), "sessão a parar"),
])
def test_what_comes_while_the_book_is_read_still_stops_the_entry(desk, monkeypatch, meanwhile, why):
    read = desk.info

    def slow(body, session=None, **kw):
        if body["type"] == "l2Book":                      # the brake, the gap, the dead feed or the stop come right here
            meanwhile(desk)
        return read(body, session, **kw)
    monkeypatch.setattr(desk, "info", slow)
    cash = desk.d.cash
    desk.open("AAA", 1, 1.0, 88, 100.0)
    assert not desk.d.positions and desk.d.cash in (cash, cash - 11.0)
    assert desk.d.events[-1]["verdict"] == "skip" and desk.d.events[-1]["reason"] == why
    assert not any(e["verdict"] == "buy" for e in desk.d.events)


def test_a_gap_whose_candles_did_not_come_is_asked_again_until_they_do(desk):
    q = put(desk, 1)
    q._tick = time.time() - 600
    tick(desk, AAA=101.0)                                 # no candles answered and the price is fine now: nothing known
    assert q in desk.d.positions and q.blind is True and not desk.d.trades
    desk.bars = minute_bars([99.5, 98.0, 99.0, 100.0])    # the next check asks again, and now they come
    tick(desk, AAA=101.0)
    t = desk.d.trades[-1]
    assert q not in desk.d.positions and t["kind"] == "stop" and t["blind"] is True
    assert abs(t["exit"] - 98.5 * (1 - SLIP)) < 1e-9 and "10 min sem vigilância" in t["reason"]


def test_candles_that_never_come_are_given_up_and_the_owner_is_told(desk):
    from deskbot import perpdesk
    q = put(desk, 1)
    q._tick = time.time() - 600

    def asked():
        return sum(c[0] == "candleSnapshot" for c in desk.calls)
    for n in range(perpdesk.REPLAY_TRIES):
        assert asked() == n and not alerts(desk)
        tick(desk, AAA=101.0)
    said = [a for a in alerts(desk) if "sem velas da Hyperliquid" in a]
    assert len(said) == 1 and "10 min sem vigilância" in said[0] and "não se sabe se o stop foi batido" in said[0]
    assert [e["coin"] for e in desk.d.events if e["verdict"] == "alert"] == ["AAA"]
    tick(desk, AAA=101.0)
    tick(desk, AAA=101.0)
    assert asked() == perpdesk.REPLAY_TRIES and len(alerts(desk)) == 1 and q in desk.d.positions


def test_a_gap_whose_candles_show_nothing_is_not_asked_again(desk):
    q = put(desk, 1)
    q._tick = time.time() - 600
    desk.bars = minute_bars([99.5, 99.0, 100.0])
    tick(desk, AAA=101.0)
    tick(desk, AAA=101.0)
    assert sum(c[0] == "candleSnapshot" for c in desk.calls) == 1 and q in desk.d.positions and not alerts(desk)


def test_top_traders_read_long_ago_do_not_back_an_entry(desk):
    desk.crowd = {"AAA": {"long": 5, "short": 0, "long_usd": 5e6, "short_usd": 0.0}}
    desk.crowd_at = time.time()
    assert desk.smarts("AAA", 1) is True
    desk.crowd_at = time.time() - 2 * desk.TRADERS_EVERY_S + 60       # one refresh failed: still the latest there is
    assert desk.smarts("AAA", 1) is True
    desk.crowd_at = time.time() - 2 * desk.TRADERS_EVERY_S - 65       # two failed: what they held then is old news
    assert desk.smarts("AAA", 1) is False
    e = desk.d.events[-1]
    assert e["verdict"] == "reject" and e["reason"] == "sem dados recentes: top traders lidos há 31 min"


def test_a_position_is_watched_from_the_moment_it_opens(desk):
    t0 = time.time()
    desk.open("AAA", 1, 1.0, 88, 100.0)
    q = desk.d.positions[0]
    assert t0 <= q._tick <= time.time()                   # before, the clock only started at its first price
    q._tick -= 600                                        # ten minutes with no price check right after the open
    q.opened = datetime.fromtimestamp(time.time() - 660, timezone.utc).isoformat(timespec="seconds")
    desk.bars = minute_bars([99.5, 98.0, 99.0, 100.0])
    tick(desk, AAA=101.0)
    assert q not in desk.d.positions and desk.d.trades[-1]["kind"] == "stop" and desk.d.trades[-1]["blind"] is True


def test_a_candle_begun_before_the_open_does_not_stop_the_position(desk):
    q = put(desk, 1)                                      # stop at 98.5
    q.opened = datetime.fromtimestamp(time.time() - 150, timezone.utc).isoformat(timespec="seconds")
    q._tick = time.time() - 150
    desk.bars = minute_bars([97.0, 100.0, 100.0], ago_s=200)          # the fall was in the minute the open came into
    tick(desk, AAA=101.0)
    assert q in desk.d.positions and q.blind is True and not desk.d.trades
    q._tick = time.time() - 150
    desk.bars = minute_bars([100.0, 97.0, 100.0], ago_s=200)          # a minute begun after the open does count
    tick(desk, AAA=101.0)
    assert q not in desk.d.positions and abs(desk.d.trades[-1]["exit"] - 98.5 * (1 - SLIP)) < 1e-9


def test_a_session_end_without_prices_says_how_old_the_last_price_is(desk):
    a, b = put(desk, 1, "AAA"), put(desk, 1, "BBB")
    tick(desk, AAA=100.5, BBB=100.7)
    a._tick = time.time() - 600                           # AAA's last price is ten minutes old
    desk.mids = None                                      # and the last reading does not answer
    desk.risk(final=True)
    by = {t["coin"]: t for t in desk.d.trades}
    assert not desk.d.positions and by["AAA"]["kind"] == by["BBB"]["kind"] == "end"
    assert by["AAA"]["blind"] is True and by["AAA"]["reason"] == "fim da sessão (último preço, de há 10 min)"
    assert by["BBB"]["blind"] is False and by["BBB"]["reason"] == "fim da sessão (último preço)"
    assert abs(by["AAA"]["exit"] - 100.5 * (1 - SLIP)) < 1e-9
    assert desk.calls[-1] == ("allMids", {"timeout": 8, "tries": 2})


def test_the_sale_itself_says_when_nobody_was_watching(desk):
    q = put(desk, 1)
    q._tick = time.time() - 600
    desk.bars = minute_bars([99.5, 98.0, 99.0, 100.0])
    tick(desk, AAA=101.0)
    put(desk, 1, "BBB", margin=10.0)
    tick(desk, BBB=98.0)                                  # this one was stopped under watch
    sells = {e["coin"]: e for e in desk.d.events if e["verdict"] == "sell"}
    assert sells["AAA"]["blind"] is True and sells["BBB"]["blind"] is False


def test_a_position_closed_while_the_bridge_wrote_its_open_leaves_no_ghost_order(desk, monkeypatch):
    orders, closes = set(), []

    def on_open(coin, side, lev, notional, ref, stop, paper_fill):
        if coin == "AAA":                                 # the price thread stops it out before the order is written down
            desk.close(desk.d.positions[-1], 98.0, "stop 1.5 ATR", "stop")
        orders.add(coin)

    def on_close(coin, side, ref, kind, pnl_usd):
        closes.append((coin, kind, ref, pnl_usd, coin in orders))
        orders.discard(coin)
    monkeypatch.setattr(desk.live, "on_open", on_open)
    monkeypatch.setattr(desk.live, "on_close", on_close)
    desk.open("AAA", 1, 1.0, 88, 100.0)
    t = desk.d.trades[-1]
    assert not desk.d.positions and not orders            # it was left open in the dry run for ever
    assert closes == [("AAA", "stop", t["exit"], t["pnl_usd"], False), ("AAA", "stop", t["exit"], t["pnl_usd"], True)]
    desk.open("BBB", 1, 1.0, 88, 100.0)                   # a position that stays open gets no close
    assert len(desk.d.positions) == 1 and orders == {"BBB"} and len(closes) == 2


def test_reading_the_top_traders_stops_at_once_when_the_session_is_stopped(desk, monkeypatch):
    rows = [{"ethAddress": f"0x{n}", "accountValue": "2000000",
             "windowPerformances": [["month", {"pnl": "100", "roi": "0.2"}], ["allTime", {"pnl": "100"}]]} for n in range(5)]

    class Board:
        def json(self):
            return {"leaderboardRows": rows}
    monkeypatch.setattr(desk.http, "get", lambda url, timeout=None: Board())
    desk.crowd, desk.crowd_at = {"OLD": {}}, 1.0
    desk.quit.set()
    desk.load_crowd()
    assert not desk.calls and desk.crowd == {"OLD": {}} and desk.crowd_at == 1.0      # not one of the 40 requests
    desk.quit.clear()
    desk.load_crowd()
    assert [c[0] for c in desk.calls] == ["clearinghouseState"] * 5 and desk.crowd == {} and desk.crowd_at > 1.0


def test_the_state_file_carries_the_moment_it_was_written(desk, tmp_path):
    desk.save()
    st = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(st["saved"])).total_seconds()
    assert -1 <= age < 5                                  # the app's "last signal" reads this, not the file's date


@pytest.mark.parametrize("lows, stopped", [
    ([99.9, 99.6, 100.2], False),                         # between the stop of the gap (98.5) and the entry: not hit
    ([99.9, 98.0, 100.2], True),                          # under the stop of the gap: hit, at that stop
])
def test_a_gap_asked_again_after_the_half_is_read_against_the_stop_it_had(desk, lows, stopped):
    q = put(desk, 1)                                      # entry 100, stop 98.5
    q._tick = time.time() - 600
    tick(desk, AAA=103.1)                                 # no candles came, and the price is at +2R: half, stop to the entry
    assert q in desk.d.positions and q.half_done and q.stop == 100.0
    assert [t["kind"] for t in desk.d.trades] == ["half"]
    desk.bars = minute_bars(lows)                         # the next check asks again, and now they come
    tick(desk, AAA=103.1)
    kinds = [t["kind"] for t in desk.d.trades]
    if not stopped:                                       # it said "stop na entrada batido durante 10 min sem vigilância"
        assert q in desk.d.positions and kinds == ["half"] and q._gap is None
        return
    t = desk.d.trades[-1]
    assert q not in desk.d.positions and kinds == ["half", "stop"]
    assert abs(t["exit"] - 98.5 * (1 - SLIP)) < 1e-9 and t["reason"] == "stop 1.5 ATR batido durante 10 min sem vigilância"


def test_a_half_reached_while_the_bridge_wrote_the_open_reaches_the_dry_run_once(desk, monkeypatch):
    orders, said = set(), []

    def on_open(coin, side, lev, notional, ref, stop, paper_fill):
        q = desk.d.positions[-1]
        if coin != "CCC":
            with desk.lock:                               # the price thread: +2R before the order is written down
                desk._half(q, 103.2)
        if coin == "AAA":                                 # and then the rest is closed too
            desk.close(q, 101.0, "trailing 1.5 ATR do máximo", "trail")
        orders.add(coin)

    def on_half(coin, side, ref, new_stop):
        said.append(("half", coin, ref, new_stop, coin in orders))    # the real bridge drops a half of a coin it has not

    def on_close(coin, side, ref, kind, pnl_usd):
        said.append(("close", coin, kind, coin in orders))
        orders.discard(coin)
    for name, fn in (("on_open", on_open), ("on_half", on_half), ("on_close", on_close)):
        monkeypatch.setattr(desk.live, name, fn)
    desk.open("AAA", 1, 1.0, 88, 100.0)
    entry = desk.d.trades[0]["entry"]
    assert not desk.d.positions and not orders
    assert said == [("close", "AAA", "trail", False), ("half", "AAA", 103.2, entry, True), ("close", "AAA", "trail", True)]
    del said[:]
    desk.open("BBB", 1, 1.0, 88, 100.0)                   # the half alone: the position stays open, halved in the dry run too
    q = desk.d.positions[-1]
    assert q.half_done and said == [("half", "BBB", 103.2, q.entry, True)]
    del said[:]
    desk.open("CCC", 1, 1.0, 88, 100.0)                   # a half after the open is told at once, and only once
    c = desk.d.positions[-1]
    assert c.coin == "CCC" and not said
    tick(desk, BBB=103.2, CCC=103.5)
    assert c.half_done and said == [("half", "CCC", 103.5, c.entry, True)]


def test_the_report_counts_a_half_with_its_position_not_as_a_trade_of_its_own(desk):
    put(desk, 1)
    tick(desk, AAA=103.1)                                 # half at +2R
    tick(desk, AAA=101.0)                                 # the rest leaves on the trailing: one winning position, two sales
    put(desk, 1, "BBB")
    tick(desk, BBB=98.0)                                  # and one position stopped out
    assert [t["kind"] for t in desk.d.trades] == ["half", "trail", "stop"]
    rep = desk.report()
    assert "- Trades: 2, 1 a ganhar (50%)" in rep         # it said "3, 2 a ganhar (67%)"
    assert rep.count(" AAA long 5x: ") == 2 and rep.count(" BBB long 5x: ") == 1      # every sale is still listed


def test_a_session_that_ends_with_the_feed_down_does_not_leave_no_prices_on_the_app(desk, tmp_path, monkeypatch):
    from deskbot import guard
    monkeypatch.setattr(guard, "keep_awake", lambda on: True)
    put(desk, 1)
    tick(desk, AAA=100.5)
    desk.mids = None                                      # the feed stops answering
    for _ in range(3):
        desk.risk()
    assert desk.d.feed_stale is True
    desk.quit.set()                                       # "Parar sessão" during the outage
    desk.run()
    st = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert st["finished"] is True and not st["positions"] and st["feed_stale"] is False
