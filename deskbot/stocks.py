"""Stocks desk (paper): the S&P 500 with a trend filter, checked once a month. No account, no orders.

Rules (the same code runs the backtest and the paper desk):
  Scout     on the last trading day of the month: is the S&P 500 ETF (SPY) above its 200-day average?
  Executor  yes: the whole balance in SPY. No: the shelter, gold (GLD) or long US bonds (TLT),
            whichever has the best 6-month return among those above their own 200-day average,
            else cash. Trades at the next day's open; fee + slippage 0.05% per side. No leverage, no shorts.
  Risk      the trend filter is the exit; there is no fixed stop (a 12% stop made the backtest worse).

Chosen out of 5 known variants (sector rotation with/without stop, global momentum top 1 / top 2,
this one) because it was the steadiest in both halves of 2008-2026: about 9.6%/year with a -26%
worst fall, against 11.2%/year and -52% for simply holding SPY.
Prices in dollars from Yahoo Finance (public chart API), cached in data/stocks/. The balance is shown
in euros at the starting EUR/USD rate, so currency moves are left out.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests

from .config import DATA_DIR
from .paperdesk import Ledger, now_iso

log = logging.getLogger(__name__)
CACHE = DATA_DIR / "stocks"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
HEADERS = {"User-Agent": "Mozilla/5.0 (ai-desk-bot paper)"}
RISK = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Small caps EUA", "EFA": "Europa e Japão",
        "EEM": "Emergentes", "XLK": "Tecnologia", "XLF": "Financeiras", "XLV": "Saúde", "XLE": "Energia",
        "XLI": "Indústria", "XLY": "Consumo", "XLP": "Consumo básico", "XLU": "Utilities"}
SHELTER = {"GLD": "Ouro", "TLT": "Obrigações EUA 20 anos"}
NAMES = {**RISK, **SHELTER}


@dataclass(frozen=True)
class StockParams:
    lookback: int = 126
    trend_sma: int = 200
    top: int = 1
    stop: float = 0.0             # 0 = no stop
    cost: float = 0.0005
    universe: tuple = ("SPY",)


def history(symbol: str, refresh: bool = True) -> pd.DataFrame:
    """Daily OHLC adjusted for dividends and splits."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    params = {"period1": 946684800, "period2": int(time.time()), "interval": "1d", "events": "div,split"}
    for attempt in range(3):
        try:
            r = requests.get(YAHOO + symbol, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            adj = res["indicators"]["adjclose"][0]["adjclose"]
            df = pd.DataFrame({"open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"],
                               "adj": adj}, index=pd.to_datetime(res["timestamp"], unit="s").normalize())
            df = df.dropna()
            f = df["adj"] / df["close"]
            for c in ("open", "high", "low", "close"):
                df[c] = df[c] * f
            df = df.drop(columns="adj")
            df = df[~df.index.duplicated(keep="last")]
            df.to_csv(path)
            return df
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            if attempt == 2:
                if path.exists():
                    log.warning("Yahoo %s failed (%s): using the cache", symbol, exc)
                    return pd.read_csv(path, index_col=0, parse_dates=True)
                raise
            time.sleep(2 * (attempt + 1))


def load(refresh: bool = True) -> dict[str, pd.DataFrame]:
    return {s: history(s, refresh) for s in NAMES}


def ranking(closes: pd.DataFrame, i: int, p: StockParams) -> list[str]:
    """What the desk wants to hold after the close of day i."""
    window = closes.iloc[: i + 1]
    if len(window) < max(p.lookback, p.trend_sma) + 1:
        return []
    last = window.iloc[-1]
    sma = window.iloc[-p.trend_sma:].mean()
    mom = last / window.iloc[-p.lookback - 1] - 1
    ok = [s for s in p.universe if s in last and np.isfinite(last[s]) and np.isfinite(sma[s]) and last[s] > sma[s]]
    picks = sorted(ok, key=lambda s: -mom[s])[: p.top]
    shelter = sorted([s for s in SHELTER if np.isfinite(last.get(s, np.nan)) and last[s] > sma[s]],
                     key=lambda s: -mom[s])
    while len(picks) < p.top:
        picks.append(shelter[0] if shelter else "CASH")
    return picks


def month_ends(index: pd.DatetimeIndex) -> set[int]:
    s = pd.Series(range(len(index)), index=index)
    return set(s.groupby([index.year, index.month]).max().tolist())


def backtest(data: dict[str, pd.DataFrame], p: StockParams = StockParams(), initial: float = 10_000.0,
             start: str = "2008-01-01") -> dict:
    closes = pd.DataFrame({s: d["close"] for s, d in data.items()}).sort_index().ffill()
    opens = pd.DataFrame({s: d["open"] for s, d in data.items()}).reindex(closes.index).ffill()
    lows = pd.DataFrame({s: d["low"] for s, d in data.items()}).reindex(closes.index).ffill()
    ends = month_ends(closes.index)
    cash, hold, trades, equity, dates = initial, {}, [], [], []
    target: list[str] | None = None
    for i, day in enumerate(closes.index):
        if target is not None:  # rebalance at today's open
            for s in list(hold):
                px = opens[s].iloc[i] * (1 - p.cost)
                q = hold.pop(s)
                cash += q["qty"] * px
                trades.append({"symbol": s, "pnl": q["qty"] * (px - q["entry"]), "why": "rebalance"})
            per = cash / p.top
            for s, n in Counter(target).items():   # the shelter can take more than one slot
                if s == "CASH":
                    continue
                px = opens[s].iloc[i] * (1 + p.cost)
                qty = per * n / px
                cash -= qty * px
                hold[s] = {"qty": qty, "entry": px}
            target = None
        for s, q in list(hold.items()):  # 12% stop
            stop = q["entry"] * (1 - p.stop)
            if p.stop > 0 and lows[s].iloc[i] <= stop:
                px = min(stop, opens[s].iloc[i]) * (1 - p.cost)
                cash += q["qty"] * px
                hold.pop(s)
                trades.append({"symbol": s, "pnl": q["qty"] * (px - q["entry"]), "why": "stop"})
        if day >= pd.Timestamp(start):
            equity.append(cash + sum(q["qty"] * closes[s].iloc[i] for s, q in hold.items()))
            dates.append(day)
            if i in ends:
                target = ranking(closes, i, p)
        elif i in ends and closes.index[min(i + 25, len(closes) - 1)] >= pd.Timestamp(start):
            target = ranking(closes, i, p)
    return {"equity": pd.Series(equity, index=dates), "trades": trades}


