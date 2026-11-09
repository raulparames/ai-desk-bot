"""Portfolio backtest with fees, slippage, gap-aware stops and walk-forward selection."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from .config import RiskParams, StrategyParams
from .strategy import (compute_signals, entry_ran, is_dead_money, regime_series, stop_distance,
                       target_price, trail_active)

MIN_NOTIONAL = 10.0


@dataclass
class Position:
    symbol: str
    entry: float
    stop: float
    qty: float
    risk_per_unit: float
    target: float
    highest_close: float
    opened: pd.Timestamp
    trail_on: bool = False


@dataclass
class Trade:
    symbol: str
    opened: pd.Timestamp
    closed: pd.Timestamp
    entry: float
    exit: float
    qty: float
    pnl: float
    r_multiple: float
    reason: str


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[Trade] = field(default_factory=list)
    exposure: pd.Series | None = None

    def trades_df(self) -> pd.DataFrame:
        return pd.DataFrame([t.__dict__ for t in self.trades])


def run_backtest(data: dict[str, pd.DataFrame], p: StrategyParams, r: RiskParams,
                 regime_symbol: str = "BTCUSDT", initial: float = 10_000.0,
                 start: str | None = None, end: str | None = None) -> BacktestResult:
    regime = regime_series(data[regime_symbol], p)
    frames = {s: compute_signals(df, regime, p) for s, df in data.items()}
    idx = sorted(set().union(*[f.index for f in frames.values()]))
    idx = pd.DatetimeIndex(idx)
    if start:
        idx = idx[idx >= pd.Timestamp(start)]
    if end:
        idx = idx[idx <= pd.Timestamp(end)]
    syms = list(frames)
    col = {c: {s: frames[s][c].reindex(idx).to_numpy(dtype=float)
               for s in syms} for c in ("open", "high", "low", "close", "atr")}
    sig = {s: frames[s]["signal"].reindex(idx).fillna(False).to_numpy(dtype=bool) for s in syms}
    score = {s: frames[s]["score"].reindex(idx).fillna(0).to_numpy(dtype=float) for s in syms}

    cash = initial
    positions: dict[str, Position] = {}
    pending: list[tuple[str, float, float]] = []   # (symbol, stop distance, signal close)
    last_close = {s: np.nan for s in syms}
    trades: list[Trade] = []
    equity = np.empty(len(idx))
    exposure = np.zeros(len(idx))
    buy_cost = 1 + r.slippage
    sell_cost = 1 - r.slippage

    def close_pos(sym: str, raw_px: float, when, reason: str):
        nonlocal cash
        pos = positions.pop(sym)
        px = raw_px * sell_cost
        cash += pos.qty * px * (1 - r.fee)
        pnl = pos.qty * (px * (1 - r.fee) - pos.entry * (1 + r.fee))
        trades.append(Trade(sym, pos.opened, when, pos.entry, px, pos.qty, pnl,
                            (px - pos.entry) / pos.risk_per_unit, reason))

    for i, day in enumerate(idx):
        o = {s: col["open"][s][i] for s in syms}
        # 1) stops gapped through at the open
        for s, pos in list(positions.items()):
            if np.isfinite(o[s]) and o[s] <= pos.stop:
                close_pos(s, o[s], day, "stop_gap")
        # 2) fills for yesterday's signals at today's open
        if pending:
            mark = cash + sum(pos.qty * (o[s] if np.isfinite(o[s]) else last_close[s])
                              for s, pos in positions.items())
            for s, dist, sig_close in pending:
                if len(positions) >= r.max_positions or s in positions or not np.isfinite(o[s]):
                    continue
                if entry_ran(o[s], sig_close, p):       # Executor re-check: ran before the fill
                    continue
                entry = o[s] * buy_cost
                stop = entry - dist
                if dist <= 0 or stop <= 0:
                    continue
                qty = mark * r.risk_per_trade / dist
                qty = min(qty, mark * r.max_position_pct / entry, cash / (entry * (1 + r.fee)))
                if qty * entry < MIN_NOTIONAL:
                    continue
                cash -= qty * entry * (1 + r.fee)
                positions[s] = Position(s, entry, stop, qty, dist, target_price(entry, dist, p),
                                        entry, day)
            pending = []
        # 3) intraday stop / target (stop assumed first when both touch: conservative)
        for s, pos in list(positions.items()):
            lo, hi = col["low"][s][i], col["high"][s][i]
            if not np.isfinite(lo):
                continue
            if lo <= pos.stop:
                close_pos(s, pos.stop, day, "stop")
            elif pos.target > 0 and hi >= pos.target:
                close_pos(s, pos.target, day, "target")
        # 4) at the close: trail stops, mark to market, queue tomorrow's entries
        for s in syms:
            c = col["close"][s][i]
            if np.isfinite(c):
                last_close[s] = c
        for s, pos in list(positions.items()):
            c, a = col["close"][s][i], col["atr"][s][i]
            if np.isfinite(c) and np.isfinite(a):
                pos.highest_close = max(pos.highest_close, c)
                pos.trail_on = pos.trail_on or trail_active(pos.highest_close, pos.entry, pos.risk_per_unit, p)
                if pos.trail_on:
                    pos.stop = max(pos.stop, pos.highest_close - p.trail_atr * a)
                if is_dead_money((day - pos.opened).days, c, pos.entry, pos.risk_per_unit, p):
                    close_pos(s, c, day, "timeout")
        held = sum(pos.qty * last_close[s] for s, pos in positions.items())
        equity[i] = cash + held
        exposure[i] = held / equity[i] if equity[i] > 0 else 0
        cands = [(s, score[s][i]) for s in syms if sig[s][i] and s not in positions]
        cands.sort(key=lambda x: -x[1])
        pending = [(s, stop_distance(col["close"][s][i], col["atr"][s][i], p), col["close"][s][i])
                   for s, _ in cands]

    for s in list(positions):
        close_pos(s, last_close[s], idx[-1], "end")
    return BacktestResult(pd.Series(equity, index=idx), trades, pd.Series(exposure, index=idx))


# ---------------------------------------------------------------- metrics

def metrics(equity: pd.Series, trades: list[Trade] | None = None, periods: int = 365) -> dict:
    equity = equity.dropna()
    if len(equity) < 2:
        return {}
    rets = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    dd = equity / equity.cummax() - 1
    vol = rets.std() * np.sqrt(periods)
    downside = rets[rets < 0].std() * np.sqrt(periods)
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    out = {
        "start": str(equity.index[0].date()), "end": str(equity.index[-1].date()),
        "total_return": total, "cagr": cagr, "max_drawdown": dd.min(),
        "sharpe": rets.mean() * periods / vol if vol > 0 else 0.0,
        "sortino": rets.mean() * periods / downside if downside > 0 else 0.0,
        "calmar": cagr / abs(dd.min()) if dd.min() < 0 else 0.0,
        "volatility": vol,
    }
    if trades is not None:
        pnl = np.array([t.pnl for t in trades])
        rm = np.array([t.r_multiple for t in trades])
        wins, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
        out.update({
            "trades": len(trades),
            "win_rate": float((pnl > 0).mean()) if len(pnl) else 0.0,
            "profit_factor": float(wins / losses) if losses > 0 else float("inf") if wins > 0 else 0.0,
            "avg_r": float(rm.mean()) if len(rm) else 0.0,
            "best_r": float(rm.max()) if len(rm) else 0.0,
            "worst_r": float(rm.min()) if len(rm) else 0.0,
        })
    return out


def buy_and_hold(df: pd.DataFrame, start=None, end=None, initial: float = 10_000.0,
                 fee: float = 0.001) -> pd.Series:
    c = df["close"]
    if start:
        c = c[c.index >= pd.Timestamp(start)]
    if end:
        c = c[c.index <= pd.Timestamp(end)]
    return initial * (1 - fee) * c / c.iloc[0]


def equal_weight(data: dict[str, pd.DataFrame], start=None, end=None,
                 initial: float = 10_000.0) -> pd.Series:
    """Daily-rebalanced equal weight of whatever coins are listed that day (no fees)."""
    rets = pd.DataFrame({s: df["close"].pct_change() for s, df in data.items()})
    if start:
        rets = rets[rets.index >= pd.Timestamp(start)]
    if end:
        rets = rets[rets.index <= pd.Timestamp(end)]
    daily = rets.mean(axis=1, skipna=True).fillna(0)
    daily.iloc[0] = 0
    return initial * (1 + daily).cumprod()


# ---------------------------------------------------------------- walk-forward

DEFAULT_GRID = {
    "breakout_lookback": [20, 55],
    "volume_mult": [1.0, 1.5, 2.0],
    "trail_atr": [2.5, 3.5],
    "take_profit_r": [0.0, 3.0],
    "min_room_r": [0.0, 2.0],
}


def grid_params(base: StrategyParams, grid: dict) -> list[StrategyParams]:
    keys = list(grid)
    return [replace(base, **dict(zip(keys, combo))) for combo in itertools.product(*grid.values())]


def _slice_score(eq: pd.Series, trades: list[Trade], a, b, min_trades: int) -> float:
    seg = eq[(eq.index >= a) & (eq.index < b)]
    n = sum(1 for t in trades if a <= t.opened < b)
    if len(seg) < 30 or n < min_trades:
        return -np.inf
    rets = seg.pct_change().dropna()
    sd = rets.std()
    return float(rets.mean() / sd * np.sqrt(365)) if sd > 0 else -np.inf


@dataclass
class WalkForwardResult:
    oos_equity: pd.Series
    selections: list[dict]
    oos_trades: list[Trade]
    runs: dict


def walk_forward(data: dict[str, pd.DataFrame], base: StrategyParams, r: RiskParams,
                 grid: dict | None = None, regime_symbol: str = "BTCUSDT",
                 train_days: int = 730, test_days: int = 182, min_trades: int = 8,
                 initial: float = 10_000.0) -> WalkForwardResult:
    """Each window picks the parameter set with the best in-sample Sharpe over the previous
    `train_days`, then is scored only on the following `test_days` it has never seen."""
    candidates = grid_params(base, grid or DEFAULT_GRID)
    runs = {cp: run_backtest(data, cp, r, regime_symbol, initial) for cp in candidates}
    idx = next(iter(runs.values())).equity.index
    first_test = idx[0] + pd.Timedelta(days=train_days + base.regime_ema)
    pieces = [pd.Series([initial], index=[first_test - pd.Timedelta(days=1)])]
    selections, oos_trades = [], []
    level = initial
    a = first_test
    while a < idx[-1]:
        b = a + pd.Timedelta(days=test_days)
        best = max(candidates, key=lambda cp: _slice_score(runs[cp].equity, runs[cp].trades,
                                                           a - pd.Timedelta(days=train_days), a,
                                                           min_trades))
        eq = runs[best].equity
        seg = eq[(eq.index >= a - pd.Timedelta(days=1)) & (eq.index < b)]
        if len(seg) > 1:
            growth = seg / seg.iloc[0]
            piece = level * growth.iloc[1:]
            pieces.append(piece)
            level = float(piece.iloc[-1])
        oos_trades += [t for t in runs[best].trades if a <= t.opened < b]
        selections.append({"test_start": str(a.date()), "test_end": str(min(b, idx[-1]).date()),
                           **{k: getattr(best, k) for k in (grid or DEFAULT_GRID)}})
        a = b
    oos = pd.concat(pieces) if pieces else pd.Series(dtype=float)
    return WalkForwardResult(oos, selections, oos_trades, runs)
