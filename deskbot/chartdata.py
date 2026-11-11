"""Live market context for the "Gráfico" desk: PUBLIC data from Hyperliquid, read only.

A screenshot shows one timeframe and no funding; this module fetches, for the asset the model read off the
image, what a trader would also look at: the trend of the higher timeframes, the ATR, funding, open interest
and exact pivot prices. CODE computes every figure here; the model only gets them as text.

Nothing here needs a key, places an order or knows who the user is. The only text that ever reaches a request
is a coin name taken from Hyperliquid's own list (coin_for matches the model's text against it, never the
other way round).

  coin_for   the model's "BTCUSDT.P" -> the name in Hyperliquid's universe, or None
  snapshot   -> {"mercado": MARKET, "prompt": text block for the model} or None; never raises
  fresh      is the screenshot's price still the live price? (pure; used before and after the main read)
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("chartdata")

INFO = "https://api.hyperliquid.xyz/info"
HEADERS = {"Content-Type": "application/json", "User-Agent": "ai-desk-bot-paper/1.0"}
LADDER = ("1m", "5m", "15m", "1h", "4h", "1d")
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
UNKNOWN_TFS = ("15m", "1h", "4h")   # the context when the chart's own timeframe is not known at all
BETWEEN = {"3m": ("15m", "1h", "4h"), "30m": ("1h", "4h", "1d")}   # read off the chart, not fetched: all above it
HIGHER = 3                          # how many timeframes above the chart's are fetched
BARS = 120
MIN_BARS = 60                       # fewer candles than this (a new listing) say nothing about EMA50
CALL_TIMEOUT_S = 8
PIVOT_SIDE = 3                      # a fractal pivot: higher (lower) than the 3 candles on each side
PIVOTS = 6
LAST_CANDLES = 30
FRESH_MIN_PCT = 0.5                 # the screenshot may be this far from the live price, or FRESH_ATRS ATRs
FRESH_ATRS = 3
DEV_MAX_PCT = 1_000_000.0           # "desvio_pct" never says more than this: it must stay a finite number
MAX_BODY_BYTES = 4_000_000          # an answer larger than this is not the API we know (the universe is well under)
MAX_ASSET_TEXT = 40

QUOTES = ("FDUSD", "USDT", "USDC", "USDE", "USD")            # what a pair is quoted in; never a coin to read
NOISE = {"PERP", "PERPS", "PERPETUAL", "P", "SWAP", "FUTURES", "CONTRACT"}


# ------------------------------------------------------------ the asset
def coin_for(ativo, universe) -> str | None:
    """The Hyperliquid name for what the model read ("BTCUSDT", "BTC/USDT", "ETHUSD Perpetual", "1000PEPEUSDT").

    The text is reduced to a base symbol that must EQUAL a name of `universe` (case apart); what is returned is
    the universe's own string. Unknown, ambiguous or not a symbol at all -> None."""
    if not isinstance(ativo, str) or not 0 < len(ativo) <= MAX_ASSET_TEXT:
        return None
    names: dict[str, str | None] = {}
    for name in universe if isinstance(universe, (list, tuple, set, frozenset)) else ():
        if isinstance(name, str) and name:
            key = name.lower()
            names[key] = None if key in names and names[key] != name else name   # two names, one spelling: neither
    text = ativo.upper().rsplit(":", 1)[-1]                   # "BINANCE:BTCUSDT" -> "BTCUSDT"
    tokens = [t for t in re.split(r"[\s/\-_.]+", text) if t]
    if not tokens or not all(re.fullmatch(r"[A-Z0-9]+", t) for t in tokens):
        return None
    tokens = [t for t in tokens if t not in NOISE] or tokens  # a coin called "P" alone is still a symbol
    if len(tokens) == 2 and tokens[1] in QUOTES:
        bases = [tokens[0]]
    elif len(tokens) == 1:
        word = tokens[0]
        if word.endswith("PERP") and len(word) > 4:
            word = word[:-4]
        # Every way of cutting ONE quote off the end is tried ("ARBUSD" is ARB, "BTCFDUSD" is BTC and not BTCFD);
        # the universe decides, and two different coins that both fit mean None below.
        bases = [word] + [word[:-len(q)] for q in QUOTES if word.endswith(q) and len(word) > len(q)]
    else:
        return None
    found = set()
    for base in bases:
        if base in QUOTES:                                    # a stablecoin against a stablecoin is not a chart to read
            continue
        for key in (base, "K" + base[4:] if base.startswith("1000") and len(base) > 4 else None):
            if key and key.lower() in names:
                found.add(names[key.lower()])
    found.discard(None)
    return found.pop() if len(found) == 1 else None


