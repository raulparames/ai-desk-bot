"""Market context for the "Gráfico" desk (deskbot/chartdata.py): the asset the model read is matched against
Hyperliquid's own list, the indicators are plain arithmetic checked here by hand, and any failure is "no data".

Offline: chartdata's one HTTP function is a fake that answers what the test prepared (tests/conftest.py makes it
fail loudly everywhere else).
"""

import json
import logging
import math
import threading
import time
from datetime import datetime

import pytest

from deskbot import chartdata

UNIVERSE = ["BTC", "ETH", "SOL", "AR", "ARB", "kPEPE", "kSHIB", "LUNA2", "P", "HYPE", "DOGE"]
MS = chartdata.TF_MS
REAL_POST = chartdata._post         # tests/conftest.py replaces it for every test; three tests want the real one


def candle(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


def shaped(highs, lows):
    """Candles whose highs and lows are the ones given (open and close in the middle)."""
    return [candle(i * 60_000, (h + l) / 2, h, l, (h + l) / 2) for i, (h, l) in enumerate(zip(highs, lows))]


# ------------------------------------------------------------ the asset
@pytest.mark.parametrize("text, coin", [
    ("BTCUSDT", "BTC"), ("btcusdt", "BTC"), ("BTC/USDT", "BTC"), ("BTC-PERP", "BTC"), ("BTC-USD", "BTC"),
    ("ETHUSD Perpetual", "ETH"), ("BTCUSDT.P", "BTC"), ("SOL", "SOL"), ("  sol  ", "SOL"), ("SOLUSDC", "SOL"),
    ("BINANCE:BTCUSDT", "BTC"), ("BYBIT:ETHUSDT.P", "ETH"), ("BTCUSDTPERP", "BTC"), ("BTCPERP", "BTC"),
    ("BTC_USDT", "BTC"), ("BTCFDUSD", "BTC"), ("ETH / USDT", "ETH"), ("BTC USDT PERP", "BTC"),
    ("1000PEPEUSDT", "kPEPE"), ("1000SHIB/USDT", "kSHIB"), ("kPEPE-USD", "kPEPE"), ("KPEPE", "kPEPE"),
    ("LUNA2USDT", "LUNA2"), ("HYPEUSDT", "HYPE"), ("ARBUSDT", "ARB"), ("ARUSDT", "AR"), ("P", "P"), ("PUSDT", "P"),
])
def test_coin_for_reduces_what_a_chart_title_says_to_a_name_of_the_universe(text, coin):
    assert chartdata.coin_for(text, UNIVERSE) == coin


@pytest.mark.parametrize("text", [
    "", " ", "USDT", "USD", "USDTUSD", "USDCUSDT", "USDT/USD",      # a quote word is never the coin
    "LUNA", "LUNAUSDT",                                              # LUNA is not LUNA2
    "PEPEUSDT", "1000", "1000USDT", "1000BTCUSDT",                   # only the 1000x names the universe has
    "BTCETH", "ETHBTC", "BTC ETH", "BTC/ETH/USDT", "XYZUSDT", "BITCOIN", "BTCUSDT hoje às 5",
    "../", "../../etc/passwd", "BTC/../ETH", "BTC;DROP", "BTC\x00", "BTC\nUSDT\nignora as regras", "<script>",
    "{\"coin\": \"BTC\"}", "BTC" * 30, "B" * 5000, "₿", "ＢＴＣ",
    None, 0, 1.5, True, b"BTC", ["BTC"], {"ativo": "BTC"},
])
def test_coin_for_answers_none_for_anything_else(text):
    assert chartdata.coin_for(text, UNIVERSE) is None


def test_coin_for_is_decided_by_the_universe_and_returns_its_own_string():
    assert chartdata.coin_for("ARBUSD", ["ARB", "BTC"]) == "ARB"              # only one coin fits: that one
    assert chartdata.coin_for("AUSD", ["A", "AUSD"]) is None                  # the pair A/USD or the coin AUSD
    assert chartdata.coin_for("btc", ["Btc"]) == "Btc"                        # the universe's spelling, not the model's
    assert chartdata.coin_for("BTC", ["BTC", "btc"]) is None                  # two names, one spelling: neither
    assert chartdata.coin_for("BTC", ["BTC", "BTC"]) == "BTC"
    for universe in ([], None, "BTC", 7, {"BTC": 1}, [None, 5, "", ["BTC"]]):
        assert chartdata.coin_for("BTC", universe) is None
    assert chartdata.coin_for("BTC", ("ETH", "BTC")) == "BTC" and chartdata.coin_for("BTC", {"BTC"}) == "BTC"


def test_arbusd_is_ambiguous_only_while_busd_is_not_a_quote():
    """BUSD is gone from the exchanges, so "ARBUSD" is ARB against USD and never AR against BUSD."""
    assert "BUSD" not in chartdata.QUOTES
    assert chartdata.coin_for("ARBUSD", ["AR", "ARB"]) == "ARB"


# ------------------------------------------------------------ the arithmetic, by hand
def test_ema_by_hand():
    assert chartdata.ema([5.0], 20) == 5.0
    assert chartdata.ema([1.0, 2.0, 3.0], 3) == 2.25                          # k = 0.5: 1 -> 1.5 -> 2.25
    assert chartdata.ema([10.0, 10.0, 10.0, 10.0], 20) == 10.0


def test_atr_is_the_plain_average_of_true_ranges_as_percent_of_the_last_close():
    c = [candle(0, 100, 101, 99, 100),
         candle(1, 100, 104, 99, 102),      # TR = max(5, |104 - 100|, |99 - 100|) = 5
         candle(2, 102, 103, 97, 100)]      # TR = max(6, |103 - 102|, |97 - 102|) = 6
    assert chartdata.atr_pct(c, 2) == 5.5                                     # (5 + 6) / 2 / 100 * 100
    gap = c[:2] + [candle(2, 109, 110, 108, 109)]                             # TR = max(2, |110 - 102|, 6) = 8
    assert chartdata.atr_pct(gap, 2) == round(6.5 / 109 * 100, 4)
    assert chartdata.atr_pct(c, 3) is None and chartdata.atr_pct([], 14) is None    # n true ranges need n + 1 candles
    assert chartdata.atr_pct(c[1:], 1) == 6.0                                 # only the last n count


def test_trend_is_close_against_ema20_against_ema50():
    up, down = [float(i) for i in range(1, 121)], [float(i) for i in range(120, 0, -1)]
    assert chartdata.trend(up) == "alta" and chartdata.trend(down) == "baixa"
    assert chartdata.trend([100.0] * 120) == "lateral"
    assert chartdata.trend(up + [90.0]) == "lateral"       # EMA20 still above EMA50, but the close fell under both
    assert chartdata.trend(down + [40.0]) == "lateral"


def test_pivots_are_strict_fractals_with_three_candles_on_each_side():
    c = shaped([10, 11, 12, 20, 12, 11, 10, 11, 12, 13], [5, 6, 7, 8, 7, 6, 1, 6, 7, 8])
    assert chartdata.pivots(c) == ([(3 * 60_000, 20)], [(6 * 60_000, 1)])
    tie = shaped([10, 11, 12, 20, 20, 11, 10, 11, 12, 13], [5, 6, 7, 8, 7, 6, 1, 1, 7, 8])
    assert chartdata.pivots(tie) == ([], [])                                  # equal neighbours: neither is the pivot
    assert chartdata.pivots(c[:6]) == ([], []) and chartdata.pivots([]) == ([], [])   # the edges are never pivots
    assert chartdata.pivots(c, side=1)[0][0] == (3 * 60_000, 20)


def test_frame_and_the_timeframes_fetched():
    c = [candle(i, 100 + i, 101.5 + i, 99 + i, 101 + i) for i in range(60)]
    f = chartdata.frame("5m", c)
    assert f == {"tf": "5m", "tendencia": "alta", "var_pct": 60.0, "max": 160.5, "min": 99,
                 "atr_pct": round(2.5 / 160 * 100, 4)}                        # TR = max(2.5, 1.5, 1) every candle
    assert chartdata.LADDER == ("1m", "5m", "15m", "1h", "4h", "1d")
    assert chartdata.context_tfs("1m") == ("1m", ["1m", "5m", "15m", "1h"], True)
    assert chartdata.context_tfs("5m") == ("5m", ["5m", "15m", "1h", "4h"], True)
    assert chartdata.context_tfs("4h") == ("4h", ["4h", "1d"], True)
    assert chartdata.context_tfs("1d") == ("1d", ["1d"], True)
    # read off the chart but not fetched: the context is made only of timeframes above it
    assert chartdata.context_tfs("3m") == (None, ["15m", "1h", "4h"], True)
    assert chartdata.context_tfs("30m") == (None, ["1h", "4h", "1d"], True)
    # nobody knows the chart's timeframe: context to show, never "higher timeframes"
    for unknown in ("outro", "auto", "", None, 5, ["5m"], "2h", "1w"):
        assert chartdata.context_tfs(unknown) == (None, ["15m", "1h", "4h"], False)


# ------------------------------------------------------------ freshness
def market(atr=0.1, **over):
    return {"fonte": "Hyperliquid", "moeda": "BTC", "tf": "5m", "preco": 100.0, "em": "2026-11-10T12:00:00+00:00",
            "funding_h_pct": 0.00125, "oi_usd": 100000, "vol_24h_usd": 5000001, "var_24h_pct": 25.0, "atr_pct": atr,
            "tfs": [{"tf": "5m", "tendencia": "alta", "var_pct": 1.0, "max": 101.0, "min": 99.0, "atr_pct": atr},
                    {"tf": "15m", "tendencia": "baixa", "var_pct": -2.0, "max": 104.0, "min": 98.0, "atr_pct": 5.0}],
            "desvio_pct": None, "fresco": None, "aviso": "", **over}


def test_fresh_at_its_boundaries():
    m = market(atr=0.1)                                    # 3 ATR = 0.3%: the floor of 0.5% is what counts
    ok = chartdata.fresh(m, 100.5)
    assert (ok["desvio_pct"], ok["fresco"], ok["aviso"]) == (0.5, True, "")
    assert chartdata.fresh(m, 99.5)["fresco"] is True and chartdata.fresh(m, 100)["desvio_pct"] == 0.0
    far = chartdata.fresh(m, 100.51)
    assert (far["desvio_pct"], far["fresco"]) == (0.51, False)
    assert "0,51%" in far["aviso"] and "BTC" in far["aviso"] and "antiga ou é de outro mercado" in far["aviso"]
    wide = market(atr=0.4)                                 # 3 ATR = 1.2%, of the LOWEST timeframe (not the 15m's 5%)
    assert chartdata.fresh(wide, 101.2)["fresco"] is True and chartdata.fresh(wide, 98.8)["fresco"] is True
    assert chartdata.fresh(wide, 101.21)["fresco"] is False
    assert chartdata.fresh(market(atr=None), 100.5)["fresco"] is True and chartdata.fresh(market(atr=None), 101)["fresco"] is False
    assert m["desvio_pct"] is None and m["fresco"] is None                    # a copy: what came in is not touched
    assert {k: v for k, v in ok.items() if k not in ("desvio_pct", "fresco", "aviso")} == \
           {k: v for k, v in m.items() if k not in ("desvio_pct", "fresco", "aviso")}


@pytest.mark.parametrize("price", [None, 0, -5, "100", True, float("nan"), float("inf"), [100], {}])
def test_fresh_says_nothing_without_a_price_from_the_image(price):
    old = market(desvio_pct=9.0, fresco=False, aviso="antigo")                # and an older verdict does not survive
    out = chartdata.fresh(old, price)
    assert (out["desvio_pct"], out["fresco"], out["aviso"]) == (None, None, "")
    assert chartdata.fresh(market(preco=0), 100)["fresco"] is None and chartdata.fresh(market(tfs=[]), 100.4)["fresco"] is True


@pytest.mark.parametrize("price, live", [(1e308, 0.001), (1.7e308, 5e-324), (1e300, 100.0), (2e6, 0.0001)])
def test_fresh_keeps_the_deviation_a_finite_number_whatever_price_the_image_gave(price, live):
    """1e308 against 0.001 overflowed to infinity: RESULT then could not be sent to the phone as JSON."""
    out = chartdata.fresh(market(preco=live), price)
    assert out["desvio_pct"] == chartdata.DEV_MAX_PCT == 1_000_000 and out["fresco"] is False
    assert "mais de 1000000,00%" in out["aviso"] and "inf" not in out["aviso"]
    assert json.loads(json.dumps(out, allow_nan=False))["desvio_pct"] == 1_000_000
    assert "inf" not in chartdata.stale_block(out)
    near = chartdata.fresh(market(preco=100.0), 1_000_100.0)                  # under the cap: the number itself
    assert near["desvio_pct"] == 1_000_000.0 and "a 1000000,00%" in near["aviso"] and "mais de" not in near["aviso"]


# ------------------------------------------------------------ snapshot, with a fake API
CTX = {"funding": "0.0000125", "openInterest": "1000", "prevDayPx": "80.0", "dayNtlVlm": "5000000.7",
       "markPx": "100.0", "oraclePx": "100.1"}


def rows(req, n=121):
    """n candles as Hyperliquid sends them (numbers as text): close 90.0, 90.1, ... each 0.1 above its open, wicks
    of 0.05, and one spike at candle 50."""
    ms = MS[req["interval"]]
    out = []
    for i in range(n):
        c = 90 + i / 10
        out.append({"t": req["endTime"] - (n - 1 - i) * ms, "T": 0, "s": req["coin"], "i": req["interval"],
                    "o": f"{c - 0.1:.4f}", "c": f"{c:.4f}", "h": f"{c + (5 if i == 50 else 0.05):.4f}",
                    "l": f"{c - 0.15:.4f}", "v": "12.5", "n": 7})
    return out


class Api:
    """chartdata._post, faked: the universe, then candles; `meta`/`candles` replace an answer."""

    def __init__(self, ctx=None, meta=None, candles=None, clock=None, step=0.0):
        self.ctx, self.meta, self.candles, self.calls, self.clock, self.step = ctx or CTX, meta, candles, [], clock, step

    def __call__(self, payload, timeout):
        self.calls.append((payload, timeout))
        if self.clock is not None:
            self.clock[0] += self.step
        if payload["type"] == "metaAndAssetCtxs":
            if self.meta is not None:
                return self.meta
            names = [{"name": "BTC", "szDecimals": 5}, {"name": "OLD", "isDelisted": True}, {"name": "kPEPE"}]
            return [{"universe": names, "marginTables": []}, [self.ctx, self.ctx, self.ctx]]
        assert payload["type"] == "candleSnapshot"
        return self.candles(payload["req"]) if self.candles else rows(payload["req"])


@pytest.fixture
def api(monkeypatch):
    def use(**kw):
        fake = Api(**kw)
        monkeypatch.setattr(chartdata, "_post", fake)
        return fake
    return use


def test_snapshot_happy_path_by_hand(api):
    fake = api()
    snap = chartdata.snapshot("BINANCE:BTCUSDT.P", "5m")
    assert set(snap) == {"mercado", "prompt"}
    m = snap["mercado"]
    line = {"tendencia": "alta", "var_pct": round((102 / 89.9 - 1) * 100, 2), "max": 102.05, "min": 89.85,
            "atr_pct": round(0.2 / 102 * 100, 4)}                             # TR = high - low = 0.2 on every candle
    assert line["var_pct"] == 13.46 and line["atr_pct"] == 0.1961
    assert datetime.fromisoformat(m.pop("em")).utcoffset().total_seconds() == 0
    assert m == {"fonte": "Hyperliquid", "moeda": "BTC", "tf": "5m", "tfs_acima": True, "preco": 100.0,
                 "funding_h_pct": 0.00125,                                    # 0.0000125 an hour, as a percentage
                 "oi_usd": 100000, "vol_24h_usd": 5000001, "var_24h_pct": 25.0, "atr_pct": 0.1961,
                 "tfs": [{"tf": tf, **line} for tf in ("5m", "15m", "1h", "4h")],
                 "desvio_pct": None, "fresco": None, "aviso": ""}
    # what was asked: the universe once, then one candle request per timeframe, each with a short timeout
    assert [p for p, _ in fake.calls][0] == {"type": "metaAndAssetCtxs"} and len(fake.calls) == 5
    assert all(0 < t <= chartdata.CALL_TIMEOUT_S for _, t in fake.calls)
    reqs = [p["req"] for p, _ in fake.calls[1:]]
    assert [r["interval"] for r in reqs] == ["5m", "15m", "1h", "4h"] and {r["coin"] for r in reqs} == {"BTC"}
    assert all(set(r) == {"coin", "interval", "startTime", "endTime"} and isinstance(r["startTime"], int)
               and r["endTime"] - r["startTime"] == chartdata.BARS * MS[r["interval"]] for r in reqs)
    # the text for the model: the summary, the pivots of the chart's timeframe and of the next, the last candles
    p = snap["prompt"]
    assert p.startswith(chartdata.summary({**m, "em": "x"}).split("\n")[1][:0] + "Hyperliquid, BTC perpétuo")
    assert "Preço ao vivo 100.000" in p and "Funding +0.00125%/hora" in p and "variação 24h +25.00%" in p
    assert "- 5m: alta, variação +13.46%, máx 102.050, mín 89.8500, ATR(14) 0.1961%" in p and "- 4h: alta" in p
    assert "Pivôs 5m, máximos: 100.000 (" in p and "Pivôs 15m, máximos: 100.000 (" in p and "Pivôs 1h" not in p
    assert "Últimas 30 velas de 5m" in p and len(p.split("ainda está aberta):\n")[1].split("\n")) == 30
    assert p.rstrip().endswith("101.900 102.050 101.850 102.000")
    assert len(p) < 4000


def test_snapshot_with_an_unknown_chart_timeframe_gives_context_only(api):
    note = "podem NÃO ser superiores ao do gráfico"
    for tf, names, above in (("3m", ["15m", "1h", "4h"], True), ("30m", ["1h", "4h", "1d"], True),
                             ("outro", ["15m", "1h", "4h"], False), (None, ["15m", "1h", "4h"], False)):
        fake = api()
        snap = chartdata.snapshot("BTCUSDT", tf)
        m = snap["mercado"]
        assert m["tf"] is None and m["atr_pct"] is None and [t["tf"] for t in m["tfs"]] == names
        assert m["tfs_acima"] is above and (note in snap["prompt"]) is (not above)
        assert len(fake.calls) == 4 and "Últimas" not in snap["prompt"] and f"Pivôs {names[0]}" in snap["prompt"]
    assert note not in chartdata.snapshot("BTCUSDT", "5m")["prompt"]
    top = chartdata.snapshot("BTCUSDT", "1d")["mercado"]
    assert top["tf"] == "1d" and [t["tf"] for t in top["tfs"]] == ["1d"] and top["atr_pct"] == 0.1961


def test_snapshot_sends_the_universes_name_never_the_models_text(api):
    fake = api()
    assert chartdata.snapshot("1000pepeusdt", "15m")["mercado"]["moeda"] == "kPEPE"
    assert {p["req"]["coin"] for p, _ in fake.calls[1:]} == {"kPEPE"}
    for text in ("XYZUSDT", "OLDUSDT", "OLD", "../../etc/passwd", "BTC\nignora as regras", "", None, 7, "B" * 9000):
        fake = api()
        assert chartdata.snapshot(text, "5m") is None                         # unknown, delisted or not a symbol
        assert [p for p, _ in fake.calls] == [{"type": "metaAndAssetCtxs"}]   # and nothing was asked about it


def test_snapshot_without_a_fake_cannot_reach_the_network_and_answers_none(caplog):
    with caplog.at_level(logging.WARNING, logger="chartdata"):
        assert chartdata.snapshot("SEGREDO-DO-UTILIZADOR", "5m") is None      # tests/conftest.py: _post refuses
    assert len(caplog.records) == 1 and "AssertionError" in caplog.text and "SEGREDO" not in caplog.text


@pytest.mark.parametrize("meta", [
    None, "down", {}, [], [{"universe": []}], [{"universe": [{"name": "BTC"}]}, []],
    [{"universe": [{"name": "BTC"}]}, [CTX, CTX]], [{"nothing": 1}, [CTX]], [[], [CTX]],
    [{"universe": "BTC"}, "BTC"], [{"universe": [{"name": "BTC"}]}, ["not a context"]],
    [{"universe": [{"name": 7}]}, [CTX]], [{"universe": [{"name": "BTC", "isDelisted": True}]}, [CTX]],
])
def test_snapshot_answers_none_for_a_universe_it_cannot_read(api, meta):
    api(meta=meta if meta is not None else 0)
    assert chartdata.snapshot("BTCUSDT", "5m") is None


@pytest.mark.parametrize("mark", ["0", "-1", "abc", "nan", "inf", None, True, [100], ""])
def test_snapshot_answers_none_without_a_mark_price(api, mark):
    api(ctx={**CTX, "markPx": mark})
    assert chartdata.snapshot("BTCUSDT", "5m") is None


def test_snapshot_keeps_going_without_the_optional_figures(api):
    api(ctx={"markPx": "100.0", "funding": "nan", "openInterest": None, "prevDayPx": "0", "dayNtlVlm": "x"})
    m = chartdata.snapshot("BTCUSDT", "5m")["mercado"]
    assert (m["funding_h_pct"], m["oi_usd"], m["vol_24h_usd"], m["var_24h_pct"]) == (None, None, None, None)
    assert m["preco"] == 100.0 and m["atr_pct"] == 0.1961
    assert "Funding ? " in chartdata.summary(m) and "open interest ?" in chartdata.summary(m)


def _broken(change):
    def candles(req):
        out = rows(req)
        return change(out)
    return candles


@pytest.mark.parametrize("change", [
    lambda r: "nope", lambda r: None, lambda r: {"rows": r}, lambda r: [],
    lambda r: r[:chartdata.MIN_BARS - 1],                                     # a listing too new to say anything
    lambda r: r[:60] + [{**r[60], "h": "nan"}] + r[61:],
    lambda r: r[:60] + [{**r[60], "c": "inf"}] + r[61:],
    lambda r: r[:60] + [{**r[60], "l": "0"}] + r[61:],
    lambda r: r[:60] + [{**r[60], "o": None}] + r[61:],
    lambda r: r[:60] + [{k: v for k, v in r[60].items() if k != "c"}] + r[61:],
    lambda r: r[:60] + [{**r[60], "h": "1.0"}] + r[61:],                      # a high below its own close
    lambda r: r[:60] + ["candle"] + r[61:],
    lambda r: r[::-1],                                                        # newest first
    lambda r: r + r[-1:],                                                     # the same candle twice
])
def test_snapshot_answers_none_for_candles_it_cannot_trust(api, change):
    api(candles=_broken(change))
    assert chartdata.snapshot("BTCUSDT", "5m") is None


def test_a_higher_timeframe_with_too_few_candles_is_left_out(api):
    api(candles=lambda req: rows(req, 30 if req["interval"] in ("4h", "1h") else 121))
    m = chartdata.snapshot("BTCUSDT", "5m")["mercado"]
    assert [t["tf"] for t in m["tfs"]] == ["5m", "15m"] and m["tf"] == "5m"


def test_snapshot_gives_up_when_the_budget_is_spent(api, monkeypatch):
    fake = api()
    assert chartdata.snapshot("BTCUSDT", "5m", budget_s=0) is None and fake.calls == []
    assert chartdata.snapshot("BTCUSDT", "5m", budget_s=-3) is None and fake.calls == []
    clock = [1000.0]
    monkeypatch.setattr(chartdata, "_clock", lambda: clock[0])
    slow = api(clock=clock, step=9.0)                      # every request takes 9 s of a 20 s budget
    assert chartdata.snapshot("BTCUSDT", "5m", budget_s=20) is None
    assert [t for _, t in slow.calls] == [8, 8, 2]         # 8 s at most each, never past the deadline, no 4th
    quick = api(clock=clock, step=1.0)
    assert chartdata.snapshot("BTCUSDT", "5m", budget_s=20)["mercado"]["moeda"] == "BTC" and len(quick.calls) == 5


def test_a_request_that_never_answers_is_waited_for_no_longer_than_the_budget(monkeypatch, caplog):
    """requests' timeout is per wait on the socket: a server that trickles bytes held the job far past the budget."""
    release, calls = threading.Event(), []

    def hangs(payload, timeout):
        calls.append(timeout)
        release.wait(30)                                   # far longer than any budget below
        return [{"universe": [{"name": "BTC"}]}, [CTX]]

    monkeypatch.setattr(chartdata, "_post", hangs)
    try:
        start = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="chartdata"):
            assert chartdata.snapshot("BTCUSDT", "5m", budget_s=0.3) is None
        assert time.monotonic() - start < 5 and len(calls) == 1 and 0 < calls[0] < 0.31
        assert [r.getMessage() for r in caplog.records] == ["chartdata: no market data for the chart: TimeoutError"]
    finally:
        release.set()


