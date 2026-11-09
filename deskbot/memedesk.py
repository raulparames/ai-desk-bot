"""Memes mode: a 5-agent Solana memecoin desk in paper, laid out like the "gmgn mode" video.

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

GMGN itself (smart money and KOL wallets, rug scores) needs a personal GMGN API key; this desk does
not use the public demo key, which GMGN says is for testing only.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta

import requests

from . import memetest as M
from .memetest import MemeParams, MemeTest, Position, _f, _now, _pt

log = logging.getLogger("memetest")
SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
AGENTS = ("TRENDS", "SMARTS", "DEBATE", "ROUTER", "EXEC")
REPOS = {
    "SMARTS": ("GMGNAI/gmgn-skills", "smart money · baleias · segurança"),
    "TRENDS": ("8bitsats/Solana-MCP-Trading-Server", "tokens em alta"),
    "DEBATE": ("TauricResearch/TradingAgents", "debate bull/bear · equipa de risco"),
    "ROUTER": ("sendaifun/solana-agent-kit", "rotas Jupiter"),
    "EXEC": ("0xsyncroot/opentrade", "execução rápida"),
}
BUCKET = {"TRENDS": "trends", "SMARTS": "smarts", "DEBATE": "debate", "ROUTER": "router", "EXEC": "exec",
          "CORE": "core"}


def memes_params() -> MemeParams:
    return MemeParams(stop_pct=0.08, cost_per_side=0.005)


class MemesDesk(MemeTest):
    WHALE_USD = 500
    WHALE_WINDOW_MIN = 30
    MIN_WHALES = 3
    MAX_IMPACT = 0.02
    RISK_EVERY_S = 5

    def __init__(self, minutes: float, eur: float):
        super().__init__(minutes, eur, memes_params())
        self.d.mode = "memes"
        self.d.counts = {k: {} for k in ("trends", "smarts", "debate", "router", "exec", "core")}
        self.d.agents = {a: {"coin": None, "text": "à espera", "ok": None, "ts": None} for a in AGENTS}
        self._sol = (0.0, 0.0)   # (price, fetched at)
        self._rank: dict[str, int] = {}

    # ------------------------------------------------------------ helpers
    def event(self, agent: str, coin: str, verdict: str, reason: str = "", **values):
        e = {"ts": _now().isoformat(timespec="seconds"), "agent": agent, "coin": coin, "verdict": verdict,
             "reason": reason, **values}
        with self.lock:
            self.d.events.append(e)
            with self.events_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            if verdict in ("cut", "reject", "skip", "kill"):
                c = self.d.counts.setdefault(BUCKET.get(agent, "core"), {})
                key = reason.split(":")[0]
                c[key] = c.get(key, 0) + 1
            if agent in self.d.agents and verdict != "wait":
                self.d.agents[agent] = {"coin": coin, "text": reason, "ok": verdict in ("pass", "buy", "sell"),
                                        "ts": e["ts"]}
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
        with self.lock:
            return min(self.equity() * self.p.stack_pct, liq * self.p.max_pool_pct, self.d.cash)

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
        self.d.pools_seen = len(set(self.d.seen) | set(found) | set(self.d.waiting))
        for addr, pool in found.items():
            if addr in self.d.seen or any(p.pool == addr for p in self.d.positions):
                continue
            self.trends(addr, pool)

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
        smarts = self.smarts(addr, coin)
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
        self.event("CORE", coin, "pass", f"5/5 agentes concordaram · nota {score}", votes=votes)
        self.d.seen[addr] = "ready"
        self.ready.append({"score": score, "since": _now().isoformat(timespec="seconds"), "addr": addr,
                           "pool": pool, "coin": coin, "mint": mint, "decimals": debate["decimals"],
                           "route": route})
        self.event("EXEC", coin, "wait", f"na fila com nota {score} ({len(self.ready)} na fila)")

    # ------------------------------------------------------------ SMARTS
    def smarts(self, addr: str, coin: str) -> dict:
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
        ok = d is not None and len(buyers) >= self.MIN_WHALES and flow > 0
        if d is None:
            text = "sem dados: a API de trades não respondeu"
        elif ok:
            text = f"{len(buyers)} baleias in · {flow_txt}"
        elif flow <= 0 and n:
            text = f"baleias a vender: {flow_txt}"
        else:
            text = f"poucas baleias: {len(buyers)} · {flow_txt}"
        self.event("SMARTS", coin, "pass" if ok else "reject", text, whales=len(buyers), flow=round(flow))
        return {"ok": ok, "score": min(100, 50 + 10 * len(buyers) + (10 if flow > 20_000 else 0))}

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
        ok = not risks and bulls >= 4
        text = f"bull {bulls} / bear {bears} · " + ("risco ok" if not risks else "risco: " + ", ".join(risks))
        self.event("DEBATE", coin, "pass" if ok else "reject", text, votes=votes, risks=risks)
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
        self.event("ROUTER", coin, "pass" if ok else "reject", f"jupiter · {_pt(impact * 100, 1)}% slip · {hops}")
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
        q = self.quote(SOL_MINT, mint, size / sol * 1e9) if sol > 0 and size >= 5 else None
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
            self.d.positions.append(pos)
            self.d.seen[addr] = "bought"
        self.event("EXEC", coin, "buy", f"comprou {_pt(size / self.d.eur_usd)} € · nota {score:.0f} · papel",
                   mint=mint, price=px, entry=entry)

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

    def _close(self, pos: Position, px: float, reason: str):
        """Exit priced with a real Jupiter sell quote when there is one."""
        sol = self._sol[0]
        got = None
        if sol > 0:
            raw = int(pos.qty * 10 ** getattr(pos, "decimals", 6))
            q = self.quote(pos.mint, SOL_MINT, raw, self.http_risk) if raw > 0 else None
            if q:
                got = int(q["outAmount"]) / 1e9 * sol * (1 - self.p.cost_per_side)
        if got is None:  # no route right now: price model with the pool's depth
            impact = 2 * pos.qty * px / max(pos.liquidity, 1.0)
            got = pos.qty * px * (1 - self.p.cost_per_side - min(impact, 0.5))
        self.d.cash += got
        self.d.positions.remove(pos)
        pnl = got - pos.cost_usd
        t = {"id": pos.id, "coin": pos.coin, "mint": pos.mint, "opened": pos.opened,
             "closed": _now().isoformat(timespec="seconds"), "cost_usd": pos.cost_usd, "got_usd": got, "pnl_usd": pnl,
             "pnl_eur": pnl / self.d.eur_usd, "pct": got / pos.cost_usd - 1, "raw_move": px / pos.raw_entry - 1,
             "peak_move": pos.peak / pos.raw_entry - 1, "reason": reason, "score": pos.score}
        self.d.trades.append(t)
        sign = "+" if pnl >= 0 else "-"
        self.event("EXEC", pos.coin, "sell", f"vendeu ({reason}) {sign}{_pt(abs(pnl) / self.d.eur_usd)} € · "
                   f"{'+' if t['pct'] >= 0 else ''}{_pt(t['pct'] * 100, 1)}% · papel", mint=pos.mint,
                   pnl_eur=round(pnl / self.d.eur_usd, 2), pct=round(t["pct"], 4))

    def report(self) -> str:
        base = super().report()
        # the base report lists the classic agents' cuts; replace that part with the five agents
        head = base.split("## Onde as moedas foram cortadas")[0]
        lines = ["## Onde as moedas foram cortadas", ""]
        for agent in ("TRENDS", "SMARTS", "DEBATE", "ROUTER", "EXEC"):
            c = self.d.counts.get(BUCKET[agent], {})
            lines.append(f"- **{agent}:** " + (", ".join(f"{k} ({v})" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))
                                                  if c else "nada cortado"))
        wins = [t for t in self.d.trades if t["pnl_usd"] > 0]
        rate = f"{len(wins) / len(self.d.trades):.0%}" if self.d.trades else "—"
        lines += ["", f"Acerto: {rate}. Stop de −8% em cada trade, como no vídeo. Preços de entrada e saída com "
                      "cotações reais da Jupiter, mais 0,5% de custos. Um resultado de poucas horas é sobretudo sorte."]
        return head.replace("# Teste de memecoins em papel", "# Modo memes (5 agentes) em papel") + "\n".join(lines) + "\n"


def run(minutes: float, eur: float) -> str:
    return MemesDesk(minutes, eur).run()
