"""The memes desk's safety net, offline: sizing, the brakes, honest fills, a feed that stops answering, time
without watching, one failure that must not take the rest with it, the wide scan's first rug filter, and
the cases a review found (section H)."""

import json
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest
import requests

SOL = 150.0
OLD = "2026-01-01T00:00:00+00:00"


class NoNetwork:
    """Stands where a requests session would: a call from a test is a bug in the test."""

    def get(self, *args, **kw):
        raise AssertionError("a test reached for the network")

    post = get


def build(monkeypatch, tmp_path, eur=100, style="poucas"):
    from deskbot import guard, memedesk, memetest
    monkeypatch.setattr(memetest, "OUT", tmp_path)
    monkeypatch.setattr(memetest, "STOP_FILE", tmp_path / "stop")     # never the real desk's stop file
    monkeypatch.setattr(memetest.MemeTest, "_eur_usd", lambda self: 1.0)
    monkeypatch.setattr(guard, "keep_awake", lambda on: True)
    d = memedesk.MemesDesk(30, eur, style=style)
    d.gmgn.key = ""                       # no GMGN calls, whatever is in the environment
    d.http = d.http_risk = NoNetwork()
    d._sol = (SOL, time.time())
    d.live = {}                           # pool -> what DexScreener answers
    d.jup = {}                            # mint -> Jupiter's price, the second source
    d.market = {}                         # mint -> the price Jupiter's quotes are filled at
    d.sell = "ok"                         # what a sell quote answers: "ok", "no_route" or "unavailable"
    d.asked = []                          # (feed, keyword arguments) of every call

    def pairs(pools, session=None, **kw):
        d.asked.append(("pairs", kw))
        return {p: d.live[p] for p in pools if p in d.live}

    def buy_quote(a, b, amount, session=None):
        px = d.market.get(b, 0)
        if not px:
            return None
        return {"outAmount": str(int(amount / 1e9 * SOL / px * 1e6)), "priceImpactPct": "0.001", "routePlan": []}

    def sell_quote(a, b, amount, session=None, timeout=8):
        d.asked.append(("sell", {"timeout": timeout}))
        px = d.market.get(a, 0)
        if d.sell != "ok" or not px:
            return ("no_route" if d.sell == "ok" else d.sell), None
        return "ok", {"outAmount": str(int(amount / 1e6 * px / SOL * 1e9))}

    monkeypatch.setattr(d, "_pairs", pairs)
    monkeypatch.setattr(d, "_jup_prices", lambda mints: {m: d.jup[m] for m in mints if m in d.jup})
    monkeypatch.setattr(d, "_refresh_sol", lambda: None)
    monkeypatch.setattr(d, "sol_usd", lambda: SOL)
    monkeypatch.setattr(d, "quote", buy_quote)
    monkeypatch.setattr(d, "_quote", sell_quote)
    monkeypatch.setattr(d, "_gecko", lambda path, params=None: None)
    monkeypatch.setattr(d, "_mint_info", lambda mint: None)
    return d


@pytest.fixture
def desk(monkeypatch, tmp_path):
    return build(monkeypatch, tmp_path)


def put(desk, pool="A", cost=15.0, liq=100_000.0):
    """An open position bought at $1.00: `cost` tokens for `cost` dollars, pool liquidity `liq`."""
    from deskbot import memetest
    p = memetest.Position(pool.lower(), "$" + pool, pool, pool, "MINT" + pool, OLD, 1.0, 1.0, cost, cost, liq, 1.0, 1.0,
                          liq0=liq)
    desk.d.cash -= cost
    desk.d.positions.append(p)
    desk.market[p.mint] = 1.0
    return p


def tick(desk, final=False, **readings):
    """One turn of RISK. readings: pool=(price, liquidity); a pool left out gets no answer from DexScreener."""
    desk.live = {pool: {"priceUsd": str(px), "liquidity": {"usd": liq}} for pool, (px, liq) in readings.items()}
    for pool, (px, _) in readings.items():
        if px > 0:
            desk.market["MINT" + pool] = px
    desk.risk(final)


def gecko_pool(addr, mint, name="DEEP"):
    return {"attributes": {"address": addr, "name": f"{name} / SOL", "reserve_in_usd": "250000",
                           "base_token_price_usd": "0.01", "pool_created_at": OLD,
                           "transactions": {"h1": {"buyers": 300, "buys": 900, "sells": 400}},
                           "volume_usd": {"m5": 9000, "h1": 90000}, "price_change_percentage": {"m5": -2, "h1": 10}},
            "relationships": {"base_token": {"data": {"id": "solana_" + mint}}, "dex": {"data": {"id": "raydium"}}}}


def on_the_market(desk, addr, mint):
    desk.live[addr] = {"priceUsd": "0.01", "liquidity": {"usd": 250_000}, "volume": {"m5": 9_000, "h1": 90_000},
                       "priceChange": {"m5": -2, "h1": 10}}
    desk.market[mint] = 0.01


def buy(desk, addr, mint, name="DEEP"):
    """EXEC is handed a coin the other agents agreed on; it trades at $0.01 in a deep pool."""
    on_the_market(desk, addr, mint)
    desk.executor(addr, gecko_pool(addr, mint, name), "$" + name, mint, 80)
    return next((p for p in desk.d.positions if p.pool == addr), None)


def agreed(desk, addr, mint, name="NEW"):
    """A coin waiting in EXEC's queue."""
    on_the_market(desk, addr, mint)
    return {"score": 80, "since": datetime.now(timezone.utc).isoformat(timespec="seconds"), "addr": addr,
            "pool": gecko_pool(addr, mint, name), "coin": "$" + name, "mint": mint, "decimals": 6, "route": {}}


def everyone_agrees(desk, monkeypatch):
    """SMARTS and DEBATE say yes to whatever they are shown (whales buying, clean coin)."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    whales = [{"attributes": {"block_timestamp": now, "kind": "buy", "tx_from_address": f"w{i}",
                              "volume_in_usd": "900"}} for i in range(4)]
    info = {"data": {"attributes": {"holders": {"distribution_percentage": {"top_10": "12"}}, "is_honeypot": "no"}}}
    monkeypatch.setattr(desk, "_gecko", lambda path, params=None: {"data": whales} if "/trades" in path else info)
    monkeypatch.setattr(desk, "_mint_info", lambda mint: {"mintAuthority": None, "freezeAuthority": None, "decimals": 6})


def reasons(desk, verdict):
    return [e["reason"] for e in desk.d.events if e["verdict"] == verdict]


def alerts(desk):
    return reasons(desk, "alert")


def lock_is_free(desk):
    """Asked from another thread, because the desk's lock is re-entrant."""
    got = []

    def ask():
        if desk.lock.acquire(timeout=1):
            desk.lock.release()
            got.append(True)
    t = threading.Thread(target=ask)
    t.start()
    t.join()
    return got == [True]


# ---------------------------------------------------------------- A. sizing
def test_a_buy_is_15_percent_of_the_desk_and_the_app_says_so(desk):
    assert desk.trade_size(10_000_000) == 15.0            # it was 40.0: 40% of the free cash
    pos = buy(desk, "POOL1", "MINT1")
    assert pos is not None and abs(pos.cost_usd - 15.0) < 1e-9 and abs(desk.d.cash - 85.0) < 1e-9
    said = reasons(desk, "buy")[0]
    assert "15% da mesa" in said and "saldo livre" not in said


def test_free_cash_and_the_pool_are_ceilings_on_a_buy(desk):
    assert desk._sizing(500) == (10.0, "teto de 2% da liquidez da pool")      # 2% of a $500 pool
    put(desk, "A", cost=80.0)                                                 # $20 of cash left
    size, rule = desk._sizing(10_000_000)
    assert abs(size - 8.0) < 1e-9 and rule == "teto de 40% do dinheiro livre"


def test_a_third_buy_is_refused_when_more_than_35_percent_would_be_deployed(desk):
    assert buy(desk, "POOL1", "MINT1", "ONE") and buy(desk, "POOL2", "MINT2", "TWO")
    cash = desk.d.cash
    assert buy(desk, "POOL3", "MINT3", "THREE") is None
    assert len(desk.d.positions) == 2 and desk.d.cash == cash
    assert reasons(desk, "skip") == ["já há muito investido: com esta compra passava de 35% da mesa"]


def test_the_same_coin_through_another_pool_is_not_bought_twice(desk):
    assert buy(desk, "POOL1", "MINT1")
    assert buy(desk, "POOL2", "MINT1") is None
    assert len(desk.d.positions) == 1
    assert reasons(desk, "skip") == ["já na carteira: outra pool da mesma moeda"]


