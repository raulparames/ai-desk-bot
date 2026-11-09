"""GMGN data for the memes desk: smart money and KOL wallets, rug scores, trending tokens.

Read-only. It needs your own GMGN API key (GMGN_API_KEY in .env, created at https://gmgn.ai/ai).
Only the read routes are here: this module has no private key, no swap and no order route, so it
cannot trade even if someone wanted it to. Without a key every call returns None and the desk
falls back to GeckoTerminal.

API as the official gmgn-cli uses it: https://openapi.gmgn.ai, header X-APIKEY, query timestamp +
client_id, answers {"code": 0, "data": ...}. Rate limit: about 20 light calls per second.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

import requests
from dotenv import load_dotenv

from .config import ROOT, env

log = logging.getLogger("gmgn")
BASE = "https://openapi.gmgn.ai"


class Gmgn:
    def __init__(self):
        load_dotenv(ROOT / ".env")
        self.key = env("GMGN_API_KEY")
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "ai-desk-bot/1.0 (paper)"})
        self.paused_until = 0.0
        self.error = ""
        self.calls = 0
        self.last_ok = 0.0
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def status(self) -> dict:
        if not self.enabled:
            return {"on": False, "text": "sem chave GMGN: dados do GeckoTerminal"}
        if self.error and time.time() < self.paused_until:
            return {"on": False, "text": f"GMGN em pausa: {self.error}"}
        return {"on": True, "text": f"GMGN ligada · {self.calls} pedidos" + (f" · {self.error}" if self.error else "")}

    def _get(self, path: str, **query) -> dict | list | None:
        if not self.enabled or time.time() < self.paused_until:
            return None
        query.update(timestamp=int(time.time()), client_id=str(uuid.uuid4()))
        try:
            r = self.http.get(BASE + path, params=query, headers={"X-APIKEY": self.key}, timeout=12)
        except requests.RequestException as exc:
            self.error = "sem ligação"
            log.warning("GMGN %s failed: %s", path, type(exc).__name__)
            return None
        self.calls += 1
        if r.status_code == 429:
            reset = r.headers.get("x-ratelimit-reset")
            wait = max(5.0, float(reset) - time.time()) if reset and reset.isdigit() else 30.0
            self.paused_until, self.error = time.time() + min(wait, 300), "limite de pedidos"
            return None
        if r.status_code in (401, 403):
            # a refused key (or IPv6, which GMGN does not accept): stop asking for 10 minutes
            self.paused_until, self.error = time.time() + 600, f"chave recusada ({r.status_code})"
            log.warning("GMGN refused the key (%s)", r.status_code)
            return None
        try:
            j = r.json()
        except ValueError:
            self.error = f"resposta inválida ({r.status_code})"
            return None
        if j.get("code") != 0:
            self.error = str(j.get("message") or j.get("error") or j.get("code"))[:60]
            return None
        self.error, self.last_ok = "", time.time()
        return j.get("data")

    def _cached(self, key: str, ttl: float, fn):
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        val = fn()
        with self._lock:
            self._cache[key] = (time.time(), val)
        return val

    # ------------------------------------------------------------ what the desk uses
    def trending(self, interval: str = "5m", limit: int = 50) -> list[dict]:
        """Solana trending tokens with mint and freeze renounced (GMGN's own safe default)."""
        data = self._cached(f"rank:{interval}", 50, lambda: self._get(
            "/v1/market/rank", chain="sol", interval=interval, order_by="volume", direction="desc",
            limit=limit, filters=["renounced", "frozen"]))
        rank = data.get("rank") if isinstance(data, dict) else data
        return rank if isinstance(rank, list) else []

    def token(self, mint: str) -> dict | None:
        """Smart money, KOLs and risk for one token, merged from token info and token security."""
        def fetch():
            info = self._get("/v1/token/info", chain="sol", address=mint)
            sec = self._get("/v1/token/security", chain="sol", address=mint)
            if not isinstance(info, dict) and not isinstance(sec, dict):
                return None
            info, sec = info if isinstance(info, dict) else {}, sec if isinstance(sec, dict) else {}
            tags = info.get("wallet_tags_stat") or {}
            stat = info.get("stat") or {}
            return {
                "smart": _i(tags.get("smart_wallets")),
                "kol": _i(tags.get("renowned_wallets")),
                "rug": _f(sec.get("rug_ratio")),
                "wash": str(sec.get("is_wash_trading")).lower() in ("true", "1", "yes"),
                "bundler": _f(sec.get("bundler_trader_amount_rate", stat.get("top_bundler_trader_percentage"))),
                "rat": _f(sec.get("rat_trader_amount_rate", stat.get("top_rat_trader_percentage"))),
                "top10": _f(sec.get("top_10_holder_rate", stat.get("top_10_holder_rate"))),
                "insider": _f(sec.get("suspected_insider_hold_rate")),
            }
        return self._cached(f"token:{mint}", 60, fetch)


def from_rank(item: dict) -> dict:
    """The same smart-money and risk numbers, straight from a trending row (no extra call)."""
    return {"smart": _i(item.get("smart_degen_count")), "kol": _i(item.get("renowned_count")),
            "rug": _f(item.get("rug_ratio")), "wash": bool(item.get("is_wash_trading")),
            "bundler": _f(item.get("bundler_rate")), "rat": _f(item.get("rat_trader_amount_rate")),
            "top10": _f(item.get("top_10_holder_rate")), "insider": 0.0}


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0
