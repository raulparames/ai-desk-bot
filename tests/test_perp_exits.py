"""The futures desk's exit engine, offline: liquidation, stop, half at 2R, trailing, session end, missing
prices, funding and costs. Every test drives PerpDesk.risk() with made-up prices; nothing touches the network."""

import time

import pytest

FEE, SLIP = 0.00045, 0.0002


@pytest.fixture
def desk(monkeypatch, tmp_path):
    from deskbot import perpdesk
    monkeypatch.setattr(perpdesk, "OUT", tmp_path)
    monkeypatch.setattr(perpdesk.PerpDesk, "_eur_usd", lambda self: 1.0)
    d = perpdesk.PerpDesk(30, 100, lev=5)
    d.live.mode = "off"                                  # the dry-run bridge is tested in test_live.py
    d.meta = {"AAA": {"max_lev": 10, "funding": 0.0}, "BBB": {"max_lev": 10, "funding": 0.0}}
    d.mids = {}
    monkeypatch.setattr(d, "info", lambda body, session=None, **kw: dict(d.mids) if body["type"] == "allMids" else None)
    return d


def put(desk, side, coin="AAA", entry=100.0, margin=20.0, lev=5, atr=1.0):
    """A position as open() would have made it: 1.5 ATR stop, liquidation at 1/lev less the maintenance margin."""
    from deskbot import perpdesk
    qty = margin * lev / entry
    fee = qty * entry * FEE
    q = perpdesk.Perp(coin.lower(), coin, side, lev, "2026-01-01T00:00:00+00:00", entry, qty, margin, atr,
                      entry - side * 1.5 * atr, entry * (1 - side * (1 / lev - 0.05)), entry, entry, fees=fee)
    desk.d.cash -= margin + fee
    desk.d.positions.append(q)
    return q


def tick(desk, final=False, **mids):
    desk.mids = {k: str(v) for k, v in mids.items()}
    desk.risk(final)


@pytest.mark.parametrize("side", [1, -1])
def test_stop_closes_at_the_price_seen_less_slippage_and_fee(desk, side):
    q = put(desk, side)
    cash = desk.d.cash
    tick(desk, AAA=100 - side * 1.4)                     # not there yet
    assert q in desk.d.positions and not desk.d.trades
    px = 100 - side * 1.6
    tick(desk, AAA=px)
    t = desk.d.trades[-1]
    exit_px = px * (1 - side * SLIP)
    fee_out = q.qty * exit_px * FEE
    got = 20.0 + side * q.qty * (exit_px - 100.0) - fee_out
    assert not desk.d.positions and t["kind"] == "stop" and "1.5 ATR" in t["reason"]
    assert t["exit"] == pytest.approx(exit_px) and (exit_px - px) * side < 0      # slippage is against us
    assert desk.d.cash == pytest.approx(cash + got)
    assert t["pnl_usd"] == pytest.approx(got - 20.0 - q.fees) and t["pnl_usd"] < 0
    assert t["fees"] == pytest.approx(q.fees + fee_out)
    assert desk.d.events[-1]["verdict"] == "sell" and desk.d.events[-1]["agent"] == "RISK"


@pytest.mark.parametrize("side", [1, -1])
def test_liquidation_takes_the_whole_margin_and_comes_before_the_stop(desk, side):
    q = put(desk, side)
    cash = desk.d.cash
    assert (q.stop - q.liq) * side > 0                   # the stop sits before liquidation
    tick(desk, AAA=100 - side * 16)                      # through both in one reading
    t = desk.d.trades[-1]
    assert t["kind"] == "liq" and not desk.d.positions
    assert desk.d.cash == pytest.approx(cash)            # nothing comes back
    assert t["pnl_usd"] == pytest.approx(-20.0 - q.fees) and t["mult"] < 0.01


