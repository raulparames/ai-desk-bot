from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from deskbot.backtest import metrics, run_backtest
from deskbot.config import RiskParams, StrategyParams, load_settings
from deskbot.desk import BUY, PASS, Desk
from deskbot.indicators import atr, ema, rsi
from deskbot.strategy import compute_signals, make_plan, regime_series


def synthetic(n=600, seed=0, drift=0.002):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.03, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.02, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.02, n))
    vol = rng.uniform(1e6, 3e6, n) * (1 + 3 * (rng.uniform(size=n) > 0.93))
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def test_indicators_basic():
    df = synthetic()
    assert ema(df["close"], 10).iloc[:9].isna().all()
    assert (atr(df) > 0).iloc[20:].all()
    r = rsi(df["close"]).dropna()
    assert r.between(0, 100).all()


def test_signals_do_not_look_ahead():
    """Appending future bars must not change any earlier signal."""
    df = synthetic(700)
    p = StrategyParams(regime_ema=50)
    short, full = df.iloc[:500], df
    s_short = compute_signals(short, regime_series(short, p), p)["signal"]
    s_full = compute_signals(full, regime_series(full, p), p)["signal"].iloc[:500]
    pd.testing.assert_series_equal(s_short, s_full)


def test_backtest_costs_reduce_returns():
    data = {"BTCUSDT": synthetic(800, 1), "ETHUSDT": synthetic(800, 2)}
    p = StrategyParams(regime_ema=50, volume_mult=1.0)
    free = run_backtest(data, p, RiskParams(fee=0, slippage=0))
    costly = run_backtest(data, p, RiskParams(fee=0.002, slippage=0.002))
    assert len(free.trades) > 0
    assert costly.equity.iloc[-1] < free.equity.iloc[-1]


def test_losses_capped_near_one_r_without_gaps():
    data = {"BTCUSDT": synthetic(800, 3)}
    res = run_backtest(data, StrategyParams(regime_ema=50, volume_mult=1.0), RiskParams())
    stops = [t.r_multiple for t in res.trades if t.reason == "stop"]
    assert stops and min(stops) > -1.6  # gaps can exceed 1R slightly, never wildly on this data


def test_position_size_respects_risk():
    data = {"BTCUSDT": synthetic(800, 4)}
    r = RiskParams(risk_per_trade=0.01, max_position_pct=1.0, max_positions=1)
    res = run_backtest(data, StrategyParams(regime_ema=50, volume_mult=1.0), r, initial=10_000)
    first = next(t for t in res.trades if abs(t.r_multiple) > 0.05)
    eq_before = res.equity[res.equity.index < first.opened].iloc[-1]
    risk_per_unit = (first.exit - first.entry) / first.r_multiple
    assert first.qty * risk_per_unit == pytest.approx(0.01 * eq_before, rel=0.02)

    capped = run_backtest(data, StrategyParams(regime_ema=50, volume_mult=1.0),
                          replace(r, risk_per_trade=0.05, max_position_pct=0.10), initial=10_000)
    t0 = capped.trades[0]
    assert t0.qty * t0.entry <= 0.10 * 10_000 * 1.001


def test_metrics_drawdown():
    eq = pd.Series([100, 120, 60, 90], index=pd.date_range("2024-01-01", periods=4))
    assert metrics(eq)["max_drawdown"] == pytest.approx(-0.5)


class _NoLLM:
    enabled = False
    calls = 0
    cost_usd = 0.0


def _plan(**over):
    df = synthetic(400, 5)
    p = StrategyParams(regime_ema=50)
    f = compute_signals(df, regime_series(df, p), p)
    row = f.iloc[-1].copy()
    for k, v in over.items():
        row[k] = v
    return make_plan("BTCUSDT", row, p)


def test_hard_rules_block_without_ai():
    desk = Desk(_NoLLM(), RiskParams(min_rr=2.0))
    plan = _plan(regime_on=False)
    v = desk.discuss(plan, ["s"], [])
    assert v.decision == PASS and v.hard_checks


def test_rules_only_buys_clean_plan():
    desk = Desk(_NoLLM(), RiskParams(min_rr=2.0))
    plan = _plan(regime_on=True, resistance=np.nan)
    v = desk.discuss(plan, ["s"], [])
    assert v.decision == BUY and v.size_multiplier == 1.0


