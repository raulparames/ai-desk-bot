"""The live paper-trading loop.

Every `check_minutes`: close positions whose stop or target was hit.
Once per new daily candle (shortly after 00:00 UTC): trail stops, find signals,
let the desk discuss each one, open the approved trades.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import pandas as pd

from . import auditor, data, notify
from .brokers import BrokerError
from .config import STATE_FILE, Settings, load_controls
from .desk import BUY, Desk, Verdict, px
from .indicators import ema
from .journal import Journal
from .llm import LLM
from .strategy import (compute_signals, is_dead_money, make_plan, regime_series, stop_distance,
                       target_price, trail_active)

log = logging.getLogger(__name__)
MIN_NOTIONAL = 10.0


@dataclass
class OpenPosition:
    id: str
    sleeve: str
    symbol: str
    qty: float
    entry: float
    stop: float
    risk_per_unit: float
    target: float
    highest_close: float
    opened: str
    trail_on: bool = False


@dataclass
class BotState:
    positions: list[OpenPosition] = field(default_factory=list)
    peak_equity: float = 0.0
    last_bar: str = ""

    @classmethod
    def load(cls) -> "BotState":
        if not STATE_FILE.exists():
            return cls()
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return cls([OpenPosition(**p) for p in raw.get("positions", [])],
                   raw.get("peak_equity", 0.0), raw.get("last_bar", ""))

    def save(self):
        STATE_FILE.write_text(json.dumps({"positions": [asdict(p) for p in self.positions],
                                          "peak_equity": self.peak_equity, "last_bar": self.last_bar},
                                         indent=2), encoding="utf-8")


class Bot:
    def __init__(self, settings: Settings, broker, llm: LLM, journal: Journal):
        self.s = settings
        self.broker = broker
        self.llm = llm
        self.journal = journal
        self.desk = Desk(llm, settings.risk, settings.ai.news_web_search, settings.ai.boss_effort)
        self.state = BotState.load()
        self.sleeves = {sl.name: sl for sl in settings.sleeves}

    # ------------------------------------------------------------ helpers
    def _signals(self, frames: dict[str, pd.DataFrame]) -> dict[str, dict[str, pd.DataFrame]]:
        btc = frames[self.s.market.regime_symbol]
        out = {}
        for name, sl in self.sleeves.items():
            reg = regime_series(btc, sl.params)
            out[name] = {sym: compute_signals(df, reg, sl.params) for sym, df in frames.items()}
        return out

    def _held(self, sleeve: str, symbol: str) -> bool:
        return any(p.sleeve == sleeve and p.symbol == symbol for p in self.state.positions)

    def _count(self, sleeve: str) -> int:
        return sum(1 for p in self.state.positions if p.sleeve == sleeve)

    def _portfolio(self, prices) -> list[dict]:
        return [{"sleeve": p.sleeve, "symbol": p.symbol, "entry": p.entry, "stop": round(p.stop, 6),
                 "pnl_pct": round(prices.get(p.symbol, p.entry) / p.entry - 1, 4)}
                for p in self.state.positions]

    def account(self, prices=None) -> dict:
        prices = prices or data.last_prices(self.s.market.universe)
        acct = self.broker.account(prices)
        self.state.peak_equity = max(self.state.peak_equity, acct["equity"])
        acct["drawdown"] = acct["equity"] / self.state.peak_equity - 1 if self.state.peak_equity else 0.0
        acct["halted"] = acct["drawdown"] <= -self.s.risk.max_drawdown_halt
        return acct

    def _params(self, sleeve: str):
        sl = self.sleeves.get(sleeve)
        return sl.params if sl else self.s.strategy

    # ------------------------------------------------------------ exits
    def _close(self, pos: OpenPosition, price: float, reason: str) -> bool:
        try:
            fill = self.broker.sell(pos.symbol, pos.qty, price, f"{reason} [{pos.sleeve}]")
        except BrokerError as exc:
            log.error("Sell failed for %s: %s", pos.symbol, exc)
            return False
        self.state.positions.remove(pos)
        r_mult = (fill - pos.entry) / pos.risk_per_unit
        self.journal.order(pos.symbol, pos.sleeve, "sell", pos.qty, fill, f"{reason} {r_mult:+.2f}R")
        msg = (f"VENDA {pos.symbol} [{pos.sleeve}] {reason}: {pos.qty:.6g} @ {px(fill)} "
               f"({fill / pos.entry - 1:+.1%}, {r_mult:+.2f}R)")
        log.info(msg)
        notify.send(msg)
        return True

    def check_exits(self, prices=None) -> int:
        prices = prices or data.last_prices(self.s.market.universe)
        closed = 0
        for pos in list(self.state.positions):
            price = prices.get(pos.symbol)
            if price is None:
                continue
            reason = "stop" if price <= pos.stop else "alvo" if pos.target and price >= pos.target else None
            if reason and self._close(pos, price, reason):
                closed += 1
        self.state.save()
        return closed

    # ------------------------------------------------------------ daily
    def daily(self, force: bool = False) -> list[Verdict]:
        m = self.s.market
        frames = data.load_universe(m.universe, m.timeframe, m.history_start, refresh=True)
        last_bar = frames[m.regime_symbol].index[-1]
        bar = str(last_bar.date())
        if self.state.last_bar == bar and not force:
            log.info("Daily candle %s already processed", bar)
            return []
        prices = data.last_prices(m.universe)
        acct = self.account(prices)
        sigs = self._signals(frames)

        # Risk: trail stops on the new close (stops only ever move up) and close dead money
        for pos in list(self.state.positions):
            f = sigs.get(pos.sleeve, {}).get(pos.symbol)
            if f is None or last_bar not in f.index:
                continue
            row, p = f.loc[last_bar], self._params(pos.sleeve)
            close = float(row["close"])
            pos.highest_close = max(pos.highest_close, close)
            pos.trail_on = pos.trail_on or trail_active(pos.highest_close, pos.entry, pos.risk_per_unit, p)
            if pos.trail_on:
                pos.stop = max(pos.stop, pos.highest_close - p.trail_atr * float(row["atr"]))
            days = (last_bar.date() - date.fromisoformat(pos.opened)).days
            if is_dead_money(days, close, pos.entry, pos.risk_per_unit, p):
                self._close(pos, prices.get(pos.symbol, close), f"tempo ({days} dias)")

        # Auditor: record where each coin was cut today (for the dashboard's daily report)
        btc = frames[m.regime_symbol]
        scans = []
        for name, by_sym in sigs.items():
            p = self.sleeves[name].params
            gap = float(btc["close"].iloc[-1] / ema(btc["close"], p.regime_ema).iloc[-1] - 1)
            for sym, f in by_sym.items():
                if last_bar in f.index:
                    a = auditor.audit(f.loc[last_bar], p, gap)
                    fl = a["failed"] or {}
                    scans.append((name, sym, int(a["passed"]), fl.get("key"), fl.get("value")))
        self.journal.scans(bar, scans)

        cands: dict[str, list] = defaultdict(list)
        for name, by_sym in sigs.items():
            for sym, f in by_sym.items():
                if last_bar in f.index and bool(f.loc[last_bar, "signal"]) and not self._held(name, sym):
                    cands[sym].append(self.sleeves[name])
        order = sorted(cands, key=lambda s: -float(sigs[cands[s][0].name][s].loc[last_bar, "score"]))
        if order and load_controls()["entries_paused"]:
            log.info("Entries paused from the dashboard: %d signal(s) ignored (%s). Risk keeps guarding.",
                     len(order), ", ".join(order))
            order = []

        verdicts = []
        for sym in order:
            sleeves = [sl for sl in cands[sym] if self._count(sl.name) < self.s.risk.max_positions]
            if not sleeves or sym not in prices:
                continue
            row = sigs[sleeves[0].name][sym].loc[last_bar]
            plan = make_plan(sym, row, sleeves[0].params, entry=prices[sym])
            verdict = self.desk.discuss(plan, [sl.name for sl in sleeves], self._portfolio(prices),
                                        acct["halted"])
            verdicts.append(verdict)
            self.journal.decision(bar, verdict, [sl.name for sl in sleeves])
            notify.send(f"Mesa de trading {bar} - {sym}\n{verdict.chat_text()}")
            if verdict.decision == BUY:
                for sl in sleeves:
                    self._open(sl, sym, row, prices[sym], verdict.size_multiplier, acct["equity"])

        self.state.last_bar = bar
        self.state.save()
        acct = self.account(prices)
        self.journal.equity(acct["equity"], acct["cash"])
        log.info("Daily run %s: %d signals, equity %.2f, %d open positions, LLM calls %d (~$%.3f)",
                 bar, len(verdicts), acct["equity"], len(self.state.positions), self.llm.calls, self.llm.cost_usd)
        return verdicts

    def _open(self, sleeve, sym: str, row: pd.Series, price: float, mult: float, equity: float):
        p = sleeve.params
        dist = stop_distance(float(row["close"]), float(row["atr"]), p)
        sleeve_equity = equity / len(self.sleeves)
        qty = sleeve_equity * self.s.risk.risk_per_trade * mult / dist
        qty = min(qty, sleeve_equity * self.s.risk.max_position_pct / price)
        cash = self.broker.account({sym: price})["cash"]
        qty = min(qty, cash * 0.98 / price)
        if qty * price < MIN_NOTIONAL:
            log.info("Skip %s [%s]: size %.2f below minimum", sym, sleeve.name, qty * price)
            return
        try:
            fill = self.broker.buy(sym, qty, price, f"breakout [{sleeve.name}] stop {price - dist:.6g}")
        except BrokerError as exc:
            log.error("Buy failed for %s: %s", sym, exc)
            return
        pos = OpenPosition(uuid.uuid4().hex[:8], sleeve.name, sym, qty, fill, fill - dist, dist,
                           target_price(fill, dist, p), fill, str(data.utc_now().date()))
        self.state.positions.append(pos)
        self.state.save()
        self.journal.order(sym, sleeve.name, "buy", qty, fill, f"stop {pos.stop:.6g} alvo {pos.target:.6g}")
        msg = (f"COMPRA {sym} [{sleeve.name}] {qty:.6g} @ {px(fill)} | stop {px(pos.stop)}"
               + (f" | alvo {px(pos.target)}" if pos.target else " | trailing"))
        log.info(msg)
        notify.send(msg)

    # ------------------------------------------------------------ scheduling
    def daily_due(self) -> bool:
        now = data.utc_now()
        expected = str((now - timedelta(days=1)).date())
        after_open = now.hour > 0 or now.minute >= self.s.live.daily_run_utc_minute
        return after_open and self.state.last_bar < expected

    def run_once(self) -> list[Verdict]:
        prices = data.last_prices(self.s.market.universe)
        closed = self.check_exits(prices)
        verdicts = self.daily() if self.daily_due() else []
        try:  # one point per cycle for the equity chart
            acct = self.account(prices)
            self.journal.equity(acct["equity"], acct["cash"])
            self.state.save()
        except Exception as exc:  # the broker being down must not stop the stops
            log.warning("Equity snapshot skipped: %s", exc)
        log.info("Cycle done: %d closed, %d open positions, last daily candle %s",
                 closed, len(self.state.positions), self.state.last_bar or "none")
        return verdicts

    def loop(self):
        log.info("Loop started: checking every %d min. Ctrl+C to stop.", self.s.live.check_minutes)
        while True:
            try:
                self.run_once()
            except Exception:  # keep the loop alive; the error is in the log
                log.exception("Cycle failed")
            time.sleep(self.s.live.check_minutes * 60)