class Resp:
    """What requests.post gives back, as _post uses it: a status, the body in pieces, and a close."""

    def __init__(self, chunks=(b'{"ok": [1, "\xc3\xa9"]}',), status=200, clock=None, step=0.0):
        self.chunks, self.status_code, self.clock, self.step, self.read, self.closed = chunks, status, clock, step, 0, False

    def iter_content(self, size):
        for chunk in self.chunks:
            if self.clock is not None:
                self.clock[0] += self.step
            self.read += 1
            yield chunk

    def json(self):
        raise AssertionError("the body is read against a size limit, not all at once")

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def posting(monkeypatch, resp) -> list:
    """requests.post replaced by one that records how it was called and answers `resp` (nothing real is reached)."""
    asked = []

    def post(url, **kw):
        asked.append((url, kw))
        return resp

    monkeypatch.setattr(chartdata.requests, "post", post)
    return asked


def test_the_one_http_call_follows_no_redirect_and_reads_the_body_in_pieces(monkeypatch):
    resp = Resp(chunks=(b'{"ok": [1, ', b'"\xc3\xa9"]}'))
    asked = posting(monkeypatch, resp)
    assert REAL_POST({"type": "metaAndAssetCtxs"}, 8) == {"ok": [1, "é"]} and resp.closed
    assert asked == [("https://api.hyperliquid.xyz/info", {
        "json": {"type": "metaAndAssetCtxs"}, "headers": chartdata.HEADERS, "timeout": 8, "allow_redirects": False,
        "stream": True})]