def stats(eq: pd.Series, trades: list | None = None) -> dict:
    rets = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    total = eq.iloc[-1] / eq.iloc[0] - 1
    sd = rets.std()
    out = {"cagr": (1 + total) ** (1 / years) - 1, "max_drawdown": float((eq / eq.cummax() - 1).min()),
           "sharpe": float(rets.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0, "total": float(total)}
    if trades is not None:
        pnl = np.array([t["pnl"] for t in trades])
        out.update(trades=len(trades), win_rate=float((pnl > 0).mean()) if len(pnl) else 0.0,
                   stops=sum(1 for t in trades if t["why"] == "stop"))
    return out


# ------------------------------------------------------------------ paper desk
RULES = [
    "No último dia de cada mês vê se o S&P 500 (SPY) está acima da média de 200 dias",
    "Se está, o saldo todo fica no SPY (ações das 500 maiores empresas dos EUA, com dividendos)",
    "Se não está, passa para o refúgio: ouro (GLD) ou obrigações dos EUA (TLT), o que estiver em tendência; senão cash",
    "Sem stop fixo: a saída é o filtro de tendência (um stop de 12% piorou o backtest)",
    "Sem alavancagem e sem shorts; custos de 0,05% por lado; o saldo aparece em € ao câmbio do início",
]


class StocksDesk:
    def __init__(self, p: StockParams = StockParams()):
        self.p = p
        self.L = Ledger.load("acoes")
        self.L.extra["rules"] = RULES

    def equity(self, last: dict) -> float:
        return self.L.cash + sum(q["qty"] * last.get(q["symbol"], q["last"]) for q in self.L.positions)

    def _sell(self, q: dict, price: float, why: str):
        px = price * (1 - self.p.cost)
        self.L.cash += q["qty"] * px
        self.L.positions.remove(q)
        pnl = q["qty"] * (px - q["entry"])
        self.L.trades.append({"symbol": q["symbol"], "side": 1, "opened": q["opened"], "closed": now_iso(),
                              "pnl_usd": pnl, "pnl_eur": pnl / self.L.eur_usd, "pct": px / q["entry"] - 1, "why": why})
        self.L.event("Risk" if why == "stop" else "Executor", q["symbol"], "sell",
                     f"vendeu ({why}): {'+' if pnl >= 0 else '-'}{abs(pnl) / self.L.eur_usd:,.2f} €".replace(",", "X").replace(".", ",").replace("X", " "))

    def step(self, refresh: bool = True):
        p, L = self.p, self.L
        data = load(refresh)
        closes = pd.DataFrame({s: d["close"] for s, d in data.items()}).sort_index().ffill()
        last = {s: float(closes[s].iloc[-1]) for s in closes}
        L.ticker = [{"symbol": s, "pct": float(closes[s].iloc[-1] / closes[s].iloc[-22] - 1), "price": last[s]}
                    for s in NAMES if s in closes and len(closes[s].dropna()) > 22]
        for q in list(L.positions):  # Risk: 12% stop on the last close
            q["last"] = last.get(q["symbol"], q["last"])
            if p.stop > 0 and q["last"] <= q["entry"] * (1 - p.stop):
                self._sell(q, q["last"], "stop")
        i = len(closes) - 1
        bar = str(closes.index[-1].date())
        month = bar[:7]
        due = L.extra.get("month") != month and (closes.index[-1].day >= 1)
        # rebalance once a month (first cycle of a new month, or the very first run)
        if due and not L.paused:
            want = ranking(closes, i, p)
            L.extra["month"] = month
            L.extra["ranking"] = want
            L.event("Scout", "ranking", "pass", "escolha do mês: " + ", ".join(want))
            for q in list(L.positions):
                if q["symbol"] not in want:
                    self._sell(q, last[q["symbol"]], "rebalance")
            have = {q["symbol"] for q in L.positions}
            eq = self.equity(last)
            per = eq / p.top
            for s, n in Counter(want).items():   # the shelter can take more than one slot
                if s == "CASH" or s in have:
                    continue
                px = last[s] * (1 + p.cost)
                amount = min(per * n, L.cash)
                if amount < 10:
                    continue
                L.cash -= amount
                L.positions.append({"symbol": s, "side": 1, "qty": amount / px, "entry": px, "last": last[s],
                                    "opened": now_iso(), "name": NAMES.get(s, s), "stop": px * (1 - p.stop)})
                L.event("Executor", s, "buy", f"comprou {NAMES.get(s, s)}: {amount / L.eur_usd:,.2f} €".replace(",", "X").replace(".", ",").replace("X", " "))
        elif due and L.paused:
            L.event("Scout", "ranking", "skip", "rebalance adiado: mesa em pausa")
        L.last_bar = bar
        L.save(self.equity(last))
        return L


def run_cycle(refresh: bool = True):
    try:
        return StocksDesk().step(refresh)
    except Exception:
        log.exception("stocks desk cycle failed")
        return None
