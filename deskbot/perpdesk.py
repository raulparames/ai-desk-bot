"""Futures mode: a 5-agent perpetual futures desk in paper, the same terminal as the memes mode, on the
pairs and prices of Hyperliquid, the exchange the INVO app trades on.

    python -m deskbot perpdesk --minutes 720 --eur 110 --lev 5

  TRENDS  the 25 most traded Hyperliquid perps (24 h volume of $20M or more). 15-minute candles: EMA 20
          above EMA 50 with both rising is a long, the mirror a short. Cuts coins that already ran more
          than 3 ATR in the last hour (chasing) and quiet coins (ATR under 0.15%).
  SMARTS  the best traders on Hyperliquid's public leaderboard: accounts of $1M or more, profitable this
          month and all-time, top 40 by this month's profit. Their open positions, refreshed every
          15 min. Needs 3 or more of them on our side and more money on our side than against.
  DEBATE  5 bull/bear votes for the side (1-hour trend agrees, RSI not stretched, funding not crowded on
          our side, volume picking up, price on the right side of the day's VWAP) plus the risk team
          (15-min ATR between 0.15% and 3%, the coin allows the leverage). Needs risk ok and 4 votes.
  ROUTER  Hyperliquid's order book for the trade size: spread of 0.05% or less and a fill within
          0.10% of the mid.
  EXEC    paper fill walking the book plus the 0.045% taker fee. Compounder sizing: margin 40% of the
          free cash at the session's leverage (max 10x, never more than the coin allows), at most 2 open,
          one new trade every 2 min. Prices every 5 s: stop 1.5 ATR; at +2R (twice the stop distance)
          half is closed and the stop moves to the entry; the rest trails 1.5 ATR off its best price.
          Liquidation when the margin is gone, funding charged by the hour.
  CORE    opens only when all five agree (5/5).

The app shows the same five cards as the memes mode: SCANNER (TRENDS + SMARTS), ANALYST (DEBATE + ROUTER,
score 0-100), COMPOUNDER (sizing), EXECUTOR and RISK.

Paper only: no wallet, no keys, no orders. INVO has no public API, so the app links to it and to the
Hyperliquid page of each pair; deciding to trade there is yours. Next to every paper trade, hl_live
builds the real Hyperliquid order in DRY-RUN (written down, never signed or sent).
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from . import hl_live
from .config import ROOT

log = logging.getLogger("perpdesk")
OUT = ROOT / "perpdesk"
STOP_FILE = OUT / "stop"
INFO = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HEADERS = {"Content-Type": "application/json", "User-Agent": "ai-desk-bot-paper/1.0"}
AGENTS = ("SCANNER", "ANALYST", "COMPOUNDER", "EXECUTOR", "RISK")
STAGES = ("TRENDS", "SMARTS", "DEBATE", "ROUTER", "EXEC")
AGENT_OF = {"TRENDS": "SCANNER", "SMARTS": "SCANNER", "DEBATE": "ANALYST", "ROUTER": "ANALYST", "CORE": "ANALYST",
            "EXEC": "EXECUTOR"}
REPOS = {
    "SCANNER": ("freqtrade/freqtrade", "tendência EMA 20/50 · top traders da Hyperliquid"),
    "ANALYST": ("TauricResearch/TradingAgents", "debate bull/bear · risco · livro de ordens · nota 0–100"),
    "COMPOUNDER": ("hyperliquid-dex/hyperliquid-python-sdk", "margem 40% do saldo livre · máx. 2 abertas"),
    "EXECUTOR": ("ccxt/ccxt", "preenche no livro · taxa 0,045%"),
    "RISK": ("nautechsystems/nautilus_trader", "metade a 2R · stop na entrada · trailing · liquidação"),
}
BUCKET = {"TRENDS": "trends", "SMARTS": "smarts", "DEBATE": "debate", "ROUTER": "router", "EXEC": "exec",
          "CORE": "core"}
LEVERAGES = (2, 5, 10)


@dataclass
class PerpParams:
    universe: int = 25
    min_volume: float = 20e6
    min_atr: float = 0.0015
    max_atr: float = 0.03
    chase_atr: float = 3.0
    traders: int = 40
    min_traders: int = 3
    max_spread: float = 0.0005
    max_impact: float = 0.0010
    margin_pct: float = 0.40      # Compounder: of the free cash
    max_positions: int = 2
    open_every_s: float = 120
    stop_atr: float = 1.5
    half_r: float = 2.0           # half closes at twice the stop distance
    trail_atr: float = 1.5
    fee: float = 0.00045
    cooldown_min: float = 10
    crowded_funding: float = 0.00003   # per hour (Hyperliquid's usual rate is 0.00125%)


@dataclass
class Perp:
    id: str
    coin: str
    side: int                 # 1 long, -1 short
    lev: float
    opened: str
    entry: float              # fill price, slippage included
    qty: float
    margin: float
    atr: float
    stop: float
    liq: float
    peak: float               # best price in our favour
    last: float
    fees: float = 0.0
    funding: float = 0.0
    trail_on: bool = False
    score: float = 0.0
    series: list = field(default_factory=list)   # [ms, price] since the open (coin page chart)
    half_done: bool = False
    half_px: float = 0.0
    half_ts: int = 0


@dataclass
class PerpState:
    p: PerpParams
    lev: float
    start_usd: float
    eur_usd: float
    cash: float
    started: str
    ends: str
    positions: list[Perp] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    curve: list[list] = field(default_factory=list)
    counts: dict = field(default_factory=lambda: {k: {} for k in BUCKET.values()})
    agents: dict = field(default_factory=dict)
    ticker: list[dict] = field(default_factory=list)
    coins_seen: int = 0
    api_errors: int = 0
    finished: bool = False
    mode: str = "futuros"
    venue: str = "invo"
    feeds: dict = field(default_factory=dict)
    live: dict = field(default_factory=dict)      # Hyperliquid dry-run: the real orders, never sent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _pt(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",")


def ema(xs: list[float], n: int) -> list[float]:
    k, out = 2 / (n + 1), []
    for x in xs:
        out.append(x if not out else out[-1] + k * (x - out[-1]))
    return out


def atr(c: list[dict], n: int = 14) -> float:
    tr = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(c, c[1:])]
    return sum(tr[-n:]) / max(1, len(tr[-n:]))


def rsi(xs: list[float], n: int = 14) -> float:
    gains = [max(0.0, b - a) for a, b in zip(xs, xs[1:])][-n:]
    losses = [max(0.0, a - b) for a, b in zip(xs, xs[1:])][-n:]
    g, lo = sum(gains) / n, sum(losses) / n
    return 100.0 if lo == 0 else 100 - 100 / (1 + g / lo)


class PerpDesk:
    RISK_EVERY_S = 5
    SCAN_EVERY_S = 180
    TRADERS_EVERY_S = 900

    def __init__(self, minutes: float, eur: float, lev: float = 5, params: PerpParams | None = None):
        self.p = params or PerpParams()
        self.http = requests.Session()
        self.http.headers.update(HEADERS)
        self.http_risk = requests.Session()
        self.http_risk.headers.update(HEADERS)
        rate = self._eur_usd()
        now = _now()
        self.d = PerpState(self.p, float(min(max(lev, 1), 10)), eur * rate, rate, eur * rate,
                           now.isoformat(timespec="seconds"), (now + timedelta(minutes=minutes)).isoformat(timespec="seconds"))
        self.d.agents = {a: {"coin": None, "text": "à espera", "ok": None, "ts": None} for a in AGENTS}
        self.d.feeds = {"hyperliquid": {"on": True, "text": "Hyperliquid: preços, livro e funding"},
                        "invo": {"on": False, "text": "INVO sem API pública: abre a app para negociar"}}
        OUT.mkdir(exist_ok=True)
        self.events_file = OUT / "events.jsonl"
        self.events_file.write_text("", encoding="utf-8")
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.meta: dict[str, dict] = {}       # coin -> {max_lev, funding, mark, vol, oi, day_pct}
        self.crowd: dict[str, dict] = {}      # coin -> top traders' positions
        self.crowd_at = 0.0
        self.cool: dict[str, float] = {}      # coin -> time it may be looked at again
        self.queue: list[tuple[str, int, dict]] = []
        self.ready: list[dict] = []
        self.last_open = 0.0
        self.live = hl_live.Bridge(self.info, OUT)

    # ------------------------------------------------------------ data
    def _eur_usd(self) -> float:
        try:
            r = requests.get("https://data-api.binance.vision/api/v3/ticker/price", params={"symbol": "EURUSDT"},
                             timeout=15)
            return float(r.json()["price"])
        except (requests.RequestException, KeyError, ValueError):
            return 1.10

    def info(self, body: dict, session: requests.Session | None = None):
        for attempt in range(3):
            try:
                r = (session or self.http).post(INFO, json=body, timeout=15)
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning("Hyperliquid %s failed: %s", body.get("type"), exc)
                time.sleep(2)
        self.d.api_errors += 1
        return None

    def candles(self, coin: str, interval: str, bars: int) -> list[dict]:
        ms = {"15m": 900_000, "1h": 3_600_000}[interval]
        now = int(time.time() * 1000)
        raw = self.info({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                                                           "startTime": now - bars * ms, "endTime": now}}) or []
        return [{"t": int(c["t"]), "o": _f(c["o"]), "h": _f(c["h"]), "l": _f(c["l"]), "c": _f(c["c"]), "v": _f(c["v"])}
                for c in raw]

    def load_meta(self) -> list[str]:
        """The universe: the most traded perps, with their max leverage, funding and 24 h move."""
        res = self.info({"type": "metaAndAssetCtxs"})
        if not res:
            return []
        meta, ctxs = res
        rows = []
        for a, c in zip(meta["universe"], ctxs):
            if a.get("isDelisted"):
                continue
            mark, prev = _f(c.get("markPx")), _f(c.get("prevDayPx"))
            self.meta[a["name"]] = {"max_lev": int(a.get("maxLeverage") or 1), "funding": _f(c.get("funding")),
                                    "mark": mark, "vol": _f(c.get("dayNtlVlm")), "oi": _f(c.get("openInterest")) * mark,
                                    "day_pct": mark / prev - 1 if prev else 0.0}
            rows.append((self.meta[a["name"]]["vol"], a["name"]))
        rows.sort(reverse=True)
        coins = [n for v, n in rows if v >= self.p.min_volume][:self.p.universe]
        self.d.ticker = [{"symbol": n, "pct": self.meta[n]["day_pct"]} for n in coins[:14]]
        self.d.coins_seen = len(rows)        # every perp read; the 25 most traded go on
        return coins

    def load_crowd(self):
        """Open positions of the best traders on Hyperliquid's public leaderboard."""
        try:
            r = self.http.get(LEADERBOARD, timeout=90)
            rows = r.json()["leaderboardRows"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("leaderboard failed: %s", exc)
            self.d.api_errors += 1
            return

        def perf(row, w):
            return {k: _f(v) for k, v in dict(row["windowPerformances"]).get(w, {}).items()}
        good = [x for x in rows if _f(x.get("accountValue")) >= 1e6 and perf(x, "month").get("pnl", 0) > 0
                and perf(x, "allTime").get("pnl", 0) > 0 and perf(x, "month").get("roi", 0) > 0.05]
        good.sort(key=lambda x: perf(x, "month")["pnl"], reverse=True)
        crowd: dict[str, dict] = {}
        for x in good[:self.p.traders]:
            st = self.info({"type": "clearinghouseState", "user": x["ethAddress"]})
            for ap in (st or {}).get("assetPositions", []):
                pos = ap.get("position") or {}
                size, value = _f(pos.get("szi")), _f(pos.get("positionValue"))
                if not size:
                    continue
                c = crowd.setdefault(pos.get("coin"), {"long": 0, "short": 0, "long_usd": 0.0, "short_usd": 0.0})
                side = "long" if size > 0 else "short"
                c[side] += 1
                c[side + "_usd"] += value
        self.crowd, self.crowd_at = crowd, time.time()
        log.info("top traders: %d accounts, positions in %d coins", min(len(good), self.p.traders), len(crowd))

    # ------------------------------------------------------------ bookkeeping
    def event(self, stage: str, coin: str, verdict: str, reason: str = "", **values):
        """stage is the step inside an agent (TRENDS, SMARTS, ...); the app shows the agent."""
        agent = "RISK" if verdict == "sell" else AGENT_OF.get(stage, stage)
        e = {"ts": _now().isoformat(timespec="seconds"), "agent": agent, "stage": stage, "coin": coin,
             "verdict": verdict, "reason": reason, **values}
        with self.lock:
            self.d.events.append(e)
            with self.events_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            if verdict in ("reject", "skip", "kill"):
                c = self.d.counts.setdefault(BUCKET.get(stage, "core"), {})
                key = reason.split(":")[0]
                c[key] = c.get(key, 0) + 1
            if agent in self.d.agents and verdict != "wait":
                self.d.agents[agent] = {"coin": coin, "text": reason, "ok": verdict in ("pass", "buy", "sell"),
                                        "ts": e["ts"], "verdict": verdict,
                                        **{k: values[k] for k in ("score", "eur", "fill_s", "kind", "side") if k in values}}
        log.info("%-7s %-6s %-6s %s", stage, coin, verdict, reason)

    def value(self, q: Perp) -> float:
        return max(0.0, q.margin + q.side * q.qty * (q.last - q.entry) - q.funding)

    def equity(self) -> float:
        with self.lock:
            return self.d.cash + sum(self.value(q) for q in self.d.positions)

    def save(self):
        with self.lock:
            self.d.live = self.live.summary()
            eq = self.equity()
            self.d.curve.append([int(time.time() * 1000), round(eq, 4)])
            state = asdict(self.d)
            state["ready"] = [{"coin": r["coin"], "side": r["side"], "score": r["score"]} for r in self.ready]
        state["events"] = state["events"][-200:]
        state["equity_usd"], state["equity_eur"] = eq, eq / self.d.eur_usd
        state["start_eur"] = self.d.start_usd / self.d.eur_usd
        tmp = OUT / "state.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        for attempt in range(10):   # on Windows the dashboard may be reading state.json right now
            try:
                tmp.replace(OUT / "state.json")
                return
            except PermissionError:
                time.sleep(0.2)

    # ------------------------------------------------------------ TRENDS
    def scan(self):
        coins = self.load_meta()
        if time.time() - self.crowd_at > self.TRADERS_EVERY_S:
            self.load_crowd()
        with self.lock:
            busy = {q.coin for q in self.d.positions} | {r["coin"] for r in self.ready} | {c for c, _, _ in self.queue}
        for coin in coins:
            if coin in busy or self.cool.get(coin, 0) > time.time():
                continue
            self.trends(coin)

    def trends(self, coin: str):
        c = self.candles(coin, "15m", 120)
        self.cool[coin] = time.time() + self.p.cooldown_min * 60
        if len(c) < 60:
            return
        closes = [b["c"] for b in c]
        e20, e50 = ema(closes, 20), ema(closes, 50)
        px, a = closes[-1], atr(c)
        atr_pct = a / px if px else 0
        if e20[-1] > e50[-1] and e20[-1] > e20[-4] and e50[-1] >= e50[-4]:
            side = 1
        elif e20[-1] < e50[-1] and e20[-1] < e20[-4] and e50[-1] <= e50[-4]:
            side = -1
        else:
            return self.event("TRENDS", coin, "kill", "sem tendência: EMA 20 e 50 cruzadas")
        name = "long" if side == 1 else "short"
        ran = (px - closes[-5]) * side
        if atr_pct < self.p.min_atr:
            return self.event("TRENDS", coin, "kill", f"parado: ATR {_pt(atr_pct * 100)}%")
        if ran > self.p.chase_atr * a:
            return self.event("TRENDS", coin, "kill", f"já correu: {ran / a:.1f} ATR numa hora")
        self.event("TRENDS", coin, "pass", f"{name} · EMA 20/50 · {'+' if self.meta[coin]['day_pct'] >= 0 else ''}"
                                           f"{_pt(self.meta[coin]['day_pct'] * 100, 1)}% 24h", side=name)
        self.queue.append((coin, side, {"c15": c, "atr": a, "px": px}))

    # the other four agents, then CORE
    def auditor(self, coin: str, side: int, ctx: dict):
        votes = {"TRENDS": True}
        votes["SMARTS"] = self.smarts(coin, side)
        if not votes["SMARTS"]:
            return
        debate = self.debate(coin, side, ctx)
        votes["DEBATE"] = debate["ok"]
        if not debate["ok"]:
            return
        size = self.margin() * self.leverage(coin)
        route = self.router(coin, side, size)
        votes["ROUTER"] = route["ok"]
        if not route["ok"]:
            return
        with self.lock:
            room = len(self.d.positions) + len(self.ready) < self.p.max_positions + 2
        votes["EXEC"] = room
        if not room:
            return self.event("EXEC", coin, "skip", f"cheio: {self.p.max_positions} posições e fila")
        score = round((debate["score"] + route["score"] + votes_score(self.crowd.get(coin), side)) / 3)
        name = "long" if side == 1 else "short"
        self.event("CORE", coin, "pass", f"passou · {name} · nota {score} · 5/5 de acordo", votes=votes, side=name,
                   score=score)
        self.ready.append({"coin": coin, "side": side, "score": score, "since": time.time(), "atr": ctx["atr"]})
        self.event("EXEC", coin, "wait", f"na fila: {name} com nota {score}")

    # ------------------------------------------------------------ SMARTS
    def smarts(self, coin: str, side: int) -> bool:
        c = self.crowd.get(coin)
        if not self.crowd:
            self.event("SMARTS", coin, "reject", "sem dados: leaderboard da Hyperliquid não respondeu")
            return False
        if not c:
            self.event("SMARTS", coin, "reject", "nenhum top trader nesta moeda")
            return False
        mine, theirs = ("long", "short") if side == 1 else ("short", "long")
        n, usd, other = c[mine], c[mine + "_usd"], c[theirs + "_usd"]
        ok = n >= self.p.min_traders and usd > other
        txt = f"{n} top traders {mine} · ${_pt(usd / 1e6, 1)}M vs ${_pt(other / 1e6, 1)}M"
        self.event("SMARTS", coin, "pass" if ok else "reject",
                   txt if ok else (f"poucos top traders {mine}: {n}" if n < self.p.min_traders else f"top traders do outro lado: {txt}"),
                   traders=n, usd=round(usd), against=round(other))
        return ok

    # ------------------------------------------------------------ DEBATE
    def debate(self, coin: str, side: int, ctx: dict) -> dict:
        c15, a, px = ctx["c15"], ctx["atr"], ctx["px"]
        h1 = self.candles(coin, "1h", 80)
        m = self.meta.get(coin, {})
        closes = [b["c"] for b in c15]
        r = rsi(closes)
        day = c15[-96:]
        vwap = sum(b["c"] * b["v"] for b in day) / max(1e-9, sum(b["v"] for b in day))
        e_h1 = ema([b["c"] for b in h1], 50) if len(h1) >= 55 else []
        vol_now = sum(b["v"] for b in c15[-4:]) / 4
        vol_before = sum(b["v"] for b in c15[-20:-4]) / 16 if len(c15) >= 20 else vol_now
        funding = m.get("funding", 0.0)
        votes = {
            "tendência de 1h": bool(e_h1) and (h1[-1]["c"] - e_h1[-1]) * side > 0 and (e_h1[-1] - e_h1[-6]) * side > 0,
            "RSI ok": (45 <= r <= 70) if side == 1 else (30 <= r <= 55),
            "funding não lotado": funding * side < self.p.crowded_funding,
            "volume a subir": vol_now > vol_before,
            "lado certo do VWAP": (px - vwap) * side > 0,
        }
        bulls = sum(votes.values())
        risks = []
        atr_pct = a / px if px else 0
        if not self.p.min_atr <= atr_pct <= self.p.max_atr:
            risks.append(f"ATR {_pt(atr_pct * 100)}%")
        if m.get("max_lev", 1) < 2:
            risks.append("sem alavancagem")
        ok = not risks and bulls >= 4
        name = "long" if side == 1 else "short"
        text = f"{name}: bull {bulls} / bear {5 - bulls} · " + ("risco ok" if not risks else "risco: " + ", ".join(risks))
        self.event("DEBATE", coin, "pass" if ok else "reject", text, votes=votes, rsi=round(r), funding=funding,
                   score=40 + 12 * bulls)
        return {"ok": ok, "score": 40 + 12 * bulls}

    # ------------------------------------------------------------ ROUTER
    def book_fill(self, coin: str, side: int, notional: float, session=None) -> dict | None:
        b = self.info({"type": "l2Book", "coin": coin}, session)
        if not b or len(b.get("levels", [])) < 2 or not b["levels"][0] or not b["levels"][1]:
            return None
        bids, asks = b["levels"]
        bid, ask = _f(bids[0]["px"]), _f(asks[0]["px"])
        mid = (bid + ask) / 2
        left, cost, qty = notional, 0.0, 0.0
        for lv in (asks if side == 1 else bids):
            px, sz = _f(lv["px"]), _f(lv["sz"])
            take = min(left, px * sz)
            cost += take
            qty += take / px
            left -= take
            if left <= 0:
                break
        if left > 0 or qty <= 0:
            return {"mid": mid, "spread": (ask - bid) / mid, "impact": 1.0, "fill": None}
        fill = cost / qty
        return {"mid": mid, "spread": (ask - bid) / mid, "impact": abs(fill / mid - 1), "fill": fill}

    def router(self, coin: str, side: int, notional: float) -> dict:
        if notional < 10:
            self.event("ROUTER", coin, "reject", "sem saldo livre")
            return {"ok": False}
        f = self.book_fill(coin, side, notional)
        if not f:
            self.event("ROUTER", coin, "reject", "livro de ordens não respondeu")
            return {"ok": False}
        ok = f["spread"] <= self.p.max_spread and f["impact"] <= self.p.max_impact
        self.event("ROUTER", coin, "pass" if ok else "reject",
                   f"hyperliquid · spread {_pt(f['spread'] * 100, 3)}% · {_pt(f['impact'] * 100, 3)}% slip")
        return {"ok": ok, "score": 100 if f["impact"] < 0.0003 else 80}

    # ------------------------------------------------------------ EXEC (open)
    def margin(self) -> float:
        """Compounder: 40% of the free cash (wins roll into the next trade)."""
        with self.lock:
            return self.d.cash * self.p.margin_pct

    def leverage(self, coin: str) -> float:
        return float(min(self.d.lev, self.meta.get(coin, {}).get("max_lev", 1)))

    def execute_best(self):
        self.ready = [r for r in self.ready if time.time() - r["since"] < 600]
        if not self.ready or time.time() - self.last_open < self.p.open_every_s:
            return
        with self.lock:
            if len(self.d.positions) >= self.p.max_positions:
                return
        best = max(self.ready, key=lambda r: r["score"])
        self.ready.remove(best)
        self.last_open = time.time()
        self.open(best["coin"], best["side"], best["atr"], best["score"])

    def open(self, coin: str, side: int, a: float, score: float):
        lev, margin = self.leverage(coin), self.margin()
        t0 = time.time()
        f = self.book_fill(coin, side, margin * lev)
        fill_s = time.time() - t0
        name = "long" if side == 1 else "short"
        if not f or not f["fill"]:
            return self.event("EXEC", coin, "skip", "sem preço no livro na hora de abrir")
        fill = f["fill"]
        qty = margin * lev / fill
        fee = qty * fill * self.p.fee
        mm = 1 / (2 * max(1, self.meta.get(coin, {}).get("max_lev", 10)))   # Hyperliquid's maintenance margin
        liq = fill * (1 - side * (1 / lev - mm))
        stop = fill - side * self.p.stop_atr * a
        with self.lock:
            if margin + fee > self.d.cash or margin < 5:
                return self.event("EXEC", coin, "skip", "sem saldo livre")
            self.d.cash -= margin + fee
            q = Perp(uuid.uuid4().hex[:6], coin, side, lev, _now().isoformat(timespec="seconds"), fill, qty, margin,
                     a, stop, liq, fill, fill, fees=fee, score=score)
            q.series = [[int(time.time() * 1000), fill]]
            self.d.positions.append(q)
        self.event("EXEC", coin, "buy", f"{name} {lev:g}x · margem {_pt(margin / self.d.eur_usd)} € · 40% do saldo livre · "
                                        f"nota {score:.0f} · via INVO (papel)", side=name, lev=lev, price=fill, stop=stop,
                   liq=liq, eur=round(margin / self.d.eur_usd, 2), score=round(score), fill_s=round(fill_s, 1))
        self.live.on_open(coin, side, lev, margin * lev, fill, stop, fill)

    # ------------------------------------------------------------ EXEC (risk every 5 s)
    def risk_loop(self):
        while not self.stop.is_set():
            tick = time.time()
            try:
                self.risk()
                self.save()
            except Exception:
                self.d.api_errors += 1
                log.exception("Risk tick failed, carrying on")
            self.stop.wait(max(0.5, self.RISK_EVERY_S - (time.time() - tick)))

    def risk(self, final: bool = False):
        with self.lock:
            if not self.d.positions:
                return
        mids = self.info({"type": "allMids"}, self.http_risk) or {}
        with self.lock:
            now = time.time()
            for q in list(self.d.positions):
                px = _f(mids.get(q.coin))
                if px <= 0:
                    if final:
                        self.close(q, q.last, "fim da sessão (último preço)")
                    continue
                hours = (now - getattr(q, "_tick", now)) / 3600
                q._tick = now
                q.funding += q.side * q.qty * px * self.meta.get(q.coin, {}).get("funding", 0.0) * hours
                q.last = px
                q.peak = max(q.peak, px) if q.side == 1 else min(q.peak, px)
                _mark(q.series, px)
                r = self.p.stop_atr * q.atr
                trail = q.peak - q.side * self.p.trail_atr * q.atr
                if (px - q.liq) * q.side <= 0:
                    self.close(q, px, "liquidada", "liq")
                elif (px - q.stop) * q.side <= 0:
                    self.close(q, px, "stop na entrada" if q.half_done else f"stop {self.p.stop_atr:g} ATR", "stop")
                elif not q.half_done and (px - q.entry) * q.side >= self.p.half_r * r:
                    self._half(q, px)
                elif q.half_done and (px - trail) * q.side <= 0:
                    self.close(q, px, f"trailing {self.p.trail_atr:g} ATR do máximo", "trail")
                elif final:
                    self.close(q, px, "fim da sessão", "end")

    def _record(self, q: Perp, exit_px: float, got: float, margin: float, fee_in: float, fee_out: float,
                funding: float, reason: str, kind: str):
        pnl = got - margin - fee_in
        name = "long" if q.side == 1 else "short"
        t = {"id": q.id, "coin": q.coin, "side": name, "lev": q.lev, "opened": q.opened,
             "closed": _now().isoformat(timespec="seconds"), "margin": margin, "entry": q.entry, "exit": exit_px,
             "pnl_usd": pnl, "pnl_eur": pnl / self.d.eur_usd, "pct": pnl / margin if margin else 0, "reason": reason,
             "kind": kind, "funding": funding, "fees": fee_in + fee_out, "score": q.score,
             "mult": (margin + pnl) / margin if margin else 1, "half_px": q.half_px, "half_ts": q.half_ts,
             "stop": q.stop, "series": _thin_series(q.series, 120)}
        self.d.trades.append(t)
        sign = "+" if pnl >= 0 else "-"
        self.event("EXEC", q.coin, "sell", f"fechou {name} ({reason}) {sign}{_pt(abs(pnl) / self.d.eur_usd)} € · "
                                           f"{'+' if t['pct'] >= 0 else ''}{_pt(t['pct'] * 100, 1)}% · via INVO (papel)",
                   side=name, pnl_eur=round(pnl / self.d.eur_usd, 2), pct=round(t["pct"], 4), kind=kind,
                   mult=round(t["mult"], 2))

    def _half(self, q: Perp, px: float):
        """+2R: close half, move the stop to the entry, let the rest trail."""
        exit_px = px * (1 - q.side * 0.0002)
        half, m_half, f_half, fee_half = q.qty / 2, q.margin / 2, q.funding / 2, q.fees / 2
        fee = half * exit_px * self.p.fee
        got = max(0.0, m_half + q.side * half * (exit_px - q.entry) - f_half - fee)
        self.d.cash += got
        q.qty -= half
        q.margin -= m_half
        q.funding -= f_half
        q.fees -= fee_half
        q.half_done, q.half_px, q.half_ts, q.trail_on = True, px, int(time.time() * 1000), True
        q.stop = q.entry
        self._record(q, exit_px, got, m_half, fee_half, fee, f_half, "metade a 2R · stop na entrada", "half")
        self.live.on_half(q.coin, q.side, px, q.stop)

    def close(self, q: Perp, px: float, reason: str, kind: str = "end"):
        with self.lock:
            if q not in self.d.positions:
                return
            exit_px = px * (1 - q.side * 0.0002)              # half a typical spread against us
            fee = q.qty * exit_px * self.p.fee
            got = 0.0 if kind == "liq" else max(0.0, q.margin + q.side * q.qty * (exit_px - q.entry) - q.funding - fee)
            self.d.cash += got
            self.d.positions.remove(q)
            self._record(q, exit_px, got, q.margin, q.fees, fee, q.funding, reason, kind)
            self.live.on_close(q.coin, q.side, exit_px, kind, got - q.margin - q.fees)

    # ------------------------------------------------------------ loop
    def run(self) -> str:
        end = datetime.fromisoformat(self.d.ends)
        log.info("Futures paper session: %.2f EUR at %gx until %s", self.d.start_usd / self.d.eur_usd, self.d.lev, self.d.ends)
        self.save()
        (OUT / "pid").write_text(str(os.getpid()), encoding="utf-8")
        STOP_FILE.unlink(missing_ok=True)
        risk_thread = threading.Thread(target=self.risk_loop, name="perp-risk", daemon=True)
        risk_thread.start()
        next_scan = 0.0
        try:
            while _now() < end:
                if STOP_FILE.exists():
                    log.info("Stopped from the app")
                    STOP_FILE.unlink(missing_ok=True)
                    break
                tick = time.time()
                try:
                    if tick >= next_scan:
                        self.scan()
                        next_scan = time.time() + self.SCAN_EVERY_S
                    for _ in range(3):
                        if self.queue:
                            self.auditor(*self.queue.pop(0))
                    self.execute_best()
                except Exception:
                    self.d.api_errors += 1
                    log.exception("Tick failed, carrying on")
                time.sleep(max(1.0, min(5 - (time.time() - tick), (end - _now()).total_seconds())))
        except KeyboardInterrupt:
            log.info("Stopped by hand")
        self.stop.set()
        risk_thread.join(timeout=30)
        try:
            self.risk(final=True)
        except Exception:
            log.exception("Final price check failed; closing at the last known prices")
        for q in list(self.d.positions):
            self.close(q, q.last, "fim da sessão (último preço)")
        self.d.finished = True
        self.d.ends = min(self.d.ends, _now().isoformat(timespec="seconds"))
        self.save()
        (OUT / "pid").unlink(missing_ok=True)
        report = self.report()
        (OUT / "relatorio.md").write_text(report, encoding="utf-8")
        return report

    def report(self) -> str:
        d, eur = self.d, self.d.eur_usd
        end = self.equity()
        wins = [t for t in d.trades if t["pnl_usd"] > 0]
        lines = ["# Modo futuros (5 agentes) em papel", "",
                 f"De {d.started[11:16]} a {_now().isoformat()[11:16]} UTC ({d.started[:10]}), alavancagem {d.lev:g}x. "
                 "Preços e livro de ordens da Hyperliquid (onde a INVO negoceia). Nenhuma ordem real.", "",
                 f"- Saldo: {_pt(d.start_usd / eur)} € → {_pt(end / eur)} € ({'+' if end >= d.start_usd else ''}"
                 f"{_pt((end / d.start_usd - 1) * 100, 1)}%)",
                 f"- Trades: {len(d.trades)}, {len(wins)} a ganhar" + (f" ({len(wins) / len(d.trades):.0%})" if d.trades else ""),
                 f"- Taxas: {_pt(sum(t['fees'] for t in d.trades) / eur)} € · funding: {_pt(sum(t['funding'] for t in d.trades) / eur)} €",
                 "", "## Trades", ""]
        for t in d.trades:
            lines.append(f"- {t['closed'][11:16]} {t['coin']} {t['side']} {t['lev']:g}x: {'+' if t['pnl_eur'] >= 0 else ''}"
                         f"{_pt(t['pnl_eur'])} € ({t['reason']})")
        lines += ["", "## Onde as moedas foram cortadas", ""]
        for agent in STAGES:
            c = d.counts.get(BUCKET[agent], {})
            lines.append(f"- **{agent}:** " + (", ".join(f"{k} ({v})" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))
                                                  if c else "nada cortado"))
        lines += ["", "Um resultado de poucas horas é sobretudo sorte. Esta mesa não tem backtest: as regras são "
                      "razoáveis, não provadas."]
        return "\n".join(lines) + "\n"


def _mark(series: list, px: float, cap: int = 600):
    series.append([int(time.time() * 1000), px])
    if len(series) > cap:
        series[:] = series[:cap // 2:2] + series[cap // 2:]


def _thin_series(series: list, n: int) -> list:
    if len(series) <= n:
        return list(series)
    step = len(series) / n
    return [series[int(i * step)] for i in range(n)] + [series[-1]]


def votes_score(c: dict | None, side: int) -> float:
    if not c:
        return 50
    mine = c["long" if side == 1 else "short"]
    return min(100, 50 + 8 * mine)


def run(minutes: float, eur: float, lev: float = 5) -> str:
    return PerpDesk(minutes, eur, lev).run()
