"""Memes mode: a Solana memecoin desk in paper, laid out like the "trading desk" videos.

The app shows five agents: SCANNER (finds coins: the TRENDS and SMARTS steps below), ANALYST (scores
them 0-100: DEBATE and ROUTER), COMPOUNDER (sizes every buy), EXECUTOR (buys) and RISK (sells).
Compounder rules, as in the video: every buy is 40% of the free cash, never more than 2 open, no adding
to a coin already held. At 2x half is sold (the entry comes back); the rest rides a trailing stop 10%
off its high. Until 2x prints there is a hard stop at -8%.

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
  EXEC    (like opentrade) best agreed coin first, one buy every 2 min, 8% of the balance, at most
          5 positions; paper fill at the Jupiter quote. Prices every 5 s: stop -8% (the video's rule),
          trailing from +20% that sells 12% off the top; exits priced with a Jupiter sell quote.
  CORE    buys only when all five agree ("5/5").

GMGN (smart money and KOL wallets, rug scores, its own trending list) joins in when GMGN_API_KEY is
in .env (your own key from https://gmgn.ai/ai; read-only, see gmgn.py). Then TRENDS also takes GMGN's
5-minute trending coins, SMARTS counts GMGN's smart money and KOL wallets, and the risk team adds
GMGN's rug score, wash trading, bundler and insider shares. Without a key it runs as above.
The venue (GMGN or FOMO) is only where the app opens each coin: every order stays in paper.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta

import requests

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
    "COMPOUNDER": ("GMGNAI/gmgn-skills", "40% do saldo livre · máx. 2 abertas · rola os ganhos"),
    "EXECUTOR": ("sendaifun/solana-agent-kit", "cotação Jupiter · compra em papel"),
    "RISK": ("0xsyncroot/opentrade", "metade a 2× · trailing 10% · stop −8%"),
}
HALF_AT = 2.0
VENUES = {"gmgn": "GMGN", "fomo": "FOMO"}
STYLES = {   # whale size, whales needed, bull votes needed, top-10 holders limit (%)
    "poucas": {"whale_usd": 500, "min_whales": 3, "min_bulls": 4, "max_top10": 25},
    "mais": {"whale_usd": 300, "min_whales": 2, "min_bulls": 3, "max_top10": 40},
}
BUCKET = {"TRENDS": "trends", "SMARTS": "smarts", "DEBATE": "debate", "ROUTER": "router", "EXEC": "exec",
          "CORE": "core"}


def memes_params() -> MemeParams:
    # Compounder: 40% of the free cash per buy, max 2 open, trail 10% off the high after the half at 2x
    return MemeParams(stop_pct=0.08, cost_per_side=0.005, stack_pct=0.40, max_positions=2, trail_pct=0.10,
                      timeout_h=1e9)


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

    def __init__(self, minutes: float, eur: float, venue: str = "gmgn", style: str = "poucas"):
        super().__init__(minutes, eur, memes_params())
        self.d.mode = "memes"
        self.d.venue = venue if venue in VENUES else "gmgn"
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

    # ------------------------------------------------------------ helpers
    def event(self, stage: str, coin: str, verdict: str, reason: str = "", **values):
        """stage is the step inside an agent (TRENDS, SMARTS, ...); the app shows the agent."""
        agent = "RISK" if verdict == "sell" else AGENT_OF.get(stage, stage)
        e = {"ts": _now().isoformat(timespec="seconds"), "agent": agent, "stage": stage, "coin": coin,
             "verdict": verdict, "reason": reason, **values}
        with self.lock:
            self.d.events.append(e)
            with self.events_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            if verdict in ("cut", "reject", "skip", "kill"):
                c = self.d.counts.setdefault(BUCKET.get(stage, "core"), {})
                key = reason.split(":")[0]
                c[key] = c.get(key, 0) + 1
            if agent in self.d.agents and verdict != "wait":
                self.d.agents[agent] = {"coin": coin, "text": reason, "ok": verdict in ("pass", "buy", "sell"),
                                        "ts": e["ts"], "verdict": verdict,
                                        **{k: values[k] for k in ("score", "img", "eur", "fill_s", "kind") if k in values}}
        agent = stage
        log.info("%-7s %-12s %-6s %s", agent, coin, verdict, reason)

    def sol_usd(self) -> float:
        price, at = self._sol
        if time.time() - at < 60 and price > 0:
            return price
        try:
            r = self.http.get(JUP_PRICE, params={"ids": SOL_MINT}, timeout=10)
            price = float(r.json()[SOL_MINT]["usdPrice"])
            self._sol = (price, time.time())
        except (requests.RequestException, KeyError, ValueError, TypeError):
            log.warning("SOL price failed, keeping %s", price)
        return price

    def quote(self, in_mint: str, out_mint: str, amount: int, session=None) -> dict | None:
        """A real Jupiter route for this size (no transaction is built or sent)."""
        try:
            r = (session or self.http).get(JUP_QUOTE, params={"inputMint": in_mint, "outputMint": out_mint,
                                                              "amount": int(amount), "slippageBps": 100}, timeout=8)
            j = r.json()
            return j if r.ok and j.get("outAmount") else None
        except (requests.RequestException, ValueError):
            return None

    def trade_size(self, liq: float) -> float:
        """Compounder: 40% of the free cash (wins roll into the next buy), at most 2% of the pool."""
        with self.lock:
            return min(self.d.cash * self.p.stack_pct, liq * self.p.max_pool_pct)

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
        for addr, pool in found.items():
            if addr in self.d.seen or any(p.pool == addr for p in self.d.positions):
                continue
            self.trends(addr, pool)

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
        audit = t.get("audit") or {}
        if audit.get("mintAuthorityDisabled") is False:
            return "mint ativa"
        if audit.get("freezeAuthorityDisabled") is False:
            return "freeze ativo"
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
        route = self.router(coin, mint, self.trade_size(liq))
        votes["ROUTER"] = route["ok"]
        if not route["ok"]:
            self.d.seen[addr] = "router"
            return
        with self.lock:
            room = len(self.d.positions) + len(self.ready) < self.p.max_positions + 3
        votes["EXEC"] = room
        if not room:
            self.d.seen[addr] = "exec"
            return self.event("EXEC", coin, "skip", "cheio: 5 posições e fila longa")
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
        decimals = int((mi or {}).get("decimals") or 6)
        return {"ok": ok, "score": 40 + 12 * bulls, "decimals": decimals}

    # ------------------------------------------------------------ ROUTER
    def router(self, coin: str, mint: str, size_usd: float) -> dict:
        sol = self.sol_usd()
        if sol <= 0 or size_usd < 5:
            self.event("ROUTER", coin, "reject", "sem preço do SOL" if sol <= 0 else "sem saldo livre")
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
        size = self.trade_size(liq)
        sol = self.sol_usd()
        t0 = time.time()
        q = self.quote(SOL_MINT, mint, size / sol * 1e9) if sol > 0 and size >= 5 else None
        fill_s = time.time() - t0
        if not q:
            return self.event("EXEC", coin, "skip", "sem rota Jupiter na hora de comprar")
        tokens = int(q["outAmount"]) / 10 ** decimals
        if tokens <= 0:
            return self.event("EXEC", coin, "skip", "rota sem tokens")
        entry = size / tokens * (1 + self.p.cost_per_side)   # route price + priority fee and MEV allowance
        with self.lock:
            if size > self.d.cash:
                return self.event("EXEC", coin, "skip", "sem saldo livre")
            self.d.cash -= size
            pos = Position(uuid.uuid4().hex[:6], coin, pool["attributes"]["name"], addr, mint,
                           _now().isoformat(timespec="seconds"), px, entry, tokens, size, liq, px, px, score=score)
            pos.decimals = decimals
            pos.img = str((live.get("info") or {}).get("imageUrl") or "")
            pos.mcap = _f(live.get("marketCap") or live.get("fdv"))
            pos.created = str(pool["attributes"].get("pool_created_at") or "")
            pos.series = [[int(time.time() * 1000), px]]
            self.d.positions.append(pos)
            self.d.seen[addr] = "bought"
        self.event("EXEC", coin, "buy", f"comprou {_pt(size / self.d.eur_usd)} € · 40% do saldo livre · nota {score:.0f} · "
                   f"via {VENUES[self.d.venue]} (papel)",
                   mint=mint, price=px, entry=entry, eur=round(size / self.d.eur_usd, 2), score=round(score),
                   fill_s=round(fill_s, 1), img=pos.img)

    def execute_best(self):
        for r in list(self.ready):
            if (_now() - datetime.fromisoformat(r["since"])).total_seconds() / 60 > self.p.queue_max_min:
                self.ready.remove(r)
                self.event("EXEC", r["coin"], "skip", "esperou 10 min na fila")
        if not self.ready or time.time() - self.last_buy < self.p.buy_every_s:
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

    # ------------------------------------------------------------ EXEC (sell) and risk every 5 s
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

    def _apply_risk(self, live: dict, final: bool):
        """RISK every 5 s: hard stop -8% until 2x prints; at 2x half comes back; the rest trails 10% off its high."""
        for pos in list(self.d.positions):
            pr = live.get(pos.pool)
            if pr is None:
                if final:
                    self.close(pos, pos.last, "fim da sessão (último preço)")
                continue
            px, liq = _f(pr.get("priceUsd")), _f((pr.get("liquidity") or {}).get("usd"))
            if px <= 0:
                continue
            pos.last, pos.liquidity = px, liq
            pos.mcap = _f(pr.get("marketCap") or pr.get("fdv"), pos.mcap)
            pos.peak = max(pos.peak, px)
            _mark(pos.series, px)
            mult = px / pos.raw_entry
            if liq < 1_000:
                self.close(pos, px, "liquidez retirada", "liq")
            elif not pos.half_done and mult <= 1 - self.p.stop_pct:
                self.close(pos, px, f"stop −{self.p.stop_pct:.0%}", "stop")
            elif not pos.half_done and mult >= HALF_AT:
                self._half(pos, px)
            elif pos.half_done and px <= pos.peak * (1 - self.p.trail_pct):
                self.close(pos, px, f"trailing {self.p.trail_pct:.0%} do máximo", "trail")
            elif final:
                self.close(pos, px, "fim da sessão", "end")

    def close(self, pos: Position, px: float, reason: str, kind: str = "end"):
        with self.lock:
            if pos in self.d.positions:
                self._close(pos, px, reason, kind)

    def _sell(self, pos: Position, qty: float, px: float) -> float:
        """Dollars a sale of qty would bring: a real Jupiter sell quote when there is one."""
        sol = self._sol[0]
        if sol > 0:
            raw = int(qty * 10 ** getattr(pos, "decimals", 6))
            q = self.quote(pos.mint, SOL_MINT, raw, self.http_risk) if raw > 0 else None
            if q:
                return int(q["outAmount"]) / 1e9 * sol * (1 - self.p.cost_per_side)
        impact = 2 * qty * px / max(pos.liquidity, 1.0)   # no route right now: price model with the pool's depth
        return qty * px * (1 - self.p.cost_per_side - min(impact, 0.5))

    def _trade(self, pos: Position, px: float, got: float, cost: float, reason: str, kind: str) -> dict:
        pnl = got - cost
        t = {"id": pos.id, "coin": pos.coin, "mint": pos.mint, "img": pos.img, "opened": pos.opened,
             "closed": _now().isoformat(timespec="seconds"), "cost_usd": cost, "got_usd": got, "pnl_usd": pnl,
             "pnl_eur": pnl / self.d.eur_usd, "pct": got / cost - 1 if cost else 0, "raw_move": px / pos.raw_entry - 1,
             "peak_move": pos.peak / pos.raw_entry - 1, "reason": reason, "kind": kind, "score": pos.score,
             "mult": px / pos.raw_entry, "entry": pos.raw_entry, "exit": px, "half_px": pos.half_px,
             "half_ts": pos.half_ts, "series": _thin_series(pos.series, 120), "mcap": pos.mcap, "created": pos.created}
        self.d.trades.append(t)
        sign = "+" if pnl >= 0 else "-"
        self.event("EXEC", pos.coin, "sell", f"vendeu ({reason}) {sign}{_pt(abs(pnl) / self.d.eur_usd)} € · "
                   f"{'+' if t['pct'] >= 0 else ''}{_pt(t['pct'] * 100, 1)}% · via {VENUES[self.d.venue]} (papel)",
                   mint=pos.mint, pnl_eur=round(pnl / self.d.eur_usd, 2), pct=round(t["pct"], 4),
                   mult=round(t["mult"], 2), kind=kind, img=pos.img)
        return t

    def _half(self, pos: Position, px: float):
        """2x: sell half, which brings the entry back; the other half rides."""
        half = pos.qty / 2
        got = self._sell(pos, half, px)
        cost = pos.cost_usd / 2
        self.d.cash += got
        pos.qty -= half
        pos.cost_usd -= cost
        pos.half_done, pos.half_px, pos.half_ts = True, px, int(time.time() * 1000)
        pos.trail_on = True
        self._trade(pos, px, got, cost, "metade a 2× · entrada de volta", "half")

    def _close(self, pos: Position, px: float, reason: str, kind: str = "end"):
        got = self._sell(pos, pos.qty, px)
        self.d.cash += got
        self.d.positions.remove(pos)
        self._trade(pos, px, got, pos.cost_usd, reason, kind)

    def report(self) -> str:
        base = super().report()
        # the base report lists the classic agents' cuts; replace that part with the five agents
        head = base.split("## Onde as moedas foram cortadas")[0]
        lines = ["## Onde as moedas foram cortadas", ""]
        for agent in STAGES:
            c = self.d.counts.get(BUCKET[agent], {})
            lines.append(f"- **{agent}:** " + (", ".join(f"{k} ({v})" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))
                                                  if c else "nada cortado"))
        wins = [t for t in self.d.trades if t["pnl_usd"] > 0]
        rate = f"{len(wins) / len(self.d.trades):.0%}" if self.d.trades else "—"
        lines += ["", f"Acerto: {rate}. Compounder: 40% do saldo livre por compra, no máximo 2 abertas, metade a 2×, "
                      "trailing de 10% depois, stop −8% antes. Preços de entrada e saída com cotações reais da Jupiter, "
                      "mais 0,5% de custos. Um resultado de poucas horas é sobretudo sorte."]
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


def run(minutes: float, eur: float, venue: str = "gmgn", style: str = "poucas") -> str:
    return MemesDesk(minutes, eur, venue, style).run()