def test_the_scanner_queues_one_pool_per_coin(desk, monkeypatch):
    trending = {"data": [gecko_pool("POOL1", "MINTX", "XX"), gecko_pool("POOL2", "MINTX", "XX"),
                         gecko_pool("POOL3", "MINTY", "YY")]}
    monkeypatch.setattr(desk, "_gecko", lambda path, params=None: trending if "trending" in path else {"data": []})
    monkeypatch.setattr(desk, "_wide", lambda known: {})
    desk.scan()
    assert [q[0] for q in desk.queue] == ["POOL1", "POOL3"]
    desk.scan()                                           # next minute: the second pool still waits its turn
    assert [q[0] for q in desk.queue] == ["POOL1", "POOL3"]


def test_a_second_pool_of_a_coin_already_agreed_does_not_join_the_buy_queue(desk, monkeypatch):
    everyone_agrees(desk, monkeypatch)
    for addr in ("POOL1", "POOL2"):
        on_the_market(desk, addr, "MINT1")
        desk.auditor(addr, gecko_pool(addr, "MINT1"), "$DEEP")
    assert [r["addr"] for r in desk.ready] == ["POOL1"]
    assert reasons(desk, "skip") == ["já na fila: outra pool da mesma moeda"]


def test_a_20_euro_desk_gets_one_clear_skip_instead_of_a_buy(monkeypatch, tmp_path):
    small = build(monkeypatch, tmp_path, eur=20)          # 15% of 20 is 3: under the $5 minimum
    everyone_agrees(small, monkeypatch)
    on_the_market(small, "POOL1", "MINT1")
    small.auditor("POOL1", gecko_pool("POOL1", "MINT1"), "$DEEP")
    assert not small.ready and not small.d.positions
    said = [(e["stage"], e["reason"]) for e in small.d.events if e["verdict"] in ("skip", "reject")]
    assert said == [("EXEC", "aposta abaixo do mínimo: 3,00 € (mínimo 5,00 €)")]


# ---------------------------------------------------------------- B. brakes
def test_three_losses_in_a_row_pause_new_entries_and_the_owner_is_told(desk):
    for pool in "ABC":
        p = put(desk, pool, cost=10.0)
        desk.market[p.mint] = 0.9
        desk.close(p, 0.9, "stop −8%", "stop")
    desk.ready.append(agreed(desk, "POOLN", "MINTN"))
    desk.execute_best()
    assert not desk.d.positions and len(desk.ready) == 1  # the coin waits, nothing is bought
    assert len(alerts(desk)) == 1 and alerts(desk)[0].startswith("3 perdas seguidas")
    assert desk.d.events[-1]["agent"] == "RISK"
    desk.execute_best()
    assert len(alerts(desk)) == 1                         # told once, not every tick


def test_the_brakes_are_checked_before_every_entry_not_only_the_first(desk):
    desk.ready.append(agreed(desk, "POOLN", "MINTN"))
    desk.execute_best()
    assert [p.pool for p in desk.d.positions] == ["POOLN"]            # nothing wrong yet: it buys
    desk.d.cash -= 12.0                                               # the session is now 10% down
    desk.last_buy = 0.0
    desk.ready.append(agreed(desk, "POOLM", "MINTM", "MORE"))
    desk.execute_best()
    assert [p.pool for p in desk.d.positions] == ["POOLN"]
    assert "perda da sessão" in alerts(desk)[0]


def test_a_coin_that_closed_at_a_loss_is_not_bought_again_this_session(desk):
    p = buy(desk, "POOL1", "MINT1")
    desk.market["MINT1"] = 0.009
    desk.close(p, 0.009, "stop −8%", "stop")
    assert buy(desk, "POOL1", "MINT1") is None and buy(desk, "POOL2", "MINT1") is None
    assert reasons(desk, "skip") == ["já perdeu nesta sessão: não volta a entrar"] * 2


def test_the_scanner_passes_over_a_coin_that_lost_this_session(desk, monkeypatch):
    desk._blocked["MINTX"] = "$XX"
    trending = {"data": [gecko_pool("POOL1", "MINTX", "XX")]}
    monkeypatch.setattr(desk, "_gecko", lambda path, params=None: trending if "trending" in path else {"data": []})
    monkeypatch.setattr(desk, "_wide", lambda known: {})
    desk.scan()
    desk.scan()
    assert not desk.queue and desk.d.seen["POOL1"] == "blocked"
    assert reasons(desk, "skip") == ["já perdeu nesta sessão: não volta a entrar"]    # said once


def test_a_coin_that_closed_with_a_gain_may_come_back(desk):
    p = buy(desk, "POOL1", "MINT1")
    desk.market["MINT1"] = 0.012
    desk.close(p, 0.012, "teste", "end")
    assert desk.d.trades[-1]["pnl_usd"] > 0 and not desk._blocked
    assert buy(desk, "POOL2", "MINT1") is not None


def test_selling_half_is_not_a_close_for_the_brakes_and_the_whole_position_decides(desk):
    desk.guard.streak = 2                                 # two losses before this one
    p = put(desk, "A")
    tick(desk, A=(2.05, 100_000))                         # half sold at a gain
    assert p.half_done and desk.guard.streak == 2         # a full close with a gain would have reset it
    tick(desk, A=(0.5, 100_000))                          # the rest goes at a loss, the whole still won
    assert not desk.d.positions and desk.d.trades[-1]["pnl_usd"] < 0
    assert desk.guard.streak == 0 and not desk._blocked


def test_the_brakes_are_saved_in_the_state_for_the_app(desk, tmp_path):
    desk.guard.streak = 3
    desk.guard.check(desk.equity())
    desk.save()
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["guard"]["on"] is True and state["guard"]["kind"] == "streak" and state["guard"]["reason"]
    for name in ("tick_ms", "max_tick_ms", "risk_errors", "feed_stale", "blind_s", "max_gap_s", "quote_fail"):
        assert name in state


# ---------------------------------------------------------------- C. honest fills
def test_the_buy_gets_the_tokens_of_the_real_quote_less_costs(desk):
    p = buy(desk, "POOL1", "MINT1")                       # $15 at $0.01 is 1500 tokens on the quote
    assert abs(p.qty - 1500 * 0.995) < 1e-6 and p.decimals == 6 and p.liq0 == 250_000


def test_buying_and_selling_at_once_loses_about_one_percent(desk):
    p = buy(desk, "POOL1", "MINT1")
    desk.close(p, 0.01, "teste", "end")
    t = desk.d.trades[-1]
    assert t["fill_src"] == "quote" and abs(t["pct"] - (0.995 ** 2 - 1)) < 1e-6      # -0.9975%
    assert abs(desk.d.cash - (85.0 + 15.0 * 0.995 ** 2)) < 1e-6


def test_a_sale_with_no_quote_is_priced_by_the_pool_and_counted(desk):
    p = put(desk, "A", cost=50.0, liq=2_000.0)
    desk.sell = "unavailable"
    got, src = desk._sale(p, p.qty, 1.0)
    assert src == "model" and abs(got - 1000 * 50 / 1050 * 0.995) < 1e-9             # 47.381, not 49.75
    assert desk.d.quote_fail == 1
    desk.close(p, 1.0, "teste", "end")
    t = desk.d.trades[-1]
    assert t["fill_src"] == "model" and abs(t["got_usd"] - 47.381) < 0.001
    assert "preço estimado pela liquidez" in reasons(desk, "sell")[0]


def test_a_sale_bigger_than_the_pool_is_not_capped_at_half_its_value(desk):
    p = put(desk, "A", cost=50.0, liq=2_000.0)
    desk.sell = "unavailable"
    got, _ = desk._sale(p, p.qty, 100.0)                  # $5,000 of tokens into a $1,000 side
    assert abs(got - 1000 * 5000 / 6000 * 0.995) < 1e-9   # $829: the old model stopped at -50% ($2,487)


def test_no_route_and_an_empty_pool_count_the_sale_as_zero(desk):
    p = put(desk, "A", cost=15.0, liq=500.0)
    desk.sell = "no_route"
    cash = desk.d.cash
    desk.close(p, 1.0, "liquidez retirada", "liq")
    t = desk.d.trades[-1]
    assert t["fill_src"] == "zero" and t["got_usd"] == 0 and t["pnl_usd"] == -15.0 and desk.d.cash == cash
    assert "contado a zero" in reasons(desk, "sell")[0]
    assert desk.d.quote_fail == 0                         # Jupiter did answer


def test_no_route_with_a_pool_still_there_uses_the_pool_model(desk):
    p = put(desk, "A", cost=50.0, liq=2_000.0)
    desk.sell = "no_route"
    assert desk._sale(p, p.qty, 1.0) == (1000 * 50 / 1050 * 0.995, "model")
    assert desk.d.quote_fail == 0


