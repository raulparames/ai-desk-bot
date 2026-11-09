"""Local dashboard: `python -m deskbot dashboard` -> http://127.0.0.1:8899

Views of the bot (account, agents, Scout radar with the Auditor's layers, AI desk discussions,
sleeves, parameters, activity, installed tools) and four actions, none of which sends orders:
run a demo desk discussion (a few Claude calls), pause/resume new entries, backtest a parameter
change and save it to config.toml. Binds to 127.0.0.1 only; POSTs must come from this page.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import auditor, data, desks_api, phone, settings_edit
from .backtest import buy_and_hold, metrics, run_backtest
from .config import ROOT, Settings, load_controls, load_settings, save_controls
from .desk import Desk
from .indicators import ema
from .journal import Journal
from .live import BotState
from .llm import LLM
from .strategy import QUALITY_QUESTIONS, compute_signals, make_plan, regime_series, stop_distance

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "dashboard_static"
TASK_NAMES = ("AI-Trader Server", "AI Desk Bot", "AI Desk Dashboard")
OOS_START = "2021-07-19"
REPOS = Path.home() / "ai-trading-repos"

AGENTS = [
    {"id": "Scout", "kind": "código", "role": "Procura moedas a romper o máximo de 20 ou 55 dias com volume forte.",
     "looks_at": "Velas diárias da Binance: máximo recente, volume vs média de 20 dias, EMA50, BTC vs EMA200.",
     "output": "Lista de candidatas e em que estratégias (sleeves) há sinal."},
    {"id": "Auditor", "kind": "código", "role": "Passa cada moeda por camadas fixas, por ordem. A primeira vermelha corta a moeda.",
     "looks_at": "Regime do BTC, tendência, breakout, volume, RSI, espaço até à resistência e os filtros opcionais.",
     "output": "Passou ou cortada, a camada que a cortou e o número que falhou. As camadas seguintes ficam por verificar."},
    {"id": "Charts", "kind": "código", "role": "Faz o plano do trade: entrada, stop, alvo e reward:risk. Dá a nota do Analyst.",
     "looks_at": "ATR (volatilidade), resistência do último ano, RSI, inclinação da EMA50.",
     "output": "Entrada ~ preço atual, stop a 2 ATR, alvo 3R ou trailing a 3,5 ATR, R:R e uma nota de 0 a 100."},
    {"id": "News", "kind": "IA", "role": "Explica porque é que a moeda se mexe e procura red flags.",
     "looks_at": "Manchetes de CoinDesk, Cointelegraph e Decrypt das últimas 72 h (e pesquisa web, se ligada).",
     "output": "Viés (alta/baixa/neutro), red flags e as fontes que usou."},
    {"id": "Sentimento", "kind": "IA", "role": "Lê a multidão: está toda a gente já comprada?",
     "looks_at": "Fear & Greed, funding rate dos futuros, variação do open interest em 7 dias.",
     "output": "Estado da multidão, longs lotados sim/não, score de -1 a +1."},
    {"id": "Cético", "kind": "IA + regras", "role": "Ataca o trade. As regras duras dele ninguém consegue anular.",
     "looks_at": "Tudo o que os outros disseram + regras: R:R ≥ 2, regime do BTC, kill switch.",
     "output": "A objeção mais forte, severidade e sugestão."},
    {"id": "Chefe", "kind": "IA", "role": "Decide COMPRA, ESPERA ou PASA. Só pode reduzir o tamanho, nunca aumentar.",
     "looks_at": "A discussão inteira e o histórico do sistema (41% de acerto, ~+1R por trade).",
     "output": "Decisão, multiplicador de tamanho (0,25 a 1,0), confiança e o que o faria mudar de ideia."},
    {"id": "Executor", "kind": "código", "role": "Calcula o tamanho e envia a ordem de papel. Nunca escolhe moedas.",
     "looks_at": "Capital da sleeve, risco por trade, teto por posição e se o preço fugiu desde o sinal.",
     "output": "Quantidade, valor e % da sleeve. Não compra se o preço já subiu mais do que o limite."},
    {"id": "Risk", "kind": "código", "role": "Guarda cada posição até à saída. Nunca baixa um stop.",
     "looks_at": "Preço a cada 15 minutos; fecho diário para o trailing e o tempo em posição.",
     "output": "Sai no stop, no alvo, no trailing ou por tempo (dinheiro parado)."},
]
LAYER_SHORT = {"regime": "Regime do BTC", "trend": "Tendência", "breakout": "Breakout", "volume": "Volume",
               "rsi": "Sobrecompra", "room": "Espaço", "chase": "Perseguição", "quality": "Nota do Analyst"}

TOOLS = [
    {"name": "AI Desk Bot", "what": "Este bot: mesa de IA + 4 estratégias de breakout em cripto, em paper trading.",
     "path": str(ROOT), "status": "a correr", "how": r".venv\Scripts\python.exe -m deskbot status"},
    {"name": "AI-Trader (HKUDS)", "what": "Plataforma de paper trading com $100K virtuais onde o bot envia as ordens.",
     "path": str(ROOT.parent / "AI-Trader"), "status": "a correr", "how": "http://localhost:8000"},
    {"name": "TradingAgents", "what": "12 agentes que imitam uma gestora: analistas, debate bull vs bear, risco e gestor.",
     "path": str(REPOS / "TradingAgents"), "status": "pronto",
     "how": r".venv\Scripts\tradingagents --ticker BTC-USD --date AAAA-MM-DD --analysts market"},
    {"name": "Vibe-Trading", "what": "Pedes uma ideia em português ou inglês e ele testa-a no histórico.",
     "path": str(Path.home() / ".local" / "bin" / "vibe-trading.exe"), "status": "pronto",
     "how": 'vibe-trading run -p "Backtest BTC above its 200-day average since 2020 vs holding"'},
    {"name": "Freqtrade", "what": "Motor de execução de cripto com backtests e dry-run (dinheiro fictício).",
     "path": str(REPOS / "freqtrade"), "status": "pronto (dry-run)",
     "how": r".venv\Scripts\freqtrade backtesting --config user_data\config.json --strategy SampleStrategy --timeframe 1h"},
    {"name": "daily_stock_analysis", "what": "Relatório diário de ações americanas no Telegram, corre no GitHub.",
     "path": "", "status": "no GitHub (dias úteis, 10:00 UTC)", "how": "Separador Actions do teu fork no GitHub"},
    {"name": "day-trader-skill", "what": "11 skills de day trading para o Claude Code: scanner, notícias, risco...",
     "path": str(REPOS / "day-trader-skill"), "status": "pronto (plugin)",
     "how": "/day-trader-skill:trader-scanner (numa sessão do Claude Code)"},
    {"name": "Anthropic financial-services", "what": "Comandos de analista: /dcf, /comps, /earnings, /screen...",
     "path": "", "status": "pronto (plugin)", "how": "/dcf NVDA (numa sessão do Claude Code)"},
    {"name": "ai-hedge-fund", "what": "Versões IA de Buffett, Munger, Graham, Lynch e Druckenmiller.",
     "path": str(Path.home() / ".local" / "bin" / "aihf.exe"), "status": "falta chave paga", "how": "aihf"},
    {"name": "LLM-Trading-Lab", "what": "Registo do ChatGPT a gerir $100 reais durante 6 meses: acabou em $84,48.",
     "path": str(REPOS / "LLM-Trading-Lab"), "status": "só leitura", "how": "Ler Experiments/chatgpt_micro-cap"},
]


def _clean(obj):
    """JSON-safe: NaN/inf -> None, numpy/pandas scalars -> python."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    return obj


