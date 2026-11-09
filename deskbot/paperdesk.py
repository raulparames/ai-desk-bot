"""Shared pieces for the paper desks (futures, stocks): a ledger kept in a JSON file, in euros.

Each desk starts with its own fictitious balance (1000 EUR by default), converted to dollars once
at the start with the EUR/USD rate of that day. Nothing here talks to a broker.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import ROOT

log = logging.getLogger(__name__)
DESKS_DIR = ROOT / "desks"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def eur_usd() -> float:
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/ticker/price", params={"symbol": "EURUSDT"},
                         timeout=15)
        return float(r.json()["price"])
    except (requests.RequestException, KeyError, ValueError):
        return 1.10


@dataclass
class Ledger:
    desk: str
    start_eur: float
    eur_usd: float
    cash: float                                   # dollars
    started: str
    positions: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    curve: list[list] = field(default_factory=list)   # [ms, equity in EUR]
    ticker: list[dict] = field(default_factory=list)  # [{symbol, pct}] for the app's strip
    paused: bool = False
    last_bar: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return DESKS_DIR / f"{self.desk}.json"

    @classmethod
    def load(cls, desk: str, start_eur: float = 1000.0) -> "Ledger":
        path = DESKS_DIR / f"{desk}.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw.pop("equity_eur", None)
            raw.pop("updated", None)
            led = cls(**raw)
        else:
            rate = eur_usd()
            led = cls(desk, start_eur, rate, start_eur * rate, now_iso())
        led.paused = bool(controls().get(desk, {}).get("paused", False))
        return led

    def event(self, agent: str, symbol: str, verdict: str, reason: str, **values):
        e = {"ts": now_iso(), "agent": agent, "coin": symbol, "verdict": verdict, "reason": reason, **values}
        self.events.append(e)
        self.events = self.events[-300:]
        log.info("[%s] %s %s %s %s", self.desk, agent, symbol, verdict, reason)

    def save(self, equity_usd: float):
        DESKS_DIR.mkdir(exist_ok=True)
        eq_eur = equity_usd / self.eur_usd
        self.curve.append([int(time.time() * 1000), round(eq_eur, 4)])
        if len(self.curve) > 5000:  # keep the file small: thin out the oldest half
            self.curve = self.curve[:2500:2] + self.curve[2500:]
        raw = asdict(self)
        raw["equity_eur"] = eq_eur
        raw["updated"] = now_iso()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        for _ in range(10):  # the dashboard may be reading the file right now
            try:
                tmp.replace(self.path)
                return
            except PermissionError:
                time.sleep(0.2)
        log.warning("Could not update %s (file busy)", self.path.name)


def controls() -> dict:
    """Switches the app can flip per desk: {"futuros": {"paused": true}, ...}."""
    try:
        return json.loads((DESKS_DIR / "controls.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def set_paused(desk: str, paused: bool) -> dict:
    DESKS_DIR.mkdir(exist_ok=True)
    c = controls()
    c.setdefault(desk, {})["paused"] = bool(paused)
    c[desk]["changed"] = now_iso()
    (DESKS_DIR / "controls.json").write_text(json.dumps(c, indent=2), encoding="utf-8")
    return c[desk]


def read_state(desk: str) -> dict | None:
    try:
        return json.loads((DESKS_DIR / f"{desk}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