def test_liquidity_halved_on_two_readings_35_seconds_apart_closes_the_position(desk):
    p = put(desk, "A", liq=100_000.0)
    tick(desk, A=(1.0, 40_000))
    assert desk.d.positions == [p]                        # one reading: could be a blip
    tick(desk, A=(1.0, 40_000))
    assert desk.d.positions == [p]                        # twice, but only seconds apart
    desk._liq_low[p.id] -= 40                             # the first low reading was 40 s ago
    tick(desk, A=(1.0, 40_000))
    assert not desk.d.positions
    assert desk.d.trades[-1]["kind"] == "liq" and desk.d.trades[-1]["reason"] == "liquidez caiu para metade"


def test_liquidity_that_comes_back_starts_the_count_again(desk):
    p = put(desk, "A", liq=100_000.0)
    tick(desk, A=(1.0, 40_000))
    desk._liq_low[p.id] -= 40
    tick(desk, A=(1.0, 90_000))                           # back: it was a blip
    assert p.id not in desk._liq_low
    tick(desk, A=(1.0, 40_000))
    assert desk.d.positions == [p]


def test_liquidity_is_measured_against_the_day_of_the_buy_not_the_last_reading(desk):
    p = put(desk, "A", liq=100_000.0)
    for liq in (80_000, 64_000, 52_000):                  # a slow drain: each step is small
        tick(desk, A=(1.0, liq))
    assert p.liq0 == 100_000.0 and p.id not in desk._liq_low
    tick(desk, A=(1.0, 45_000))
    assert p.id in desk._liq_low                          # under half of what it was when bought


def test_an_emptied_pool_closes_at_once_even_when_no_price_comes_with_it(desk):
    put(desk, "A", liq=100_000.0)
    tick(desk, A=(0, 300))                                # no price, $300 of liquidity left
    assert not desk.d.positions
    t = desk.d.trades[-1]
    assert t["kind"] == "liq" and t["reason"] == "liquidez retirada" and t["exit"] == 1.0


def test_the_sol_price_is_refreshed_after_60_seconds_and_outside_the_lock(desk):
    from deskbot import memedesk
    calls = []

    class Jupiter:
        def get(self, url, params=None, timeout=None):
            calls.append((timeout, lock_is_free(desk)))

            class R:
                ok = True

                def json(self):
                    return {memedesk.SOL_MINT: {"usdPrice": 160.0}}
            return R()
    desk.http_risk = Jupiter()
    refresh = lambda: memedesk.MemesDesk._refresh_sol(desk)
    desk._sol = (SOL, time.time() - 10)
    refresh()
    assert calls == [] and desk._sol[0] == SOL            # young enough
    desk._sol = (SOL, time.time() - 61)
    desk._refresh_sol = refresh                           # the real one, as the risk tick calls it
    put(desk, "A")
    tick(desk, A=(1.0, 100_000))
    assert calls == [(memedesk.RISK_TIMEOUT_S, True)] and desk._sol[0] == 160.0


def test_the_report_says_how_buys_and_sales_were_priced(desk):
    a, b = put(desk, "A", cost=10.0, liq=2_000.0), put(desk, "B", cost=10.0, liq=500.0)
    desk.close(a, 1.0, "teste", "end")
    desk.sell = "no_route"
    desk.close(b, 1.0, "teste", "end")
    text = desk.report()
    assert "40% do saldo livre" not in text and "cada compra é 15% da mesa" in text
    assert "1 com cotação real da Jupiter" in text and "1 contadas a zero" in text and "0 sem cotação" in text
    assert "(0,5% por lado)" in text and "menos 0,5% de custos" in text
    assert "de 5 em 5 segundos" in text and "sorte" in text


# ---------------------------------------------------------------- D. feed health
def test_three_missed_prices_raise_one_alert_set_the_flag_and_close_nothing(desk):
    p = put(desk, "A")
    for _ in range(5):
        tick(desk)                                        # DexScreener and Jupiter both silent
    assert len(alerts(desk)) == 1 and alerts(desk)[0].startswith("sem preço há ")
    assert desk.d.events[-1]["coin"] == "$A" and desk.d.feed_stale is True
    assert desk.d.positions == [p] and not desk.d.trades and p.misses == 5


def test_two_missed_prices_are_not_yet_worth_an_alert(desk):
    put(desk, "A")
    tick(desk)
    tick(desk)
    tick(desk, A=(1.0, 100_000))
    assert not alerts(desk) and desk.d.feed_stale is False


def test_the_owner_is_told_when_the_price_is_back(desk):
    p = put(desk, "A")
    for _ in range(3):
        tick(desk)
    tick(desk, A=(1.01, 100_000))
    assert alerts(desk)[-1] == "preço de volta: stop outra vez vigiado" and len(alerts(desk)) == 2
    assert desk.d.feed_stale is False and p.misses == 0


def test_a_jupiter_fallback_price_can_trigger_the_stop(desk):
    p = put(desk, "A")
    desk.jup, desk.market[p.mint] = {p.mint: 0.90}, 0.90
    tick(desk)                                            # DexScreener silent, Jupiter says -10%
    assert not desk.d.positions and desk.d.trades[-1]["kind"] == "stop"
    assert not alerts(desk) and desk.d.feed_stale is False


def test_a_jupiter_fallback_price_never_triggers_the_liquidity_exit(desk):
    p = put(desk, "A", liq=100_000.0)
    tick(desk, A=(1.0, 40_000))
    desk._liq_low[p.id] -= 40                             # one more low DexScreener reading would close it
    desk.jup = {p.mint: 1.0}
    tick(desk)                                            # only Jupiter answers: it knows nothing of the pool
    assert desk.d.positions == [p] and p.px_src == "jup" and p.misses == 0
    tick(desk, A=(1.0, 40_000))
    assert not desk.d.positions and desk.d.trades[-1]["kind"] == "liq"


def test_the_risk_thread_gives_its_feeds_5_seconds_not_20(desk):
    p = put(desk, "A")
    tick(desk, A=(0.9, 100_000))
    assert not desk.d.positions
    assert ("pairs", {"timeout": 5}) in desk.asked and ("sell", {"timeout": 5}) in desk.asked


def test_the_price_feed_passes_the_timeout_it_is_given(desk):
    from deskbot import memetest
    seen = []

    class Dex:
        def get(self, url, timeout=None):
            seen.append(timeout)

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"pairs": [{"pairAddress": "A", "priceUsd": "1"}]}
            return R()
    assert memetest.MemeTest._pairs(desk, ["A"], Dex(), timeout=5) == {"A": {"pairAddress": "A", "priceUsd": "1"}}
    assert memetest.MemeTest._pairs(desk, ["A"], Dex())
    assert seen == [5, 20]


def test_the_state_shows_how_long_a_price_check_takes(desk, monkeypatch):
    put(desk, "A")
    slow = desk._pairs
    monkeypatch.setattr(desk, "_pairs", lambda pools, session=None, **kw: time.sleep(0.03) or slow(pools, session, **kw))
    monkeypatch.setattr(desk, "save", lambda: desk.stop.set())        # one tick, then the loop ends
    desk.risk_loop()
    assert desk.d.tick_ms >= 30 and desk.d.max_tick_ms >= desk.d.tick_ms


# ---------------------------------------------------------------- E. time without watching
def test_a_gap_without_watching_is_told_and_counted(desk):
    put(desk, "A")
    now = time.time()
    desk._loop_at = now - 600                             # the loop last turned 10 minutes ago
    desk._gap(now)
    assert alerts(desk) == ["sem vigilância durante 10 min (computador suspenso ou parado): "
                            "1 posição esteve sem stop · entradas em pausa 5 min"]
    assert abs(desk.d.blind_s - 600) < 1 and abs(desk.d.max_gap_s - 600) < 1
    desk._gap(now + 5)                                    # the next turn is a normal one
    assert len(alerts(desk)) == 1 and abs(desk.d.blind_s - 600) < 1


def test_an_ordinary_turn_of_the_loop_is_not_a_gap(desk):
    put(desk, "A")
    now = time.time()
    desk._gap(now)                                        # the very first turn has nothing to compare with
    desk._gap(now + 100)                                  # slow, but under the two minutes
    assert not alerts(desk) and desk.d.blind_s == 0 and not desk.d.positions[0].blind


def test_after_a_gap_entries_pause_and_what_was_queued_is_dropped(desk):
    desk.queue.append(("POOLQ", gecko_pool("POOLQ", "MINTQ"), "$QQ"))
    desk.ready.append(agreed(desk, "POOLN", "MINTN"))
    desk.d.seen.update(POOLQ="pass", POOLN="ready")
    now = time.time()
    desk._loop_at = now - 600
    desk._gap(now)
    desk.execute_best()
    assert not desk.queue and not desk.ready and not desk.d.positions
    assert "POOLQ" not in desk.d.seen and "POOLN" not in desk.d.seen            # they may be scanned again
    desk.ready.append(agreed(desk, "POOLN", "MINTN"))                           # found again, still in the pause
    desk.execute_best()
    assert not desk.d.positions and len(desk.ready) == 1
    desk.no_new_until = time.time() - 1                                         # five minutes later
    desk.execute_best()
    assert [p.pool for p in desk.d.positions] == ["POOLN"]


