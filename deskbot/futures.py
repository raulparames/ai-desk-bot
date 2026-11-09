"""Futures desk (paper): crypto perpetuals, long AND short, 4-hour candles. No account, no orders.

Rules (the same code runs the backtest and the paper desk):
  Scout     long when the 4h close breaks the 20-bar high and EMA50 > EMA200;
            short when it breaks the 20-bar low and EMA50 < EMA200.
  Charts    stop 2.5 ATR from the entry, trailing 3 ATR from the best close since entry.
  Executor  risk 1% of the balance per trade; leverage cap 10x (one position and all together);
            at most 3 positions. Fills at the next candle's open. With a 2.5 ATR stop the 1% risk rule
            usually means well under 1x on BTC: the cap only bites when the stop is very tight.
  Liquidation  if the balance falls under the maintenance margin (0.5% of the open notional),
            everything is closed, like an exchange would.
  Risk      stop and trailing checked on every candle (and every 15 min on the live price); an opposite
            signal closes the position; pays or receives funding every 8 h; fee 0.05% + slippage 0.02%.
Data: Binance USD-M futures public API (klines, funding history), cached in data/futures/.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests

from .config import DATA_DIR
from .indicators import atr, ema
from .paperdesk import Ledger, now_iso

log = logging.getLogger(__name__)
FAPI = "https://fapi.binance.com/fapi/v1"
CACHE = DATA_DIR / "futures"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
BAR_H = 4


@dataclass(frozen=True)
class FutParams:
    breakout: int = 20
    fast_ema: int = 50
    slow_ema: int = 200
    stop_atr: float = 2.5
    trail_atr: float = 3.0
    risk_per_trade: float = 0.01
    max_pos_lev: float = 10.0     # one position: notional <= 10x equity
    max_total_lev: float = 10.0   # all positions together: notional <= 10x equity
    maint_margin: float = 0.005   # liquidation when equity < 0.5% of the open notional
    max_positions: int = 3
    fee: float = 0.0005
    slippage: float = 0.0002


# ------------------------------------------------------------------ data
def _get(path: str, params: dict):
    for attempt in range(3):
        try:
            r = requests.get(FAPI + path, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def klines(symbol: str, start: str = "2020-01-01", refresh: bool = True) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol}_4h.csv"
    df = pd.read_csv(path, index_col=0, parse_dates=True) if path.exists() else pd.DataFrame()
    if df.empty and os.getenv("DESKBOT_SHORT_HISTORY"):   # live desk on a fresh machine: ~220 days is enough
        start = str((pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=220)).date())
    if refresh or df.empty:
        since = int((df.index[-1] if not df.empty else pd.Timestamp(start)).timestamp() * 1000)
        rows = []
        while True:
            batch = _get("/klines", {"symbol": symbol, "interval": f"{BAR_H}h", "startTime": since, "limit": 1500})
            if not batch:
                break
            rows += batch
            if len(batch) < 1500:
                break
            since = batch[-1][0] + 1
        if rows:
            new = pd.DataFrame([r[:6] for r in rows], columns=["t", "open", "high", "low", "close", "volume"])
            new.index = pd.to_datetime(new.pop("t"), unit="ms")
            new = new.astype(float)
            df = pd.concat([df, new])
            df = df[~df.index.duplicated(keep="last")].sort_index()
        now = pd.Timestamp.utcnow().tz_localize(None)
        df = df[df.index + pd.Timedelta(hours=BAR_H) <= now]  # completed candles only
        df.to_csv(path)
    return df


def funding(symbol: str, start: str = "2020-01-01", refresh: bool = True) -> pd.Series:
    path = CACHE / f"{symbol}_funding.csv"
    s = pd.read_csv(path, index_col=0, parse_dates=True)["rate"] if path.exists() else pd.Series(dtype=float)
    if refresh or s.empty:
        since = int((s.index[-1] if not s.empty else pd.Timestamp(start)).timestamp() * 1000) + 1
        rows = []
        while True:
            batch = _get("/fundingRate", {"symbol": symbol, "startTime": since, "limit": 1000})
            if not batch:
                break
            rows += batch
            if len(batch) < 1000:
                break
            since = batch[-1]["fundingTime"] + 1
        if rows:
            new = pd.Series({pd.to_datetime(r["fundingTime"], unit="ms"): float(r["fundingRate"]) for r in rows})
            s = pd.concat([s, new])
            s = s[~s.index.duplicated(keep="last")].sort_index()
        s.rename("rate").to_csv(path)
    return s


def load(refresh: bool = True) -> dict[str, tuple[pd.DataFrame, pd.Series]]:
    return {s: (klines(s, refresh=refresh), funding(s, refresh=refresh)) for s in SYMBOLS}


# ------------------------------------------------------------------ signals
def signals(df: pd.DataFrame, p: FutParams) -> pd.DataFrame:
    out = df.copy()
    out["atr"] = atr(out, 14)
    out["ema_f"] = ema(out["close"], p.fast_ema)
    out["ema_s"] = ema(out["close"], p.slow_ema)
    out["hi"] = out["high"].rolling(p.breakout).max().shift(1)
    out["lo"] = out["low"].rolling(p.breakout).min().shift(1)
    up = out["ema_f"] > out["ema_s"]
    out["long"] = (out["close"] > out["hi"]) & up & out["atr"].notna()
    out["short"] = (out["close"] < out["lo"]) & ~up & out["ema_s"].notna() & out["atr"].notna()
    return out


# ------------------------------------------------------------------ backtest
def backtest(data: dict, p: FutParams = FutParams(), initial: float = 10_000.0, start: str | None = None) -> dict:
    frames = {s: signals(df, p) for s, (df, _) in data.items()}
    fund = {s: f for s, (_, f) in data.items()}
    idx = sorted(set().union(*[f.index for f in frames.values()]))
    if start:
        idx = [t for t in idx if t >= pd.Timestamp(start)]
    cols = {s: frames[s].reindex(idx) for s in frames}
    cash = initial
    pos: dict[str, dict] = {}
    pending: list[tuple[str, int]] = []
    trades, equity = [], []
    last = {s: np.nan for s in frames}
    funding_paid = 0.0

    def eq_now(prices):
        return cash + sum(q["qty"] * q["side"] * (prices.get(s, q["entry"]) - q["entry"]) for s, q in pos.items())

    def close(s, price, when, why):
        nonlocal cash
        q = pos.pop(s)
        px = price * (1 - q["side"] * p.slippage)
        pnl = q["qty"] * q["side"] * (px - q["entry"]) - q["qty"] * px * p.fee
        cash += pnl
        trades.append({"symbol": s, "side": q["side"], "opened": q["opened"], "closed": when, "pnl": pnl - q["fees_in"],
                       "r": (pnl - q["fees_in"]) / q["risk"] if q["risk"] else 0, "why": why})

    for i, t in enumerate(idx):
        row = {s: cols[s].iloc[i] for s in cols}
        opens = {s: r["open"] for s, r in row.items() if np.isfinite(r["open"])}
        # stops that gapped at the open
        for s, q in list(pos.items()):
            o = opens.get(s)
            if o is not None and (o - q["stop"]) * q["side"] <= 0:
                close(s, o, t, "stop (gap)")
        # entries from the previous candle's signals
        if pending:
            eq = eq_now(opens)
            for s, side in pending:
                if s in pos or len(pos) >= p.max_positions or s not in opens:
                    continue
                a = cols[s]["atr"].iloc[i - 1]
                entry = opens[s] * (1 + side * p.slippage)
                dist = p.stop_atr * a
                if not np.isfinite(dist) or dist <= 0:
                    continue
                qty = eq * p.risk_per_trade / dist
                used = sum(q["qty"] * opens.get(k, q["entry"]) for k, q in pos.items())
                qty = min(qty, eq * p.max_pos_lev / entry, max(0.0, eq * p.max_total_lev - used) / entry)
                if qty * entry < 10:
                    continue
                fee_in = qty * entry * p.fee
                cash -= fee_in
                pos[s] = {"side": side, "qty": qty, "entry": entry, "stop": entry - side * dist, "best": entry,
                          "opened": t, "risk": qty * dist, "fees_in": fee_in}
            pending = []
        # intrabar stops (stop assumed hit before anything good)
        for s, q in list(pos.items()):
            r = row[s]
            if not np.isfinite(r["low"]):
                continue
            if (q["side"] == 1 and r["low"] <= q["stop"]) or (q["side"] == -1 and r["high"] >= q["stop"]):
                close(s, q["stop"], t, "stop")
        # funding paid at 00/08/16 UTC, inside this candle
        for s, q in pos.items():
            f = fund[s]
            if f.empty:
                continue
            hit = f[(f.index > t - pd.Timedelta(hours=BAR_H)) & (f.index <= t)]
            if len(hit):
                c = row[s]["close"] if np.isfinite(row[s]["close"]) else q["entry"]
                paid = float(hit.sum()) * q["qty"] * c * q["side"]
                cash -= paid
                funding_paid += paid
        # at the close: trail, opposite signals, queue new entries
        for s in frames:
            if np.isfinite(row[s]["close"]):
                last[s] = row[s]["close"]
        notional = sum(q["qty"] * last[s] for s, q in pos.items())
        if pos and eq_now(last) < p.maint_margin * notional:   # liquidated, like an exchange would
            for s in list(pos):
                close(s, last[s], t, "liquidação")
        for s, q in list(pos.items()):
            r = row[s]
            if not np.isfinite(r["close"]):
                continue
            q["best"] = max(q["best"], r["close"]) if q["side"] == 1 else min(q["best"], r["close"])
            trail = q["best"] - q["side"] * p.trail_atr * r["atr"]
            q["stop"] = max(q["stop"], trail) if q["side"] == 1 else min(q["stop"], trail)
            if (q["side"] == 1 and r["short"]) or (q["side"] == -1 and r["long"]):
                close(s, r["close"], t, "sinal contrário")
        equity.append(eq_now(last))
        cand = [(s, 1) for s in frames if row[s]["long"] and s not in pos] + \
               [(s, -1) for s in frames if row[s]["short"] and s not in pos]
        pending = cand
    for s in list(pos):
        close(s, last[s], idx[-1], "fim")
    eq = pd.Series(equity, index=idx)
    return {"equity": eq, "trades": trades, "funding_paid": funding_paid}


def stats(eq: pd.Series, trades: list | None = None) -> dict:
    eq = eq.dropna()
    rets = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    total = eq.iloc[-1] / eq.iloc[0] - 1
    per_year = 365 * 24 / BAR_H
    sd = rets.std()
    out = {"cagr": (1 + total) ** (1 / years) - 1 if total > -1 else -1, "max_drawdown": float((eq / eq.cummax() - 1).min()),
           "sharpe": float(rets.mean() / sd * np.sqrt(per_year)) if sd > 0 else 0.0, "total": float(total)}
    if trades is not None:
        pnl = np.array([t["pnl"] for t in trades])
        out.update(trades=len(trades), win_rate=float((pnl > 0).mean()) if len(pnl) else 0.0,
                   longs=sum(1 for t in trades if t["side"] == 1), shorts=sum(1 for t in trades if t["side"] == -1))
    return out


# ------------------------------------------------------------------ paper desk
RULES = [
    "Long quando a vela de 4 h fecha acima do máximo de 20 velas e a EMA50 está acima da EMA200",
    "Short quando fecha abaixo do mínimo de 20 velas e a EMA50 está abaixo da EMA200",
    "Stop a 2,5 ATR da entrada; trailing a 3 ATR do melhor fecho; um sinal contrário fecha a posição",
    "Risco de 1% do saldo por trade; alavancagem máxima 10x (uma posição e todas juntas); liquidação abaixo de 0,5% de margem",
    "No máximo 3 posições; paga ou recebe funding a cada 8 h; taxa 0,05% + slippage 0,02%",
]


class FuturesDesk:
    """One paper cycle: run it every 15 minutes (it is part of `python -m deskbot run`)."""

    def __init__(self, p: FutParams = FutParams()):
        self.p = p
        self.L = Ledger.load("futuros")
        self.L.extra["rules"] = RULES

    def prices(self) -> dict[str, float]:
        out = {}
        try:
            for item in _get("/premiumIndex", {}):
                if item["symbol"] in SYMBOLS:
                    out[item["symbol"]] = float(item["markPrice"])
                    self.L.extra.setdefault("funding_now", {})[item["symbol"]] = float(item["lastFundingRate"])
        except (requests.RequestException, KeyError, ValueError) as exc:
            log.warning("futures prices failed: %s", exc)
        return out

    def equity(self, prices: dict) -> float:
        return self.L.cash + sum(q["qty"] * q["side"] * (prices.get(q["symbol"], q["last"]) - q["entry"])
                                 for q in self.L.positions)

    def _close(self, q: dict, price: float, why: str):
        p = self.p
        px = price * (1 - q["side"] * p.slippage)
        pnl = q["qty"] * q["side"] * (px - q["entry"]) - q["qty"] * px * p.fee
        self.L.cash += pnl
        self.L.positions.remove(q)
        total = pnl - q["fees_in"] - q.get("funding", 0.0)
        t = {"symbol": q["symbol"], "side": q["side"], "opened": q["opened"], "closed": now_iso(),
             "pnl_usd": total, "pnl_eur": total / self.L.eur_usd, "pct": total / (q["qty"] * q["entry"]),
             "why": why}
        self.L.trades.append(t)
        side = "long" if q["side"] == 1 else "short"
        self.L.event("Risk", q["symbol"].replace("USDT", ""), "sell",
                     f"fechou {side} ({why}): {'+' if total >= 0 else '-'}{abs(total) / self.L.eur_usd:,.2f} €".replace(",", "X").replace(".", ",").replace("X", " "))

    def step(self, refresh: bool = True):
        p, L = self.p, self.L
        prices = self.prices()
        L.ticker = []
        # Risk on the live price, every cycle
        for q in list(L.positions):
            px = prices.get(q["symbol"])
            if px is None:
                continue
            q["last"] = px
            if (px - q["stop"]) * q["side"] <= 0:
                self._close(q, px, "stop")
        # liquidation, like an exchange: balance under the maintenance margin closes everything
        notional = sum(q["qty"] * prices.get(q["symbol"], q["last"]) for q in L.positions)
        if L.positions and self.equity(prices) < p.maint_margin * notional:
            for q in list(L.positions):
                self._close(q, prices.get(q["symbol"], q["last"]), "liquidação")
        # new 4h candle: funding, trailing, signals
        data = {s: klines(s, refresh=refresh) for s in SYMBOLS}
        bar = str(max(df.index[-1] for df in data.values()))
        for s, df in data.items():
            if len(df) > 6:
                L.ticker.append({"symbol": s.replace("USDT", ""), "pct": float(df["close"].iloc[-1] / df["close"].iloc[-7] - 1),
                                 "price": prices.get(s, float(df["close"].iloc[-1]))})
        if bar != L.last_bar:
            sig = {s: signals(df, p) for s, df in data.items()}
            for q in list(L.positions):
                r = sig[q["symbol"]].iloc[-1]
                rate = L.extra.get("funding_now", {}).get(q["symbol"], 0.0)
                paid = rate * q["qty"] * r["close"] * q["side"] * (BAR_H / 8)   # approx: half a funding period per candle
                L.cash -= paid
                q["funding"] = q.get("funding", 0.0) + paid
                q["best"] = max(q["best"], r["close"]) if q["side"] == 1 else min(q["best"], r["close"])
                trail = q["best"] - q["side"] * p.trail_atr * r["atr"]
                q["stop"] = max(q["stop"], trail) if q["side"] == 1 else min(q["stop"], trail)
                if (q["side"] == 1 and r["short"]) or (q["side"] == -1 and r["long"]):
                    self._close(q, prices.get(q["symbol"], r["close"]), "sinal contrário")
            held = {q["symbol"] for q in L.positions}
            for s, f in sig.items():
                r = f.iloc[-1]
                side = 1 if r["long"] else -1 if r["short"] else 0
                coin = s.replace("USDT", "")
                if not side or s in held:
                    continue
                if L.paused:
                    L.event("Scout", coin, "skip", "sinal ignorado: mesa em pausa")
                    continue
                if len(L.positions) >= p.max_positions:
                    L.event("Executor", coin, "skip", f"cheio: {p.max_positions} posições")
                    continue
                px = prices.get(s, float(r["close"]))
                eq = self.equity(prices)
                dist = p.stop_atr * float(r["atr"])
                entry = px * (1 + side * p.slippage)
                qty = eq * p.risk_per_trade / dist
                used = sum(q["qty"] * prices.get(q["symbol"], q["last"]) for q in L.positions)
                qty = min(qty, eq * p.max_pos_lev / entry, max(0.0, eq * p.max_total_lev - used) / entry)
                if qty * entry < 10:
                    L.event("Executor", coin, "skip", "tamanho abaixo do mínimo")
                    continue
                fee = qty * entry * p.fee
                L.cash -= fee
                lev = qty * entry / eq
                L.positions.append({"symbol": s, "side": side, "qty": qty, "entry": entry, "stop": entry - side * dist,
                                    "best": entry, "last": px, "opened": now_iso(), "fees_in": fee, "lev": lev,
                                    "liq": entry * (1 - side / max(lev, 1e-9) * 0.95)})
                L.event("Executor", coin, "buy", f"abriu {'long' if side == 1 else 'short'} {lev:.2f}x, stop a "
                        f"{dist / entry:.1%}".replace(".", ","))
            L.last_bar = bar
        L.save(self.equity(prices))
        return L


def run_cycle(refresh: bool = True):
    try:
        return FuturesDesk().step(refresh)
    except Exception:
        log.exception("futures desk cycle failed")
        return None