@pytest.mark.parametrize("status", [301, 302, 307, 308, 204, 206, 400, 404, 429, 500, 503])
def test_an_answer_that_is_not_a_200_is_never_read(monkeypatch, status):
    resp = Resp(status=status)                             # a redirect would be an address the server chose
    posting(monkeypatch, resp)
    with pytest.raises(ValueError):
        REAL_POST({"type": "metaAndAssetCtxs"}, 8)
    assert resp.read == 0 and resp.closed


def test_a_body_too_large_or_too_slow_is_dropped_before_its_end(monkeypatch):
    piece = b" " * 65536
    endless = Resp(chunks=(piece for _ in range(10 ** 9)))
    posting(monkeypatch, endless)
    with pytest.raises(ValueError):
        REAL_POST({"type": "metaAndAssetCtxs"}, 8)
    assert endless.closed and endless.read == chartdata.MAX_BODY_BYTES // 65536 + 1 and chartdata.MAX_BODY_BYTES == 4_000_000
    clock = [50.0]
    monkeypatch.setattr(chartdata, "_clock", lambda: clock[0])
    trickle = Resp(chunks=(b"[" for _ in range(10 ** 9)), clock=clock, step=3.0)     # 3 s a byte, for ever
    posting(monkeypatch, trickle)
    with pytest.raises(TimeoutError):
        REAL_POST({"type": "metaAndAssetCtxs"}, 8)
    assert trickle.closed and trickle.read == 3                                   # 3, 6, 9 s: past the 8 it had
    for broken in (b"", b"<html>", b'{"cut": '):
        posting(monkeypatch, Resp(chunks=(broken,)))
        with pytest.raises(ValueError):
            REAL_POST({"type": "metaAndAssetCtxs"}, 8)