def test_kill_switch_blocks():
    desk = Desk(_NoLLM(), RiskParams())
    v = desk.discuss(_plan(regime_on=True, resistance=np.nan), ["s"], [], halted=True)
    assert v.decision == PASS


def test_config_loads_sleeves():
    s = load_settings()
    assert len(s.sleeves) == 4
    assert {sl.params.breakout_lookback for sl in s.sleeves} == {20, 55}
    assert all(sl.params.min_room_r == s.strategy.min_room_r for sl in s.sleeves)


# ---------------------------------------------------------------- rules from the "5 agents" guide

def test_auditor_matches_signal():
    """Passing every Auditor layer is exactly the Scout's signal, whatever the optional filters."""
    from deskbot import auditor
    df = synthetic(700, seed=3)
    for p in (StrategyParams(regime_ema=50, min_room_r=2.0),
              StrategyParams(regime_ema=50, max_extension_atr=1.0, min_quality=40)):
        f = compute_signals(df, regime_series(df, p), p).dropna(subset=["atr", "breakout_level", "ema_trend"])
        got = [auditor.audit(row, p)["passed"] for _, row in f.iterrows()]
        assert got == f["signal"].tolist()


def test_auditor_first_red_cuts_the_rest():
    from deskbot import auditor
    df = synthetic(400)
    p = StrategyParams(regime_ema=50)
    f = compute_signals(df, regime_series(df, p), p).dropna(subset=["atr", "breakout_level"])
    a = next(auditor.audit(r, p) for _, r in f.iterrows() if not r["signal"])
    st = [x["status"] for x in a["layers"] if x["status"] != auditor.OFF]
    i = st.index(auditor.FAIL)
    assert all(s == auditor.OK for s in st[:i]) and all(s == auditor.SKIP for s in st[i + 1:])


def test_dead_money_and_trail_start():
    from deskbot.strategy import entry_ran, is_dead_money, trail_active
    p = StrategyParams(timeout_days=15, timeout_min_r=0.5, trail_start_r=1.0, max_entry_gap_pct=0.05)
    assert is_dead_money(15, 104.0, 100.0, 10.0, p)          # +0.4R after 15 days: out
    assert not is_dead_money(15, 105.0, 100.0, 10.0, p)      # +0.5R: stays
    assert not is_dead_money(14, 90.0, 100.0, 10.0, p)       # too early
    assert not trail_active(109.0, 100.0, 10.0, p) and trail_active(110.0, 100.0, 10.0, p)
    assert entry_ran(105.1, 100.0, p) and not entry_ran(104.9, 100.0, p)
    off = StrategyParams()
    assert not is_dead_money(999, 0.0, 100.0, 10.0, off) and trail_active(0, 100, 10, off)


def test_backtest_dead_money_exits():
    data = {"BTCUSDT": synthetic(900, seed=8), "ETHUSDT": synthetic(900, seed=9)}
    p = StrategyParams(regime_ema=50, timeout_days=10, timeout_min_r=0.5)
    res = run_backtest(data, p, RiskParams())
    timeouts = [t for t in res.trades if t.reason == "timeout"]
    assert timeouts and all((t.closed - t.opened).days >= 10 for t in timeouts)
    assert all(t.r_multiple < 0.5 for t in timeouts)


def test_settings_edit_keeps_comments(tmp_path):
    from deskbot import settings_edit
    from deskbot.config import ROOT
    cfg = tmp_path / "config.toml"
    cfg.write_text((ROOT / "config.toml").read_text(encoding="utf-8"), encoding="utf-8")
    ch = settings_edit.validate({"strategy.timeout_days": 20, "risk.risk_per_trade": 0.015})
    settings_edit.write(ch, cfg)
    text = cfg.read_text(encoding="utf-8")
    assert "timeout_days = 20" in text and "risk_per_trade = 0.015" in text
    assert text.count("#") == (ROOT / "config.toml").read_text(encoding="utf-8").count("#")
    s = load_settings(cfg)
    assert all(sl.params.timeout_days == 20 for sl in s.sleeves) and s.risk.risk_per_trade == 0.015
    with pytest.raises(ValueError):
        settings_edit.validate({"strategy.timeout_days": 999})
    with pytest.raises(ValueError):
        settings_edit.validate({"broker.kind": 1})


