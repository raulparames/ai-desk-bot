"""Loads config.toml and .env into typed settings."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
STATE_FILE = ROOT / "bot_state.json"
JOURNAL_DB = ROOT / "journal.db"
CONTROLS_FILE = ROOT / "controls.json"   # switches the dashboard can flip (pause entries)


@dataclass(frozen=True)
class StrategyParams:
    breakout_lookback: int = 20     # close above the highest high of the previous N bars
    volume_mult: float = 1.5        # volume >= mult x its 20-bar average (1.0 = filter off)
    volume_window: int = 20
    trend_ema: int = 50             # coin must be above its own EMA
    regime_ema: int = 200           # BTC must be above its EMA (risk-on regime)
    atr_period: int = 14
    stop_atr: float = 2.0           # initial stop distance in ATRs
    trail_atr: float = 3.0          # chandelier trailing stop in ATRs below the highest close
    take_profit_r: float = 0.0      # fixed target in R multiples (0 = trailing exit only)
    rsi_max: float = 85.0           # skip entries that are already extremely overbought
    min_room_r: float = 0.0         # skip if a prior 1y high sits closer than this many R (0 = off)
    stop_pct: float = 0.0           # >0 replaces the ATR stop with a fixed % stop
    take_profit_pct: float = 0.0    # >0 replaces the R target with a fixed % target
    # Rules adapted from the "5 agents" desk guide (Risk / Analyst / Executor). 0 = off.
    trail_start_r: float = 0.0      # Risk: the trailing stop only starts once the close is X R above entry
    timeout_days: int = 0           # Risk: "dead money" exit after N days...
    timeout_min_r: float = 0.5      # ...if the close is still below entry + X R
    max_extension_atr: float = 0.0  # Analyst: skip if the close is already > X ATR above the breakout level
    max_entry_gap_pct: float = 0.0  # Executor: skip if the price ran > X (0.05 = 5%) between signal and fill
    min_quality: float = 0.0        # Analyst: minimum 0-100 quality score


@dataclass(frozen=True)
class RiskParams:
    risk_per_trade: float = 0.01    # fraction of equity lost if the stop is hit
    max_positions: int = 4
    max_position_pct: float = 0.25  # max notional per position as a fraction of equity
    max_drawdown_halt: float = 0.20 # no new entries once equity falls this far from its peak
    min_rr: float = 2.0             # skeptic's hard floor for reward:risk
    fee: float = 0.001              # per side
    slippage: float = 0.0005        # per side


@dataclass(frozen=True)
class AIConfig:
    backend: str = "claude-cli"     # claude-cli | anthropic | none
    model: str = "claude-opus-5-5"
    effort: str = "low"
    boss_effort: str = "medium"
    news_web_search: bool = False
    timeout_s: int = 300


@dataclass(frozen=True)
class BrokerConfig:
    kind: str = "aitrader"          # aitrader | paper
    aitrader_url: str = "http://127.0.0.1:8000"
    paper_start_cash: float = 100000.0


@dataclass(frozen=True)
class MarketConfig:
    universe: tuple[str, ...] = (
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    )
    regime_symbol: str = "BTCUSDT"
    timeframe: str = "1d"
    history_start: str = "2019-01-01"


@dataclass(frozen=True)
class LiveConfig:
    check_minutes: int = 15
    daily_run_utc_minute: int = 10  # minutes after 00:00 UTC to run the desk on the new daily candle


@dataclass(frozen=True)
class DashboardConfig:
    lan: bool = False               # True: also reachable from the phone on the home Wi-Fi (needs the phone link)
    port: int = 8899


@dataclass(frozen=True)
class Sleeve:
    """One fixed parameter set trading its own equal share of the account."""
    name: str
    params: StrategyParams


@dataclass(frozen=True)
class Settings:
    market: MarketConfig = field(default_factory=MarketConfig)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    sleeves: tuple[Sleeve, ...] = ()
    risk: RiskParams = field(default_factory=RiskParams)
    ai: AIConfig = field(default_factory=AIConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)


def _build(cls, raw: dict):
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown keys in [{cls.__name__}]: {sorted(unknown)}")
    kwargs = {k: tuple(v) if isinstance(v, list) else v for k, v in raw.items()}
    return cls(**kwargs)


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    path = path or ROOT / "config.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    base = _build(StrategyParams, raw.get("strategy", {}))
    sleeves = []
    for item in raw.get("sleeves", []):
        item = dict(item)
        name = item.pop("name")
        _build(StrategyParams, item)  # validates the keys
        sleeves.append(Sleeve(name, replace(base, **item)))
    return Settings(
        market=_build(MarketConfig, raw.get("market", {})),
        strategy=base,
        sleeves=tuple(sleeves) or (Sleeve("base", base),),
        risk=_build(RiskParams, raw.get("risk", {})),
        ai=_build(AIConfig, raw.get("ai", {})),
        broker=_build(BrokerConfig, raw.get("broker", {})),
        live=_build(LiveConfig, raw.get("live", {})),
        dashboard=_build(DashboardConfig, raw.get("dashboard", {})),
    )


def load_controls() -> dict:
    import json
    try:
        raw = json.loads(CONTROLS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return {"entries_paused": bool(raw.get("entries_paused", False)), "changed": raw.get("changed")}


def save_controls(**changes) -> dict:
    import json
    from datetime import datetime, timezone
    cur = load_controls()
    cur.update(changes)
    cur["changed"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    CONTROLS_FILE.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()
