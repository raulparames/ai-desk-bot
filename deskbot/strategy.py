"""The quant core: volume-confirmed breakout with a trend/regime filter.

The same functions feed the backtest and the live bot, so what was tested is what runs.
Signals at bar t use only data up to and including bar t's close; entries happen
at the next bar's open.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import StrategyParams
from .indicators import atr, ema, rsi

RESISTANCE_LOOKBACK = 365


def add_indicators(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    out = df.copy()
    out["ema_trend"] = ema(out["close"], p.trend_ema)
    out["atr"] = atr(out, p.atr_period)
    out["rsi"] = rsi(out["close"], 14)
    out["vol_ratio"] = out["volume"] / out["volume"].rolling(p.volume_window).mean().shift(1)
    out["breakout_level"] = out["high"].rolling(p.breakout_lookback).max().shift(1)
    # prior major high, ignoring the bars that form the breakout itself
    out["resistance"] = out["high"].shift(p.breakout_lookback + 1).rolling(
        RESISTANCE_LOOKBACK, min_periods=60).max()
    return out


def regime_series(btc: pd.DataFrame, p: StrategyParams) -> pd.Series:
    return btc["close"] > ema(btc["close"], p.regime_ema)


def stop_distance(close: float, atr_value: float, p: StrategyParams) -> float:
    return close * p.stop_pct if p.stop_pct > 0 else p.stop_atr * atr_value


def target_price(entry: float, risk_per_unit: float, p: StrategyParams) -> float:
    """0.0 means no fixed target (exit by trailing stop)."""
    if p.take_profit_pct > 0:
        return entry * (1 + p.take_profit_pct)
    if p.take_profit_r > 0:
        return entry + p.take_profit_r * risk_per_unit
    return 0.0


def trail_active(highest_close: float, entry: float, risk_per_unit: float, p: StrategyParams) -> bool:
    """Risk agent: the trailing stop starts only after the close went trail_start_r R above entry."""
    return p.trail_start_r <= 0 or highest_close >= entry + p.trail_start_r * risk_per_unit


def is_dead_money(days_open: int, close: float, entry: float, risk_per_unit: float,
                  p: StrategyParams) -> bool:
    """Risk agent: N days in and still under entry + timeout_min_r R = dead money, out."""
    return (p.timeout_days > 0 and days_open >= p.timeout_days
            and close < entry + p.timeout_min_r * risk_per_unit)


def entry_ran(price: float, signal_close: float, p: StrategyParams) -> bool:
    """Executor re-check: the price ran more than max_entry_gap_pct since the signal close."""
    return p.max_entry_gap_pct > 0 and price > signal_close * (1 + p.max_entry_gap_pct)


def room_to_resistance_r(entry: float, resistance: float, risk_per_unit: float) -> float:
    """How many R of upside before the prior 1y high. inf when price is already above it."""
    if not np.isfinite(resistance) or resistance <= entry or risk_per_unit <= 0:
        return float("inf")
    return (resistance - entry) / risk_per_unit


def compute_signals(df: pd.DataFrame, regime: pd.Series, p: StrategyParams) -> pd.DataFrame:
    """Adds `signal` (bool) and `score` (rank among same-day signals) columns."""
    out = add_indicators(df, p)
    reg = regime.reindex(out.index).ffill().fillna(False).astype(bool)
    stop_dist = np.where(p.stop_pct > 0, out["close"] * p.stop_pct, p.stop_atr * out["atr"])
    room = (out["resistance"] - out["close"]) / stop_dist
    room = room.where(out["resistance"] > out["close"], np.inf)
    sig = (
        (out["close"] > out["breakout_level"])
        & (out["vol_ratio"] >= p.volume_mult)
        & (out["close"] > out["ema_trend"])
        & reg
        & (out["rsi"] < p.rsi_max)
        & out["atr"].notna()
    )
    if p.min_room_r > 0:
        sig &= room >= p.min_room_r
    out["regime_on"] = reg
    out["room_r"] = room
    out["extension_atr"] = (out["close"] - out["breakout_level"]) / out["atr"]
    add_quality(out)
    if p.max_extension_atr > 0:
        sig &= out["extension_atr"] <= p.max_extension_atr
    if p.min_quality > 0:
        sig &= out["quality"] >= p.min_quality
    out["signal"] = sig.fillna(False)
    out["score"] = out["vol_ratio"].fillna(0) + ((out["close"] / out["breakout_level"]) - 1).fillna(0) * 10
    return out


# The Analyst's 4 closed questions, answered by code from the candles (0 to 1 each).
QUALITY_QUESTIONS = {
    "q_trend": "A tendência está viva (EMA50 a subir)",
    "q_volume": "O volume do breakout é real (bem acima da média)",
    "q_room": "Há espaço até à resistência do último ano",
    "q_chase": "A entrada está a perseguir uma vela que já correu",
}


def add_quality(out: pd.DataFrame) -> None:
    """quality = 100 * mean(trend, volume, room, 1 - chase), like the guide's Analyst in rules mode."""
    slope = out["ema_trend"] / out["ema_trend"].shift(10) - 1
    out["q_trend"] = (slope / 0.05).clip(0, 1)
    out["q_volume"] = ((out["vol_ratio"] - 1) / 2).clip(0, 1)
    out["q_room"] = (out["room_r"] / 4).clip(0, 1).fillna(1.0)
    out["q_chase"] = (out["extension_atr"] / 2).clip(0, 1)
    q = out[["q_trend", "q_volume", "q_room"]].sum(axis=1) + (1 - out["q_chase"])
    out["quality"] = (100 * q / 4).round()