def test_phone_token_auth(monkeypatch, tmp_path):
    """Requests from this PC pass; anything else needs the phone token."""
    from deskbot import dashboard, phone
    monkeypatch.setattr(phone, "TOKEN_FILE", tmp_path / "phone_token.txt")
    tok = phone.token()
    assert len(tok) >= 30 and phone.token() == tok
    h = dashboard.Handler.__new__(dashboard.Handler)
    h.client_address, h.headers = ("192.168.1.50", 50000), {}
    assert not h._authed()
    h.headers = {"X-Desk-Token": "wrong"}
    assert not h._authed()
    h.headers = {"X-Desk-Token": tok}
    assert h._authed()
    h.client_address, h.headers = ("127.0.0.1", 50000), {}
    assert h._authed()
    new = phone.rotate()
    h.client_address, h.headers = ("192.168.1.50", 50000), {"X-Desk-Token": tok}
    assert new != tok and not h._authed()


def test_phone_password_and_lockout(monkeypatch, tmp_path):
    from deskbot import phone
    monkeypatch.setattr(phone, "PASSWORD_FILE", tmp_path / "phone_password.json")
    assert not phone.password_info()["set"] and not phone.check_password("anything")
    with pytest.raises(ValueError):
        phone.set_password("12345")
    phone.set_password("correto-cavalo")
    raw = (tmp_path / "phone_password.json").read_text(encoding="utf-8")
    assert "correto-cavalo" not in raw and phone.password_info()["set"]
    assert phone.check_password("correto-cavalo") and not phone.check_password("errado1")
    g = phone.LoginGuard(max_fails=3, lock_s=60)
    for _ in range(2):
        g.failed("1.2.3.4")
    assert g.wait("1.2.3.4") == 0
    g.failed("1.2.3.4")
    assert g.wait("1.2.3.4") > 0 and g.wait("5.6.7.8") == 0
    g.ok("1.2.3.4")
    assert g.wait("1.2.3.4") == 0


def _memedesk(monkeypatch, tmp_path):
    from deskbot import memetest
    monkeypatch.setattr(memetest, "OUT", tmp_path)
    monkeypatch.setattr(memetest.MemeTest, "_eur_usd", lambda self: 1.0)
    monkeypatch.setattr(memetest.MemeTest, "_pairs", lambda self, pools, session=None: {})  # no network
    return memetest, memetest.MemeTest(30, 1000)


def _pool(buyers, buys, vol_m5, vol_h1, h1=5.0, m5=0.0, liq=100_000.0, price=1.0):
    return {"attributes": {"name": "X / SOL", "reserve_in_usd": str(liq), "base_token_price_usd": str(price),
                           "volume_usd": {"m5": vol_m5, "h1": vol_h1},
                           "price_change_percentage": {"h1": h1, "m5": m5},
                           "transactions": {"h1": {"buyers": buyers, "buys": buys, "sells": 100}, "m5": {"buys": 5, "sells": 5}}}}


def test_meme_analyst_is_strict(monkeypatch, tmp_path):
    _, t = _memedesk(monkeypatch, tmp_path)
    t.analyst("bots", _pool(buyers=60, buys=1500, vol_m5=10_000, vol_h1=100_000), "BOTS", "m1")
    t.analyst("fading", _pool(buyers=300, buys=900, vol_m5=1_000, vol_h1=100_000), "FADE", "m2")
    t.analyst("chasing", _pool(buyers=300, buys=900, vol_m5=10_000, vol_h1=100_000, m5=12), "CHASE", "m3")
    t.analyst("good", _pool(buyers=300, buys=900, vol_m5=10_000, vol_h1=100_000), "GOOD", "m4")
    assert [r["coin"] for r in t.ready] == ["GOOD"]
    assert t.d.seen["bots"] == t.d.seen["fading"] == t.d.seen["chasing"] == "analyst"