@pytest.mark.parametrize("args", [(None, None), (123, object()), ("BTCUSDT", "5m", "soon"), ("BTCUSDT", "5m", None),
                                   ("BTCUSDT", "5m", float("nan")), (["BTC"], {"tf": "5m"})])
def test_snapshot_never_raises(api, args):
    api()
    assert chartdata.snapshot(*args) is None


def test_one_try_per_call_and_a_failure_is_one_warning_with_the_coin_only(monkeypatch, caplog):
    calls = []

    def flaky(payload, timeout):
        calls.append(payload["type"])
        if payload["type"] == "candleSnapshot":
            raise ConnectionError("https://api.hyperliquid.xyz/info refused, token=abc")
        return [{"universe": [{"name": "BTC"}]}, [CTX]]

    monkeypatch.setattr(chartdata, "_post", flaky)
    with caplog.at_level(logging.WARNING, logger="chartdata"):
        assert chartdata.snapshot("BTCUSDT", "5m") is None
    assert calls == ["metaAndAssetCtxs", "candleSnapshot"]                    # no retry, and the rest not asked
    assert [r.getMessage() for r in caplog.records] == ["chartdata: no market data for BTC: ConnectionError"]


def test_the_block_for_a_stale_screenshot_has_the_summary_and_no_levels(api):
    api()
    snap = chartdata.snapshot("BTCUSDT", "5m")
    stale = chartdata.fresh(snap["mercado"], 250.0)
    assert stale["fresco"] is False and stale["desvio_pct"] == 150.0
    block = chartdata.stale_block(stale)
    assert block.startswith(chartdata.summary(stale)) and "NÃO coincidem" in block and "150.0%" in block
    assert "Pivôs" not in block and "Últimas" not in block and "- 15m: alta" in block
    assert snap["prompt"].startswith(chartdata.summary(snap["mercado"]))


def test_prices_are_written_with_six_significant_digits_and_no_exponent():
    assert [chartdata._px(v) for v in (73661.0, 3456.789, 100.0, 1.0, 0.0123456, 0.00001234, 1234567.0)] == \
           ["73661.0", "3456.79", "100.000", "1.00000", "0.0123456", "0.0000123400", "1234567"]
    assert all(math.isfinite(float(chartdata._px(v))) for v in (1e-9, 1e12))