def _ms(ts) -> int:
    return int(pd.Timestamp(ts).timestamp() * 1000)


def _pt(v: float, d: int = 2) -> str:
    """Portuguese number format: 99 990,59"""
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",")


def _g(v: float) -> str:
    return f"{v:g}".replace(".", ",")


def _systemd_tasks() -> list[dict]:
    """On the Linux server the scheduled tasks are systemd units; report them with the same names."""
    units = {"AI Desk Bot": "deskbot-cycle.timer", "AI Desk Dashboard": "deskbot-dashboard.service",
             "AI-Trader Server": None}
    out = []
    for name, unit in units.items():
        if unit is None:
            out.append({"name": name, "state": "missing"})
            continue
        try:
            r = subprocess.run(["systemctl", "show", unit, "-p", "ActiveState", "-p", "LastTriggerUSec"],
                               capture_output=True, text=True, timeout=10)
            info = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        except (OSError, subprocess.TimeoutExpired):
            info = {}
        active = info.get("ActiveState") == "active"
        state = ("Ready" if unit.endswith(".timer") else "Running") if active else "missing"
        item = {"name": name, "state": state, "last_result": 0 if active else None, "last_run": None, "next_run": None}
        if unit.endswith(".timer"):
            try:
                r = subprocess.run(["systemctl", "show", "deskbot-cycle.service", "-p", "ExecMainExitTimestamp",
                                    "-p", "ExecMainStatus"], capture_output=True, text=True, timeout=10)
                svc = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
                item["last_result"] = int(svc.get("ExecMainStatus") or 0)
                ts = svc.get("ExecMainExitTimestamp", "").strip()
                if ts:
                    item["last_run"] = pd.Timestamp(" ".join(ts.split()[1:3])).tz_localize("UTC").isoformat()
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
        out.append(item)
    return out


def _usd(v) -> str:
    if v is None:
        return "—"
    return f"{'+' if v > 0 else '-' if v < 0 else ''}${_pt(abs(v))}"


