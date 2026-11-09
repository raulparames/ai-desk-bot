"""Paper test of a Solana memecoin desk, adapted from the "5 agents" post. No wallet, no orders.

    python -m deskbot memetest --minutes 30 --eur 1000

  Scanner   new PumpSwap pools (coins that graduated from pump.fun) + Solana trending pools
            (GeckoTerminal). Kills: liquidity < $40k, +80% in 1h, pool younger than 10 min (waits up to 60).
  Auditor   layers in order, the first red one cuts the coin: mint authority revoked, freeze authority
            revoked (Solana RPC), top 10 holders < 25% (GeckoTerminal), not a honeypot, someone has sold
            in the last hour, liquidity still >= $40k.
  Analyst   4 numbers from 0 to 1 computed from the pool (rules mode of the post):
              alive   = volume of the last 5 min x 12 / volume of the last hour (is it still trading now?)
              organic = unique buyers in 1 h / 100, times (unique buyers / buys) / 0.25 (few wallets
                        buying many times looks like bots)
              flush   = 1 if the price dropped >= 5% in 5 min, 0.5 if it dropped, else 0
              chase   = the larger of 1 h rise / 80% and 5 min rise / 15%
            Gate: alive >= 0.70, organic >= 0.65, chase <= 0.30, score >= 70.
  Executor  the coins that passed wait in a queue and the best score is bought first, at most one buy
            every 2 minutes; a coin waits up to 10 minutes. 8% of the balance, at most 2% of the pool's
            liquidity, at most 5 positions. Re-checks the price right before buying: skips if it ran
            > 15% or liquidity fell > 20%.
  Risk      runs on its own thread, price every 15 s (DexScreener): stop -6%, trailing from +20% that
            sells 12% off the top, out after 3 h if not +5%, out at once if the liquidity disappears.

Fills are simulated at the polled price with 1% cost per side plus price impact (2 x size / liquidity).
Everything is written to memetest/ (events, state for the dashboard, final report).
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from .config import ROOT

log = logging.getLogger("memetest")
OUT = ROOT / "memetest"
STOP_FILE = OUT / "stop"
GECKO = "https://api.geckoterminal.com/api/v2"
DEXS = "https://api.dexscreener.com/latest/dex/pairs/solana/"
RPC = "https://api.mainnet-beta.solana.com"
HEADERS = {"Accept": "application/json", "User-Agent": "ai-desk-bot-papertest/1.0"}
MAJORS = {"SOL", "WSOL", "USDC", "USDT", "JUP", "JTO", "RAY", "BONK", "WIF", "PYTH", "JLP", "MSOL", "JITOSOL",
          "BSOL", "ETH", "WETH", "BTC", "WBTC", "CBBTC", "TRUMP", "ORCA"}


@dataclass
class MemeParams:
    min_liquidity: float = 40_000
    max_h1_pct: float = 80
    min_age_min: float = 10
    max_wait_min: float = 60
    max_top10_pct: float = 25
    min_alive: float = 0.70
    min_organic: float = 0.65
    max_chase: float = 0.30
    min_score: float = 70
    stack_pct: float = 0.08
    max_pool_pct: float = 0.02
    max_positions: int = 5
    buy_every_s: float = 120
    queue_max_min: float = 10
    recheck_price_up: float = 0.15
    recheck_liq_drop: float = 0.20
    stop_pct: float = 0.06
    trail_start: float = 0.20
    trail_pct: float = 0.12
    timeout_h: float = 3.0
    timeout_min_gain: float = 0.05
    cost_per_side: float = 0.01


@dataclass
class Position:
    id: str
    coin: str
    name: str
    pool: str
    mint: str
    opened: str
    raw_entry: float          # market price when bought
    entry: float              # price paid, with costs
    qty: float
    cost_usd: float
    liquidity: float
    peak: float
    last: float
    trail_on: bool = False
    score: float = 0.0


@dataclass
class Desk:
    p: MemeParams
    start_usd: float
    eur_usd: float
    cash: float
    started: str
    ends: str
    positions: list[Position] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    curve: list[list] = field(default_factory=list)
    seen: dict[str, str] = field(default_factory=dict)     # pool -> final verdict
    waiting: dict[str, str] = field(default_factory=dict)  # pool -> first seen (too young)
    counts: dict[str, dict[str, int]] = field(default_factory=lambda: {
        "scanner": {}, "auditor": {}, "analyst": {}, "executor": {}})
    pools_seen: int = 0
    api_errors: int = 0
    finished: bool = False
    mode: str = "classic"
    agents: dict = field(default_factory=dict)           # agent -> its latest reading (memes mode)
    venue: str = ""                                      # app the coins open in: gmgn or fomo (memes mode)
    feeds: dict = field(default_factory=dict)            # data sources and their state (memes mode)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _pt(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",")


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


class MemeTest:
    def __init__(self, minutes: float, eur: float, params: MemeParams | None = None):
        self.p = params or MemeParams()
        self.http = requests.Session()
        self.http.headers.update(HEADERS)
        rate = self._eur_usd()
        now = _now()
        self.d = Desk(self.p, eur * rate, rate, eur * rate, now.isoformat(timespec="seconds"),
                      (now + timedelta(minutes=minutes)).isoformat(timespec="seconds"))
        OUT.mkdir(exist_ok=True)
        self.events_file = OUT / "events.jsonl"
        self.events_file.write_text("", encoding="utf-8")
        self._gecko_calls: list[float] = []
        self.queue: list[tuple[str, dict, str]] = []   # coins the Scanner passed, waiting for the Auditor
        self.ready: list[dict] = []                    # coins the Analyst passed, waiting for the Executor
        self.last_buy = 0.0
        self.lock = threading.RLock()                  # Risk runs on its own thread
        self.stop = threading.Event()
        self.http_risk = requests.Session()
        self.http_risk.headers.update(HEADERS)

    # ------------------------------------------------------------ data
    def _eur_usd(self) -> float:
        try:
            r = requests.get("https://data-api.binance.vision/api/v3/ticker/price", params={"symbol": "EURUSDT"},
                             timeout=15)
            return float(r.json()["price"])
        except (requests.RequestException, KeyError, ValueError):
            return 1.10

    GECKO_PER_MIN = 8

    def _gecko(self, path: str, params: dict | None = None):
        """The free GeckoTerminal API answers 429 above a few calls a minute: stay under 12, retry once."""
        for attempt in range(2):
            cutoff = time.time() - 60
            self._gecko_calls = [t for t in self._gecko_calls if t > cutoff]
            if len(self._gecko_calls) >= self.GECKO_PER_MIN:
                time.sleep(max(0.0, 61 - (time.time() - self._gecko_calls[0])))
            self._gecko_calls.append(time.time())
            try:
                r = self.http.get(GECKO + path, params=params or {}, timeout=20)
                if r.status_code == 429:
                    log.info("GeckoTerminal rate limit, waiting 20 s")
                    time.sleep(20)
                    continue
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning("GeckoTerminal %s failed: %s", path, exc)
                time.sleep(3)
        self.d.api_errors += 1
        return None

    def _pairs(self, pools: list[str], session: requests.Session | None = None) -> dict[str, dict]:
        out = {}
        for i in range(0, len(pools), 30):
            chunk = pools[i:i + 30]
            try:
                r = (session or self.http).get(DEXS + ",".join(chunk), timeout=20)
                r.raise_for_status()
                for pr in r.json().get("pairs") or []:
                    out[pr.get("pairAddress")] = pr
            except (requests.RequestException, ValueError) as exc:
                self.d.api_errors += 1
                log.warning("DexScreener failed: %s", exc)
        return out

    def _mint_info(self, mint: str) -> dict | None:
        for attempt in range(2):
            try:
                r = self.http.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                              "params": [mint, {"encoding": "jsonParsed"}]}, timeout=20)
                v = (r.json().get("result") or {}).get("value") or {}
                data = v.get("data")
                if isinstance(data, dict):
                    return data.get("parsed", {}).get("info", {})
            except (requests.RequestException, ValueError):
                pass
            time.sleep(2)
        return None

    # ------------------------------------------------------------ bookkeeping
    def event(self, agent: str, coin: str, verdict: str, reason: str = "", **values):
        e = {"ts": _now().isoformat(timespec="seconds"), "agent": agent, "coin": coin, "verdict": verdict,
             "reason": reason, **values}
        with self.lock:
            self.d.events.append(e)
            with self.events_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            if verdict in ("cut", "reject", "skip", "kill"):
                bucket = {"Scanner": "scanner", "Auditor": "auditor", "Analyst": "analyst", "Executor": "executor"}[agent]
                self.d.counts[bucket][reason.split(":")[0]] = self.d.counts[bucket].get(reason.split(":")[0], 0) + 1
        log.info("%-8s %-10s %-7s %s", agent, coin, verdict, reason)

    def equity(self) -> float:
        with self.lock:
            return self.d.cash + sum(p.qty * p.last * (1 - self.p.cost_per_side) for p in self.d.positions)

    def save(self):
        with self.lock:
            eq = self.equity()
            self.d.curve.append([int(time.time() * 1000), round(eq, 4)])
            state = asdict(self.d)
            state["ready"] = [{"coin": r["coin"], "score": r["score"], "since": r["since"]} for r in self.ready]
        state["events"] = state["events"][-200:]
        state["equity_usd"] = eq
        state["equity_eur"] = eq / self.d.eur_usd
        state["start_eur"] = self.d.start_usd / self.d.eur_usd
        tmp = OUT / "state.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        for attempt in range(10):  # on Windows the dashboard may be reading state.json right now
            try:
                tmp.replace(OUT / "state.json")
                return
            except PermissionError:
                time.sleep(0.2)
        log.warning("Could not update state.json (file busy); will try again next tick")

    # ------------------------------------------------------------ Scanner
    def scan(self):
        found: dict[str, dict] = {}
        for page in (1, 2):
            d = self._gecko("/networks/solana/new_pools", {"page": page, "include": "base_token,dex"})
            for pool in (d or {}).get("data", []):
                if pool["relationships"]["dex"]["data"]["id"] == "pumpswap":
                    found[pool["attributes"]["address"]] = {**pool, "source": "graduada"}
        self._scans = getattr(self, "_scans", 0) + 1
        d = self._gecko("/networks/solana/trending_pools", {"page": 1, "include": "base_token,dex"})             if self._scans % 2 == 1 else None   # trending changes slowly: every other minute
        for pool in (d or {}).get("data", []):
            found.setdefault(pool["attributes"]["address"], {**pool, "source": "em alta"})
        self.d.pools_seen = len(set(self.d.seen) | set(found) | set(self.d.waiting))
        for addr, pool in found.items():
            if addr in self.d.seen or any(p.pool == addr for p in self.d.positions):
                continue
            self.scanner(addr, pool)

    def scanner(self, addr: str, pool: dict):
        a = pool["attributes"]
        coin = a["name"].split(" / ")[0][:14]
        if coin.upper() in MAJORS:
            self.d.seen[addr] = "major"
            return
        liq = _f(a.get("reserve_in_usd"))
        h1 = _f((a.get("price_change_percentage") or {}).get("h1"))
        created = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00"))
        age = (_now() - created).total_seconds() / 60
        vals = {"liquidity": round(liq), "h1_pct": h1, "age_min": round(age, 1), "source": pool["source"]}
        if liq < self.p.min_liquidity:
            reason = f"liquidez: ${_pt(liq, 0)} < ${_pt(self.p.min_liquidity, 0)}"
        elif h1 > self.p.max_h1_pct:
            reason = f"já correu: +{h1:.0f}% em 1h"
        elif age < self.p.min_age_min:
            if addr not in self.d.waiting:
                self.d.waiting[addr] = _now().isoformat()
                self.event("Scanner", coin, "wait", f"pool com {age:.0f} min: espera pelos {self.p.min_age_min:g}", **vals)
                return
            if (_now() - datetime.fromisoformat(self.d.waiting[addr])).total_seconds() / 60 <= self.p.max_wait_min:
                return
            reason = "esperou demais"
        else:
            reason = ""
        self.d.waiting.pop(addr, None)
        if reason:
            self.d.seen[addr] = "scanner"
            self.event("Scanner", coin, "kill", reason, **vals)
            return
        self.d.seen[addr] = "pass"
        self.event("Scanner", coin, "pass", "→ Auditor", **vals)
        self.queue.append((addr, pool, coin))

    # ------------------------------------------------------------ Auditor
    def auditor(self, addr: str, pool: dict, coin: str):
        mint = pool["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
        info = self._gecko(f"/networks/solana/tokens/{mint}/info") or {}
        attrs = (info.get("data") or {}).get("attributes") or {}
        mi = self._mint_info(mint)
        a = pool["attributes"]
        tx = (a.get("transactions") or {}).get("h1") or {}
        top10 = _f(((attrs.get("holders") or {}).get("distribution_percentage") or {}).get("top_10"), -1)
        liq = _f(a.get("reserve_in_usd"))
        layers = [
            ("mint", "mint revogada", mi is not None and mi.get("mintAuthority") is None,
             "sem dados da rede" if mi is None else ("revogada" if mi.get("mintAuthority") is None else "ATIVA")),
            ("freeze", "freeze revogado", mi is not None and mi.get("freezeAuthority") is None,
             "sem dados da rede" if mi is None else ("revogado" if mi.get("freezeAuthority") is None else "ATIVO")),
            ("top10", f"top 10 < {self.p.max_top10_pct:g}%", 0 <= top10 < self.p.max_top10_pct,
             "sem dados" if top10 < 0 else f"{_pt(top10, 1)}%"),
            ("honeypot", "não é honeypot", str(attrs.get("is_honeypot", "unknown")).lower() not in ("yes", "true"),
             str(attrs.get("is_honeypot", "desconhecido"))),
            ("sells", "há vendas na última hora", int(tx.get("sells") or 0) > 0, f"{int(tx.get('sells') or 0)} vendas"),
            ("liquidity", f"liquidez ≥ ${_pt(self.p.min_liquidity, 0)}", liq >= self.p.min_liquidity, f"${_pt(liq, 0)}"),
        ]
        checks = []
        for i, (key, rule, ok, value) in enumerate(layers, 1):
            checks.append({"layer": i, "key": key, "rule": rule, "ok": ok, "value": value})
            if not ok:
                self.d.seen[addr] = "auditor"
                self.event("Auditor", coin, "cut", f"camada {i} ({rule}): {value}", layers=checks, mint=mint)
                return
        self.event("Auditor", coin, "pass", f"{len(layers)}/{len(layers)} → Analyst", layers=checks, mint=mint)
        self.analyst(addr, pool, coin, mint)

    # ------------------------------------------------------------ Analyst (rules mode)
    def analyst(self, addr: str, pool: dict, coin: str, mint: str):
        a = pool["attributes"]
        tx = a.get("transactions") or {}
        h1, m5 = tx.get("h1") or {}, tx.get("m5") or {}
        pc = a.get("price_change_percentage") or {}
        vol = a.get("volume_usd") or {}
        # a fresh DexScreener read of the pool, like the post (GeckoTerminal's numbers can be a few minutes old)
        fresh = self._pairs([addr]).get(addr) or {}
        fv, fpc, ftx = fresh.get("volume") or {}, fresh.get("priceChange") or {}, (fresh.get("txns") or {}).get("m5") or {}
        vol_m5 = _f(fv.get("m5"), _f(vol.get("m5"))) if fresh else _f(vol.get("m5"))
        vol_h1 = _f(fv.get("h1"), _f(vol.get("h1"))) if fresh else _f(vol.get("h1"))
        alive = _clamp(vol_m5 * 12 / vol_h1) if vol_h1 > 0 else 0.0   # still trading now?
        buyers, buys = _f(h1.get("buyers")), _f(h1.get("buys"))
        organic = _clamp(buyers / 100) * (_clamp(buyers / buys / 0.25) if buys > 0 else 0.0)
        m5pc = _f(fpc.get("m5"), _f(pc.get("m5"))) if fresh else _f(pc.get("m5"))
        h1pc = _f(fpc.get("h1"), _f(pc.get("h1"))) if fresh else _f(pc.get("h1"))
        b5, s5 = (_f(ftx.get("buys")), _f(ftx.get("sells"))) if fresh else (_f(m5.get("buys")), _f(m5.get("sells")))
        flush = 1.0 if m5pc <= -5 and b5 >= s5 else 0.5 if m5pc < 0 else 0.0
        chase = max(_clamp(h1pc / 80), _clamp(m5pc / 15))
        score = round(100 * (alive + organic + flush + (1 - chase)) / 4)
        ans = {"alive": round(alive, 2), "organic": round(organic, 2), "flush": flush, "chase": round(chase, 2),
               "score": score, "vol_m5": round(vol_m5), "vol_h1": round(vol_h1), "buyers_h1": int(buyers),
               "buys_h1": int(buys), "m5_pct": m5pc, "h1_pct": h1pc, "fresh": bool(fresh)}
        failed = [f"{k} {v}" for k, v, bad in (
            ("vivo", ans["alive"], alive < self.p.min_alive), ("orgânico", ans["organic"], organic < self.p.min_organic),
            ("perseguição", ans["chase"], chase > self.p.max_chase), ("nota", score, score < self.p.min_score)) if bad]
        if failed:
            self.d.seen[addr] = "analyst"
            self.event("Analyst", coin, "reject", f"{failed[0].split()[0]}: {', '.join(failed)}", answers=ans)
            return
        self.event("Analyst", coin, "pass", f"nota {score} → fila do Executor", answers=ans)
        self.d.seen[addr] = "ready"
        self.ready.append({"score": score, "since": _now().isoformat(timespec="seconds"), "addr": addr,
                           "pool": pool, "coin": coin, "mint": mint})
        self.event("Executor", coin, "wait", f"na fila com nota {score} ({len(self.ready)} na fila)")

    # ------------------------------------------------------------ Executor
    def execute_best(self):
        """Best score first, one buy every buy_every_s; a coin waits at most queue_max_min."""
        for r in list(self.ready):
            if (_now() - datetime.fromisoformat(r["since"])).total_seconds() / 60 > self.p.queue_max_min:
                self.ready.remove(r)
                with self.lock:
                    full = len(self.d.positions) >= self.p.max_positions
                self.event("Executor", r["coin"], "skip", "cheio: esperou 10 min na fila" if full else
                           "esperou 10 min na fila")
        if not self.ready or time.time() - self.last_buy < self.p.buy_every_s:
            return
        with self.lock:
            if len(self.d.positions) >= self.p.max_positions:
                return
        best = max(self.ready, key=lambda r: r["score"])
        self.ready.remove(best)
        self.last_buy = time.time()
        self.executor(best["addr"], best["pool"], best["coin"], best["mint"], best["score"])

    def executor(self, addr: str, pool: dict, coin: str, mint: str, score: float):
        a = pool["attributes"]
        scan_px, scan_liq = _f(a.get("base_token_price_usd")), _f(a.get("reserve_in_usd"))
        live = self._pairs([addr]).get(addr) or {}
        px, liq = _f(live.get("priceUsd")), _f((live.get("liquidity") or {}).get("usd"))
        self.d.seen[addr] = "executor"
        if px <= 0 or liq <= 0:
            self.event("Executor", coin, "skip", "sem preço agora")
            return
        if scan_px > 0 and px > scan_px * (1 + self.p.recheck_price_up):
            self.event("Executor", coin, "skip", f"fugiu: +{(px / scan_px - 1) * 100:.0f}% desde o Scanner")
            return
        if scan_liq > 0 and liq < scan_liq * (1 - self.p.recheck_liq_drop):
            self.event("Executor", coin, "skip", f"liquidez a cair: -{(1 - liq / scan_liq) * 100:.0f}%")
            return
        with self.lock:
            size = min(self.equity() * self.p.stack_pct, liq * self.p.max_pool_pct, self.d.cash)
            if size < 5:
                self.event("Executor", coin, "skip", "sem saldo livre")
                return
            impact = 2 * size / liq
            entry = px * (1 + self.p.cost_per_side + impact)
            qty = size / entry
            self.d.cash -= size
            pos = Position(uuid.uuid4().hex[:6], coin, a["name"], addr, mint, _now().isoformat(timespec="seconds"),
                           px, entry, qty, size, liq, px, px, score=score)
            self.d.positions.append(pos)
            self.d.seen[addr] = "bought"
        cap = "2% da liquidez" if math.isclose(size, liq * self.p.max_pool_pct) else f"{self.p.stack_pct:.0%} do saldo"
        self.event("Executor", coin, "buy", f"comprou ${_pt(size)} ({cap})", price=px, entry=entry,
                   impact_pct=round(impact * 100, 2), liquidity=round(liq))

    # ------------------------------------------------------------ Risk
    def risk_loop(self):
        """Risk on its own thread: prices every 15 s, whatever the Scanner is waiting for."""
        while not self.stop.is_set():
            tick = time.time()
            try:
                self.risk()
                self.save()
            except Exception:
                self.d.api_errors += 1
                log.exception("Risk tick failed, carrying on")
            self.stop.wait(max(1.0, 15 - (time.time() - tick)))

    def risk(self, final: bool = False):
        with self.lock:
            pools = [p.pool for p in self.d.positions]
        if not pools:
            return
        live = self._pairs(pools, self.http_risk)   # network call outside the lock
        with self.lock:
            self._apply_risk(live, final)

    def _apply_risk(self, live: dict, final: bool):
        for pos in list(self.d.positions):
            pr = live.get(pos.pool)
            if pr is None:
                if final:
                    self.close(pos, pos.last, "fim do teste (último preço)")
                continue
            px, liq = _f(pr.get("priceUsd")), _f((pr.get("liquidity") or {}).get("usd"))
            if px <= 0:
                continue
            pos.last, pos.liquidity = px, liq
            pos.peak = max(pos.peak, px)
            if liq < 1_000:
                self.close(pos, px, "liquidez retirada")
            elif px <= pos.raw_entry * (1 - self.p.stop_pct):
                self.close(pos, px, f"stop -{self.p.stop_pct:.0%}")
            else:
                if px >= pos.raw_entry * (1 + self.p.trail_start):
                    pos.trail_on = True
                hours = (_now() - datetime.fromisoformat(pos.opened)).total_seconds() / 3600
                if pos.trail_on and px <= pos.peak * (1 - self.p.trail_pct):
                    self.close(pos, px, "trailing stop")
                elif hours >= self.p.timeout_h and px < pos.raw_entry * (1 + self.p.timeout_min_gain):
                    self.close(pos, px, "dinheiro parado")
                elif final:
                    self.close(pos, px, "fim do teste")

    def close(self, pos: Position, px: float, reason: str):
        with self.lock:
            if pos in self.d.positions:
                self._close(pos, px, reason)

    def _close(self, pos: Position, px: float, reason: str):
        impact = 2 * pos.qty * px / max(pos.liquidity, 1.0)
        got = pos.qty * px * (1 - self.p.cost_per_side - min(impact, 0.5))
        self.d.cash += got
        self.d.positions.remove(pos)
        pnl = got - pos.cost_usd
        t = {"id": pos.id, "coin": pos.coin, "opened": pos.opened, "closed": _now().isoformat(timespec="seconds"),
             "cost_usd": pos.cost_usd, "got_usd": got, "pnl_usd": pnl, "pnl_eur": pnl / self.d.eur_usd,
             "pct": got / pos.cost_usd - 1, "raw_move": px / pos.raw_entry - 1, "peak_move": pos.peak / pos.raw_entry - 1,
             "reason": reason, "score": pos.score}
        self.d.trades.append(t)
        self.event("Risk", pos.coin, "sell", f"{reason}: {'+' if pnl >= 0 else '-'}${_pt(abs(pnl))} ({'+' if t['pct'] >= 0 else ''}{_pt(t['pct'] * 100, 1)}%)",
                   pnl_usd=round(pnl, 2), pct=round(t["pct"], 4))

    # ------------------------------------------------------------ loop
    def run(self):
        end = datetime.fromisoformat(self.d.ends)
        log.info("Memecoin paper test: %.2f EUR = $%.2f, until %s", self.d.start_usd / self.d.eur_usd,
                 self.d.start_usd, self.d.ends)
        self.save()
        import os
        (OUT / "pid").write_text(str(os.getpid()), encoding="utf-8")
        STOP_FILE.unlink(missing_ok=True)
        risk_thread = threading.Thread(target=self.risk_loop, name="risk", daemon=True)
        risk_thread.start()
        next_scan = 0.0
        try:
            while _now() < end:
                if STOP_FILE.exists():  # "Parar sessão" in the app
                    log.info("Stopped from the app")
                    STOP_FILE.unlink(missing_ok=True)
                    break
                tick = time.time()
                try:
                    if tick >= next_scan:
                        self.scan()
                        next_scan = tick + 60
                    for _ in range(2):  # a few audits per tick
                        if self.queue:
                            self.auditor(*self.queue.pop(0))
                    self.execute_best()
                except Exception:  # one bad tick must not end the test
                    self.d.api_errors += 1
                    log.exception("Tick failed, carrying on")
                time.sleep(max(1.0, min(15 - (time.time() - tick), (end - _now()).total_seconds())))
        except KeyboardInterrupt:
            log.info("Stopped by hand")
        self.stop.set()
        risk_thread.join(timeout=60)
        try:
            self.risk(final=True)
        except Exception:
            log.exception("Final price check failed; closing at the last known prices")
        for pos in list(self.d.positions):
            self.close(pos, pos.last, "fim do teste (último preço)")
        self.d.finished = True
        self.d.ends = min(self.d.ends, _now().isoformat(timespec="seconds"))
        self.save()
        (OUT / "pid").unlink(missing_ok=True)
        report = self.report()
        (OUT / "relatorio.md").write_text(report, encoding="utf-8")
        return report

    def report(self) -> str:
        d, eur = self.d, self.d.eur_usd

        def n(v: float, sign: bool = False) -> str:  # Portuguese format: 1 000,00
            txt = f"{v:+,.2f}" if sign else f"{v:,.2f}"
            return txt.replace(",", " ").replace(".", ",")

        end = self.equity()
        trades = d.trades
        wins = [t for t in trades if t["pnl_usd"] > 0]
        costs = sum(t["cost_usd"] * self.p.cost_per_side + t["got_usd"] * self.p.cost_per_side for t in trades)
        lines = [
            "# Teste de memecoins em papel",
            "",
            f"De {d.started[11:16]} a {_now().isoformat()[11:16]} UTC ({d.started[:10]}). Dinheiro fictício: "
            f"nenhuma carteira, nenhuma ordem real.",
            "",
            "| | € | $ |",
            "|---|---|---|",
            f"| Saldo inicial | {n(d.start_usd / eur)} | {n(d.start_usd)} |",
            f"| Saldo final | {n(end / eur)} | {n(end)} |",
            f"| Resultado | {n((end - d.start_usd) / eur, True)} ({n((end / d.start_usd - 1) * 100, True)}%) | {n(end - d.start_usd, True)} |",
            f"| Custos simulados (1% por lado) | {n(costs / eur)} | {n(costs)} |",
            "",
            f"Câmbio fixo do início: 1 € = {eur:.4f} $.".replace(".", ",", 1) + f" Pools vistos: {d.pools_seen}. Falhas de API: {d.api_errors}.",
            "",
            f"## Trades ({len(trades)}, {len(wins)} a ganhar)",
            "",
        ]
        if trades:
            lines += ["| Moeda | Entrou | Saiu | Resultado € | % | Máximo | Motivo |", "|---|---|---|---|---|---|---|"]
            for t in trades:
                lines.append(f"| {t['coin']} | {t['opened'][11:16]} | {t['closed'][11:16]} | {n(t['pnl_eur'], True)} | "
                             f"{n(t['pct'] * 100, True)}% | {n(t['peak_move'] * 100, True)}% | {t['reason']} |")
        else:
            lines.append("Nenhuma moeda passou todas as regras durante o teste.")
        lines += ["", "## Onde as moedas foram cortadas", ""]
        for agent, key in (("Scanner", "scanner"), ("Auditor", "auditor"), ("Analyst", "analyst"), ("Executor", "executor")):
            c = d.counts.get(key, {})
            lines.append(f"- **{agent}:** " + (", ".join(f"{k} ({v})" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))
                                                  if c else "nada cortado"))
        lines += ["", "Um teste de 30 minutos não diz se a estratégia ganha dinheiro: o resultado é sobretudo sorte. "
                      "Serve para ver se cada agente faz o que deve com dados reais."]
        return "\n".join(lines) + "\n"


def run(minutes: float, eur: float) -> str:
    return MemeTest(minutes, eur).run()