def test_a_trade_that_lived_through_a_gap_is_marked_blind(desk):
    a = put(desk, "A")
    now = time.time()
    desk._loop_at = now - 600
    desk._gap(now)
    b = put(desk, "B")                                    # bought after the gap
    assert a.blind and not b.blind
    desk.close(a, 1.0, "teste", "end")
    desk.close(b, 1.0, "teste", "end")
    assert [t["blind"] for t in desk.d.trades] == [True, False]


def test_the_computer_is_kept_awake_while_a_session_runs_and_released_after(desk, monkeypatch, tmp_path):
    from deskbot import guard
    calls = []
    monkeypatch.setattr(guard, "keep_awake", lambda on: calls.append(on))
    monkeypatch.setattr(desk, "scan", lambda: None)
    desk.d.ends = datetime.now(timezone.utc).isoformat(timespec="seconds")      # the session is over as it starts
    report = desk.run()
    assert calls == [True, False] and desk.d.finished
    assert (tmp_path / "relatorio.md").read_text(encoding="utf-8") == report
    assert not (tmp_path / "pid").exists()


# ---------------------------------------------------------------- F. robustness
def test_one_position_failing_its_check_does_not_stop_the_others(desk, monkeypatch):
    a, b = put(desk, "A"), put(desk, "B")
    real = desk._decide

    def decide(pos, pr, final):
        if pos is a:
            raise RuntimeError("boom")
        return real(pos, pr, final)
    monkeypatch.setattr(desk, "_decide", decide)
    tick(desk, A=(1.0, 100_000), B=(0.9, 100_000))
    assert desk.d.positions == [a] and desk.d.trades[-1]["coin"] == "$B"        # B's stop still worked
    assert desk.d.risk_errors == 1 and not alerts(desk)


def test_a_position_whose_check_fails_three_times_in_a_row_is_told_once(desk, monkeypatch):
    a = put(desk, "A")
    monkeypatch.setattr(desk, "_decide", lambda pos, pr, final: 1 / 0)
    for _ in range(4):
        tick(desk, A=(1.0, 100_000))
    assert alerts(desk) == ["a vigilância desta posição falhou 3 vezes seguidas"]
    assert desk.d.events[-1]["coin"] == "$A" and desk.d.risk_errors == 4 and desk.d.positions == [a]


def test_a_disk_error_on_the_event_log_cannot_stop_a_close(desk):
    class Broken:
        def open(self, *args, **kw):
            raise OSError("disk full")
    p = put(desk, "A")
    desk.events_file = Broken()
    tick(desk, A=(0.9, 100_000))
    assert not desk.d.positions and desk.d.trades[-1]["kind"] == "stop"
    assert abs(desk.d.cash - (85.0 + 15.0 * 0.9 * 0.995)) < 1e-6
    assert reasons(desk, "sell")                          # the app still gets the event from the state


def test_a_close_is_whole_even_when_writing_it_down_fails(desk, monkeypatch):
    p = put(desk, "A")
    monkeypatch.setattr(desk, "_trade", lambda *args, **kw: 1 / 0)
    tick(desk, A=(0.9, 100_000))
    assert not desk.d.positions                           # sold: not left open to be sold again
    assert abs(desk.d.cash - (85.0 + 15.0 * 0.9 * 0.995)) < 1e-6
    assert desk.d.risk_errors == 0


def test_a_failing_sell_event_loses_neither_the_trade_nor_the_brakes(desk, monkeypatch):
    p = put(desk, "A")
    monkeypatch.setattr(desk, "_sold", lambda pos, t: 1 / 0)
    tick(desk, A=(0.9, 100_000))
    assert not desk.d.positions and len(desk.d.trades) == 1
    assert desk.guard.streak == 1 and p.mint in desk._blocked


def test_a_position_is_never_sold_twice(desk, monkeypatch):
    p = put(desk, "A")
    answer = desk._quote

    def slow(a, b, amount, session=None, timeout=8):
        desk.close(p, 0.9, "outra vez", "end")            # someone else tries while the quote is out
        time.sleep(0.05)
        return answer(a, b, amount, session, timeout)
    monkeypatch.setattr(desk, "_quote", slow)
    desk.market[p.mint] = 0.9
    sellers = [threading.Thread(target=desk.close, args=(p, 0.9, "stop −8%", "stop")) for _ in range(3)]
    for t in sellers:
        t.start()
    for t in sellers:
        t.join()
    assert len(desk.d.trades) == 1 and not desk.d.positions
    assert abs(desk.d.cash - (85.0 + 15.0 * 0.9 * 0.995)) < 1e-6
    assert not desk._closing


def test_the_sell_quote_is_asked_for_without_holding_the_lock(desk, monkeypatch):
    put(desk, "A")
    answer, free = desk._quote, []

    def watched(a, b, amount, session=None, timeout=8):
        free.append(lock_is_free(desk))
        return answer(a, b, amount, session, timeout)
    monkeypatch.setattr(desk, "_quote", watched)
    tick(desk, A=(0.9, 100_000))
    assert free == [True] and not desk.d.positions


def test_the_stop_button_is_seen_by_the_risk_thread_within_one_tick(desk, monkeypatch, tmp_path):
    (tmp_path / "stop").write_text("1", encoding="utf-8")
    monkeypatch.setattr(desk, "save", lambda: desk.stop.set())        # one tick, then the loop ends
    desk.risk_loop()
    assert desk.quit.is_set()


def test_a_stop_ends_the_session_long_before_its_time_and_closes_what_is_open(desk, monkeypatch, tmp_path):
    desk.RISK_EVERY_S = 0.5
    p = put(desk, "A")
    desk.live = {"A": {"priceUsd": "1.02", "liquidity": {"usd": 100_000}}}
    desk.market[p.mint] = 1.02
    monkeypatch.setattr(desk, "scan", lambda: (tmp_path / "stop").write_text("1", encoding="utf-8"))
    t0 = time.time()
    desk.run()                                            # a 30-minute session
    assert time.time() - t0 < 10
    assert desk.d.finished and not desk.d.positions and desk.d.trades[-1]["reason"] == "fim da sessão"
    assert not (tmp_path / "stop").exists()


def test_waits_between_api_calls_end_at_once_when_the_session_is_stopped(desk):
    from deskbot import memedesk
    desk.quit.set()
    t0 = time.time()
    desk._nap(30)
    assert time.time() - t0 < 1
    assert memedesk.MemesDesk._gecko(desk, "/networks/solana/trending_pools") is None    # and no new slow call


# ---------------------------------------------------------------- G. the rug guard behind the wide scan
def test_a_coin_the_wide_scan_let_through_without_an_audit_still_meets_the_chain_check(desk, monkeypatch):
    old = "2026-01-01T00:00:00+00:00"
    tok = {"id": "MINT1", "symbol": "T1", "liquidity": 90_000, "mcap": 900_000, "stats1h": {"priceChange": 5},
           "firstPool": {"createdAt": old}}                # Jupiter sent no audit for it
    assert desk._wide_cut(tok) == ""
    everyone_agrees(desk, monkeypatch)
    monkeypatch.setattr(desk, "_mint_info", lambda mint: {"mintAuthority": "DEV", "freezeAuthority": None, "decimals": 6})
    on_the_market(desk, "POOL1", "MINT1")
    out = desk.debate("POOL1", gecko_pool("POOL1", "MINT1"), "$T1", "MINT1")
    assert out["ok"] is False and "mint ativa" in reasons(desk, "reject")[-1]


# ---------------------------------------------------------------- H. what the review found
class Answer:
    """A canned HTTP answer."""

    def __init__(self, body=None, status=200, broken=False):
        self.body, self.status_code, self.ok, self.broken = body, status, status < 400, broken

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        if self.broken:
            raise ValueError("not json")
        return self.body