@pytest.mark.parametrize("side", [1, -1])
def test_half_at_2r_moves_the_stop_to_the_entry_then_the_rest_trails(desk, side):
    q = put(desk, side)
    cash, fee_in = desk.d.cash, q.fees
    tick(desk, AAA=100 + side * 2.9)                     # 2R is 3.0 away
    assert not q.half_done
    px = 100 + side * 3.1
    tick(desk, AAA=px)
    exit_px = px * (1 - side * SLIP)
    got = 10.0 + side * 0.5 * (exit_px - 100.0) - 0.5 * exit_px * FEE
    half = desk.d.trades[-1]
    assert q.half_done and q in desk.d.positions and half["kind"] == "half"
    assert q.qty == pytest.approx(0.5) and q.margin == pytest.approx(10.0) and q.stop == 100.0
    assert q.fees == pytest.approx(fee_in / 2)
    assert desk.d.cash == pytest.approx(cash + got)
    assert half["pnl_usd"] == pytest.approx(got - 10.0 - fee_in / 2) and half["pnl_usd"] > 0
    tick(desk, AAA=100 + side * 6)                       # new best price: the trail follows, 1.5 ATR behind
    assert q in desk.d.positions and q.peak == 100 + side * 6
    tick(desk, AAA=100 + side * 4.6)                     # 1.4 off the best: still in
    assert q in desk.d.positions
    tick(desk, AAA=100 + side * 4.4)                     # 1.6 off the best
    t = desk.d.trades[-1]
    assert not desk.d.positions and t["kind"] == "trail" and t["pnl_usd"] > 0
    assert t["id"] == half["id"] and t["half_px"] == px
    assert [e["kind"] for e in desk.d.events if e["verdict"] == "sell"] == ["half", "trail"]


@pytest.mark.parametrize("side", [1, -1])
def test_after_the_half_a_gap_back_through_the_entry_is_a_stop_at_the_entry(desk, side):
    q = put(desk, side)
    tick(desk, AAA=100 + side * 3.2)
    assert q.half_done
    tick(desk, AAA=100 - side * 0.3)                     # straight through the trail and the entry
    t = desk.d.trades[-1]
    assert t["kind"] == "stop" and t["reason"] == "stop na entrada" and t["pnl_usd"] < 0


@pytest.mark.parametrize("side", [1, -1])
def test_session_end_closes_at_the_price_and_without_a_price_at_the_last_one(desk, side):
    a, b = put(desk, side, "AAA"), put(desk, side, "BBB")
    tick(desk, AAA=100 + side * 0.5, BBB=100 + side * 0.7)
    assert len(desk.d.positions) == 2 and b.last == 100 + side * 0.7
    tick(desk, final=True, AAA=100 + side * 1.0)         # BBB has no price in the last reading
    by = {t["coin"]: t for t in desk.d.trades}
    assert not desk.d.positions and by["AAA"]["kind"] == by["BBB"]["kind"] == "end"
    assert by["AAA"]["exit"] == pytest.approx((100 + side * 1.0) * (1 - side * SLIP))
    assert by["BBB"]["exit"] == pytest.approx((100 + side * 0.7) * (1 - side * SLIP))
    assert "último preço" in by["BBB"]["reason"]


@pytest.mark.parametrize("side", [1, -1])
def test_2r_reached_on_the_last_reading_closes_the_half_and_then_the_rest(desk, side):
    put(desk, side)
    tick(desk, final=True, AAA=100 + side * 3.1)
    assert not desk.d.positions and [t["kind"] for t in desk.d.trades] == ["half", "end"]    # the rest stayed open before
    assert desk.d.trades[-1]["reason"] == "fim da sessão" and all(t["pnl_usd"] > 0 for t in desk.d.trades)
    assert desk.d.trades[0]["id"] == desk.d.trades[1]["id"] and desk.equity() == pytest.approx(desk.d.cash)


def test_a_missing_or_zero_price_changes_nothing(desk):
    q = put(desk, 1)
    for mids in ({}, {"AAA": 0}, {"AAA": "nan"}, {"BBB": 50}):
        tick(desk, **mids)
    assert desk.d.positions == [q] and q.last == 100.0 and not desk.d.trades


@pytest.mark.parametrize("side", [1, -1])
def test_funding_is_paid_by_the_side_the_rate_is_against(desk, side):
    desk.meta["AAA"]["funding"] = 0.0001                 # per hour, positive: longs pay shorts
    q = put(desk, side)
    tick(desk, AAA=100.0)                                # first reading starts the clock
    assert q.funding == 0.0
    q._tick = time.time() - 3600                         # one hour later
    tick(desk, AAA=100.0)
    assert q.funding == pytest.approx(side * q.qty * 100.0 * 0.0001, rel=0.01)
    cash = desk.d.cash
    funding = q.funding
    desk.close(q, 100.0, "teste")
    exit_px = 100.0 * (1 - side * SLIP)
    got = 20.0 + side * 1.0 * (exit_px - 100.0) - funding - exit_px * FEE
    assert desk.d.cash == pytest.approx(cash + got)
    assert desk.d.trades[-1]["funding"] == pytest.approx(funding)


def test_equity_counts_open_positions_at_the_last_price_and_never_below_zero(desk):
    q = put(desk, 1)
    tick(desk, AAA=101.0)
    assert desk.equity() == pytest.approx(desk.d.cash + 20.0 + 1.0)
    q.last = 50.0                                        # far past liquidation: the margin is the most it can lose
    assert desk.value(q) == 0.0