@dataclass
class TradePlan:
    """What the 'Charts' agent puts on the table."""
    symbol: str
    date: str
    close: float
    entry: float
    stop: float
    target: float           # 0 = trailing exit; reference_target is then used for R:R
    reference_target: float
    risk_per_unit: float
    rr: float
    room_to_resistance_r: float
    atr: float
    rsi: float
    vol_ratio: float
    breakout_level: float
    ema_trend: float
    regime_on: bool
    trailing_atr: float
    signal_today: bool
    extension_atr: float = 0.0
    quality: float = 0.0
    answers: dict | None = None     # the Analyst's 4 answers (0-1)
    entry_gap_limit: float = 0.0    # Executor: max rise between signal close and entry (0 = off)
    trail_start_r: float = 0.0      # Risk: trailing starts after +X R (0 = from the start)
    timeout_days: int = 0           # Risk: dead-money exit after N days (0 = off)
    timeout_min_r: float = 0.0

    def as_dict(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) and np.isfinite(v) else v)
                for k, v in self.__dict__.items()}


def make_plan(symbol: str, row: pd.Series, p: StrategyParams, entry: float | None = None,
              reference_r: float = 3.0) -> TradePlan:
    close = float(row["close"])
    entry = float(entry or close)
    dist = stop_distance(close, float(row["atr"]), p)
    stop = entry - dist
    tgt = target_price(entry, dist, p)
    ref = tgt if tgt > 0 else entry + reference_r * dist
    room = room_to_resistance_r(entry, float(row.get("resistance", np.nan)), dist)
    upside = min(ref, entry + room * dist) if np.isfinite(room) else ref
    return TradePlan(
        symbol=symbol, date=str(row.name.date()) if hasattr(row.name, "date") else str(row.name),
        close=close, entry=entry, stop=stop, target=tgt, reference_target=ref,
        risk_per_unit=dist, rr=(upside - entry) / dist if dist > 0 else 0.0,
        room_to_resistance_r=room, atr=float(row["atr"]), rsi=float(row["rsi"]),
        vol_ratio=float(row["vol_ratio"]), breakout_level=float(row["breakout_level"]),
        ema_trend=float(row["ema_trend"]), regime_on=bool(row["regime_on"]),
        trailing_atr=p.trail_atr, signal_today=bool(row.get("signal", False)),
        extension_atr=_num(row.get("extension_atr")), quality=_num(row.get("quality")),
        answers={k: round(_num(row.get(k)), 2) for k in QUALITY_QUESTIONS},
        entry_gap_limit=p.max_entry_gap_pct, trail_start_r=p.trail_start_r,
        timeout_days=p.timeout_days, timeout_min_r=p.timeout_min_r,
    )


def _num(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return v if np.isfinite(v) else 0.0
