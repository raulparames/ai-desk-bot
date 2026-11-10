"""Memes mode: a Solana memecoin desk in paper, laid out like the "trading desk" videos.

The app shows five agents: SCANNER (finds coins: the TRENDS and SMARTS steps below), ANALYST (scores
them 0-100: DEBATE and ROUTER), COMPOUNDER (sizes every buy), EXECUTOR (buys) and RISK (sells).
Compounder rules: a buy is 15% of the desk's equity, never more than 40% of the free cash or 2% of the
pool; at most 35% of the equity is deployed at once, never more than 2 open, no adding to a coin already
held (whatever pool it comes through). At 2x half is sold (the entry comes back); the rest rides a
trailing stop 10% off its high. Until 2x prints there is a hard stop at -8%.

Safety, as on the futures desk: the brakes of guard.py stop new entries after a bad session, day or
streak, and a coin that closed at a loss is not bought again in the session. The risk thread tells the
owner when a position has no price (its stop is not watched), when the computer was away (entries pause,
what was queued is dropped) and when a check keeps failing. Sales are priced by a real Jupiter sell quote;
with no quote, by a constant-product model of the pool; with no quote and an empty pool, or no route and no
fresh reading of the pool, at zero.

Wide scan: every minute SCANNER also reads Jupiter's token lists (top trending, top traded, top organic
score, newest: about 300 Solana tokens) and applies the TRENDS cuts to all of them with Jupiter's own
numbers (liquidity, 1 h move, age, mint and freeze, size). The best two that pass, by Jupiter's organic
score, become GeckoTerminal pools for the other agents. The app counts every token actually read.

Two entry styles, picked when the session starts (paper either way):
  poucas  "few and good", the rules below: 3 whales of $500+, 4 of 5 bull votes, top 10 holders < 25%.
  mais    "more entries": 2 whales of $300+, 3 of 5 bull votes, top 10 holders < 40%. Mint and freeze
          revoked, no honeypot and someone selling stay required in both: they are the rug-pull guard.

Steps inside the agents (originally laid out like the "gmgn mode" video):

    python -m deskbot memedesk --minutes 720 --eur 110

The video wires 5 GitHub repos to 5 agents and a core that only trades when they agree. Same shape
here, with free public data and paper money (no wallet, no keys, no orders anywhere):

  TRENDS  (like Solana-MCP-Trading-Server) new PumpSwap pools (coins that left the pump.fun curve) and
          Solana trending pools on GeckoTerminal. Cuts thin liquidity (< $40k), coins up > 80% in 1 h,
          pools younger than 10 min.
  SMARTS  (like gmgn-skills) whale wallets: GeckoTerminal trades of $500 or more in the last 30 min.
          Needs 3+ different whale buyers and more whale buying than selling.
  DEBATE  (like TradingAgents) 5 bull/bear votes (still trading now, real distinct buyers, not
          chasing, buying a dip, deep liquidity) plus the risk team (mint and freeze revoked, top 10
          holders < 25%, no honeypot, sells happen). Needs risk ok and 4+ bulls.
  ROUTER  (like solana-agent-kit) a real Jupiter quote for the trade size: route and price impact.
          Refuses more than 2% impact.
  EXEC    (like opentrade) best agreed coin first, one buy every 2 min, sized by the Compounder; paper
          fill at the Jupiter quote less costs. Prices every 5 s: stop -8% (the video's rule), half at
          2x, then a trail 10% off the high; exits priced with a Jupiter sell quote.
  CORE    buys only when all five agree ("5/5").

GMGN (smart money and KOL wallets, rug scores, its own trending list) joins in when GMGN_API_KEY is
in .env (your own key from https://gmgn.ai/ai; read-only, see gmgn.py). Then TRENDS also takes GMGN's
5-minute trending coins, SMARTS counts GMGN's smart money and KOL wallets, and the risk team adds
GMGN's rug score, wash trading, bundler and insider shares. Without a key it runs as above.
The venue is FOMO: it is only where the app opens each coin, every order stays in paper.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests

from . import guard
from . import memetest as M
from .gmgn import Gmgn, from_rank
from .memetest import MemeParams, MemeTest, Position, _f, _now, _pt

log = logging.getLogger("memetest")
SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
JUP_TOKENS = "https://lite-api.jup.ag/tokens/v2"
WIDE_LISTS = ("toptrending/5m", "toptraded/5m", "toporganicscore/5m", "toptrending/1h", "recent")
WIDE_FORWARD = 2          # best wide-scan tokens sent on to the other agents per scan
MAX_MCAP = 50e6           # bigger than this is not a memecoin play any more
AGENTS = ("SCANNER", "ANALYST", "COMPOUNDER", "EXECUTOR", "RISK")
STAGES = ("TRENDS", "SMARTS", "DEBATE", "ROUTER", "EXEC")
AGENT_OF = {"TRENDS": "SCANNER", "SMARTS": "SCANNER", "DEBATE": "ANALYST", "ROUTER": "ANALYST", "CORE": "ANALYST",
            "EXEC": "EXECUTOR"}
REPOS = {
    "SCANNER": ("8bitsats/Solana-MCP-Trading-Server", "tokens em alta · baleias · smart money (GMGN)"),
    "ANALYST": ("TauricResearch/TradingAgents", "debate bull/bear · risco · rota · nota 0–100"),
    "COMPOUNDER": ("GMGNAI/gmgn-skills", "15% da mesa por compra · máx. 35% investido · máx. 2 abertas"),
    "EXECUTOR": ("sendaifun/solana-agent-kit", "cotação Jupiter · compra em papel"),
    "RISK": ("0xsyncroot/opentrade", "metade a 2× · trailing 10% · stop −8%"),
}
HALF_AT = 2.0
HALF_REASON = "metade a 2× · entrada de volta"
MIN_TRADE_USD = 5.0       # a smaller buy is not worth its costs
GAP_S = 120               # the risk loop was away longer than this: nothing watched the stops
PAUSE_AFTER_GAP_S = 300   # no new entries for this long after such a gap
MISS_ALERT = 3            # price readings missed in a row before the owner is told
LIQ_GONE_USD = 1_000      # pool below this: out at once; a sale with no quote there is worth nothing
LIQ_DROP = 0.5            # liquidity under this share of what it was at the buy ...
LIQ_CONFIRM_S = 35        # ... on two readings at least this far apart: out
SOL_MAX_AGE_S = 60        # the SOL price the sell quotes are turned into dollars with
SOL_STALE_S = 5 * SOL_MAX_AGE_S   # older than this it is not a price any more: no buy, no quote in dollars
RISK_TIMEOUT_S = 5        # network calls of the risk thread: a slow answer must not hold the other stops
VENUES = {"fomo": "FOMO"}
STYLES = {   # whale size, whales needed, bull votes needed, top-10 holders limit (%)
    "poucas": {"whale_usd": 500, "min_whales": 3, "min_bulls": 4, "max_top10": 25},
    "mais": {"whale_usd": 300, "min_whales": 2, "min_bulls": 3, "max_top10": 40},
}
BUCKET = {"TRENDS": "trends", "SMARTS": "smarts", "DEBATE": "debate", "ROUTER": "router", "EXEC": "exec",
          "CORE": "core"}


def memes_params() -> MemeParams:
    # Compounder: 15% of the equity per buy (ceilings: 40% of the free cash, 2% of the pool), 35% of the
    # equity deployed at most, max 2 open, trail 10% off the high after the half at 2x
    return MemeParams(stop_pct=0.08, cost_per_side=0.005, stack_pct=0.40, max_positions=2, trail_pct=0.10,
                      timeout_h=1e9, max_trade_pct=0.15, max_deployed_pct=0.35)


def _mint_of(pool: dict) -> str:
    """The coin behind a GeckoTerminal pool ('' when the pool does not say)."""
    ident = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
    return str(ident).split("_", 1)[-1]


class MemesDesk(MemeTest):
    WHALE_USD = 500
    WHALE_WINDOW_MIN = 30
    MIN_WHALES = 3
    MAX_IMPACT = 0.02
    RISK_EVERY_S = 5

    GMGN_SMART_MIN = 3        # smart money + KOL wallets holding, when GMGN is on
    GMGN_RUG_MAX = 0.30       # GMGN's own line between normal and high rug risk
    GMGN_BOTS_MAX = 0.30      # bundler or insider share of the volume

    MIN_BULLS = 4

    def __init__(self, minutes: float, eur: float, venue: str = "fomo", style: str = "poucas"):
        super().__init__(minutes, eur, memes_params())
        self.d.mode = "memes"
        self.d.venue = venue if venue in VENUES else "fomo"
        self.d.style = style if style in STYLES else "poucas"
        st = STYLES[self.d.style]
        self.WHALE_USD, self.MIN_WHALES, self.MIN_BULLS = st["whale_usd"], st["min_whales"], st["min_bulls"]
        self.p.max_top10_pct = st["max_top10"]
        self.gmgn = Gmgn()
        self._gm: dict[str, dict] = {}          # mint -> GMGN numbers from its trending row
        self._gm_done: set[str] = set()         # GMGN coins already looked up on GeckoTerminal
        self._mints_seen: set[str] = set()      # every token the scanner really read (the app's counter)
        self._wide_verdict: dict[str, str] = {}  # mint -> last wide-scan cut (counted once per change)
        self._wide_done: set[str] = set()       # wide-scan tokens already sent on
        self.d.feeds = {"gmgn": self.gmgn.status()}
        self.d.counts = {k: {} for k in ("trends", "smarts", "debate", "router", "exec", "core")}
        self.d.agents = {a: {"coin": None, "text": "à espera", "ok": None, "ts": None} for a in AGENTS}
        self._sol = (0.0, 0.0)   # (price, fetched at)
        self._rank: dict[str, int] = {}
        self.guard = guard.Guard(M.OUT, self.d.start_usd)   # brakes on new entries; day.json in the desk's folder
        self.d.guard = self.guard.state()
        self.quit = threading.Event()           # "Parar sessão": set by the risk thread within one tick
        self.stale = threading.Event()          # a gap was found: the main loop drops what it had queued
        self.no_new_until = 0.0                 # entries paused after a gap without watching
        self._loop_at = 0.0                     # last turn of the risk loop
        self._blocked: dict[str, str] = {}      # mint -> coin that closed at a loss this session
        self._closing: set[str] = set()         # positions being sold right now (never sold twice)
        self._fails: dict[str, int] = {}        # position -> risk checks failed in a row
        self._liq_low: dict[str, float] = {}    # position -> first reading with the liquidity halved
        self._miss_since: dict[str, float] = {}  # position -> first price reading missed

    # ------------------------------------------------------------ helpers
    def event(self, stage: str, coin: str, verdict: str, reason: str = "", **values):
        """stage is the step inside an agent (TRENDS, SMARTS, ...); the app shows the agent."""
        agent = "RISK" if verdict in ("sell", "alert") else AGENT_OF.get(stage, stage)
        e = {"ts": _now().isoformat(timespec="seconds"), "agent": agent, "stage": stage, "coin": coin,
             "verdict": verdict, "reason": reason, **values}
        with self.lock:
            self.d.events.append(e)
            try:
                with self.events_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            except (OSError, TypeError, ValueError) as exc:   # the session and its exits matter more than the log file
                log.warning("events.jsonl not written: %s", exc)
            if verdict in ("cut", "reject", "skip", "kill"):
                c = self.d.counts.setdefault(BUCKET.get(stage, "core"), {})
                key = reason.split(":")[0]
                c[key] = c.get(key, 0) + 1
            if agent in self.d.agents and verdict not in ("wait", "alert"):
                self.d.agents[agent] = {"coin": coin, "text": reason, "ok": verdict in ("pass", "buy", "sell"),
                                        "ts": e["ts"], "verdict": verdict,
                                        **{k: values[k] for k in ("score", "img", "eur", "fill_s", "kind") if k in values}}
        log.info("%-7s %-12s %-6s %s", stage, coin, verdict, reason)

    def alert(self, text: str, coin: str = "—"):
        """Something the owner must know (a brake, a gap, a dead feed). The app and the phone show it."""
        self.event("RISK", coin, "alert", text)

    def save(self, final: bool = False):
        with self.lock:
            self.d.guard = self.guard.state()
        super().save(final)

    def _nap(self, seconds: float):
        self.quit.wait(seconds)               # a wait between API calls ends at once when the session is stopped

    def _gecko(self, path: str, params: dict | None = None):
        if self.quit.is_set():                # asked to stop: no more slow calls, the session is ending
            return None
        return super()._gecko(path, params)

    def sol_usd(self) -> float:
        price, at = self._sol
        if time.time() - at < SOL_MAX_AGE_S and price > 0:
            return price
        try:
            r = self.http.get(JUP_PRICE, params={"ids": SOL_MINT}, timeout=10)
            price = float(r.json()[SOL_MINT]["usdPrice"])
            self._sol = (price, time.time())
        except (requests.RequestException, KeyError, ValueError, TypeError):
            price = self._sol_fresh()             # the last one, while it is not too old to trade on
            log.warning("SOL price failed, keeping %s", price)
        return price

    def _sol_fresh(self) -> float:
        """The last SOL price, or 0 when it is too old to turn a quote into dollars with."""
        price, at = self._sol
        return price if price > 0 and time.time() - at < SOL_STALE_S else 0.0

    def _refresh_sol(self):
        """RISK keeps the SOL price young for its sell quotes: own session, short timeout, never under the lock."""
        if self._sol[0] > 0 and time.time() - self._sol[1] < SOL_MAX_AGE_S:
            return
        try:
            r = self.http_risk.get(JUP_PRICE, params={"ids": SOL_MINT}, timeout=RISK_TIMEOUT_S)
            price = float(r.json()[SOL_MINT]["usdPrice"])
            if price > 0:
                self._sol = (price, time.time())
        except (requests.RequestException, KeyError, ValueError, TypeError):
            log.warning("SOL price failed, keeping %s", self._sol[0])

    def _quote(self, in_mint: str, out_mint: str, amount: int, session=None, timeout: float = 8) -> tuple[str, dict | None]:
        """A real Jupiter route for this size (no transaction is built or sent), and what the answer means:
        'ok', 'no_route' (Jupiter answered: nothing to trade this through) or 'unavailable' (no answer worth
        the name: timeout, rate limit, server error)."""
        try:
            r = (session or self.http).get(JUP_QUOTE, params={"inputMint": in_mint, "outputMint": out_mint,
                                                              "amount": int(amount), "slippageBps": 100}, timeout=timeout)
            j = r.json()
        except (requests.RequestException, ValueError):
            return "unavailable", None
        if r.ok and isinstance(j, dict) and _f(j.get("outAmount")) > 0:
            return "ok", j
        if r.ok or r.status_code == 400:      # 200 without an amount, or Jupiter's "no route" / "not tradable"
            return "no_route", None
        return "unavailable", None

    def quote(self, in_mint: str, out_mint: str, amount: int, session=None) -> dict | None:
        """The route for a buy: the answer, or None when there is none for whatever reason."""
        return self._quote(in_mint, out_mint, amount, session)[1]

    def _sizing(self, liq: float) -> tuple[float, str]:
        """Compounder: the size of the next buy and the rule that set it. 15% of the desk's equity, never more
        than 40% of the free cash (so the second buy still fits) or 2% of the pool."""
        with self.lock:
            caps = ((self.equity() * self.p.max_trade_pct, f"{self.p.max_trade_pct:.0%} da mesa"),
                    (self.d.cash * self.p.stack_pct, f"teto de {self.p.stack_pct:.0%} do dinheiro livre"),
                    (liq * self.p.max_pool_pct, f"teto de {self.p.max_pool_pct:.0%} da liquidez da pool"))
        size, rule = min(caps, key=lambda c: c[0])
        return max(0.0, size), rule

    def trade_size(self, liq: float) -> float:
        return self._sizing(liq)[0]

    def _too_small(self, size: float) -> str:
        eur = self.d.eur_usd
        return f"aposta abaixo do mínimo: {_pt(size / eur)} € (mínimo {_pt(MIN_TRADE_USD / eur)} €)"

    def _no_room(self, size: float) -> str:
        """Why a buy of this size does not fit the desk right now ('' when it does)."""
        with self.lock:
            if size < MIN_TRADE_USD:
                return self._too_small(size)
            if size > self.d.cash:
                return "sem saldo livre"
            deployed = sum(p.cost_usd for p in self.d.positions)
            if deployed + size > self.equity() * self.p.max_deployed_pct + 1e-9:
                return f"já há muito investido: com esta compra passava de {self.p.max_deployed_pct:.0%} da mesa"
        return ""

    def _refuse(self, mint: str) -> str:
        """Why this coin is not to be bought, whatever pool it comes through ('' when it may)."""
        with self.lock:
            if mint in self._blocked:
                return "já perdeu nesta sessão: não volta a entrar"
            if any(p.mint == mint for p in self.d.positions):
                return "já na carteira: outra pool da mesma moeda"
        return ""

    def _busy_mints(self) -> set[str]:
        """Coins held, queued or agreed: a second pool of one of them is not looked at."""
        with self.lock:
            held = {p.mint for p in self.d.positions}
        return held | {_mint_of(q[1]) for q in self.queue} | {r["mint"] for r in self.ready}

    # ------------------------------------------------------------ TRENDS
    def scan(self):
        d = self._gecko("/networks/solana/trending_pools", {"page": 1, "include": "base_token,dex"})
        self._rank = {p["attributes"]["address"]: i + 1 for i, p in enumerate((d or {}).get("data", []))}
        found = {}
        for pool in (d or {}).get("data", []):
            found[pool["attributes"]["address"]] = {**pool, "source": "em alta"}
        n = self._gecko("/networks/solana/new_pools", {"page": 1, "include": "base_token,dex"})
        for pool in (n or {}).get("data", []):
            if pool["relationships"]["dex"]["data"]["id"] == "pumpswap":
                found.setdefault(pool["attributes"]["address"], {**pool, "source": "saiu do pump.fun"})
        found.update(self._gmgn_pools(found))
        known = {pool["relationships"]["base_token"]["data"]["id"].split("_", 1)[1] for pool in found.values()}
        self._mints_seen.update(known)
        found.update(self._wide(known))
        self.d.feeds["gmgn"] = self.gmgn.status()
        self.d.pools_seen = len(self._mints_seen)
        busy = self._busy_mints()
        for addr, pool in found.items():
            if addr in self.d.seen or any(p.pool == addr for p in self.d.positions):
                continue
            mint = _mint_of(pool)
            if mint in busy:                  # another pool of a coin already held or on its way
                continue
            if mint in self._blocked:
                self.d.seen[addr] = "blocked"
                self.event("EXEC", "$" + pool["attributes"]["name"].split(" / ")[0][:12], "skip", self._refuse(mint))
                continue
            queued = len(self.queue)
            self.trends(addr, pool)
            if len(self.queue) > queued:
                busy.add(mint)

    def _gmgn_pools(self, found: dict) -> dict:
        """GMGN's 5-minute trending coins as GeckoTerminal pools. At most 2 lookups a scan: the free
        GeckoTerminal API allows only a few calls a minute."""
        if not self.gmgn.enabled:
            return {}
        known = {pool["relationships"]["base_token"]["data"]["id"].split("_", 1)[1] for pool in found.values()}
        out, lookups = {}, 0
        for i, item in enumerate(self.gmgn.trending("5m", 50)[:20]):
            mint = item.get("address") or ""
            if not mint:
                continue
            self._gm[mint] = from_rank(item)
            if (mint in known or mint in self._gm_done or str(item.get("symbol", "")).upper() in M.MAJORS
                    or lookups >= 2):
                continue
            lookups += 1
            self._gm_done.add(mint)
            d = self._gecko(f"/networks/solana/tokens/{mint}/pools", {"page": 1, "include": "base_token,dex"})
            pools = (d or {}).get("data") or []
            if pools:
                top = max(pools, key=lambda p: _f(p["attributes"].get("reserve_in_usd")))
                out[top["attributes"]["address"]] = {**top, "source": f"GMGN trending #{i + 1}"}
        return out

    def _wide(self, known: set[str]) -> dict:
        """About 300 tokens a minute from Jupiter's lists; TRENDS cuts on all of them; the best go on."""
        toks: dict[str, dict] = {}
        for path in WIDE_LISTS:
            if self.quit.is_set():            # asked to stop: no more slow calls, the session is ending
                return {}
            try:
                r = self.http.get(f"{JUP_TOKENS}/{path}", params={"limit": 100}, timeout=15)
                for t in (r.json() if r.ok else []):
                    if isinstance(t, dict) and t.get("id"):
                        toks.setdefault(t["id"], {**t, "_list": path})
            except (requests.RequestException, ValueError):
                self.d.api_errors += 1
        self._mints_seen.update(toks)
        passed, newly_cut = [], []
        for mint, t in toks.items():
            if mint in known or mint in self._wide_done:
                continue
            reason = self._wide_cut(t)
            if not reason:
                passed.append(t)
                self._wide_verdict.pop(mint, None)
                continue
            key = reason.split(":")[0]                    # "liquidez", not "liquidez: $12k" (the value moves)
            if self._wide_verdict.get(mint) != key:       # each token counted once per reason
                self._wide_verdict[mint] = key
                newly_cut.append((t, reason))
        # a couple of real cuts go on the app's Scanner card as events (event() counts those);
        # the rest are only counted, not one event per token
        shown = sorted((x for x in newly_cut if x[1] != "grande"), key=lambda x: -_f(x[0].get("organicScore")))[:2]
        c = self.d.counts.setdefault("trends", {})
        for t, reason in newly_cut:
            if (t, reason) in shown:
                self.event("TRENDS", "$" + str(t.get("symbol") or "?")[:12], "kill", reason, img=str(t.get("icon") or ""))
            else:
                c[reason.split(":")[0]] = c.get(reason.split(":")[0], 0) + 1
        out = {}
        for t in sorted(passed, key=lambda t: -_f(t.get("organicScore")))[:WIDE_FORWARD]:
            mint = t["id"]
            self._wide_done.add(mint)
            d = self._gecko(f"/networks/solana/tokens/{mint}/pools", {"page": 1, "include": "base_token,dex"})
            pools = (d or {}).get("data") or []
            if pools:
                top = max(pools, key=lambda p: _f(p["attributes"].get("reserve_in_usd")))
                label = {"recent": "nova na Jupiter"}.get(t["_list"], "Jupiter " + t["_list"].replace("top", "top ").replace("/", " "))
                out[top["attributes"]["address"]] = {**top, "source": label}
        return out

    def _wide_cut(self, t: dict) -> str:
        """The TRENDS cuts, with Jupiter's own numbers (no extra calls)."""
        sym = str(t.get("symbol") or "").upper()
        if sym in M.MAJORS or _f(t.get("mcap")) > MAX_MCAP:
            return "grande"
        liq = _f(t.get("liquidity"))
        if liq < self.p.min_liquidity:
            return f"liquidez: ${liq / 1000:.0f}k < ${self.p.min_liquidity / 1000:.0f}k"
        h1 = _f((t.get("stats1h") or {}).get("priceChange"))
        if h1 > self.p.max_h1_pct:
            return f"já correu: +{h1:.0f}% em 1h"
        audit = t.get("audit")
        if isinstance(audit, dict):           # an audit that does not say "disabled" counts as active
            if audit.get("mintAuthorityDisabled") is not True:
                return "mint ativa"
            if audit.get("freezeAuthorityDisabled") is not True:
                return "freeze ativo"
        if t.get("mintAuthority"):
            return "mint ativa"
        if t.get("freezeAuthority"):
            return "freeze ativo"
        # no audit at all: the coin goes on and DEBATE reads both authorities on the chain itself
        created = (t.get("firstPool") or {}).get("createdAt") or t.get("createdAt")
        if created:
            age = (_now() - datetime.fromisoformat(str(created).replace("Z", "+00:00"))).total_seconds() / 60
            if age < self.p.min_age_min:
                return f"muito nova: {age:.0f} min"
        return ""

    def gm(self, mint: str) -> dict | None:
        """GMGN numbers for a coin: from its trending row if we saw one, else a fresh lookup."""
        if not self.gmgn.enabled or not mint:
            return None
        return self._gm.get(mint) or self.gmgn.token(mint)

    def trends(self, addr: str, pool: dict):
        a = pool["attributes"]
        coin = "$" + a["name"].split(" / ")[0][:12]
        if coin[1:].upper() in M.MAJORS:
            self.d.seen[addr] = "major"
            return
        liq = _f(a.get("reserve_in_usd"))
        h1 = _f((a.get("price_change_percentage") or {}).get("h1"))
        age = (_now() - datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00"))).total_seconds() / 60
        rank = self._rank.get(addr)
        where = f"trending #{rank}" if rank else pool["source"]
        if liq < self.p.min_liquidity:
            self.d.seen[addr] = "trends"
            return self.event("TRENDS", coin, "kill", f"liquidez: ${liq / 1000:.0f}k < ${self.p.min_liquidity / 1000:.0f}k")
        if h1 > self.p.max_h1_pct:
            self.d.seen[addr] = "trends"
            return self.event("TRENDS", coin, "kill", f"já correu: +{h1:.0f}% em 1h")
        if age < self.p.min_age_min:
            if addr not in self.d.waiting:
                self.d.waiting[addr] = _now().isoformat()
                self.event("TRENDS", coin, "wait", f"pool com {age:.0f} min: espera pelos {self.p.min_age_min:g}")
                return
            if (_now() - datetime.fromisoformat(self.d.waiting[addr])).total_seconds() / 60 <= self.p.max_wait_min:
                return
        self.d.waiting.pop(addr, None)
        self.d.seen[addr] = "pass"
        self.event("TRENDS", coin, "pass", f"{where} · {'+' if h1 >= 0 else ''}{_pt(h1, 0)}% 1h")
        self.queue.append((addr, pool, coin))

    # the base loop calls self.auditor(...) for queued coins: here that runs the other four agents
    def auditor(self, addr: str, pool: dict, coin: str):
        mint = pool["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
        votes = {"TRENDS": True}
        smarts = self.smarts(addr, coin, mint)
        votes["SMARTS"] = smarts["ok"]
        if not smarts["ok"]:
            self.d.seen[addr] = "smarts"
            return
        debate = self.debate(addr, pool, coin, mint)
        votes["DEBATE"] = debate["ok"]
        if not debate["ok"]:
            self.d.seen[addr] = "debate"
            return
        live = self._pairs([addr]).get(addr) or {}
        liq = _f((live.get("liquidity") or {}).get("usd")) or _f(pool["attributes"].get("reserve_in_usd"))
        size = self.trade_size(liq)
        if size < MIN_TRADE_USD:
            self.d.seen[addr] = "exec"
            return self.event("EXEC", coin, "skip", self._too_small(size))
        route = self.router(coin, mint, size)
        votes["ROUTER"] = route["ok"]
        if not route["ok"]:
            self.d.seen[addr] = "router"
            return
        with self.lock:
            held, queued = len(self.d.positions), len(self.ready)
            room = held + queued < self.p.max_positions + 3
        votes["EXEC"] = room
        if not room:
            self.d.seen[addr] = "exec"
            return self.event("EXEC", coin, "skip", f"cheio: {held} abertas e {queued} na fila")
        again = self._refuse(mint) or ("já na fila: outra pool da mesma moeda"
                                       if any(r["mint"] == mint for r in self.ready) else "")
        if again:
            self.d.seen[addr] = "exec"
            return self.event("EXEC", coin, "skip", again)
        score = round((smarts["score"] + debate["score"] + route["score"]) / 3)
        self.event("CORE", coin, "pass", f"passou · nota {score} · 5/5 de acordo", votes=votes, score=score)
        self.d.seen[addr] = "ready"
        self.ready.append({"score": score, "since": _now().isoformat(timespec="seconds"), "addr": addr,
                           "pool": pool, "coin": coin, "mint": mint, "decimals": debate["decimals"],
                           "route": route})
        self.event("EXEC", coin, "wait", f"na fila com nota {score} ({len(self.ready)} na fila)")

    # ------------------------------------------------------------ SMARTS
    def smarts(self, addr: str, coin: str, mint: str = "") -> dict:
        d = self._gecko(f"/networks/solana/pools/{addr}/trades", {"trade_volume_in_usd_greater_than": self.WHALE_USD})
        since = _now() - timedelta(minutes=self.WHALE_WINDOW_MIN)
        buyers, flow, n = set(), 0.0, 0
        for t in (d or {}).get("data", []):
            a = t["attributes"]
            if datetime.fromisoformat(a["block_timestamp"].replace("Z", "+00:00")) < since:
                continue
            usd = _f(a.get("volume_in_usd"))
            if a.get("kind") == "buy":
                buyers.add(a.get("tx_from_address"))
                flow += usd
            else:
                flow -= usd
            n += 1
        flow_txt = f"{'+' if flow >= 0 else '-'}${_pt(abs(flow) / 1000, 1)}k"
        g = self.gm(mint)
        sm = (g["smart"] + g["kol"]) if g else 0
        whales_ok = d is not None and len(buyers) >= self.MIN_WHALES and flow > 0
        smart_ok = g is not None and sm >= self.GMGN_SMART_MIN and flow >= 0
        ok = whales_ok or smart_ok
        tag = f"{g['smart']} SM · {g['kol']} KOL · " if g else ""
        if d is None and g is None:
            text = "sem dados: a API de trades não respondeu"
        elif ok:
            text = f"{tag}{len(buyers)} baleias in · {flow_txt}"
        elif flow < 0 and n:
            text = f"{tag}baleias a vender: {flow_txt}"
        else:
            text = f"{tag}poucas baleias: {len(buyers)} · {flow_txt}"
        self.event("SMARTS", coin, "pass" if ok else "reject", text, whales=len(buyers), flow=round(flow),
                   smart=g["smart"] if g else None, kol=g["kol"] if g else None)
        return {"ok": ok, "score": min(100, 50 + 10 * len(buyers) + 5 * sm + (10 if flow > 20_000 else 0))}

    # ------------------------------------------------------------ DEBATE (bulls, bears, risk team)
    def debate(self, addr: str, pool: dict, coin: str, mint: str) -> dict:
        info = self._gecko(f"/networks/solana/tokens/{mint}/info") or {}
        attrs = (info.get("data") or {}).get("attributes") or {}
        mi = self._mint_info(mint)
        a = pool["attributes"]
        tx = (a.get("transactions") or {}).get("h1") or {}
        top10 = _f(((attrs.get("holders") or {}).get("distribution_percentage") or {}).get("top_10"), -1)
        risks = []
        if mi is None:
            risks.append("sem dados da rede")
        else:
            if mi.get("mintAuthority") is not None:
                risks.append("mint ativa")
            if mi.get("freezeAuthority") is not None:
                risks.append("freeze ativo")
        if not 0 <= top10 < self.p.max_top10_pct:
            risks.append("top 10 " + ("sem dados" if top10 < 0 else f"{_pt(top10, 0)}%"))
        if str(attrs.get("is_honeypot", "")).lower() in ("yes", "true"):
            risks.append("honeypot")
        if int(tx.get("sells") or 0) == 0:
            risks.append("ninguém vende")
        g = self.gm(mint)
        if g:
            if g["rug"] > self.GMGN_RUG_MAX:
                risks.append(f"rug {_pt(g['rug'], 2)}")
            if g["wash"]:
                risks.append("wash trading")
            if max(g["bundler"], g["rat"]) > self.GMGN_BOTS_MAX:
                risks.append(f"bots/insiders {max(g['bundler'], g['rat']):.0%}")
        fresh = self._pairs([addr]).get(addr) or {}
        fv, fpc = fresh.get("volume") or {}, fresh.get("priceChange") or {}
        vol = a.get("volume_usd") or {}
        vol_m5, vol_h1 = _f(fv.get("m5"), _f(vol.get("m5"))), _f(fv.get("h1"), _f(vol.get("h1")))
        pc = a.get("price_change_percentage") or {}
        m5, h1 = _f(fpc.get("m5"), _f(pc.get("m5"))), _f(fpc.get("h1"), _f(pc.get("h1")))
        buyers, buys = _f(tx.get("buyers")), _f(tx.get("buys"))
        liq = _f((fresh.get("liquidity") or {}).get("usd"), _f(a.get("reserve_in_usd")))
        votes = {
            "ainda a negociar": vol_h1 > 0 and vol_m5 * 12 / vol_h1 >= 0.7,
            "compradores reais": buyers >= 65 and buys > 0 and buyers / buys >= 0.16,
            "não persegue": h1 <= 24 and m5 <= 4.5,
            "a comprar uma queda": m5 < 0,
            "liquidez funda": liq >= 100_000,
        }
        bulls = sum(votes.values())
        bears = len(votes) - bulls
        ok = not risks and bulls >= self.MIN_BULLS
        text = f"bull {bulls} / bear {bears} · " + ("risco ok" if not risks else "risco: " + ", ".join(risks))
        self.event("DEBATE", coin, "pass" if ok else "reject", text, votes=votes, risks=risks, score=40 + 12 * bulls)
        dec = (mi or {}).get("decimals")
        decimals = int(dec) if dec is not None else 6     # 0 is a real answer: a token without decimals
        return {"ok": ok, "score": 40 + 12 * bulls, "decimals": decimals}

    # ------------------------------------------------------------ ROUTER
    def router(self, coin: str, mint: str, size_usd: float) -> dict:
        sol = self.sol_usd()
        if sol <= 0 or size_usd < MIN_TRADE_USD:
            self.event("ROUTER", coin, "reject", "sem preço do SOL" if sol <= 0 else self._too_small(size_usd))
            return {"ok": False}
        q = self.quote(SOL_MINT, mint, size_usd / sol * 1e9)
        if not q:
            self.event("ROUTER", coin, "reject", "Jupiter sem rota")
            return {"ok": False}
        impact = _f(q.get("priceImpactPct"))
        hops = " → ".join(x["swapInfo"]["label"] for x in q.get("routePlan", [])[:2]) or "rota direta"
        ok = impact <= self.MAX_IMPACT
        self.event("ROUTER", coin, "pass" if ok else "reject", f"jupiter · {_pt(impact * 100, 1)}% slip · {hops}",
                   impact=round(impact, 4))
        return {"ok": ok, "score": 100 if impact < 0.005 else 80, "impact": impact, "hops": hops}

    # ------------------------------------------------------------ EXEC (buy)
    def executor(self, addr: str, pool: dict, coin: str, mint: str, score: float):
        decimals = (getattr(self, "_buying", None) or {}).get("decimals", 6)
        live = self._pairs([addr]).get(addr) or {}
        px, liq = _f(live.get("priceUsd")), _f((live.get("liquidity") or {}).get("usd"))
        self.d.seen[addr] = "exec"
        scan_px = _f(pool["attributes"].get("base_token_price_usd"))
        if px <= 0 or liq <= 0:
            return self.event("EXEC", coin, "skip", "sem preço agora")
        if scan_px > 0 and px > scan_px * (1 + self.p.recheck_price_up):
            return self.event("EXEC", coin, "skip", f"fugiu: +{(px / scan_px - 1) * 100:.0f}% desde o TRENDS")
        if liq < self.p.min_liquidity:        # the floor TRENDS asked for, on the pool as it is now
            return self.event("EXEC", coin, "skip", f"liquidez caiu desde o TRENDS: ${liq / 1000:.0f}k < "
                                                    f"${self.p.min_liquidity / 1000:.0f}k")
        size, rule = self._sizing(liq)
        why = self._refuse(mint) or self._no_room(size)
        if why:
            return self.event("EXEC", coin, "skip", why)
        sol = self.sol_usd()
        if sol <= 0:
            return self.event("EXEC", coin, "skip", "sem preço do SOL")
        t0 = time.time()
        q = self.quote(SOL_MINT, mint, size / sol * 1e9)
        fill_s = time.time() - t0
        if not q:
            return self.event("EXEC", coin, "skip", "sem rota Jupiter na hora de comprar")
        impact = _f(q.get("priceImpactPct"))  # ROUTER's limit, on the quote that is really filled
        if impact > self.MAX_IMPACT:
            return self.event("EXEC", coin, "skip", f"impacto subiu desde o ROUTER: {_pt(impact * 100, 1)}% slip")
        # the tokens this route really gives for the size, less the priority fee and MEV allowance
        tokens = int(q["outAmount"]) / 10 ** decimals * (1 - self.p.cost_per_side)
        if tokens <= 0:
            return self.event("EXEC", coin, "skip", "rota sem tokens")
        entry = size / tokens
        with self.lock:
            # again, now that nothing can change under us: the quote took its time
            why = self._refuse(mint) or self._no_room(size) or self._on_hold()
            if why:
                return self.event("EXEC", coin, "skip", why)
            self.d.cash -= size
            pos = Position(uuid.uuid4().hex[:6], coin, pool["attributes"]["name"], addr, mint,
                           _now().isoformat(timespec="seconds"), px, entry, tokens, size, liq, px, px, score=score,
                           decimals=decimals, liq0=liq)
            pos.img = str((live.get("info") or {}).get("imageUrl") or "")
            pos.mcap = _f(live.get("marketCap") or live.get("fdv"))
            pos.created = str(pool["attributes"].get("pool_created_at") or "")
            pos.series = [[int(time.time() * 1000), px]]
            self.d.positions.append(pos)
            self.d.seen[addr] = "bought"
        self.event("EXEC", coin, "buy", f"comprou {_pt(size / self.d.eur_usd)} € · {rule} · nota {score:.0f} · "
                   f"via {VENUES[self.d.venue]} (papel)",
                   mint=mint, price=px, entry=entry, eur=round(size / self.d.eur_usd, 2), score=round(score),
                   fill_s=round(fill_s, 1), img=pos.img)

    def execute_best(self):
        now = time.time()
        self._drop_stale()
        for r in list(self.ready):
            if (_now() - datetime.fromisoformat(r["since"])).total_seconds() / 60 > self.p.queue_max_min:
                self.ready.remove(r)
                self.event("EXEC", r["coin"], "skip", "esperou 10 min na fila")
        with self.lock:                                   # the brakes: only new entries stop, exits carry on
            lines = self.guard.check(self.equity())       # (under the lock: RISK feeds them from its thread)
        for line in lines:
            self.alert(line)
        if not self.guard.ok or now < self.no_new_until or self._unwatched() or self.quit.is_set():
            return
        if not self.ready or now - self.last_buy < self.p.buy_every_s:
            return
        with self.lock:
            if len(self.d.positions) >= self.p.max_positions:
                return
        best = max(self.ready, key=lambda r: r["score"])
        self.ready.remove(best)
        self.last_buy = time.time()
        self._buying = best
        try:
            self.executor(best["addr"], best["pool"], best["coin"], best["mint"], best["score"])
        finally:
            self._buying = {}

    def _unwatched(self) -> bool:
        """The risk loop has not turned for longer than a gap. It will say so when it is back; until then
        nothing watches the stops, so nothing is bought."""
        return bool(self._loop_at) and time.time() - self._loop_at > GAP_S

    def _on_hold(self) -> str:
        """Why a buy may not be booked right now ('' when it may). Asked under the lock, at the last moment:
        a brake, a gap or a stop can have come while the quote was out."""
        for line in self.guard.check(self.equity()):
            self.alert(line)
        if not self.guard.ok:
            return "travão ligado enquanto se preparava a compra"
        if self._unwatched() or time.time() < self.no_new_until:
            return "computador esteve parado: entradas em pausa"
        if self.quit.is_set():
            return "sessão a parar"
        return ""

    def _drop_stale(self):
        """After a gap without watching, what was found before it is not trusted: it is scanned again."""
        if not self.stale.is_set():
            return
        self.stale.clear()
        for addr in [q[0] for q in self.queue] + [r["addr"] for r in self.ready]:
            self.d.seen.pop(addr, None)
        self.queue.clear()
        self.ready.clear()

    # ------------------------------------------------------------ RISK: every 5 s, on its own thread
    def risk_loop(self):
        while not self.stop.is_set():
            tick = time.time()
            try:
                if M.STOP_FILE.exists():                  # "Parar sessão": seen here within one tick
                    self.quit.set()
                self._gap(tick)
                self.risk()
                ms = int((time.time() - tick) * 1000)
                self.d.tick_ms, self.d.max_tick_ms = ms, max(self.d.max_tick_ms, ms)
                self.save()
            except Exception:
                self.d.api_errors += 1
                log.exception("Risk tick failed, carrying on")
            self.stop.wait(max(0.5, self.RISK_EVERY_S - (time.time() - tick)))

    def _gap(self, tick: float):
        """The loop was away (the computer slept, the process hung): nothing watched the stops meanwhile."""
        gap, self._loop_at = (tick - self._loop_at if self._loop_at else 0.0), tick
        if gap <= GAP_S:
            return
        self.d.blind_s += gap
        self.d.max_gap_s = max(self.d.max_gap_s, gap)
        self.no_new_until = tick + PAUSE_AFTER_GAP_S
        self.stale.set()
        with self.lock:
            for p in self.d.positions:
                p.blind = True
            n = len(self.d.positions)
        self.alert(f"sem vigilância durante {guard.dur(gap)} (computador suspenso ou parado)"
                   + (f": {n} {'posição esteve' if n == 1 else 'posições estiveram'} sem stop" if n else "")
                   + f" · entradas em pausa {guard.dur(PAUSE_AFTER_GAP_S)}")

    def risk(self, final: bool = False):
        with self.lock:
            held = [(p.pool, p.mint) for p in self.d.positions]
        if not held:
            self.d.feed_stale = False
            return
        # every network call of a tick happens here, outside the lock and with a short timeout
        self._refresh_sol()
        try:
            live = dict(self._pairs([pool for pool, _ in held], self.http_risk, timeout=RISK_TIMEOUT_S) or {})
        except Exception:                                 # an answer in a shape nobody expected: a missed reading
            self.d.api_errors += 1
            log.exception("DexScreener read failed in RISK, falling back")
            live = {}
        lost = [mint for pool, mint in held if _f((live.get(pool) or {}).get("priceUsd")) <= 0]
        if lost:
            alt = self._quiet("Jupiter prices", self._jup_prices, lost) or {}
            for pool, mint in held:
                if mint in lost and alt.get(mint, 0) > 0:
                    live[pool] = {**(live.get(pool) or {"_nodex": True}), "_jup": alt[mint]}
        self._apply_risk(live, final)

    def _jup_prices(self, mints: list[str]) -> dict[str, float]:
        """Second source for a price DexScreener did not give: Jupiter's price API. It says nothing about the
        pool, so such a price can move a stop, a half or a trail but never the liquidity exit."""
        try:
            r = self.http_risk.get(JUP_PRICE, params={"ids": ",".join(mints)}, timeout=RISK_TIMEOUT_S)
            j = r.json() if r.ok else {}
        except (requests.RequestException, ValueError):
            return {}
        out = {}
        for mint in mints:
            row = j.get(mint) if isinstance(j, dict) else None
            px = _f(row.get("usdPrice")) if isinstance(row, dict) else 0.0
            if px > 0:
                out[mint] = px
        return out

    def _apply_risk(self, live: dict, final: bool):
        """RISK every 5 s: hard stop -8% until 2x prints; at 2x half comes back; the rest trails 10% off its
        high. One position failing its check never stops the check of the others."""
        with self.lock:
            held = list(self.d.positions)
        for pos in held:
            try:
                self._check(pos, live.get(pos.pool), final)
                self._fails.pop(pos.id, None)
            except Exception:
                self.d.risk_errors += 1
                log.exception("Risk check of %s failed, carrying on with the others", pos.coin)
                n = self._fails[pos.id] = self._fails.get(pos.id, 0) + 1
                if n == 3:
                    self._quiet("alert", self.alert, "a vigilância desta posição falhou 3 vezes seguidas", pos.coin)
        with self.lock:
            self.d.feed_stale = any(p.misses >= MISS_ALERT for p in self.d.positions)

    def _quiet(self, what: str, fn, *args, **kw):
        """A side effect (a record, a brake, an alert) that must never undo or block an exit."""
        try:
            return fn(*args, **kw)
        except Exception:
            log.exception("%s failed, carrying on", what)
            return None

    def _check(self, pos: Position, pr: dict | None, final: bool):
        with self.lock:
            if pos not in self.d.positions or pos.id in self._closing:
                return
            act = self._decide(pos, pr, final)
        if act:
            self._exit(pos, *act)                         # the sell quote is asked for outside the lock

    def _decide(self, pos: Position, pr: dict | None, final: bool) -> tuple[float, str, str] | None:
        """Marks one position with this reading; returns (price, reason, kind) when it has to be left."""
        now = time.time()
        dex = pr is not None and not pr.get("_nodex")     # a DexScreener reading: the only one that knows the pool
        pos.liq_fresh = dex
        px = _f(pr.get("priceUsd")) if dex else 0.0
        if dex:                                           # liquidity first: a pool can be emptied with no price left
            if pos.liq0 <= 0:
                pos.liq0 = pos.liquidity
            liq = pos.liquidity = _f((pr.get("liquidity") or {}).get("usd"))
            out = px if px > 0 else pos.last
            if liq < LIQ_GONE_USD:
                return out, "liquidez retirada", "liq"
            if liq < LIQ_DROP * pos.liq0:
                if now - self._liq_low.setdefault(pos.id, now) >= LIQ_CONFIRM_S:   # seen twice, not a blip
                    return out, "liquidez caiu para metade", "liq"
            else:
                self._liq_low.pop(pos.id, None)
        src = "dex"
        if px <= 0 and pr is not None and _f(pr.get("_jup")) > 0:
            px, src = _f(pr.get("_jup")), "jup"
        if px <= 0:
            pos.misses += 1
            since = self._miss_since.setdefault(pos.id, now)
            if pos.misses == MISS_ALERT:
                self.alert(f"sem preço há {guard.dur(now - since + self.RISK_EVERY_S)}: stop sem vigilância", pos.coin)
            return (pos.last, "fim da sessão (último preço)", "end") if final else None
        if pos.misses >= MISS_ALERT:
            self.alert("preço de volta" + (" (pela Jupiter)" if src == "jup" else "") + ": stop outra vez vigiado", pos.coin)
        pos.misses, pos.px_src = 0, src
        self._miss_since.pop(pos.id, None)
        pos.last = px
        if dex:
            pos.mcap = _f(pr.get("marketCap") or pr.get("fdv"), pos.mcap)
        pos.peak = max(pos.peak, px)
        _mark(pos.series, px)
        mult = px / pos.raw_entry
        if not pos.half_done and mult <= 1 - self.p.stop_pct:
            return px, f"stop −{self.p.stop_pct:.0%}", "stop"
        if not pos.half_done and mult >= HALF_AT:
            return px, HALF_REASON, "half"
        if pos.half_done and px <= pos.peak * (1 - self.p.trail_pct):
            return px, f"trailing {self.p.trail_pct:.0%} do máximo", "trail"
        if final:
            return px, "fim da sessão", "end"
        return None

    # ------------------------------------------------------------ EXEC (sell)
    def close(self, pos: Position, px: float, reason: str, kind: str = "end"):
        self._exit(pos, px, reason, kind)

    def _close(self, pos: Position, px: float, reason: str, kind: str = "end"):
        self._exit(pos, px, reason, kind)

    def _half(self, pos: Position, px: float):
        """2x: sell half, which brings the entry back; the other half rides."""
        self._exit(pos, px, HALF_REASON, "half")

    def _exit(self, pos: Position, px: float, reason: str, kind: str):
        """Sells a position (kind 'half': half of it). Whoever marks it as closing first is the only one to sell
        it. The quote is asked for outside the lock; the booking is one step under it, money and position
        first, so a log line, a brake or an alert that fails afterwards cannot leave the sale half done. A
        sale whose position changed while the quote was out is dropped, not booked."""
        half = kind == "half"
        with self.lock:
            if pos not in self.d.positions or pos.id in self._closing:
                return
            self._closing.add(pos.id)
            held = pos.qty
            qty = held / 2 if half else held
        try:
            got, src = self._sale(pos, qty, px)
            with self.lock:
                if pos not in self.d.positions or pos.qty != held:    # sold by someone else while the quote was out
                    log.warning("Sale of %s dropped: the position was already sold", pos.coin)
                    return
                cost = pos.cost_usd / 2 if half else pos.cost_usd
                self.d.cash += got
                if half:
                    pos.qty -= qty
                    pos.cost_usd -= cost
                    pos.half_done, pos.half_px, pos.half_ts, pos.trail_on = True, px, int(time.time() * 1000), True
                else:
                    self.d.positions.remove(pos)
                    for book in (self._fails, self._liq_low, self._miss_since):
                        book.pop(pos.id, None)
                t = self._quiet("trade record", self._trade, pos, px, got, cost, reason, kind, src)
                self._quiet("brakes", self._brake, pos, got - cost, not half)
                if t:
                    self._quiet("sell event", self._sold, pos, t)
        finally:
            with self.lock:
                self._closing.discard(pos.id)

    def _brake(self, pos: Position, pnl: float, full: bool):
        """Every close feeds the brakes; a coin that closed at a loss is not bought again this session."""
        total = self.guard.closed(pos.id, pnl, full)
        if total is not None and total < 0:
            self._blocked[pos.mint] = pos.coin

    def _sale(self, pos: Position, qty: float, px: float) -> tuple[float, str]:
        """Dollars a sale of qty brings, and where the number comes from:
          'quote'  a real Jupiter sell quote for this amount in whole raw units, less costs
          'model'  no quote: a constant-product pool with the liquidity last seen (half of it on each side),
                   so the bigger the sale against the pool, the worse the price; less costs
          'zero'   no quote and the pool is empty, or Jupiter says there is no route and the liquidity on
                   the position is not a DexScreener reading of this tick: the tokens are counted as nothing"""
        cost, sol = self.p.cost_per_side, self._sol_fresh()
        state, q = "unavailable", None
        raw = int(qty * 10 ** pos.decimals)
        if sol > 0 and raw > 0:
            try:
                state, q = self._quote(pos.mint, SOL_MINT, raw, self.http_risk, timeout=RISK_TIMEOUT_S)
            except Exception:
                log.exception("Sell quote of %s failed", pos.coin)
                state, q = "unavailable", None
        if state == "ok" and q:
            # Jupiter is asked in whole raw units; the fraction cut off is paid at the same price. A token with no
            # decimals: 14.925 booked (15 less costs), 14 quoted - without this the 0.925 was lost on top of the costs
            return _f(q.get("outAmount")) / 1e9 * sol * (1 - cost) * (qty * 10 ** pos.decimals / raw), "quote"
        if state != "no_route":
            with self.lock:
                self.d.quote_fail += 1
        if pos.liquidity < LIQ_GONE_USD:                  # an emptied pool: nothing to model, whatever Jupiter said
            return 0.0, "zero"
        if state == "no_route" and not pos.liq_fresh:     # no route, and the pool was not seen this tick
            return 0.0, "zero"
        side, value = max(pos.liquidity, 0.0) / 2, max(qty * px, 0.0)
        got = side * value / (side + value) if side + value > 0 else 0.0
        return got * (1 - cost), "model"

    def _sell(self, pos: Position, qty: float, px: float) -> float:
        return self._sale(pos, qty, px)[0]

    def _trade(self, pos: Position, px: float, got: float, cost: float, reason: str, kind: str,
               src: str = "model") -> dict:
        pnl = got - cost
        base = pos.raw_entry if pos.raw_entry > 0 else (px if px > 0 else 1.0)
        t = {"id": pos.id, "coin": pos.coin, "mint": pos.mint, "img": pos.img, "opened": pos.opened,
             "closed": _now().isoformat(timespec="seconds"), "cost_usd": cost, "got_usd": got, "pnl_usd": pnl,
             "pnl_eur": pnl / self.d.eur_usd, "pct": got / cost - 1 if cost else 0, "raw_move": px / base - 1,
             "peak_move": pos.peak / base - 1, "reason": reason, "kind": kind, "score": pos.score,
             "mult": px / base, "entry": pos.raw_entry, "exit": px, "half_px": pos.half_px,
             "half_ts": pos.half_ts, "series": _thin_series(pos.series, 120), "mcap": pos.mcap, "created": pos.created,
             "fill_src": src, "blind": pos.blind}
        self.d.trades.append(t)
        return t

    def _sold(self, pos: Position, t: dict):
        pnl = t["pnl_usd"]
        how = {"model": " · preço estimado pela liquidez (sem cotação)",
               "zero": " · sem cotação e pool vazia ou sem leitura: contado a zero"}.get(t["fill_src"], "")
        self.event("EXEC", pos.coin, "sell", f"vendeu ({t['reason']}) {'+' if pnl >= 0 else '-'}"
                   f"{_pt(abs(pnl) / self.d.eur_usd)} € · {'+' if t['pct'] >= 0 else ''}{_pt(t['pct'] * 100, 1)}% · "
                   f"via {VENUES[self.d.venue]} (papel){how}",
                   mint=pos.mint, pnl_eur=round(pnl / self.d.eur_usd, 2), pct=round(t["pct"], 4),
                   mult=round(t["mult"], 2), kind=t["kind"], img=pos.img, fill_src=t["fill_src"], blind=t["blind"])

    # ------------------------------------------------------------ loop
    def run(self) -> str:
        end = datetime.fromisoformat(self.d.ends)
        log.info("Memes paper session: %.2f EUR = $%.2f, until %s", self.d.start_usd / self.d.eur_usd,
                 self.d.start_usd, self.d.ends)
        self.save()
        (M.OUT / "pid").write_text(str(os.getpid()), encoding="utf-8")
        M.STOP_FILE.unlink(missing_ok=True)
        guard.keep_awake(True)                # no idle sleep while the session runs (a closed lid still sleeps)
        risk_thread = threading.Thread(target=self.risk_loop, name="meme-risk", daemon=True)
        risk_thread.start()
        next_scan = 0.0
        try:
            while _now() < end and not self.quit.is_set():
                if M.STOP_FILE.exists():      # "Parar sessão" in the app (the risk thread sees it too)
                    break
                tick = time.time()
                try:
                    self._drop_stale()
                    if tick >= next_scan:
                        self.scan()
                        next_scan = tick + 60
                    for _ in range(2):        # a few audits per tick
                        self._drop_stale()
                        if self.queue and not self.quit.is_set():
                            self.auditor(*self.queue.pop(0))
                    if not self.quit.is_set():
                        self.execute_best()
                except Exception:             # one bad tick must not end the session
                    self.d.api_errors += 1
                    log.exception("Tick failed, carrying on")
                self.quit.wait(max(1.0, min(15 - (time.time() - tick), (end - _now()).total_seconds())))
        except KeyboardInterrupt:
            log.info("Stopped by hand")
        if self.quit.is_set() or M.STOP_FILE.exists():
            log.info("Stopped from the app")
        self.stop.set()
        risk_thread.join(timeout=30)
        M.STOP_FILE.unlink(missing_ok=True)
        guard.keep_awake(False)
        try:
            self._gap(time.time())            # a gap that ran into the end: the risk thread never came back to say it
        except Exception:
            log.exception("Last gap check failed")
        try:
            self.risk(final=True)
        except Exception:
            log.exception("Final price check failed; closing at the last known prices")
        self._close_leftovers()
        self.d.finished = True
        self.d.ends = min(self.d.ends, _now().isoformat(timespec="seconds"))
        self.save(final=True)
        (M.OUT / "pid").unlink(missing_ok=True)
        report = self.report()
        (M.OUT / "relatorio.md").write_text(report, encoding="utf-8")
        return report

    def _close_leftovers(self):
        """Session over: what is still open goes at its last price. A sale the risk thread was in the middle
        of gets a few seconds to finish first. After that its claim is dropped; should it still come back,
        _exit books only one of the two sales."""
        deadline, late = time.time() + 20, 0
        while True:
            with self.lock:
                left = list(self.d.positions)
                if time.time() >= deadline:
                    late += 1
                    self._closing.clear()                 # whoever held it is gone, or will find it sold
            for pos in left:
                self._quiet("last close", self.close, pos, pos.last, "fim da sessão (último preço)")
            with self.lock:
                if not self.d.positions or late >= 3:     # a late pass can lose to a half that was hanging: again
                    return
            if not late:
                time.sleep(0.5)

    def report(self) -> str:
        base = super().report()
        # the base report lists the classic agents' cuts; replace that part with the five agents
        head = base.split("## Onde as moedas foram cortadas")[0]
        lines = ["## Onde as moedas foram cortadas", ""]
        for agent in STAGES:
            c = self.d.counts.get(BUCKET[agent], {})
            lines.append(f"- **{agent}:** " + (", ".join(f"{k} ({v})" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))
                                                  if c else "nada cortado"))
        d, p = self.d, self.p
        closed = guard.results(d.trades)      # one result per position: a half sold on the way is not a trade
        rate = f"{sum(1 for v in closed if v > 0) / len(closed):.0%}" if closed else "—"
        by = {k: sum(1 for t in d.trades if t.get("fill_src") == k) for k in ("quote", "model", "zero")}
        cost = _pt(p.cost_per_side * 100, 1).rstrip("0").rstrip(",")
        blind = sum(1 for t in d.trades if t.get("blind"))
        g = self.guard.state()
        lines += [
            "", f"Acerto: {rate}. Compounder: cada compra é {p.max_trade_pct:.0%} da mesa (nunca mais de "
                f"{p.stack_pct:.0%} do dinheiro livre nem de {p.max_pool_pct:.0%} da liquidez da pool), no máximo "
                f"{p.max_deployed_pct:.0%} da mesa investido e {p.max_positions} posições abertas; metade a 2×, "
                f"trailing de {p.trail_pct:.0%} depois, stop −{p.stop_pct:.0%} antes.",
            "", "## O que estes números assumem", "",
            f"- Compras: os tokens que a cotação real da Jupiter dava para aquele valor, menos {cost}% de custos.",
            f"- Vendas ({len(d.trades)}): {by['quote']} com cotação real da Jupiter para a quantidade vendida, menos "
            f"{cost}% de custos; {by['model']} sem cotação, estimadas pela liquidez da pool (quanto maior a venda "
            f"face à pool, pior o preço); {by['zero']} contadas a zero, porque não havia cotação e a pool "
            "estava vazia ou sem leitura.",
            f"- O preço é lido de {self.RISK_EVERY_S} em {self.RISK_EVERY_S} segundos: um stop de −{p.stop_pct:.0%} "
            "sai ao preço que houver nessa leitura, que pode ser pior.",
            f"- Cotações de venda sem resposta: {d.quote_fail}. Verificações de posição que falharam: {d.risk_errors}.",
            f"- Tempo sem vigilância (computador suspenso ou parado): {guard.dur(d.blind_s)}"
            + (f", o maior intervalo de {guard.dur(d.max_gap_s)}" if d.max_gap_s else "")
            + f". Trades que passaram por um intervalo desses: {blind}.",
            "- Travões das entradas: " + (g["reason"] if g["on"] else "nenhum ligado no fim da sessão") + ".",
            "- Em papel não há ordens reais. Com dinheiro a sério há transações que falham, bots que passam à "
            "frente e preços que fogem mais do que aqui.",
            "", "Um resultado de poucas horas é sobretudo sorte."]
        return head.replace("# Teste de memecoins em papel", "# Modo memes (5 agentes) em papel") + "\n".join(lines) + "\n"


def _mark(series: list, px: float, cap: int = 600):
    """Price point for the coin page; past the cap the older points are thinned out."""
    series.append([int(time.time() * 1000), px])
    if len(series) > cap:
        series[:] = series[:cap // 2:2] + series[cap // 2:]


def _thin_series(series: list, n: int) -> list:
    if len(series) <= n:
        return list(series)
    step = len(series) / n
    return [series[int(i * step)] for i in range(n)] + [series[-1]]


def run(minutes: float, eur: float, venue: str = "fomo", style: str = "poucas") -> str:
    return MemesDesk(minutes, eur, venue, style).run()