def test_meme_executor_buys_best_score_first(monkeypatch, tmp_path):
    _, t = _memedesk(monkeypatch, tmp_path)
    bought = []
    monkeypatch.setattr(t, "executor", lambda addr, pool, coin, mint, score: bought.append(coin))
    now = "2099-01-01T00:00:00+00:00"
    t.ready = [{"score": 72, "since": now, "addr": "a", "pool": {}, "coin": "A", "mint": ""},
               {"score": 95, "since": now, "addr": "b", "pool": {}, "coin": "B", "mint": ""}]
    t.execute_best()
    t.execute_best()  # too soon: one buy every 2 minutes
    assert bought == ["B"]
    t.last_buy = 0
    t.execute_best()
    assert bought == ["B", "A"]


def test_meme_risk_exits(monkeypatch, tmp_path):
    memetest, t = _memedesk(monkeypatch, tmp_path)
    now = memetest._now().isoformat()
    for pid in ("stop", "trail", "hold"):
        t.d.positions.append(memetest.Position(pid, pid, pid, pid, "m", now, 1.0, 1.02, 50, 51, 100_000, 1.0, 1.0))
    t.d.cash -= 153
    t._apply_risk({"stop": {"priceUsd": "0.93", "liquidity": {"usd": 100_000}},
                   "trail": {"priceUsd": "1.30", "liquidity": {"usd": 100_000}},
                   "hold": {"priceUsd": "1.05", "liquidity": {"usd": 100_000}}}, final=False)
    assert [p.id for p in t.d.positions] == ["trail", "hold"] and t.d.trades[0]["reason"].startswith("stop")
    t._apply_risk({"trail": {"priceUsd": "1.14", "liquidity": {"usd": 100_000}},
                   "hold": {"priceUsd": "1.05", "liquidity": {"usd": 500}}}, final=False)
    assert [tr["reason"] for tr in t.d.trades] == ["stop -6%", "trailing stop", "liquidez retirada"]
    assert not t.d.positions


def test_futures_desk_goes_long_and_short_without_lookahead():
    from deskbot import futures as F
    idx = pd.date_range("2021-01-01", periods=900, freq="4h")
    up = np.r_[np.linspace(100, 200, 450), np.linspace(200, 90, 450)]
    rng = np.random.default_rng(1)
    close = up * (1 + rng.normal(0, 0.004, 900))
    df = pd.DataFrame({"open": close, "high": close * 1.004, "low": close * 0.996, "close": close, "volume": 1.0}, index=idx)
    p = F.FutParams(slow_ema=100)
    a = F.signals(df.iloc[:600], p)[["long", "short"]]
    b = F.signals(df, p)[["long", "short"]].iloc[:600]
    pd.testing.assert_frame_equal(a, b)          # future candles never change past signals
    res = F.backtest({"BTCUSDT": (df, pd.Series(dtype=float))}, p, 10_000)
    sides = {t["side"] for t in res["trades"]}
    assert sides == {1, -1}                       # it trades both ways
    assert res["equity"].iloc[-1] > 0


def test_stocks_shelter_can_fill_several_slots():
    from collections import Counter
    from deskbot import stocks as S
    idx = pd.bdate_range("2015-01-01", periods=400)
    falling = pd.Series(np.linspace(200, 100, 400), index=idx)
    rising = pd.Series(np.linspace(100, 200, 400), index=idx)
    closes = pd.DataFrame({s: falling for s in S.RISK} | {"GLD": rising, "TLT": falling})
    want = S.ranking(closes, 399, S.StockParams(top=3, universe=tuple(S.RISK)))
    assert Counter(want) == Counter({"GLD": 3})   # nothing in trend but gold: all three slots in gold


def test_stocks_desk_without_stop_keeps_a_losing_position(monkeypatch, tmp_path):
    """stop = 0 means no stop: a small loss must not sell (bug found on the first live day)."""
    from deskbot import paperdesk, stocks as S
    monkeypatch.setattr(paperdesk, "DESKS_DIR", tmp_path)
    monkeypatch.setattr(paperdesk, "eur_usd", lambda: 1.0)
    idx = pd.bdate_range("2020-01-01", periods=300)
    up = pd.DataFrame({"open": np.linspace(100, 200, 300), "high": np.linspace(101, 201, 300),
                       "low": np.linspace(99, 199, 300), "close": np.linspace(100, 200, 300)}, index=idx)
    monkeypatch.setattr(S, "load", lambda refresh=True: {s: up for s in S.NAMES})
    desk = S.StocksDesk()
    desk.step()
    assert [q["symbol"] for q in desk.L.positions] == ["SPY"]
    dip = up.copy()
    dip.iloc[-1, dip.columns.get_loc("close")] = 199.0   # a 0.5% dip
    monkeypatch.setattr(S, "load", lambda refresh=True: {s: dip for s in S.NAMES})
    desk2 = S.StocksDesk()
    desk2.step()
    assert [q["symbol"] for q in desk2.L.positions] == ["SPY"] and not desk2.L.trades


