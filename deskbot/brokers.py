"""Paper brokers only. There is deliberately no real-money broker in this project."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from .config import ROOT, RiskParams, env

log = logging.getLogger(__name__)


def coin_of(symbol: str) -> str:
    return symbol.removesuffix("USDT")


class BrokerError(RuntimeError):
    pass


class PaperBroker:
    """Local ledger in paper_account.json. Fills at the reference price plus slippage and fees."""

    name = "paper"

    def __init__(self, start_cash: float, risk: RiskParams, path: Path = ROOT / "paper_account.json"):
        self.path = path
        self.risk = risk
        if path.exists():
            self.state = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.state = {"cash": start_cash, "holdings": {}}
            self._save()

    def _save(self):
        self.path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def account(self, prices: dict[str, float]) -> dict:
        h = self.state["holdings"]
        value = sum(q * prices.get(f"{c}USDT", 0.0) for c, q in h.items())
        return {"cash": self.state["cash"], "holdings": dict(h), "equity": self.state["cash"] + value}

    def buy(self, symbol: str, qty: float, ref_price: float, note: str = "") -> float:
        px = ref_price * (1 + self.risk.slippage)
        cost = qty * px * (1 + self.risk.fee)
        if cost > self.state["cash"]:
            raise BrokerError(f"insufficient paper cash for {symbol}: need {cost:.2f}")
        self.state["cash"] -= cost
        c = coin_of(symbol)
        self.state["holdings"][c] = self.state["holdings"].get(c, 0.0) + qty
        self._save()
        return px

    def sell(self, symbol: str, qty: float, ref_price: float, note: str = "") -> float:
        c = coin_of(symbol)
        held = self.state["holdings"].get(c, 0.0)
        qty = min(qty, held)
        if qty <= 0:
            raise BrokerError(f"no {c} to sell")
        px = ref_price * (1 - self.risk.slippage)
        self.state["cash"] += qty * px * (1 - self.risk.fee)
        left = held - qty
        if left > 1e-12:
            self.state["holdings"][c] = left
        else:
            self.state["holdings"].pop(c, None)
        self._save()
        return px


class AITraderBroker:
    """Paper trades on a self-hosted AI-Trader server (POST /api/signals/realtime, executed_at=now).
    The server prices the fill itself and charges its own fee."""

    name = "aitrader"

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.agent_name = env("AITRADER_NAME")
        self.password = env("AITRADER_PASSWORD")
        if not self.agent_name or not self.password:
            raise BrokerError("Set AITRADER_NAME and AITRADER_PASSWORD in .env (see README)")
        self.token = None

    def _login(self):
        try:
            r = requests.post(f"{self.base}/api/claw/agents/login",
                              json={"name": self.agent_name, "password": self.password}, timeout=20)
        except requests.RequestException as exc:
            raise BrokerError(f"AI-Trader server not reachable at {self.base}: {exc}") from exc
        if r.status_code != 200:
            raise BrokerError(f"AI-Trader login failed ({r.status_code}): {r.text[:200]}")
        self.token = r.json()["token"]

    def _req(self, method: str, path: str, **kw):
        if not self.token:
            self._login()
        for attempt in range(2):
            try:
                r = requests.request(method, f"{self.base}{path}",
                                     headers={"Authorization": f"Bearer {self.token}"}, timeout=30, **kw)
            except requests.RequestException as exc:
                raise BrokerError(f"AI-Trader request failed: {exc}") from exc
            if r.status_code == 401 and attempt == 0:
                self._login()
                continue
            if r.status_code >= 400:
                raise BrokerError(f"AI-Trader {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()

    def account(self, prices: dict[str, float]) -> dict:
        data = self._req("GET", "/api/positions")
        holdings: dict[str, float] = {}
        for p in data.get("positions", []):
            if p.get("market") == "crypto" and p.get("side", "long") == "long":
                holdings[p["symbol"]] = holdings.get(p["symbol"], 0.0) + float(p["quantity"])
        value = sum(q * prices.get(f"{c}USDT", 0.0) for c, q in holdings.items())
        cash = float(data.get("cash", 0.0))
        return {"cash": cash, "holdings": holdings, "equity": cash + value}

    def _trade(self, action: str, symbol: str, qty: float, note: str) -> float:
        body = {"market": "crypto", "action": action, "symbol": coin_of(symbol), "price": 0,
                "quantity": round(qty, 8), "content": note[:500], "executed_at": "now"}
        res = self._req("POST", "/api/signals/realtime", json=body)
        if not res.get("success"):
            raise BrokerError(f"AI-Trader rejected {action} {symbol}: {res}")
        return float(res["price"])

    def buy(self, symbol: str, qty: float, ref_price: float, note: str = "") -> float:
        return self._trade("buy", symbol, qty, note)

    def sell(self, symbol: str, qty: float, ref_price: float, note: str = "") -> float:
        return self._trade("sell", symbol, qty, note)


def make_broker(settings):
    b = settings.broker
    if b.kind == "aitrader":
        return AITraderBroker(b.aitrader_url)
    if b.kind == "paper":
        return PaperBroker(b.paper_start_cash, settings.risk)
    raise ValueError(f"Unknown broker kind {b.kind!r} (use 'aitrader' or 'paper')")