def _num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class App:
    def __init__(self, settings: Settings):
        self.s = settings
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self.guard = phone.LoginGuard()
        self._broker = None
        self.demo = {"running": False, "symbol": None, "started": None, "error": None, "result": None}

    def cached(self, key: str, ttl: float, fn):
        """TTL cache. Concurrent callers of the same key wait for one computation instead of repeating it."""
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > time.time():
                return hit[1]
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                hit = self._cache.get(key)
                if hit and hit[0] > time.time():
                    return hit[1]
            value = fn()
            with self._lock:
                self._cache[key] = (time.time() + ttl, value)
            return value

    def stale_ok(self, key: str, ttl: float, fn):
        """Like cached(), but an expired value is returned at once and refreshed in the background."""
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] <= time.time():
                self._cache[key] = (time.time() + ttl, hit[1])  # one refresh at a time
        if hit is None:
            return self.cached(key, ttl, fn)
        if hit[0] <= time.time():
            def refresh():
                try:
                    value = fn()
                except Exception:
                    log.exception("background refresh of %s failed", key)
                    return
                with self._lock:
                    self._cache[key] = (time.time() + ttl, value)
            threading.Thread(target=refresh, daemon=True).start()
        return hit[1]

    # ------------------------------------------------------------ sources
    def prices(self) -> dict[str, float]:
        return self.stale_ok("prices", 20, lambda: data.last_prices(self.s.market.universe))

    def frames(self) -> dict[str, pd.DataFrame]:
        m = self.s.market
        return self.cached("frames", 600, lambda: data.load_universe(m.universe, m.timeframe, m.history_start,
                                                                     refresh=False))

    def broker(self):
        if self._broker is None:
            from .brokers import make_broker
            self._broker = make_broker(self.s)
        return self._broker

    def tasks(self) -> list[dict]:
        """Last known Task Scheduler state; refreshed by a background thread (PowerShell takes seconds)."""
        with self._lock:
            known = self._cache.get("tasks")
            if known is None:  # first query still running in warm_up: answer now, fill in later
                return [{"name": n, "state": "checking"} for n in TASK_NAMES]
        if known[0] <= time.time():
            with self._lock:
                self._cache["tasks"] = (time.time() + 30, known[1])
            threading.Thread(target=self._query_tasks, daemon=True).start()
        return known[1]

    def reload(self):
        """Pick up a config.toml change: new settings, drop every cache built from the old ones."""
        self.s = load_settings()
        with self._lock:
            for k in ("signals", "radar", "strategies", "params_base"):
                self._cache.pop(k, None)

    def warm_up(self):
        """Fill the slow caches in the background so the first page load is quick."""
        for fn in (self._query_tasks, self.prices, self.radar, self.strategies):
            try:
                fn()
            except Exception:
                log.exception("dashboard warm-up step failed")

    def _query_tasks(self) -> list[dict]:
        if os.name != "nt":
            value = _systemd_tasks()
            with self._lock:
                self._cache["tasks"] = (time.time() + 30, value)
            return value

        def query():
            names = ",".join(f"'{n}'" for n in TASK_NAMES)
            ps = (f"Get-ScheduledTask -TaskName {names} -ErrorAction SilentlyContinue | ForEach-Object {{ "
                  "$i = $_ | Get-ScheduledTaskInfo; [pscustomobject]@{ name=$_.TaskName; state=[string]$_.State; "
                  "last_run=$(if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) {$i.LastRunTime.ToString('o')}); "
                  "last_result=$i.LastTaskResult; "
                  "next_run=$(if ($i.NextRunTime) {$i.NextRunTime.ToString('o')}) } } | ConvertTo-Json -Compress")
            try:
                out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True,
                                     text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                parsed = json.loads(out.stdout or "[]")
                parsed = parsed if isinstance(parsed, list) else [parsed]
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
                parsed = []
            found = {}
            for t in parsed:
                for k in ("last_run", "next_run"):  # PowerShell emits {} for an empty $( )
                    if not isinstance(t.get(k), str):
                        t[k] = None
                found[t["name"]] = t
            return [found.get(n, {"name": n, "state": "missing"}) for n in TASK_NAMES]
        value = query()
        with self._lock:
            self._cache["tasks"] = (time.time() + 30, value)
        return value

    def server_ok(self) -> bool:
        def ping():
            try:
                return requests.get(f"{self.s.broker.aitrader_url.rstrip('/')}/api/health", timeout=3).ok
            except requests.RequestException:
                return False
        return self.cached("server_ok", 15, ping)

    def log_tail(self, n: int = 150) -> list[str]:
        path = ROOT / "logs" / "bot.log"
        if not path.exists():
            return []
        with path.open(encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in fh.readlines()[-n:]]

    # ------------------------------------------------------------ views
    def overview(self) -> dict:
        prices = self.prices()
        state = BotState.load()
        acct, err = None, None
        if self.s.broker.kind == "aitrader" and not self.server_ok():
            err = "O servidor AI-Trader não responde."
        else:
            try:
                acct = self.cached("account", 20, lambda: self.broker().account(prices))
            except Exception as exc:  # broker errors are shown, never fatal
                err = str(exc)
        peak = max(state.peak_equity, acct["equity"] if acct else 0)
        dd = acct["equity"] / peak - 1 if acct and peak else 0.0
        positions = []
        params = {sl.name: sl.params for sl in self.s.sleeves}
        today = datetime.now(timezone.utc).date()
        for p in state.positions:
            now = prices.get(p.symbol, p.entry)
            sp = params.get(p.sleeve, self.s.strategy)
            days = (today - datetime.fromisoformat(p.opened).date()).days
            positions.append({"sleeve": p.sleeve, "symbol": p.symbol, "qty": p.qty, "entry": p.entry, "now": now,
                              "pnl_pct": now / p.entry - 1, "r_now": (now - p.entry) / p.risk_per_unit,
                              "stop": p.stop, "to_stop_pct": p.stop / now - 1, "target": p.target or None,
                              "opened": p.opened, "value": p.qty * now, "days_open": days,
                              "trail_on": p.trail_on or sp.trail_start_r <= 0, "trail_start_r": sp.trail_start_r,
                              "timeout_left": sp.timeout_days - days if sp.timeout_days else None,
                              "timeout_min_r": sp.timeout_min_r})
        cycle = None
        for line in reversed(self.log_tail(400)):
            if "Cycle done" in line or "Daily run" in line or "Loop started" in line:
                cycle = line[:19]
                break
        now = datetime.now(timezone.utc)
        nxt = now.replace(hour=0, minute=self.s.live.daily_run_utc_minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return {
            "now_utc": now.isoformat(), "account": acct, "account_error": err, "peak": peak, "drawdown": dd,
            "halted": dd <= -self.s.risk.max_drawdown_halt, "halt_at": self.s.risk.max_drawdown_halt,
            "positions": positions, "last_bar": state.last_bar, "last_cycle_local": cycle,
            "next_desk_utc": nxt.isoformat(), "server_ok": self.server_ok(), "tasks": self.tasks(),
            "broker": self.s.broker.kind, "ai": {"backend": self.s.ai.backend, "model": self.s.ai.model},
            "risk_per_trade": self.s.risk.risk_per_trade, "check_minutes": self.s.live.check_minutes,
            "controls": load_controls(), "agents": self._agent_cards(state),
        }

    def _agent_cards(self, state) -> dict:
        """What each code agent is doing right now (the overview's agent strip)."""
        try:
            rd = self.radar()
        except Exception:  # radar needs market data; the overview still loads without it
            log.exception("radar for agent cards failed")
            rd = {"rows": [], "report": {"counts": [], "passed": 0, "total": 0}}
        p, r = self.s.strategy, self.s.risk
        best = max(rd["rows"], key=lambda x: x["quality"] or 0, default=None)
        orders = Journal().orders(500)
        return {
            "scout": {"watching": len(self.s.market.universe),
                      "signals": sum(1 for x in rd["rows"] if any(sl["signal"] for sl in x["sleeves"]))},
            "auditor": {"passed": rd["report"]["passed"], "total": rd["report"]["total"],
                        "top": rd["report"]["counts"][0] if rd["report"]["counts"] else None},
            "analyst": {"coin": best["coin"] if best else None, "quality": best["quality"] if best else None,
                        "min_quality": p.min_quality},
            "executor": {"risk_per_trade": r.risk_per_trade, "max_position_pct": r.max_position_pct,
                         "buys": sum(1 for o in orders if o["side"] == "buy"), "gap": p.max_entry_gap_pct},
            "risk": {"open": len(state.positions), "stop_atr": p.stop_atr, "trail_atr": p.trail_atr,
                     "trail_start_r": p.trail_start_r, "timeout_days": p.timeout_days,
                     "timeout_min_r": p.timeout_min_r, "sells": sum(1 for o in orders if o["side"] == "sell")},
        }

    def radar(self) -> dict:
        return self.stale_ok("radar", 15, self._radar)

    def _radar(self) -> dict:
        def build():
            frames = self.frames()
            btc = frames[self.s.market.regime_symbol]
            # exit-only settings don't change the signals: sleeves that differ only there share the work
            exit_only = dict(take_profit_r=0.0, take_profit_pct=0.0, trail_atr=0.0, trail_start_r=0.0,
                             timeout_days=0, timeout_min_r=0.0, max_entry_gap_pct=0.0)
            done, sigs = {}, {}
            for sl in self.s.sleeves:
                key = replace(sl.params, **exit_only)
                if key not in done:
                    done[key] = {sym: compute_signals(df, regime_series(btc, sl.params), sl.params)
                                 for sym, df in frames.items()}
                sigs[sl.name] = done[key]
            return frames, sigs
        frames, sigs = self.stale_ok("signals", 600, build)
        prices = self.prices()
        first = self.s.sleeves[0]
        btc = frames[self.s.market.regime_symbol]["close"]
        btc_gap = float(btc.iloc[-1] / ema(btc, first.params.regime_ema).iloc[-1] - 1)
        acct = self._cache.get("account")
        equity = acct[1]["equity"] if acct else self.s.broker.paper_start_cash
        sleeve_eq = equity / len(self.s.sleeves)
        rows = []
        for sym in self.s.market.universe:
            f0 = sigs[first.name][sym]
            last = f0.iloc[-1]
            price = prices.get(sym, float(last["close"]))
            sleeves = []
            for sl in self.s.sleeves:
                r = sigs[sl.name][sym].iloc[-1]
                p = sl.params
                level = float(r["breakout_level"])
                a = auditor.audit(r, p, btc_gap)
                sleeves.append({
                    "name": sl.name, "lookback": p.breakout_lookback, "level": level, "dist_close": float(r["close"]) / level - 1,
                    "dist_now": price / level - 1, "signal": bool(r["signal"]), "audit": a,
                    "checks": {x["rule"]: x["status"] == auditor.OK for x in a["layers"] if x["status"] != auditor.OFF},
                    "plan": self._plan_preview(r, p, price, sleeve_eq),
                })
            tail = f0.tail(150)
            levels = {}
            for sl in self.s.sleeves:
                lb = sl.params.breakout_lookback
                if lb not in levels:
                    levels[lb] = [[_ms(t), v] for t, v in sigs[sl.name][sym]["breakout_level"].tail(150).items()]
            rows.append({
                "symbol": sym, "coin": sym.removesuffix("USDT"), "price": price, "close": float(last["close"]),
                "change_vs_close": price / float(last["close"]) - 1, "vol_ratio": float(last["vol_ratio"]),
                "rsi": float(last["rsi"]), "ema50": float(last["ema_trend"]), "atr": float(last["atr"]),
                "above_ema": bool(last["close"] > last["ema_trend"]), "regime_on": bool(last["regime_on"]),
                "bar_date": str(f0.index[-1].date()), "sleeves": sleeves,
                "quality": _num(last.get("quality")),
                "answers": {k: {"q": q, "v": _num(last.get(k))} for k, q in QUALITY_QUESTIONS.items()},
                "series": {"close": [[_ms(t), v] for t, v in tail["close"].items()],
                           "ema50": [[_ms(t), v] for t, v in tail["ema_trend"].items()],
                           "levels": {str(k): v for k, v in levels.items()}},
            })
        rows.sort(key=lambda r: -max(s["dist_close"] for s in r["sleeves"]))
        return {"rows": rows, "bar_date": rows[0]["bar_date"] if rows else None,
                "volume_mult": self.s.strategy.volume_mult, "btc_gap": btc_gap,
                "min_quality": self.s.strategy.min_quality,
                "report": self._cut_report(rows), "history": self._cut_history()}

    def _plan_preview(self, row, p, price: float, sleeve_eq: float) -> dict:
        """What the Charts/Executor/Risk agents would do if this coin were bought now."""
        dist = stop_distance(float(row["close"]), float(row["atr"]), p)
        r = self.s.risk
        value = min(sleeve_eq * r.risk_per_trade / dist * price, sleeve_eq * r.max_position_pct) if dist > 0 else 0
        return {"entry": price, "stop": price - dist, "stop_pct": -dist / price if price else None,
                "target": price + p.take_profit_r * dist if p.take_profit_r > 0 else None,
                "trail_from": price + p.trail_start_r * dist if p.trail_start_r > 0 else None,
                "timeout_days": p.timeout_days, "timeout_price": price + p.timeout_min_r * dist,
                "value": value, "value_pct": value / sleeve_eq if sleeve_eq else None,
                "capped": value >= sleeve_eq * r.max_position_pct - 1e-9, "risk_usd": value * dist / price if price else 0}

    def _cut_report(self, rows: list[dict]) -> dict:
        """Today's cut: for each coin, the layer that stopped its furthest-going breakout."""
        order = [x["key"] for x in rows[0]["sleeves"][0]["audit"]["layers"]] if rows else []
        counts: dict[str, list[str]] = {}
        passed = 0
        for r in rows:
            best = max((s["audit"] for s in r["sleeves"]),
                       key=lambda a: 99 if a["passed"] else order.index(a["failed"]["key"]))
            if best["passed"]:
                passed += 1
            else:
                counts.setdefault(best["failed"]["key"], []).append(r["coin"])
        out = [{"key": k, "name": LAYER_SHORT.get(k, k), "layer": order.index(k) + 1, "count": len(v), "coins": v}
               for k, v in counts.items()]
        out.sort(key=lambda x: (-x["count"], x["layer"]))
        return {"counts": out, "passed": passed, "total": len(rows)}

    def _cut_history(self) -> list[dict]:
        """Coins cut per layer per day, from what the live bot recorded (best sleeve per coin)."""
        try:
            raw = Journal().scan_history(30)
        except Exception:
            return []
        by_day: dict[str, dict[str, int]] = {}
        seen: dict[tuple[str, str], tuple[int, str | None]] = {}
        order = list(LAYER_SHORT)
        for r in raw:
            rank = 99 if r["passed"] else order.index(r["layer"]) if r["layer"] in order else 0
            k = (r["bar_date"], r["symbol"])
            if k not in seen or rank > seen[k][0]:
                seen[k] = (rank, None if r["passed"] else r["layer"])
        for (day, _), (_, layer) in seen.items():
            by_day.setdefault(day, {})
            key = layer or "passed"
            by_day[day][key] = by_day[day].get(key, 0) + 1
        return [{"bar_date": d, "counts": c} for d, c in sorted(by_day.items())]

    # ------------------------------------------------------------ parameters
    def params(self) -> dict:
        return {"params": settings_edit.schema(self.s), "changes": Journal().changes(30),
                "oos_start": OOS_START, "base": self._cache.get("params_base", (0, None))[1]}

    def _combined(self, s: Settings) -> dict:
        frames = self.frames()
        reg = s.market.regime_symbol
        runs = [run_backtest(frames, sl.params, s.risk, reg, 10_000 / len(s.sleeves)) for sl in s.sleeves]
        eq = sum(r.equity for r in runs)
        trades = [t for r in runs for t in r.trades]
        out = {}
        for name, a, b in (("oos", OOS_START, None), ("a", OOS_START, "2023-12-31"), ("b", "2024-01-01", None)):
            e = eq[eq.index >= pd.Timestamp(a)]
            t = [x for x in trades if x.opened >= pd.Timestamp(a)]
            if b:
                e = e[e.index <= pd.Timestamp(b)]
                t = [x for x in t if x.opened <= pd.Timestamp(b)]
            m = metrics(e, t)
            out[name] = {k: m.get(k) for k in ("cagr", "max_drawdown", "sharpe", "trades", "win_rate", "avg_r")}
        start = eq.index[s.strategy.regime_ema]
        w = (eq[eq.index >= start] / eq[eq.index >= start].iloc[0]).resample("W").last().dropna()
        out["curve"] = [[_ms(t), v] for t, v in w.items()]
        return out

    def test_params(self, changes: dict) -> tuple[int, dict]:
        try:
            ch = settings_edit.validate(changes)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        base = self.cached("params_base", 6 * 3600, lambda: self._combined(self.s))
        return 200, {"current": base, "proposed": self._combined(settings_edit.apply(self.s, ch))}

    def save_params(self, changes: dict) -> tuple[int, dict]:
        try:
            ch = settings_edit.validate(changes)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        now = settings_edit.current(self.s)
        ch = {k: v for k, v in ch.items() if now[f"{k[0]}.{k[1]}"] != v}
        if not ch:
            return 200, {"saved": 0, **self.params()}
        try:
            settings_edit.write(ch)
        except Exception as exc:
            log.exception("saving parameters failed")
            return 500, {"error": f"Não consegui guardar: {exc}"}
        j = Journal()
        for (sec, key), v in ch.items():
            j.change(f"{sec}.{key}", now[f"{sec}.{key}"], v)
        log.info("Parameters changed from the dashboard: %s",
                 ", ".join(f"{k}={v}" for (_, k), v in ch.items()))
        self.reload()
        return 200, {"saved": len(ch), **self.params()}

    def set_controls(self, body: dict) -> tuple[int, dict]:
        if "entries_paused" not in body:
            return 400, {"error": "falta entries_paused"}
        paused = bool(body["entries_paused"])
        c = save_controls(entries_paused=paused)
        log.info("Entries %s from the dashboard", "paused" if paused else "resumed")
        return 200, c

    def desk(self) -> dict:
        j = Journal()
        items = []
        for kind, rows in (("real", j.decisions()), ("demo", j.demos())):
            for r in rows:
                v = json.loads(r["verdict"])
                items.append({"kind": kind, "id": f"{kind}-{r['id']}", "ts": r["ts"], "bar_date": r["bar_date"],
                              "symbol": r["symbol"], "decision": r["decision"],
                              "size": v.get("size_multiplier"), "chat": v.get("chat", []),
                              "hard_checks": v.get("hard_checks", []),
                              "sources": (v.get("news") or {}).get("sources", []),
                              "plan": v.get("plan", {})})
        items.sort(key=lambda x: x["ts"], reverse=True)
        return {"agents": AGENTS, "discussions": items, "demo": self.demo,
                "universe": list(self.s.market.universe), "ai": {"backend": self.s.ai.backend}}

    def start_demo(self, symbol: str) -> tuple[int, dict]:
        symbol = symbol.upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        if symbol not in self.s.market.universe:
            return 400, {"error": f"{symbol} não está no universo do bot"}
        with self._lock:
            if self.demo["running"]:
                return 409, {"error": "Já há uma reunião a decorrer"}
            self.demo = {"running": True, "symbol": symbol, "started": datetime.now(timezone.utc).isoformat(),
                         "error": None, "result": None}
        threading.Thread(target=self._run_demo, args=(symbol,), daemon=True).start()
        return 202, self.demo

    def _run_demo(self, symbol: str):
        try:
            frames = data.load_universe(self.s.market.universe, refresh=False)
            sl = self.s.sleeves[0]
            f = compute_signals(frames[symbol], regime_series(frames[self.s.market.regime_symbol], sl.params),
                                sl.params)
            row = f.iloc[-1]
            plan = make_plan(symbol, row, sl.params, entry=self.prices().get(symbol, float(row["close"])))
            llm = LLM(self.s.ai)
            verdict = Desk(llm, self.s.risk, self.s.ai.news_web_search, self.s.ai.boss_effort).discuss(
                plan, [x.name for x in self.s.sleeves], [], force_ai=True)
            Journal().demo(str(row.name.date()), verdict)
            result = {"decision": verdict.decision, "calls": llm.calls, "cost_usd": llm.cost_usd}
            with self._lock:
                self.demo.update(running=False, result=result)
        except Exception as exc:
            log.exception("Demo desk failed")
            with self._lock:
                self.demo.update(running=False, error=str(exc))

    def strategies(self) -> dict:
        def build():
            frames = self.frames()
            reg = self.s.market.regime_symbol
            out = []
            start = frames[reg].index[self.s.strategy.regime_ema]
            for sl in self.s.sleeves:
                res = run_backtest(frames, sl.params, self.s.risk, reg, 10_000)
                eq = res.equity[res.equity.index >= start]
                weekly = (eq / eq.iloc[0]).resample("W").last().dropna()
                oos_eq = res.equity[res.equity.index >= OOS_START]
                p = sl.params
                out.append({
                    "name": sl.name, "lookback": p.breakout_lookback, "take_profit_r": p.take_profit_r,
                    "trail_atr": p.trail_atr, "stop_atr": p.stop_atr, "volume_mult": p.volume_mult,
                    "min_room_r": p.min_room_r,
                    "full": metrics(res.equity, res.trades),
                    "oos": metrics(oos_eq, [t for t in res.trades if t.opened >= pd.Timestamp(OOS_START)]),
                    "curve": [[_ms(t), v] for t, v in weekly.items()],
                })
            btc = buy_and_hold(frames[reg], start)
            btc_w = (btc / btc.iloc[0]).resample("W").last().dropna()
            return {"sleeves": out, "btc": [[_ms(t), v] for t, v in btc_w.items()],
                    "btc_oos": metrics(btc[btc.index >= OOS_START]), "oos_start": OOS_START}
        res = dict(self.stale_ok("strategies", 6 * 3600, build))
        state = BotState.load()
        orders = Journal().orders(500)
        for sl in res["sleeves"]:
            sl["open"] = [p.symbol for p in state.positions if p.sleeve == sl["name"]]
            sl["closed_live"] = sum(1 for o in orders if o["sleeve"] == sl["name"] and o["side"] == "sell")
        return res

    # ------------------------------------------------------------ phone view
    def mobile(self) -> dict:
        """Everything the phone page shows, in one call."""
        ov = self.overview()
        rd = self.radar()
        j = Journal()
        acct = ov["account"] or {}
        equity = acct.get("equity")
        start = self.s.broker.paper_start_cash
        sleeve_eq = (equity or start) / len(self.s.sleeves)

        # trades: pair every sell with the buy it closes, for $ and % results
        trades, open_buys = [], {}
        for o in reversed(j.orders(1000)):
            k = (o["symbol"], o["sleeve"])
            coin = o["symbol"].removesuffix("USDT")
            if o["side"] == "buy":
                open_buys[k] = o
                trades.append({"ts": o["ts"], "kind": "buy", "coin": coin, "sleeve": o["sleeve"],
                               "value": o["qty"] * o["price"], "pct_sleeve": o["qty"] * o["price"] / sleeve_eq,
                               "why": o["reason"]})
                continue
            b = open_buys.pop(k, None)
            pnl = (o["price"] - b["price"]) * o["qty"] if b else None
            pct = o["price"] / b["price"] - 1 if b else None
            reason = (o["reason"] or "").split(" ")[0]
            how = {"alvo": "alvo", "tempo": "dinheiro parado"}.get(reason, "stop")
            if how == "stop" and pnl is not None and pnl > 0:
                how = "trailing stop"
            trades.append({"ts": o["ts"], "kind": "win" if (pnl or 0) > 0 else "loss", "coin": coin,
                           "sleeve": o["sleeve"], "pnl": pnl, "pct": pct, "how": how})

        curve = [[_ms(r["ts"]), r["equity"]] for r in j.equity_history()]
        if equity is not None:
            curve.append([_ms(datetime.now(timezone.utc)), equity])

        decisions = [{"ts": r["ts"], "coin": r["symbol"].removesuffix("USDT"), "decision": r["decision"],
                      "size": r["size_multiplier"]} for r in j.decisions()[-30:]]
        demos = [{"ts": r["ts"], "coin": r["symbol"].removesuffix("USDT"), "decision": r["decision"]}
                 for r in j.demos(10)]
        cuts = rd.get("history") or []

        # activity feed: trades, desk meetings, the Auditor's daily cut
        feed = []
        for t in trades:
            if t["kind"] == "buy":
                feed.append({"ts": t["ts"], "kind": "buy", "title": f"Comprou {t['coin']}", "agent": "Executor",
                             "sub": "Executor", "value": t["value"], "note": f"{_pt(t['pct_sleeve'] * 100, 1)}% da sleeve"})
            else:
                feed.append({"ts": t["ts"], "kind": t["kind"],
                             "title": ("Vendeu " if t["kind"] == "win" else "Stop em ") + t["coin"],
                             "agent": "Risk", "sub": f"Risk · {t['how']}", "value": t["pnl"], "pct": t["pct"]})
        for d in decisions:
            feed.append({"ts": d["ts"], "kind": "desk", "title": f"Mesa: {d['coin']}", "agent": "Chefe",
                         "sub": "Chefe" + (f" · tamanho ×{_pt(d['size'] or 0)}" if d["decision"] == "COMPRA" else ""),
                         "decision": d["decision"]})
        for d in demos:
            feed.append({"ts": d["ts"], "kind": "demo", "title": f"Simulação: {d['coin']}", "agent": "Chefe",
                         "sub": "Chefe · sem ordens", "decision": d["decision"]})
        for c in cuts[-14:]:
            counts = dict(c["counts"])
            passed = counts.pop("passed", 0)
            top = max(counts.items(), key=lambda kv: kv[1]) if counts else None
            feed.append({"ts": c["bar_date"] + "T23:59:59+00:00", "kind": "cut", "agent": "Auditor",
                         "title": f"Cortou {sum(counts.values())} moedas",
                         "sub": "Auditor · " + (f"camada {list(LAYER_SHORT).index(top[0]) + 1}, "
                                                f"{LAYER_SHORT.get(top[0], top[0])}" if top else "nenhuma cortada"),
                         "passed": passed})
        feed.sort(key=lambda x: x["ts"], reverse=True)

        rows = rd["rows"]
        report = rd["report"]
        best = max(rows, key=lambda x: x["quality"] or 0, default=None)
        furthest = []
        for r in rows:
            a = max((s["audit"] for s in r["sleeves"]),
                    key=lambda a: 99 if a["passed"] else [x["status"] for x in a["layers"]].index("fail"))
            near = max(s["dist_close"] for s in r["sleeves"])
            furthest.append({"coin": r["coin"], "passed": a["passed"], "near": near,
                             "reason": None if a["passed"] else a["failed"]["name"],
                             "layers": [x["status"] for x in a["layers"]], "quality": r["quality"]})
        furthest.sort(key=lambda x: (not x["passed"], -x["near"]))
        ag = ov["agents"]
        last_sell = next((t for t in reversed(trades) if t["kind"] != "buy"), None)
        last_buy = next((t for t in reversed(trades) if t["kind"] == "buy"), None)
        last_dec = decisions[-1] if decisions else None
        positions = ov["positions"]
        closest = min(positions, key=lambda p: -p["to_stop_pct"], default=None)
        top_cut = report["counts"][0] if report["counts"] else None
        near = furthest[0] if furthest else None
        rk = ag["risk"]
        bar_ts = (rd.get("bar_date") or "") + "T00:10:00+00:00"
        cycle_ts = None
        if ov.get("last_cycle_local"):
            try:
                cycle_ts = datetime.fromisoformat(ov["last_cycle_local"]).astimezone(timezone.utc).isoformat()
            except ValueError:
                cycle_ts = None
        notes = [
            {"agent": "Bot", "ts": cycle_ts,
             "text": (f"Capital ${_pt(equity)}. " if equity is not None else "Sem ligação à conta. ")
                     + f"{len(positions)} posição(ões) aberta(s)."
                     + (" Compras novas em pausa." if ov["controls"]["entries_paused"] else "")},
            {"agent": "Risk", "ts": last_sell["ts"] if last_sell else cycle_ts,
             "text": (f"Vendeu {last_sell['coin']} ({last_sell['how']}): {_usd(last_sell['pnl'])}."
                      if last_sell else
                      f"A guardar {len(positions)} posição(ões). Mais perto do stop: {closest['symbol'].removesuffix('USDT')} "
                      f"a {_pt(abs(closest['to_stop_pct']) * 100, 1)}%." if closest else
                      f"Sem posições. Stop a {_g(rk['stop_atr'])} ATR, trailing a {_g(rk['trail_atr'])} ATR, "
                      f"sai ao fim de {rk['timeout_days']} dias se não passar +{_g(rk['timeout_min_r'])}R.")},
            {"agent": "Executor", "ts": last_buy["ts"] if last_buy else cycle_ts,
             "text": (f"{last_buy['coin']}: ${_pt(last_buy['value'])} investidos, {_pt(last_buy['pct_sleeve'] * 100, 1)}% da sleeve."
                      if last_buy else f"Ainda sem compras. {_g(ag['executor']['risk_per_trade'] * 100)}% de risco por trade.")},
            {"agent": "Chefe", "ts": last_dec["ts"] if last_dec else bar_ts,
             "text": (f"{last_dec['coin']}: {last_dec['decision']}." if last_dec else
                      "Ainda não houve reuniões: nenhuma moeda passou o Auditor.")},
            {"agent": "Analyst", "ts": bar_ts,
             "text": f"Melhor nota hoje: {best['coin']} {best['quality']:.0f}/100." if best and best["quality"] is not None
                     else "Sem notas hoje."},
            {"agent": "Auditor", "ts": bar_ts,
             "text": f"{report['passed']}/{report['total']} passaram."
                     + (f" A camada {top_cut['layer']} ({top_cut['name']}) cortou {top_cut['count']}." if top_cut else "")},
            {"agent": "Scout", "ts": bar_ts,
             "text": f"{ag['scout']['watching']} moedas vigiadas, {ag['scout']['signals']} com sinal."
                     + (f" Mais perto: {near['coin']} a {'+' if near['near'] > 0 else ''}{_pt(near['near'] * 100, 1)}% do máximo."
                        if near else "")},
        ]
        notes.sort(key=lambda n: n["ts"] or "", reverse=True)
        return {
            "now": datetime.now(timezone.utc).isoformat(), "mode": "PAPER", "start": start,
            "account": ov["account"], "account_error": ov["account_error"], "drawdown": ov["drawdown"],
            "peak": ov["peak"], "halted": ov["halted"], "positions": positions, "controls": ov["controls"],
            "server_ok": ov["server_ok"], "tasks": ov["tasks"], "last_bar": ov["last_bar"],
            "last_cycle": cycle_ts, "next_desk_utc": ov["next_desk_utc"], "ai": ov["ai"],
            "curve": curve, "trades": trades, "feed": feed[:80], "notes": notes,
            "agents": ag, "scout": furthest, "best": best and {"coin": best["coin"], "quality": best["quality"],
                                                                "answers": best["answers"]},
            "report": report, "bar_date": rd.get("bar_date"),
            "roles": {x["id"]: x["role"] for x in AGENTS},
            "params": {"stop_atr": self.s.strategy.stop_atr, "trail_atr": self.s.strategy.trail_atr,
                       "trail_start_r": self.s.strategy.trail_start_r, "timeout_days": self.s.strategy.timeout_days,
                       "timeout_min_r": self.s.strategy.timeout_min_r, "risk_per_trade": self.s.risk.risk_per_trade,
                       "max_positions": self.s.risk.max_positions, "halt": self.s.risk.max_drawdown_halt,
                       "volume_mult": self.s.strategy.volume_mult, "gap": self.s.strategy.max_entry_gap_pct,
                       "min_rr": self.s.risk.min_rr},
        }

    def memetest(self) -> dict:
        """The memecoin paper test (python -m deskbot memetest), from memetest/state.json."""
        path = ROOT / "memetest" / "state.json"
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # busy or being replaced: show the last good read
            return self._cache.get("memetest_last", (0, {"exists": False}))[1]
        eur = st.get("eur_usd") or 1.0
        prices = {}
        positions = []
        for p in st.get("positions", []):
            value = p["qty"] * p["last"] * (1 - st["p"]["cost_per_side"])
            positions.append({"coin": p["coin"], "opened": p["opened"], "cost_eur": p["cost_usd"] / eur,
                              "value_eur": value / eur, "pct": value / p["cost_usd"] - 1,
                              "move": p["last"] / p["raw_entry"] - 1, "trail_on": p["trail_on"]})
        curve = st.get("curve", [])
        step = max(1, len(curve) // 200)
        ends = st.get("ends")
        out = {
            "exists": True, "running": not st.get("finished") and ends and datetime.fromisoformat(ends) > datetime.now(timezone.utc),
            "finished": bool(st.get("finished")), "started": st.get("started"), "ends": ends,
            "start_eur": st.get("start_eur"), "equity_eur": st.get("equity_eur"), "eur_usd": eur,
            "positions": positions, "pools_seen": st.get("pools_seen"), "api_errors": st.get("api_errors"),
            "trades": [{**t, "cost_eur": t["cost_usd"] / eur} for t in st.get("trades", [])][-30:][::-1],
            "events": [{k: e.get(k) for k in ("ts", "agent", "coin", "verdict", "reason")} for e in st.get("events", [])][-60:][::-1],
            "counts": st.get("counts", {}), "ready": st.get("ready", []),
            "curve": [[t, v / eur] for t, v in curve[::step]] + ([[curve[-1][0], curve[-1][1] / eur]] if curve else []),
            "updated": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        }
        self._cache["memetest_last"] = (0, out)
        return out

    def phone_info(self, port: int) -> dict:
        url = phone.link(port)
        return {"lan": self.s.dashboard.lan, "ip": phone.lan_ip(), "port": port, "url": url,
                "qr": phone.qr_svg(url) if url and self.s.dashboard.lan else None,
                "password": phone.password_info()}

    def login(self, ip: str, body: dict) -> tuple[int, dict]:
        """Password -> phone key, for the home-screen app (it can't see the QR link's key)."""
        if not phone.password_info()["set"]:
            return 409, {"error": "Ainda não há password. Define-a no painel do PC (botão Telemóvel)."}
        wait = self.guard.wait(ip)
        if wait:
            return 429, {"error": f"Demasiadas tentativas. Tenta outra vez daqui a {wait // 60 + 1} min."}
        if not phone.check_password(body.get("password")):
            self.guard.failed(ip)
            time.sleep(1)  # slows guessing
            log.warning("Wrong phone password from %s", ip)
            return 401, {"error": "Password errada."}
        self.guard.ok(ip)
        log.info("Phone logged in with the password from %s", ip)
        return 200, {"token": phone.token()}

    def push_subscribe(self, body: dict) -> tuple[int, dict]:
        """The phone (or this PC's browser) turns lock-screen notifications on or off."""
        from . import push
        from .relay import Relay
        sub = push.clean_sub(body.get("subscription") or {})
        if not sub:
            return 400, {"error": "subscrição inválida"}
        relay = Relay()
        if not relay.enabled:
            return 503, {"error": "a ponte (relay) não está configurada neste PC"}
        gone = body.get("action") == "unsubscribe"
        relay.push({push.sub_key(sub["endpoint"]): {"gone": True, "at": sub["at"]} if gone else sub})
        return 200, {"ok": True, "on": not gone}

    def set_phone_password(self, body: dict) -> tuple[int, dict]:
        try:
            phone.set_password(body.get("password"))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        log.info("Phone password changed")
        return 200, {"password": phone.password_info()}

    def activity(self) -> dict:
        j = Journal()
        return {"orders": j.orders(), "equity": j.equity_history(), "log": self.log_tail()}

    def tools(self) -> dict:
        items = []
        for t in TOOLS:
            items.append({**t, "exists": bool(t["path"]) and Path(t["path"]).exists() if t["path"] else None})
        return {"tools": items}


class Handler(BaseHTTPRequestHandler):
    app: App = None  # set by serve()

    def log_message(self, fmt, *args):  # default writes to stderr, which is None under pythonw
        log.debug("dashboard %s", fmt % args)

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(_clean(obj), ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _local(self) -> bool:
        """Only a request made on this machine itself. Anything that came through the HTTPS proxy
        (it adds X-Forwarded-For) is from the internet, even though it arrives from 127.0.0.1."""
        if self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP"):
            return False
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For")
        return xff.split(",")[0].strip() if xff else self.client_address[0]

    def _authed(self) -> bool:
        """This PC is trusted; anything else (the phone on the Wi-Fi) must send the phone token."""
        if self._local():
            return True
        sent = self.headers.get("X-Desk-Token") or ""
        return bool(sent) and hmac.compare_digest(sent, phone.token())

    STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                    "/index.html": ("index.html", "text/html; charset=utf-8"),
                    "/m": ("mobile.html", "text/html; charset=utf-8"),
                    "/app": ("app.html", "text/html; charset=utf-8"),
                    "/memes": ("memes.html", "text/html; charset=utf-8"),
                    "/perps": ("memes.html", "text/html; charset=utf-8"),   # same terminal, futures mode
                    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),   # lock-screen notifications
                    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                    "/icon-180.png": ("icon-180.png", "image/png"),
                    "/icon-512.png": ("icon-512.png", "image/png")}

    def do_GET(self):
        port = self.server.server_port
        routes = {"/api/overview": self.app.overview, "/api/radar": self.app.radar, "/api/desk": self.app.desk,
                  "/api/strategies": self.app.strategies, "/api/activity": self.app.activity,
                  "/api/tools": self.app.tools, "/api/params": self.app.params, "/api/mobile": self.app.mobile,
                  "/api/memetest": self.app.memetest,
                  "/api/desks": lambda: desks_api.overview(desks_api.backtests()),
                  "/api/desk/memes": lambda: desks_api.desk("memes", desks_api.backtests()),
                  "/api/desk/futuros": lambda: desks_api.desk("futuros", desks_api.backtests()),
                  "/api/desk/perps": lambda: desks_api.desk("perps", desks_api.backtests()),
                  "/api/desk/acoes": lambda: desks_api.desk("acoes", desks_api.backtests())}
        path = self.path.split("?")[0]
        if path in self.STATIC_FILES:  # pages and icons carry no data; the API below is what needs the token
            name, ctype = self.STATIC_FILES[path]
            self._send(200, (STATIC / name).read_bytes(), ctype)
        elif path == "/api/login":  # tells the lock screen whether a password exists (no data)
            self._json(200, {"password_set": phone.password_info()["set"]})
        elif path == "/api/phone":  # the secret link and its QR code: only on this PC
            if not self._local():
                return self._json(403, {"error": "só no PC"})
            self._json(200, self.app.phone_info(port))
        elif path in routes and not self._authed():
            self._json(401, {"error": "falta a chave do telemóvel"})
        elif path in routes:
            try:
                self._json(200, routes[path]())
            except Exception as exc:
                log.exception("dashboard %s failed", path)
                self._json(500, {"error": str(exc)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        actions = {"/api/desk/demo": lambda b: self.app.start_demo(re.sub(r"[^A-Za-z]", "", str(b.get("symbol", "")))),
                   "/api/params/test": lambda b: self.app.test_params(dict(b.get("changes") or {})),
                   "/api/params/save": lambda b: self.app.save_params(dict(b.get("changes") or {})),
                   "/api/controls": self.app.set_controls,
                   "/api/phone/rotate": lambda b: (200, {"rotated": bool(phone.rotate()),
                                                         **self.app.phone_info(self.server.server_port)}),
                   "/api/phone/password": self.app.set_phone_password,
                   "/api/desk/pause": lambda b: desks_api.pause(str(b.get("name")), bool(b.get("paused"))),
                   "/api/desk/memes/start": lambda b: desks_api.start_memes(int(b.get("minutes") or 0),
                                                                             float(b.get("eur") or 110),
                                                                             str(b.get("venue") or "gmgn")),
                   "/api/desk/memes/stop": lambda b: desks_api.stop_memes(),
                   "/api/desk/perps/start": lambda b: desks_api.start_perps(int(b.get("minutes") or 0),
                                                                             float(b.get("eur") or 110),
                                                                             float(b.get("lev") or 5)),
                   "/api/desk/perps/stop": lambda b: desks_api.stop_perps(),
                   "/api/push/subscribe": self.app.push_subscribe,
                   "/api/login": lambda b: self.app.login(self._client_ip(), b)}
        if path not in actions:
            return self._json(404, {"error": "not found"})
        if path == "/api/phone/rotate" and not self._local():
            return self._json(403, {"error": "só no PC"})
        if path != "/api/login" and not self._authed():
            return self._json(401, {"error": "falta a chave do telemóvel"})
        port = self.server.server_port
        allowed = {None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if not self._local():
            allowed.add(f"http://{self.headers.get('Host', '')}")
            allowed.add(f"https://{self.headers.get('Host', '')}")
        if self.headers.get("Origin") not in allowed:
            return self._json(403, {"error": "origin not allowed"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            return self._json(413, {"error": "pedido demasiado grande"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "invalid body"})
        try:
            code, payload = actions[path](body)
        except Exception as exc:
            log.exception("dashboard %s failed", path)
            code, payload = 500, {"error": str(exc)}
        self._json(code, payload)


def serve(settings: Settings, port: int | None = None, lan: bool | None = None):
    port = port or settings.dashboard.port
    lan = settings.dashboard.lan if lan is None else lan
    phone.ensure_icons()
    phone.token()
    Handler.app = App(settings)
    threading.Thread(target=Handler.app.warm_up, daemon=True).start()
    from . import relay
    threading.Thread(target=relay.pc_loop, name="relay", daemon=True).start()   # phone app via Supabase
    httpd = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", port), Handler)
    log.info("Dashboard on http://127.0.0.1:%d%s", port,
             f" and on the home network at http://{phone.lan_ip()}:{port}/m (phone link needed)" if lan else "")
    httpd.serve_forever()