# ------------------------------------------------------------ the arithmetic
def _f(v) -> float | None:
    """A finite float, or None (Hyperliquid sends numbers as text; a bool is not a number)."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def ema(xs: list[float], n: int) -> float:
    """Exponential average, seeded with the first value (the same one deskbot.perpdesk uses)."""
    k = 2 / (n + 1)
    e = xs[0]
    for x in xs[1:]:
        e = x * k + e * (1 - k)
    return e


def atr_pct(c: list[dict], n: int = 14) -> float | None:
    """ATR(n) as % of the last close: the plain average of the last n true ranges. None with too few candles."""
    if len(c) < n + 1 or c[-1]["c"] <= 0:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(c[-n - 1:], c[-n:])]
    return round(sum(trs) / n / c[-1]["c"] * 100, 4)


def trend(closes: list[float]) -> str:
    """"alta" when close > EMA20 > EMA50, "baixa" when close < EMA20 < EMA50, otherwise "lateral"."""
    e20, e50, last = ema(closes, 20), ema(closes, 50), closes[-1]
    return "alta" if last > e20 > e50 else "baixa" if last < e20 < e50 else "lateral"


def pivots(c: list[dict], side: int = PIVOT_SIDE) -> tuple[list[tuple], list[tuple]]:
    """Fractal (highs, lows) as [(t, price)]: a candle strictly above (below) the `side` candles on each side."""
    highs, lows = [], []
    for i in range(side, len(c) - side):
        around = c[i - side:i] + c[i + 1:i + side + 1]
        if all(c[i]["h"] > o["h"] for o in around):
            highs.append((c[i]["t"], c[i]["h"]))
        if all(c[i]["l"] < o["l"] for o in around):
            lows.append((c[i]["t"], c[i]["l"]))
    return highs, lows


def frame(tf: str, c: list[dict]) -> dict:
    """One line of MARKET["tfs"] from the candles of one timeframe."""
    return {"tf": tf, "tendencia": trend([x["c"] for x in c]),
            "var_pct": round((c[-1]["c"] / c[0]["o"] - 1) * 100, 2),
            "max": max(x["h"] for x in c), "min": min(x["l"] for x in c), "atr_pct": atr_pct(c)}


def context_tfs(tf) -> tuple[str | None, list[str], bool]:
    """-> (the chart timeframe or None when it is not one we fetch, the timeframes to fetch, lowest first,
    whether those timeframes (the chart's own aside) are known to be ABOVE the chart's).

    Only timeframes known to be above the chart's may count as "the higher timeframes are against this trade":
    with a chart whose timeframe nobody could tell (it may be a 4 hour or a weekly one), the context is still
    worth showing, but it proves nothing against an option."""
    if isinstance(tf, str) and tf in LADDER:
        i = LADDER.index(tf)
        return tf, list(LADDER[i:i + 1 + HIGHER]), True
    if isinstance(tf, str) and tf in BETWEEN:
        return None, list(BETWEEN[tf]), True
    return None, list(UNKNOWN_TFS), False


# ------------------------------------------------------------ freshness
def _pt(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


def fresh(market: dict, image_price) -> dict:
    """A copy of MARKET with "desvio_pct", "fresco" and "aviso" filled from the price read off the screenshot.

    Not fresh: the screenshot is further from the live price than FRESH_MIN_PCT % and than FRESH_ATRS ATRs of
    the lowest timeframe, so it is old or shows another market. Price unknown -> nothing can be said (None)."""
    out = dict(market)
    out.update(desvio_pct=None, fresco=None, aviso="")
    price, live = _f(image_price), _f(market.get("preco"))
    if isinstance(image_price, str) or price is None or price <= 0 or not live or live <= 0:
        return out
    tfs = market.get("tfs")
    low = _f(tfs[0].get("atr_pct")) if isinstance(tfs, list) and tfs and isinstance(tfs[0], dict) else None
    # Capped: an absurd price read off the image (1e308) against a small live price overflows to infinity, and an
    # infinity in RESULT cannot be sent to the phone (the relay's JSON refuses it) for as long as it is in history.
    dev = abs(price - live) / live * 100
    out["desvio_pct"] = round(min(dev, DEV_MAX_PCT), 2)
    out["fresco"] = out["desvio_pct"] <= max(FRESH_MIN_PCT, FRESH_ATRS * (low or 0))
    if not out["fresco"]:
        more = "mais de " if dev > DEV_MAX_PCT else ""
        out["aviso"] = (f"O preço da captura está a {more}{_pt(out['desvio_pct'])}% do preço ao vivo de "
                        f"{market.get('moeda')} na Hyperliquid: a captura é antiga ou é de outro mercado, e os "
                        "níveis podem já não valer.")
    return out


# ------------------------------------------------------------ the text for the model
def _px(v: float) -> str:
    """A price with 6 significant digits and no exponent (73661.0, 0.0123456)."""
    d = 5 - math.floor(math.log10(abs(v))) if v else 0
    return f"{v:.{min(max(d, 0), 12)}f}"


def _big(v) -> str:
    return "?" if v is None else f"{v / 1e6:.1f}M USD"


def _sgn(v, unit: str = "%", d: int = 2) -> str:
    return "?" if v is None else f"{v:+.{d}f}{unit}"


def _when(t: float) -> str:
    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%m-%d %H:%M")


def summary(m: dict) -> str:
    """The lines every prompt gets, built from MARKET alone (so a stale screenshot still gets them)."""
    lines = [f"Hyperliquid, {m['moeda']} perpétuo, {m['em']} (UTC). Preço ao vivo {_px(m['preco'])}.",
             f"Funding {_sgn(m['funding_h_pct'], '%/hora', 5)} (positivo: os longs pagam) | open interest "
             f"{_big(m['oi_usd'])} | volume 24h {_big(m['vol_24h_usd'])} | variação 24h {_sgn(m['var_24h_pct'])}",
             f"Tendência por tempo (EMA20/EMA50 de ~{BARS} velas; variação, máximo e mínimo dessa janela):"]
    for t in m["tfs"]:
        atr = "?" if t["atr_pct"] is None else f"{t['atr_pct']:.4f}%"
        lines.append(f"- {t['tf']}: {t['tendencia']}, variação {_sgn(t['var_pct'])}, máx {_px(t['max'])}, "
                     f"mín {_px(t['min'])}, ATR(14) {atr}")
    if m.get("tfs_acima") is False:
        lines.append("O tempo do gráfico da imagem não foi identificado: estes tempos podem NÃO ser superiores ao do "
                     "gráfico. Diz qual é o tempo do gráfico antes de os tratares como tendência de fundo.")
    return "\n".join(lines)


def stale_block(m: dict) -> str:
    """What the model gets when the screenshot's price is not the live price: the summary and why no levels."""
    return (summary(m) + f"\nATENÇÃO: o preço da imagem está a {m.get('desvio_pct')}% do preço ao vivo. A imagem e "
            "o mercado ao vivo NÃO coincidem (captura antiga ou outro mercado): não uses estes dados para níveis.")


def _detail(tf: str | None, order: list[str], candles: dict) -> str:
    lines = []
    for name in order[:2]:
        highs, lows = pivots(candles[name])
        for label, pts in (("máximos", highs), ("mínimos", lows)):
            if pts:
                lines.append(f"Pivôs {name}, {label}: " + "; ".join(f"{_px(p)} ({_when(t)})" for t, p in pts[-PIVOTS:]))
    if tf:
        lines.append(f"Últimas {LAST_CANDLES} velas de {tf} (mês-dia hora UTC: abertura máximo mínimo fecho; a última "
                     "ainda está aberta):")
        lines += [f"{_when(x['t'])}: {_px(x['o'])} {_px(x['h'])} {_px(x['l'])} {_px(x['c'])}"
                  for x in candles[tf][-LAST_CANDLES:]]
    return "\n".join(lines)


# ------------------------------------------------------------ the network
def _post(payload: dict, timeout: float):
    """The ONE place this module touches the network (tests replace it). One try, no retry, no sleep.

    Only a 200 from INFO itself is read: a redirect is an address that came from outside and is not followed.
    The body is read in pieces against a size limit and the clock, so that a call _call has already given up on
    cannot go on filling memory. (How long the caller waits is limited in _call, not here.)"""
    end = _clock() + timeout
    with requests.post(INFO, json=payload, headers=HEADERS, timeout=timeout, allow_redirects=False, stream=True) as r:
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")
        body = bytearray()
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                raise ValueError("answer too large")
            if _clock() > end:
                raise TimeoutError("answer too slow")
    return json.loads(body)


def _clock() -> float:
    return time.monotonic()


def _call(payload: dict, deadline: float):
    """One request, waited for at most CALL_TIMEOUT_S and never past the deadline.

    requests' timeout only limits each wait on the socket: a server that sends its headers or its body a few
    bytes at a time is never "timed out". So the request runs in a thread of its own and it is the WAIT that is
    limited; a request given up on is left to end by itself (a daemon thread, its answer thrown away)."""
    left = deadline - _clock()
    if not left > 0:       # also a budget that is not a number (NaN compares false with everything)
        raise TimeoutError("budget spent")
    limit, box = min(CALL_TIMEOUT_S, left), []

    def work():
        try:
            box.append((_post(payload, limit), None))
        except Exception as exc:
            box.append((None, exc))

    worker = threading.Thread(target=work, name="chartdata-call", daemon=True)
    worker.start()
    worker.join(limit)
    if not box:
        raise TimeoutError("no answer in time")
    res, exc = box[0]
    if exc is not None:
        raise exc
    return res


def _universe(res) -> dict[str, dict]:
    """metaAndAssetCtxs -> {name: its context} for the coins still listed. Raises on any other shape."""
    meta, ctxs = res
    uni = meta["universe"]
    if not isinstance(uni, list) or not isinstance(ctxs, list) or len(uni) != len(ctxs):
        raise ValueError("universe and contexts do not match")
    return {a["name"]: c for a, c in zip(uni, ctxs)
            if isinstance(a, dict) and isinstance(c, dict) and isinstance(a.get("name"), str) and not a.get("isDelisted")}


def _candles(rows) -> list[dict]:
    """candleSnapshot -> [{"t", "o", "h", "l", "c"}], oldest first. Raises on anything that is not a clean candle."""
    if not isinstance(rows, list):
        raise ValueError("not a list of candles")
    out = []
    for r in rows:
        x = {k: _f(r[k]) for k in ("t", "o", "h", "l", "c")}
        if any(v is None or v <= 0 for v in x.values()) or x["h"] < max(x["o"], x["c"]) or x["l"] > min(x["o"], x["c"]):
            raise ValueError("bad candle")
        out.append(x)
    if any(a["t"] >= b["t"] for a, b in zip(out, out[1:])):
        raise ValueError("candles out of order")
    return out


def snapshot(ativo, tf, budget_s: float = 20) -> dict | None:
    """-> {"mercado": MARKET, "prompt": text} for the asset the model read, or None. Never raises.

    None is the normal answer for anything that is not a perpetual listed on Hyperliquid, and for any failure
    (API down, slow, odd answer): the analysis then simply runs on the screenshot alone. `budget_s` is the most
    the caller waits, whatever the server does (see _call)."""
    coin = None
    try:
        deadline = _clock() + float(budget_s)
        uni = _universe(_call({"type": "metaAndAssetCtxs"}, deadline))
        coin = coin_for(ativo, list(uni))
        if coin is None:
            log.info("chartdata: the asset on the chart is not a Hyperliquid perpetual")
            return None
        ctx = uni[coin]
        mark = _f(ctx.get("markPx"))
        if mark is None or mark <= 0:
            raise ValueError("no mark price")
        chart_tf, order, above = context_tfs(tf)
        candles, now = {}, int(time.time() * 1000)
        for name in order:
            rows = _candles(_call({"type": "candleSnapshot", "req": {
                "coin": coin, "interval": name, "startTime": now - BARS * TF_MS[name], "endTime": now}}, deadline))
            if len(rows) >= MIN_BARS:
                candles[name] = rows
            elif name == order[0]:
                raise ValueError("too few candles")
        order = [n for n in order if n in candles]
        tfs = [frame(n, candles[n]) for n in order]
        funding, oi, prev = _f(ctx.get("funding")), _f(ctx.get("openInterest")), _f(ctx.get("prevDayPx"))
        vol = _f(ctx.get("dayNtlVlm"))
        market = {
            "fonte": "Hyperliquid", "moeda": coin, "tf": chart_tf, "tfs_acima": above, "preco": mark,
            "em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "funding_h_pct": None if funding is None else round(funding * 100, 5),
            "oi_usd": None if oi is None or oi < 0 else round(oi * mark),
            "vol_24h_usd": None if vol is None or vol < 0 else round(vol),
            "var_24h_pct": round((mark / prev - 1) * 100, 2) if prev and prev > 0 else None,
            "atr_pct": tfs[0]["atr_pct"] if chart_tf else None,
            "tfs": tfs, "desvio_pct": None, "fresco": None, "aviso": ""}
        if not all(math.isfinite(v) for t in tfs for v in (t["var_pct"], t["max"], t["min"], t["atr_pct"] or 0)):
            raise ValueError("numbers not finite")
        return {"mercado": market, "prompt": summary(market) + "\n" + _detail(chart_tf, order, candles)}
    except Exception as exc:                                  # the desk never fails because of market data
        log.warning("chartdata: no market data for %s: %s", coin or "the chart", type(exc).__name__)
        return None