def test_requests_through_the_https_proxy_are_not_trusted(monkeypatch, tmp_path):
    """On the server every request reaches the dashboard from 127.0.0.1 via the proxy: it must still need the key."""
    from deskbot import dashboard, phone
    monkeypatch.setattr(phone, "TOKEN_FILE", tmp_path / "phone_token.txt")
    tok = phone.token()
    h = dashboard.Handler.__new__(dashboard.Handler)
    h.client_address = ("127.0.0.1", 50000)
    h.headers = {"X-Forwarded-For": "203.0.113.9"}
    assert not h._local() and not h._authed() and h._client_ip() == "203.0.113.9"
    h.headers = {"X-Forwarded-For": "203.0.113.9", "X-Desk-Token": tok}
    assert h._authed()


def test_login_guard_locks_everyone_after_many_failures():
    from deskbot import phone
    g = phone.LoginGuard(max_fails=5, lock_s=60, global_fails=6)
    for i in range(6):
        g.failed(f"10.0.0.{i}")          # one wrong guess each from 6 addresses
    assert g.wait("10.0.0.99") > 0       # a new address is locked out too


def test_memes_mode_buys_only_when_all_five_agree(monkeypatch, tmp_path):
    """TRENDS → SMARTS → DEBATE → ROUTER → EXEC: one 'no' stops the coin (no network in this test)."""
    from datetime import datetime, timezone
    from deskbot import memetest, memedesk
    monkeypatch.setattr(memetest, "OUT", tmp_path)
    monkeypatch.setattr(memetest.MemeTest, "_eur_usd", lambda self: 1.0)
    desk = memedesk.MemesDesk(30, 110)
    desk.gmgn.key = ""   # no GMGN calls in tests, whatever is in .env
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    whales = [{"attributes": {"block_timestamp": now, "kind": "buy", "tx_from_address": f"w{i}",
                              "volume_in_usd": "900"}} for i in range(4)]
    info = {"data": {"attributes": {"holders": {"distribution_percentage": {"top_10": "12"}}, "is_honeypot": "no"}}}
    monkeypatch.setattr(desk, "_gecko", lambda path, params=None: {"data": whales} if "/trades" in path else info)
    monkeypatch.setattr(desk, "_mint_info", lambda mint: {"mintAuthority": None, "freezeAuthority": None, "decimals": 6})
    fresh = {"priceUsd": "0.01", "liquidity": {"usd": 250_000}, "volume": {"m5": 9_000, "h1": 90_000},
             "priceChange": {"m5": -2, "h1": 10}}
    monkeypatch.setattr(desk, "_pairs", lambda pools, session=None: {p: fresh for p in pools})
    monkeypatch.setattr(desk, "sol_usd", lambda: 150.0)
    monkeypatch.setattr(desk, "quote", lambda a, b, amount, session=None: {"outAmount": "1000000000", "priceImpactPct": "0.004",
                                                                          "routePlan": [{"swapInfo": {"label": "Meteora"}}]})
    pool = {"attributes": {"name": "DEEP / SOL", "reserve_in_usd": "250000", "base_token_price_usd": "0.01",
                           "transactions": {"h1": {"buyers": 300, "buys": 900, "sells": 400}},
                           "volume_usd": {"m5": 9000, "h1": 90000}, "price_change_percentage": {"m5": -2, "h1": 10}},
            "relationships": {"base_token": {"data": {"id": "solana_MINT1"}}}}
    desk.auditor("POOL1", pool, "$DEEP")
    assert [r["coin"] for r in desk.ready] == ["$DEEP"]
    assert any(e["stage"] == "CORE" and e["agent"] == "ANALYST" and "5/5" in e["reason"] for e in desk.d.events)
    # whales selling: SMARTS says no and nothing else runs
    sellers = [{"attributes": {**w["attributes"], "kind": "sell"}} for w in whales]
    monkeypatch.setattr(desk, "_gecko", lambda path, params=None: {"data": sellers} if "/trades" in path else info)
    desk.auditor("POOL2", pool, "$BRICK")
    assert desk.d.seen["POOL2"] == "smarts" and len(desk.ready) == 1