class Feed:
    """Stands where a requests session would: gives one canned answer (or raises it) and keeps what it was asked."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# -- a sale is booked once, whatever the session end does meanwhile
def session_ends_on_a_hanging_sale(desk, monkeypatch, kind, px, half_lands_first=False):
    """RISK is selling position A (kind) when the session ends. Its quote hangs longer than the 20 s the end
    waits for it and answers only after the end sold the position itself (half_lands_first: while the end's
    own quote is still out)."""
    from deskbot import memedesk
    p = put(desk, "A")
    desk.market[p.mint] = px
    answer, inside, release, asked = desk._quote, threading.Event(), threading.Event(), []
    risk = threading.Thread(target=desk._exit, args=(p, px, "venda da RISK", kind))

    def quote(a, b, amount, session=None, timeout=8):
        asked.append(amount)
        if len(asked) == 1:
            inside.set()
            release.wait(10)                              # RISK's quote hangs
        elif half_lands_first and len(asked) == 2:
            release.set()
            risk.join(5)                                  # RISK books its half before the end gets its answer
        return answer(a, b, amount, session, timeout)
    monkeypatch.setattr(desk, "_quote", quote)
    skipped = [0.0]                                       # a clock whose sleep() jumps past the 20 s at once
    monkeypatch.setattr(memedesk, "time", types.SimpleNamespace(
        time=lambda: time.time() + skipped[0], sleep=lambda s: skipped.__setitem__(0, skipped[0] + 30)))
    risk.start()
    assert inside.wait(5)
    desk._close_leftovers()
    release.set()
    risk.join(5)
    assert not risk.is_alive()
    return p


def test_a_stop_whose_quote_outlives_the_session_end_is_not_paid_twice(desk, monkeypatch):
    session_ends_on_a_hanging_sale(desk, monkeypatch, "stop", 0.9)
    assert len(desk.d.trades) == 1 and not desk.d.positions
    assert abs(desk.d.cash - (85.0 + 15.0 * 0.9 * 0.995)) < 1e-6          # one sale, not two
    assert desk.d.risk_errors == 0 and not desk._closing


def test_a_half_whose_quote_outlives_the_session_end_is_not_paid_on_top_of_the_full_sale(desk, monkeypatch):
    p = session_ends_on_a_hanging_sale(desk, monkeypatch, "half", 2.0)
    assert [t["kind"] for t in desk.d.trades] == ["end"] and not desk.d.positions
    assert abs(desk.d.cash - (85.0 + 15.0 * 2.0 * 0.995)) < 1e-6          # 15 tokens sold once, not 22.5
    assert not p.half_done and desk.d.risk_errors == 0


def test_a_half_that_lands_during_the_last_close_leaves_no_half_position_behind(desk, monkeypatch):
    session_ends_on_a_hanging_sale(desk, monkeypatch, "half", 2.0, half_lands_first=True)
    assert [t["kind"] for t in desk.d.trades] == ["half", "end"] and not desk.d.positions
    assert abs(sum(t["cost_usd"] for t in desk.d.trades) - 15.0) < 1e-9
    assert abs(desk.d.cash - (85.0 + 15.0 * 2.0 * 0.995)) < 1e-6          # the two halves, each paid once


# -- no entry while nothing watches the stops, and the last look before a buy is booked
def test_nothing_is_bought_while_the_risk_loop_is_away_even_before_it_says_so(desk):
    desk._loop_at = time.time() - 300                     # RISK last turned 5 minutes ago and has not noticed yet
    desk.ready.append(agreed(desk, "POOLN", "MINTN"))
    desk.execute_best()
    assert not desk.d.positions and len(desk.ready) == 1  # not bought, and not thrown away by guesswork
    desk._gap(time.time())                                # RISK is back and says so
    desk.execute_best()
    assert not desk.ready and not desk.d.positions
    assert len(alerts(desk)) == 1 and "sem stop" not in alerts(desk)[0]      # no position went through the gap


def test_a_buy_on_its_way_is_dropped_when_the_risk_loop_turns_out_to_be_away(desk):
    desk._loop_at = time.time() - 300
    assert buy(desk, "POOL1", "MINT1") is None and desk.d.cash == 100.0
    assert reasons(desk, "skip") == ["computador esteve parado: entradas em pausa"]


def while_the_buy_quote_is_out(desk, monkeypatch, happens):
    """EXEC takes the next coin of its queue; `happens` runs while Jupiter is answering the buy quote."""
    answer = desk.quote

    def slow(a, b, amount, session=None):
        happens()
        return answer(a, b, amount, session)
    monkeypatch.setattr(desk, "quote", slow)
    desk.ready.append(agreed(desk, "POOLN", "MINTN"))
    desk.execute_best()


def test_a_brake_that_comes_on_while_the_buy_quote_is_out_stops_that_buy(desk, monkeypatch):
    desk.guard.streak = 2                                 # two losses in a row before this one
    p = put(desk, "A")
    desk.market[p.mint] = 0.9
    while_the_buy_quote_is_out(desk, monkeypatch, lambda: desk.close(p, 0.9, "stop −8%", "stop"))   # the third
    assert not desk.d.positions and abs(desk.d.cash - (85.0 + 15.0 * 0.9 * 0.995)) < 1e-6      # nothing bought
    assert [e["verdict"] for e in desk.d.events if e["stage"] == "EXEC"][-1] == "skip"
    assert reasons(desk, "skip") == ["travão ligado enquanto se preparava a compra"]
    assert len(alerts(desk)) == 1 and alerts(desk)[0].startswith("3 perdas seguidas")


def test_a_gap_found_while_the_buy_quote_is_out_stops_that_buy(desk, monkeypatch):
    while_the_buy_quote_is_out(desk, monkeypatch, lambda: setattr(desk, "no_new_until", time.time() + 300))
    assert not desk.d.positions and desk.d.cash == 100.0
    assert reasons(desk, "skip") == ["computador esteve parado: entradas em pausa"]


def test_a_stop_asked_for_while_the_buy_quote_is_out_stops_that_buy(desk, monkeypatch):
    while_the_buy_quote_is_out(desk, monkeypatch, desk.quit.set)
    assert not desk.d.positions and desk.d.cash == 100.0
    assert reasons(desk, "skip") == ["sessão a parar"]


# -- a feed that answers nonsense
def test_a_price_feed_answering_nonsense_is_a_missed_reading_not_a_broken_tick(desk, monkeypatch):
    from deskbot import memedesk
    p = put(desk, "A")

    def nonsense(pools, session=None, **kw):
        raise AttributeError("'NoneType' object has no attribute 'get'")
    monkeypatch.setattr(desk, "_pairs", nonsense)
    for _ in range(memedesk.MISS_ALERT):
        desk.risk()                                       # must not raise
    assert p.misses == memedesk.MISS_ALERT and desk.d.feed_stale is True and desk.d.positions == [p]
    assert len(alerts(desk)) == 1 and alerts(desk)[0].startswith("sem preço há ")
    assert desk.d.api_errors == memedesk.MISS_ALERT


def test_a_second_price_source_that_breaks_does_not_stop_the_tick(desk, monkeypatch):
    a, b = put(desk, "A"), put(desk, "B")
    monkeypatch.setattr(desk, "_jup_prices", lambda mints: 1 / 0)
    tick(desk, B=(0.9, 100_000))                          # A has no price, B must still get its stop
    assert desk.d.positions == [a] and a.misses == 1 and desk.d.trades[-1]["coin"] == "$B"


def test_a_null_body_or_a_null_row_from_dexscreener_is_read_as_no_answer(desk):
    from deskbot import memetest
    good = {"pairAddress": "A", "priceUsd": "1"}
    read = lambda body: memetest.MemeTest._pairs(desk, ["A"], Feed(Answer(body)), timeout=5)
    assert read(None) == {} and read([]) == {} and read({"pairs": None}) == {} and read({"pairs": 7}) == {}
    assert read({"pairs": [None, good, "x"]}) == {"A": good}                 # the good row is kept


# -- when a sale is worth nothing
def test_a_position_knows_whether_its_liquidity_was_read_on_the_last_tick(desk):
    p = put(desk, "A")
    assert p.liq_fresh is True
    desk.jup = {p.mint: 1.0}
    tick(desk)                                            # only Jupiter answers: it says nothing about the pool
    assert p.liq_fresh is False and p.liquidity == 100_000.0
    tick(desk, A=(1.0, 90_000))
    assert p.liq_fresh is True and p.liquidity == 90_000


def test_no_route_at_the_session_end_with_the_pool_long_unseen_counts_the_sale_as_zero(desk):
    put(desk, "A", liq=80_000.0)
    tick(desk, A=(1.0, 80_000))                           # one good reading
    desk.sell = "no_route"
    for _ in range(4):
        tick(desk)                                        # then silence: the $80k is a memory, not a reading
    tick(desk, final=True)
    t = desk.d.trades[-1]
    assert t["kind"] == "end" and t["fill_src"] == "zero" and t["got_usd"] == 0 and t["pnl_usd"] == -15.0
    assert "contado a zero" in reasons(desk, "sell")[0]


def test_no_route_on_a_stop_seen_only_through_jupiter_counts_the_sale_as_zero(desk):
    p = put(desk, "A", liq=80_000.0)
    desk.jup, desk.sell = {p.mint: 0.90}, "no_route"
    tick(desk)                                            # DexScreener silent: nobody saw the pool this tick
    t = desk.d.trades[-1]
    assert t["kind"] == "stop" and t["fill_src"] == "zero" and t["got_usd"] == 0


def test_no_answer_from_jupiter_with_the_pool_unseen_still_uses_the_pool_model(desk):
    put(desk, "A", liq=80_000.0)
    desk.sell = "unavailable"                             # the network is down, which says nothing about the coin
    tick(desk, final=True)
    t = desk.d.trades[-1]
    assert t["fill_src"] == "model" and abs(t["got_usd"] - 40_000 * 15 / 40_015 * 0.995) < 1e-9
    assert desk.d.quote_fail == 1


def test_an_emptied_pool_with_no_answer_from_jupiter_counts_the_sale_as_zero(desk):
    put(desk, "A", liq=100_000.0)
    desk.sell = "unavailable"
    tick(desk, A=(0, 300))                                # no price, $300 of liquidity left, no quote either
    t = desk.d.trades[-1]
    assert t["kind"] == "liq" and t["fill_src"] == "zero" and t["got_usd"] == 0 and t["pnl_usd"] == -15.0
    assert desk.d.quote_fail == 1 and "contado a zero" in reasons(desk, "sell")[0]


def test_the_report_says_why_a_sale_was_counted_as_zero(desk):
    p = put(desk, "A", liq=500.0)
    desk.sell = "unavailable"
    desk.close(p, 1.0, "liquidez retirada", "liq")
    text = desk.report()
    assert "1 contadas a zero, porque não havia cotação e a pool estava vazia ou sem leitura." in text


# -- the SOL price has an age limit
def test_a_sol_price_older_than_5_minutes_does_not_price_a_sale(desk):
    from deskbot import memedesk
    p = put(desk, "A", cost=50.0, liq=2_000.0)
    desk._sol = (SOL, time.time() - memedesk.SOL_STALE_S - 1)
    got, src = desk._sale(p, p.qty, 1.0)
    assert src == "model" and abs(got - 1000 * 50 / 1050 * 0.995) < 1e-9
    assert desk.d.quote_fail == 1 and not desk.asked      # no quote was even asked for


def test_a_sol_price_a_couple_of_minutes_old_still_prices_a_sale(desk):
    from deskbot import memedesk
    p = put(desk, "A")
    desk._sol = (SOL, time.time() - 120)
    assert memedesk.SOL_MAX_AGE_S < 120 < memedesk.SOL_STALE_S
    got, src = desk._sale(p, p.qty, 1.0)
    assert src == "quote" and abs(got - 15.0 * 0.995) < 1e-6 and desk.d.quote_fail == 0


def test_no_buy_is_made_on_a_sol_price_too_old_when_a_new_one_cannot_be_had(desk, monkeypatch):
    from deskbot import memedesk
    monkeypatch.setattr(desk, "sol_usd", lambda: memedesk.MemesDesk.sol_usd(desk))       # the real one
    desk.http = Feed(requests.ConnectionError("down"))
    desk._sol = (SOL, time.time() - memedesk.SOL_STALE_S - 1)
    assert buy(desk, "POOL1", "MINT1") is None and desk.d.cash == 100.0
    assert reasons(desk, "skip") == ["sem preço do SOL"]


def test_a_sol_price_that_cannot_be_refreshed_is_kept_while_it_is_young_enough(desk):
    from deskbot import memedesk
    desk.http = Feed(requests.ConnectionError("down"))
    desk._sol = (SOL, time.time() - 120)
    assert memedesk.MemesDesk.sol_usd(desk) == SOL and len(desk.http.calls) == 1


# -- what a Jupiter quote answer means (the real _quote, a canned session)
def real_quote(desk, answer):
    from deskbot import memedesk
    jup = Feed(answer)
    return memedesk.MemesDesk._quote(desk, "MINTA", memedesk.SOL_MINT, 1000, jup, timeout=5), jup


def test_a_jupiter_answer_with_an_amount_is_a_quote(desk):
    from deskbot import memedesk
    out, jup = real_quote(desk, Answer({"outAmount": "123"}))
    assert out == ("ok", {"outAmount": "123"})
    url, params, timeout = jup.calls[0]
    assert url == memedesk.JUP_QUOTE and timeout == 5 and len(jup.calls) == 1
    assert (params["inputMint"], params["outputMint"], params["amount"]) == ("MINTA", memedesk.SOL_MINT, 1000)


def test_a_jupiter_answer_without_an_amount_means_no_route(desk):
    assert real_quote(desk, Answer({}))[0] == ("no_route", None)
    assert real_quote(desk, Answer({"outAmount": "0"}))[0] == ("no_route", None)


def test_a_400_from_jupiter_means_no_route(desk):
    assert real_quote(desk, Answer({"error": "Could not find any route"}, status=400))[0] == ("no_route", None)


def test_a_rate_limit_or_a_server_error_from_jupiter_means_no_answer(desk):
    assert real_quote(desk, Answer({"error": "slow down"}, status=429))[0] == ("unavailable", None)
    assert real_quote(desk, Answer({}, status=500))[0] == ("unavailable", None)


def test_a_timeout_or_a_broken_body_from_jupiter_means_no_answer(desk):
    assert real_quote(desk, requests.Timeout("slow"))[0] == ("unavailable", None)
    assert real_quote(desk, Answer(broken=True))[0] == ("unavailable", None)


def test_the_buy_route_is_the_quote_itself_or_nothing(desk, monkeypatch):
    from deskbot import memedesk
    monkeypatch.setattr(desk, "_quote", lambda *a, **kw: memedesk.MemesDesk._quote(desk, *a, **kw))    # the real one
    ask = lambda answer: memedesk.MemesDesk.quote(desk, memedesk.SOL_MINT, "MINTA", 1000, Feed(answer))
    assert ask(Answer({"outAmount": "123"})) == {"outAmount": "123"}
    assert ask(Answer({})) is None and ask(Answer({}, status=500)) is None and ask(requests.Timeout("slow")) is None


# -- the real risk loop finds a gap by itself
def test_the_risk_loop_itself_finds_a_gap_tells_it_and_pauses_entries(desk, monkeypatch):
    a = put(desk, "A")
    desk.live = {"A": {"priceUsd": "1.0", "liquidity": {"usd": 100_000}}}
    desk._loop_at = time.time() - 600                     # it last turned 10 minutes ago
    monkeypatch.setattr(desk, "save", lambda: desk.stop.set())        # one tick, then the loop ends
    desk.risk_loop()
    assert len(alerts(desk)) == 1 and alerts(desk)[0].startswith("sem vigilância durante 10 min")
    assert abs(desk.d.blind_s - 600) < 5 and abs(desk.d.max_gap_s - 600) < 5
    assert desk.no_new_until > time.time() and desk.stale.is_set()
    assert a.blind and desk.d.positions == [a]            # still open and watched again


# -- the second price source (the real _jup_prices, a canned session)
def jup_prices(desk, answer):
    from deskbot import memedesk
    desk.http_risk = Feed(answer)
    return memedesk.MemesDesk._jup_prices(desk, ["MINTA", "MINTB"])


def test_the_second_price_source_asks_jupiter_for_all_the_coins_at_once_with_the_short_timeout(desk):
    from deskbot import memedesk
    assert jup_prices(desk, Answer({"MINTA": {"usdPrice": 0.9}, "MINTB": None})) == {"MINTA": 0.9}
    assert desk.http_risk.calls == [(memedesk.JUP_PRICE, {"ids": "MINTA,MINTB"}, memedesk.RISK_TIMEOUT_S)]


def test_a_jupiter_price_written_as_text_counts_and_a_price_of_zero_does_not(desk):
    assert jup_prices(desk, Answer({"MINTA": {"usdPrice": "0.9"}, "MINTB": {"usdPrice": 0}})) == {"MINTA": 0.9}


def test_a_jupiter_price_answer_that_is_not_a_table_gives_no_prices(desk):
    assert jup_prices(desk, Answer([])) == {} and jup_prices(desk, Answer(None)) == {}


def test_a_jupiter_price_error_or_a_broken_body_gives_no_prices(desk):
    assert jup_prices(desk, Answer({"MINTA": {"usdPrice": 0.9}}, status=500)) == {}
    assert jup_prices(desk, Answer(broken=True)) == {}


def test_a_jupiter_price_call_that_times_out_or_cannot_connect_gives_no_prices(desk):
    assert jup_prices(desk, requests.Timeout("slow")) == {}
    assert jup_prices(desk, requests.ConnectionError("down")) == {}


# -- a token with 9 decimals
def test_a_9_decimals_token_is_bought_and_sold_in_its_own_raw_units(desk, monkeypatch):
    sold = []

    def buy_quote(a, b, amount, session=None):            # Jupiter answers in the token's raw units: 9 decimals
        return {"outAmount": str(int(amount / 1e9 * SOL / 0.01 * 1e9)), "priceImpactPct": "0.001", "routePlan": []}

    def sell_quote(a, b, amount, session=None, timeout=8):
        sold.append(amount)
        return "ok", {"outAmount": str(int(amount / 1e9 * 0.01 / SOL * 1e9))}
    monkeypatch.setattr(desk, "quote", buy_quote)
    monkeypatch.setattr(desk, "_quote", sell_quote)
    r = agreed(desk, "POOL9", "MINT9")
    r["decimals"] = 9
    desk.ready.append(r)
    desk.execute_best()
    p = desk.d.positions[0]
    assert p.decimals == 9 and abs(p.qty - 1500 * 0.995) < 1e-6          # $15 at $0.01, not a thousand times more
    raw = int(p.qty * 10 ** 9)
    desk.close(p, 0.01, "teste", "end")
    t = desk.d.trades[-1]
    assert sold == [raw] and t["fill_src"] == "quote" and abs(t["pct"] - (0.995 ** 2 - 1)) < 1e-6


def test_a_token_with_no_decimals_does_not_lose_the_fraction_it_cannot_sell(desk, monkeypatch):
    sold = []

    def buy_quote(a, b, amount, session=None):            # whole tokens only: $15 at $1.00 is 15 of them
        return {"outAmount": str(int(amount / 1e9 * SOL / 1.0)), "priceImpactPct": "0.001", "routePlan": []}

    def sell_quote(a, b, amount, session=None, timeout=8):
        sold.append(amount)
        return "ok", {"outAmount": str(int(amount * 1.0 / SOL * 1e9))}
    monkeypatch.setattr(desk, "quote", buy_quote)
    monkeypatch.setattr(desk, "_quote", sell_quote)
    r = agreed(desk, "POOL0", "MINT0")
    r["decimals"] = 0
    r["pool"]["attributes"]["base_token_price_usd"] = "1.0"
    desk.live["POOL0"]["priceUsd"] = "1.0"
    desk.ready.append(r)
    desk.execute_best()
    p = desk.d.positions[0]
    assert p.decimals == 0 and abs(p.qty - 15 * 0.995) < 1e-9            # 14.925 booked: 15 less the costs
    half, _ = desk._sale(p, p.qty / 2, 1.0)                              # 7.4625 booked, 7 quoted
    assert sold == [7] and abs(half - 15 / 2 * 0.995 ** 2) < 1e-6
    desk.close(p, 1.0, "teste", "end")
    t = desk.d.trades[-1]
    assert sold == [7, 14] and t["fill_src"] == "quote"                  # Jupiter is still asked in whole units
    assert abs(t["pct"] - (0.995 ** 2 - 1)) < 1e-6                       # -1.0% as for any coin, not -7.1%
    assert abs(desk.d.cash - (85.0 + 15.0 * 0.995 ** 2)) < 1e-6


def test_a_token_with_no_decimals_at_all_is_not_booked_as_one_with_six(desk, monkeypatch):
    everyone_agrees(desk, monkeypatch)
    on_the_market(desk, "POOL1", "MINT1")
    pool = gecko_pool("POOL1", "MINT1")
    monkeypatch.setattr(desk, "_mint_info", lambda mint: {"mintAuthority": None, "freezeAuthority": None, "decimals": 0})
    out = desk.debate("POOL1", pool, "$DEEP", "MINT1")
    assert out["ok"] is True and out["decimals"] == 0     # 0 is what the chain said: it was read as "no answer"
    monkeypatch.setattr(desk, "_mint_info", lambda mint: {"mintAuthority": None, "freezeAuthority": None})
    assert desk.debate("POOL1", pool, "$DEEP", "MINT1")["decimals"] == 6      # only a missing answer falls back


# ---------------------------------------------------------------- I. the second review
# -- the rug guard, one risk at a time on a coin that is clean otherwise
CLEAN_MINT = {"mintAuthority": None, "freezeAuthority": None, "decimals": 6}
CLEAN_INFO = {"holders": {"distribution_percentage": {"top_10": "12"}}, "is_honeypot": "no"}
CLEAN_GMGN = {"smart": 0, "kol": 0, "rug": 0.0, "wash": False, "bundler": 0.0, "rat": 0.0}
RUGS = [   # what is wrong with the coin, and the risk DEBATE must name for it
    ({"mint": None}, "sem dados da rede"),
    ({"mint": {**CLEAN_MINT, "mintAuthority": "DEV"}}, "mint ativa"),
    ({"mint": {**CLEAN_MINT, "freezeAuthority": "DEV"}}, "freeze ativo"),
    ({"info": {"is_honeypot": "no"}}, "top 10 sem dados"),
    ({"info": {**CLEAN_INFO, "holders": {"distribution_percentage": {"top_10": "45"}}}}, "top 10 45%"),
    ({"info": {**CLEAN_INFO, "is_honeypot": "yes"}}, "honeypot"),
    ({"info": {**CLEAN_INFO, "is_honeypot": True}}, "honeypot"),
    ({"sells": 0}, "ninguém vende"),
    ({"sells": None}, "ninguém vende"),
    ({"gmgn": {**CLEAN_GMGN, "rug": 0.6}}, "rug 0,60"),
    ({"gmgn": {**CLEAN_GMGN, "wash": True}}, "wash trading"),
    ({"gmgn": {**CLEAN_GMGN, "bundler": 0.31}}, "bots/insiders 31%"),
    ({"gmgn": {**CLEAN_GMGN, "rat": 0.31}}, "bots/insiders 31%"),
]


def debated(desk, monkeypatch, mint=CLEAN_MINT, info=CLEAN_INFO, sells=400, gmgn=CLEAN_GMGN):
    """DEBATE on a deep, lively pool. Returns its answer and the event it wrote."""
    monkeypatch.setattr(desk, "_gecko", lambda path, params=None: {"data": {"attributes": info}})
    monkeypatch.setattr(desk, "_mint_info", lambda m: mint)
    monkeypatch.setattr(desk, "gm", lambda m: gmgn)
    on_the_market(desk, "POOL1", "MINT1")
    pool = gecko_pool("POOL1", "MINT1")
    if sells is None:
        del pool["attributes"]["transactions"]["h1"]["sells"]
    else:
        pool["attributes"]["transactions"]["h1"]["sells"] = sells
    out = desk.debate("POOL1", pool, "$DEEP", "MINT1")
    return out, [e for e in desk.d.events if e["stage"] == "DEBATE"][-1]


@pytest.mark.parametrize("style", ["poucas", "mais"])
def test_the_coin_the_rug_cases_start_from_is_clean(monkeypatch, tmp_path, style):
    out, said = debated(build(monkeypatch, tmp_path, style=style), monkeypatch)
    assert out["ok"] is True and said["verdict"] == "pass" and said["risks"] == []


@pytest.mark.parametrize("style", ["poucas", "mais"])
@pytest.mark.parametrize("wrong, risk", RUGS)
def test_each_rug_sign_alone_stops_the_coin_in_both_styles(monkeypatch, tmp_path, style, wrong, risk):
    out, said = debated(build(monkeypatch, tmp_path, style=style), monkeypatch, **wrong)
    assert out["ok"] is False and said["verdict"] == "reject"
    assert said["risks"] == [risk] and risk in said["reason"]


@pytest.mark.parametrize("style", ["poucas", "mais"])
def test_a_clean_coin_only_one_bull_speaks_for_is_not_bought(monkeypatch, tmp_path, style):
    d = build(monkeypatch, tmp_path, style=style)
    everyone_agrees(d, monkeypatch)
    pool = gecko_pool("POOL1", "MINT1")
    pool["attributes"]["transactions"]["h1"] = {"buyers": 10, "buys": 900, "sells": 400}
    # a deep pool, but trading has died, few real buyers, and it is being chased up
    d.live["POOL1"] = {"priceUsd": "0.01", "liquidity": {"usd": 250_000}, "volume": {"m5": 100, "h1": 90_000},
                       "priceChange": {"m5": 10, "h1": 50}}
    out = d.debate("POOL1", pool, "$DEEP", "MINT1")
    said = [e for e in d.d.events if e["stage"] == "DEBATE"][-1]
    assert out["ok"] is False and said["risks"] == [] and said["reason"] == "bull 1 / bear 4 · risco ok"


# -- the other gates on the way to a buy
def scanned(desk, **attrs):
    pool = {**gecko_pool("POOL1", "MINT1"), "source": "teste"}
    pool["attributes"].update(attrs)
    desk.trends("POOL1", pool)
    return [(e["verdict"], e["reason"]) for e in desk.d.events if e["stage"] == "TRENDS"]


def test_trends_lets_a_deep_calm_old_pool_through(desk):
    assert scanned(desk) == [("pass", "teste · +10% 1h")] and [q[0] for q in desk.queue] == ["POOL1"]


def test_trends_cuts_a_shallow_pool(desk):
    assert scanned(desk, reserve_in_usd="5000") == [("kill", "liquidez: $5k < $40k")]
    assert not desk.queue and desk.d.seen["POOL1"] == "trends"


def test_trends_cuts_a_coin_that_already_ran(desk):
    assert scanned(desk, price_change_percentage={"m5": -2, "h1": 300}) == [("kill", "já correu: +300% em 1h")]
    assert not desk.queue and desk.d.seen["POOL1"] == "trends"


def test_trends_makes_a_pool_three_minutes_old_wait(desk):
    born = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    assert scanned(desk, pool_created_at=born) == [("wait", "pool com 3 min: espera pelos 10")]
    assert not desk.queue and "POOL1" in desk.d.waiting
    assert len(scanned(desk, pool_created_at=born)) == 1 and not desk.queue      # next minute: still waiting, said once


def test_the_router_refuses_a_route_that_moves_the_price_5_percent(desk, monkeypatch):
    monkeypatch.setattr(desk, "quote", lambda a, b, amount, session=None: {
        "outAmount": "1500000000", "priceImpactPct": "0.05", "routePlan": []})
    assert desk.router("$DEEP", "MINT1", 15.0)["ok"] is False
    assert reasons(desk, "reject") == ["jupiter · 5,0% slip · rota direta"]


def test_exec_does_not_chase_a_price_20_percent_above_the_one_trends_saw(desk):
    on_the_market(desk, "POOL1", "MINT1")
    desk.live["POOL1"]["priceUsd"] = "0.012"              # TRENDS saw $0.01
    desk.executor("POOL1", gecko_pool("POOL1", "MINT1"), "$DEEP", "MINT1", 80)
    assert not desk.d.positions and desk.d.cash == 100.0
    assert reasons(desk, "skip") == ["fugiu: +20% desde o TRENDS"]


def test_exec_does_not_buy_into_a_pool_that_drained_since_trends_saw_it(desk):
    on_the_market(desk, "POOL1", "MINT1")
    desk.live["POOL1"]["liquidity"]["usd"] = 30_000       # it was $250k at the scan; the floor is $40k
    desk.executor("POOL1", gecko_pool("POOL1", "MINT1"), "$DEEP", "MINT1", 80)
    assert not desk.d.positions and desk.d.cash == 100.0
    assert reasons(desk, "skip") == ["liquidez caiu desde o TRENDS: $30k < $40k"]


def test_exec_does_not_fill_a_quote_that_moves_the_price_more_than_the_router_allows(desk, monkeypatch):
    on_the_market(desk, "POOL1", "MINT1")
    monkeypatch.setattr(desk, "quote", lambda a, b, amount, session=None: {
        "outAmount": "1500000000", "priceImpactPct": "0.05", "routePlan": []})
    desk.executor("POOL1", gecko_pool("POOL1", "MINT1"), "$DEEP", "MINT1", 80)
    assert not desk.d.positions and desk.d.cash == 100.0
    assert reasons(desk, "skip") == ["impacto subiu desde o ROUTER: 5,0% slip"]


def test_a_coin_that_waited_11_minutes_in_the_queue_is_dropped_not_bought(desk):
    r = agreed(desk, "POOLN", "MINTN")
    r["since"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(timespec="seconds")
    desk.ready.append(r)
    desk.execute_best()
    assert not desk.ready and not desk.d.positions and desk.d.cash == 100.0
    assert reasons(desk, "skip") == ["esperou 10 min na fila"]


def test_a_second_buy_waits_two_minutes_after_the_first(desk):
    desk.ready.append(agreed(desk, "POOLN", "MINTN"))
    desk.last_buy = time.time()                           # something was bought just now
    desk.execute_best()
    assert not desk.d.positions and len(desk.ready) == 1  # it waits its turn
    desk.last_buy = time.time() - desk.p.buy_every_s - 1
    desk.execute_best()
    assert [p.pool for p in desk.d.positions] == ["POOLN"]


def test_no_third_position_is_opened_however_small_the_other_two_are(desk):
    for pool in "AB":
        put(desk, pool, cost=5.0).half_done = True        # $10 of a $100 desk: the 35% ceiling is far away
    desk.ready.append(agreed(desk, "POOLN", "MINTN"))
    desk.last_buy = 0.0
    desk.execute_best()
    assert len(desk.d.positions) == 2 and len(desk.ready) == 1 and not reasons(desk, "skip")
    desk.close(desk.d.positions[0], 1.0, "teste", "end")  # one of the two is gone: now there is room
    desk.execute_best()
    assert [p.pool for p in desk.d.positions] == ["B", "POOLN"]


def test_a_full_desk_says_how_many_are_open_and_how_many_wait(desk, monkeypatch):
    everyone_agrees(desk, monkeypatch)
    for pool in "AB":
        put(desk, pool, cost=5.0)
    for n in range(3):
        desk.ready.append(agreed(desk, f"POOL{n}", f"MINT{n}", f"N{n}"))
    on_the_market(desk, "POOLX", "MINTX")
    desk.auditor("POOLX", gecko_pool("POOLX", "MINTX", "XX"), "$XX")
    assert len(desk.ready) == 3 and desk.d.seen["POOLX"] == "exec"
    assert reasons(desk, "skip") == ["cheio: 2 abertas e 3 na fila"]


# -- the session's last minute, the state file, the wide scan
def test_a_gap_that_runs_into_the_session_end_is_still_told_and_marks_the_trade(desk, monkeypatch):
    put(desk, "A")
    desk.live = {"A": {"priceUsd": "1.0", "liquidity": {"usd": 100_000}}}
    monkeypatch.setattr(desk, "scan", lambda: None)
    monkeypatch.setattr(desk, "risk_loop", lambda: None)  # the risk thread never came back from the gap
    desk._loop_at = time.time() - 600                     # it last turned 10 minutes before the end
    desk.d.ends = datetime.now(timezone.utc).isoformat(timespec="seconds")
    desk.run()
    assert len(alerts(desk)) == 1 and alerts(desk)[0].startswith("sem vigilância durante 10 min")
    assert abs(desk.d.blind_s - 600) < 5 and [t["blind"] for t in desk.d.trades] == [True]
    assert [e["blind"] for e in desk.d.events if e["verdict"] == "sell"] == [True]     # the app's "sem vigia"


def test_the_balance_curve_gets_a_point_every_30_seconds_not_at_every_save(desk):
    from deskbot import memetest
    desk.save()
    n = len(desk.d.curve)
    for _ in range(5):
        desk.save()                                       # the risk loop saves every 5 s
    assert len(desk.d.curve) == n
    desk._curve_at -= memetest.CURVE_EVERY_S + 1
    desk.save()
    assert len(desk.d.curve) == n + 1
    desk.save(final=True)                                 # the last balance is always on the curve
    assert len(desk.d.curve) == n + 2


def test_a_state_that_grows_while_it_is_copied_is_copied_again(desk, monkeypatch, tmp_path):
    from deskbot import memetest
    real, calls = memetest.asdict, []

    def flaky(obj):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("dictionary changed size during iteration")
        return real(obj)
    monkeypatch.setattr(memetest, "asdict", flaky)
    desk.save()
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert len(calls) == 3 and state["mode"] == "memes"
    assert abs((datetime.now(timezone.utc) - datetime.fromisoformat(state["saved"])).total_seconds()) < 5

    def broken(obj):
        calls.append(1)
        raise RuntimeError("dictionary changed size during iteration")
    calls.clear()
    monkeypatch.setattr(memetest, "asdict", broken)
    with pytest.raises(RuntimeError):
        desk.save()                                       # three tries, then the caller hears of it
    assert len(calls) == 3


def test_the_wide_scan_makes_no_slow_call_once_the_session_is_stopped(desk):
    desk.quit.set()
    assert desk._wide(set()) == {}                        # NoNetwork would have raised on the first list


# -- a trade is a position, not a sale
def test_one_result_per_position_the_half_counts_with_it():
    from deskbot import guard
    trades = [{"id": "a", "kind": "half", "pnl_usd": 5.0, "pnl_eur": 4.0}, {"id": "a", "kind": "trail", "pnl_usd": -1.0},
              {"id": "b", "kind": "stop", "pnl_usd": -2.0}, {"id": "c", "kind": "half", "pnl_usd": 3.0}]
    assert guard.results(trades) == [4.0, -2.0]           # c has only its half sold: still open, no result yet
    assert guard.results(trades, "pnl_eur") == [4.0, 0.0]
    assert guard.results([{"pnl_usd": 1.0}, {"pnl_usd": -1.0}]) == [1.0, -1.0]        # old trades carry no id
    assert guard.results([]) == []


def test_the_report_counts_a_half_and_its_rest_as_one_winning_trade(desk):
    put(desk, "A")
    tick(desk, A=(2.05, 100_000))                         # half sold at a gain
    tick(desk, A=(0.5, 100_000))                          # the rest at a loss: the position as a whole won
    assert len(desk.d.trades) == 2 and sum(t["pnl_usd"] for t in desk.d.trades) > 0
    text = desk.report()
    assert "## Trades (1, 1 a ganhar)" in text and "Acerto: 100%." in text