def test_memes_gmgn_smart_money_can_pass_smarts_and_rug_score_blocks(monkeypatch, tmp_path):
    """With a GMGN key: smart money + KOLs can carry SMARTS, and a high rug score stops DEBATE."""
    from deskbot import memetest, memedesk
    monkeypatch.setattr(memetest, "OUT", tmp_path)
    monkeypatch.setattr(memetest.MemeTest, "_eur_usd", lambda self: 1.0)
    desk = memedesk.MemesDesk(30, 110, "fomo")
    assert desk.d.venue == "fomo"
    desk.gmgn.key = "test"
    gm = {"smart": 3, "kol": 1, "rug": 0.1, "wash": False, "bundler": 0.05, "rat": 0.02, "top10": 0.2, "insider": 0}
    monkeypatch.setattr(desk.gmgn, "token", lambda mint: dict(gm))
    monkeypatch.setattr(desk, "_gecko", lambda path, params=None: {"data": []})   # no whale trades at all
    assert desk.smarts("POOL", "$X", "MINT")["ok"] is True
    assert "3 SM · 1 KOL" in desk.d.agents["SCANNER"]["text"]
    gm.update(smart=0, kol=1)
    assert desk.smarts("POOL", "$X", "MINT")["ok"] is False
    gm.update(rug=0.6)
    info = {"data": {"attributes": {"holders": {"distribution_percentage": {"top_10": "12"}}}}}
    monkeypatch.setattr(desk, "_gecko", lambda path, params=None: info)
    monkeypatch.setattr(desk, "_mint_info", lambda mint: {"mintAuthority": None, "freezeAuthority": None, "decimals": 6})
    monkeypatch.setattr(desk, "_pairs", lambda pools, session=None: {})
    pool = {"attributes": {"reserve_in_usd": "250000", "transactions": {"h1": {"buyers": 300, "buys": 900, "sells": 400}},
                           "volume_usd": {"m5": 9000, "h1": 90000}, "price_change_percentage": {"m5": -2, "h1": 10}}}
    out = desk.debate("POOL", pool, "$X", "MINT")
    assert out["ok"] is False and "rug 0,60" in desk.d.agents["ANALYST"]["text"]


def test_futures_mode_opens_only_on_5_of_5_and_respects_leverage(monkeypatch, tmp_path):
    """Futures terminal: top traders against us stop the trade; when all agree it opens at most the coin's leverage."""
    from deskbot import perpdesk
    monkeypatch.setattr(perpdesk, "OUT", tmp_path)
    monkeypatch.setattr(perpdesk.PerpDesk, "_eur_usd", lambda self: 1.0)
    desk = perpdesk.PerpDesk(30, 110, lev=10)
    desk.meta = {"AAA": {"max_lev": 5, "funding": 0.0000125, "mark": 100.0, "vol": 5e8, "oi": 1e8, "day_pct": 0.02}}
    # a steady up-trend with rising volume, 15-min and 1-hour candles
    c15 = [{"t": i, "o": 100 + i * 0.05, "h": 100.4 + i * 0.05, "l": 99.8 + i * 0.05, "c": 100.1 + i * 0.05,
            "v": 100 + (40 if i > 115 else 0)} for i in range(120)]
    c15[-1]["c"] = c15[-2]["c"] + 0.05
    h1 = [{"t": i, "o": 90 + i * 0.2, "h": 90.5 + i * 0.2, "l": 89.8 + i * 0.2, "c": 90.2 + i * 0.2, "v": 1000} for i in range(80)]
    desk.live.mode = "off"                                # the dry-run bridge has its own tests
    monkeypatch.setattr(desk, "candles", lambda coin, interval, bars, *a, **kw: c15 if interval == "15m" else h1)
    book = {"levels": [[{"px": "105.99", "sz": "500"}], [{"px": "106.00", "sz": "500"}]]}
    monkeypatch.setattr(desk, "info", lambda body, session=None, **kw: book if body["type"] == "l2Book" else None)
    desk.crowd = {"AAA": {"long": 1, "short": 6, "long_usd": 1e6, "short_usd": 9e6}}
    desk.crowd_at = __import__("time").time()             # the top traders were read just now
    desk.trends("AAA")
    assert desk.queue and desk.queue[0][1] == 1           # TRENDS says long
    desk.auditor(*desk.queue.pop(0))
    assert not desk.ready and desk.d.agents["SCANNER"]["ok"] is False
    desk.cool.clear()
    desk.crowd = {"AAA": {"long": 6, "short": 1, "long_usd": 9e6, "short_usd": 1e6}}
    desk.trends("AAA")
    desk.auditor(*desk.queue.pop(0))
    assert [r["coin"] for r in desk.ready] == ["AAA"]
    assert any(e["stage"] == "CORE" and e["agent"] == "ANALYST" and "5/5" in e["reason"] for e in desk.d.events)
    desk.execute_best()
    q = desk.d.positions[0]
    assert q.side == 1 and q.lev == 5                     # session asked 10x, the coin allows 5x
    assert q.liq < q.stop < q.entry                       # the stop comes before liquidation
    desk.close(q, q.entry * 1.01, "teste")
    assert desk.d.trades[0]["pnl_usd"] > 0 and not desk.d.positions


def test_compounder_sizes_15pct_of_the_desk_sells_half_at_2x_then_trails(monkeypatch, tmp_path):
    """Memes Compounder: 15% of the desk per buy (it was 40% of free cash, now only a ceiling), max 2 open;
    -8% stop before 2x; half at 2x; trail 10% after."""
    from deskbot import memetest, memedesk
    monkeypatch.setattr(memetest, "OUT", tmp_path)
    monkeypatch.setattr(memetest, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(memetest.MemeTest, "_eur_usd", lambda self: 1.0)
    desk = memedesk.MemesDesk(30, 100)
    desk.gmgn.key = ""
    assert desk.p.max_positions == 2 and desk.trade_size(10_000_000) == 15.0
    assert desk.p.stack_pct == 0.40 and desk.p.max_trade_pct == 0.15 and desk.p.max_deployed_pct == 0.35
    desk._sol = (0.0, 0.0)            # no SOL price: exits use the pool model, no network
    def pos(pool, cost):
        p = memetest.Position(pool, "$" + pool, pool, pool, "MINT" + pool, "2026-01-01T00:00:00+00:00",
                              1.0, 1.0, cost, cost, 1e12, 1.0, 1.0)
        desk.d.cash -= cost
        desk.d.positions.append(p)
        return p
    a, b = pos("A", 40.0), pos("B", 24.0)
    live = lambda px_a, px_b: {"A": {"priceUsd": px_a, "liquidity": {"usd": 1e12}},
                               "B": {"priceUsd": px_b, "liquidity": {"usd": 1e12}}}
    desk._apply_risk(live(2.05, 0.91), False)          # A doubles, B falls 9%
    assert a.half_done and abs(a.qty - 20.0) < 1e-9   # half sold, the rest rides
    assert b not in desk.d.positions and desk.d.trades[-1]["kind"] == "stop"
    half = next(t for t in desk.d.trades if t["kind"] == "half")
    assert half["got_usd"] >= 40.0 * 0.98              # the half brought the entry back (less costs)
    desk._apply_risk(live(3.0, 0.91), False)           # new high: trail moves up
    desk._apply_risk(live(2.6, 0.91), False)           # more than 10% off 3.0
    assert a not in desk.d.positions and desk.d.trades[-1]["kind"] == "trail"
    assert {e["agent"] for e in desk.d.events if e["verdict"] == "sell"} == {"RISK"}
    assert {t["fill_src"] for t in desk.d.trades} == {"model"}      # no quote was possible: said on every trade


def test_push_only_to_real_push_services_and_texts():
    from deskbot import push
    assert push.push_host_ok("https://web.push.apple.com/QGx1")
    assert push.push_host_ok("https://fcm.googleapis.com/fcm/send/x")
    assert push.push_host_ok("https://wns2-db5p.notify.windows.com/w/?token=x")
    for bad in ("http://web.push.apple.com/x", "https://evil.example.com/x", "https://web.push.apple.com.evil.com/x",
                "https://evilpush.apple.com/x", "not a url"):
        assert not push.push_host_ok(bad), bad
    keys = {"p256dh": "B" + "x" * 86, "auth": "a" * 22}
    assert push.clean_sub({"endpoint": "https://web.push.apple.com/abc", "keys": keys})["keys"] == keys
    assert push.clean_sub({"endpoint": "https://example.com/abc", "keys": keys}) is None
    buy = push.message("memes", {"verdict": "buy", "coin": "$FLINT", "eur": 21.36, "score": 81, "ts": "t"}, "GMGN")
    assert buy["title"] == "Comprou $FLINT · 21,36 €" and "na GMGN (papel)" in buy["body"]
    assert buy["url"] == "memes.html#/moeda/%24FLINT"
    half = push.message("memes", {"verdict": "sell", "kind": "half", "coin": "$W", "pnl_eur": 20.14, "ts": "t"}, "GMGN")
    assert half["title"] == "Vendeu metade de $W · +20,14 €" and "entrada voltou" in half["body"]
    stop = push.message("perps", {"verdict": "sell", "kind": "stop", "coin": "SOL", "pnl_eur": -1.31, "ts": "t"}, "INVO")
    assert stop["title"] == "Parou SOL · −1,31 €"


def test_pusher_announces_only_new_trades_once(monkeypatch, tmp_path):
    """First look announces nothing old; a new buy goes out once; what the other runner already sent is skipped."""
    import json as _json
    import pywebpush
    from deskbot import push
    sent = []
    monkeypatch.setattr(pywebpush, "webpush", lambda **kw: sent.append(_json.loads(kw["data"])))
    state = tmp_path / "memes.json"
    monkeypatch.setattr(push, "DESKS", {"memes": state})
    ev = lambda ts, coin: {"ts": ts, "agent": "EXECUTOR", "coin": coin, "verdict": "buy", "eur": 10, "score": 70}
    write = lambda events, **kw: state.write_text(_json.dumps({"events": events, "venue": "fomo", "started": "s1", **kw}))

    class FakeRelay:
        enabled = True
        def __init__(self):
            self.kv = {"push:sub:aaa": {"value": {"endpoint": "https://web.push.apple.com/a", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}}}
        def get(self, prefix):
            return {k: v for k, v in self.kv.items() if k.startswith(prefix)}
        def push(self, items):
            self.kv.update({k: {"value": v} for k, v in items.items()})

    relay = FakeRelay()
    p = push.Pusher(relay)
    p.key = "test"
    write([ev("2026-11-09T10:00:00+00:00", "$OLD")])
    p.tick()
    assert sent == []                                   # first look: old trades and old phones stay quiet
    write([ev("2026-11-09T10:00:00+00:00", "$OLD"), ev("2026-11-09T10:05:00+00:00", "$NEW")])
    p.tick()
    assert [m["title"] for m in sent] == ["Comprou $NEW · 10,00 €"] and "na FOMO" in sent[0]["body"]
    p.tick()
    assert len(sent) == 1                               # never twice
    other = push.Pusher(relay)                          # e.g. GitHub after the PC: it sees the same file
    other.key = "test"
    other.seen = {"memes": "2026-11-09T10:00:00+00:00"}
    other.ended = {"memes": ""}
    other.tick()
    assert len(sent) == 1                               # already announced by the first runner
    relay.kv["push:sub:bbb"] = {"value": {"endpoint": "https://fcm.googleapis.com/x", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}}
    p.subs_at = 0
    write([ev("2026-11-09T10:05:00+00:00", "$NEW")], finished=True, start_eur=110, equity_eur=121, trades=[{}, {}])
    p.tick()
    titles = [m["title"] for m in sent[1:]]
    assert titles[0] == "Notificações ligadas ✓"        # the new phone gets a first one
    assert any(t.startswith("Sessão de memes terminou · 121,00 €") for t in titles)
