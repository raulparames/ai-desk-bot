"""The three paper desks as the phone app sees them: Memecoins, Futuros, Ações.

Normalises each desk's state into one shape, starts/stops memecoin sessions, pauses desks and
keeps a cached backtest summary per desk. Used by dashboard.py.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import ROOT
from .paperdesk import DESKS_DIR, controls, read_state, set_paused

log = logging.getLogger(__name__)
MEME_DIR = ROOT / "memetest"
PERP_DIR = ROOT / "perpdesk"
DURATIONS = (5, 10, 15, 30, 120, 720)

META = {
    "memes": {"title": "Memecoins", "mode": "memecoins", "color": "#ff4fa3",
              "subtitle": "memecoins da Solana · sessões de 5 min a 12 h",
              "blurb": "5 agentes (Scanner, Analista, Compounder, Executor, Risco). 40% do saldo livre por compra, metade a 2×, trailing 10%, stop −8%."},
    "futuros": {"title": "Futuros", "mode": "futuros", "color": "#7b61ff",
                "subtitle": "perpétuos de cripto · long e short · velas de 4 h",
                "blurb": "BTC, ETH, SOL, BNB e XRP. Long e short, alavancagem máxima 10x, 1% de risco por trade."},
    "perps": {"title": "Futuros", "mode": "futuros", "color": "#7b61ff",
              "subtitle": "perpétuos da Hyperliquid · INVO · sessões de 5 min a 12 h",
              "blurb": "5 agentes na Hyperliquid (a da INVO). Long e short até 10x, 40% do saldo livre de margem, metade a 2R, trailing."},
    "acoes": {"title": "Ações", "mode": "ações", "color": "#1e9bf0",
              "subtitle": "S&P 500 com filtro de tendência · revisto todos os meses",
              "blurb": "SPY quando está acima da média de 200 dias; senão ouro ou obrigações. Sem alavancagem."},
}


def _alive(pid: int) -> bool:
    """Is this process still running? (never use os.kill(pid, 0) on Windows: there it sends Ctrl+C)"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259
    except (AttributeError, OSError):
        return False


def _running(folder: Path) -> bool:
    try:
        return _alive(int((folder / "pid").read_text().strip()))
    except (OSError, ValueError):
        return False


def meme_running() -> bool:
    return _running(MEME_DIR)


def perp_running() -> bool:
    return _running(PERP_DIR)


STAKES = (20, 50, 110, 500, 1000)
VENUES = ("gmgn", "fomo")       # where the memes app opens each coin (orders stay in paper)
LEVERAGES = (2, 5, 10)


def _check(minutes: int, eur: float) -> str:
    if minutes not in DURATIONS:
        return f"duração tem de ser {', '.join(map(str, DURATIONS))} minutos"
    if eur not in STAKES:
        return f"o saldo tem de ser {', '.join(map(str, STAKES))} €"
    return ""


def _spawn(folder: Path, args: list[str]):
    folder.mkdir(exist_ok=True)
    win = ROOT / ".venv" / "Scripts" / "python.exe"
    py = win if win.exists() else Path(sys.executable)
    out = open(folder / "run_output.txt", "w", encoding="utf-8")
    extra = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000200} if os.name == "nt"
             else {"start_new_session": True})
    subprocess.Popen([str(py), "-m", "deskbot", *args], cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, **extra)
    for _ in range(40):  # wait for the new state file
        time.sleep(0.25)
        if _running(folder):
            break


def start_memes(minutes: int, eur: float = 110, venue: str = "gmgn") -> tuple[int, dict]:
    if err := _check(minutes, eur):
        return 400, {"error": err}
    venue = venue if venue in VENUES else "gmgn"
    if meme_running():
        return 409, {"error": "Já há uma sessão de memes a correr"}
    _spawn(MEME_DIR, ["memedesk", "--minutes", str(minutes), "--eur", str(eur), "--venue", venue])
    log.info("Memes session started from the app: %d min, %s EUR, %s", minutes, eur, venue)
    return 202, {"started": True, "minutes": minutes, "eur": eur, "venue": venue}


def stop_memes() -> tuple[int, dict]:
    if not meme_running():
        return 409, {"error": "Não há sessão a correr"}
    (MEME_DIR / "stop").write_text("stop", encoding="utf-8")
    log.info("Memecoin session stop requested from the app")
    return 202, {"stopping": True}


def start_perps(minutes: int, eur: float = 110, lev: float = 5) -> tuple[int, dict]:
    if err := _check(minutes, eur):
        return 400, {"error": err}
    if lev not in LEVERAGES:
        return 400, {"error": f"alavancagem tem de ser {', '.join(map(str, LEVERAGES))}x (máximo 10x)"}
    if perp_running():
        return 409, {"error": "Já há uma sessão de futuros a correr"}
    _spawn(PERP_DIR, ["perpdesk", "--minutes", str(minutes), "--eur", str(eur), "--lev", str(lev)])
    log.info("Futures session started from the app: %d min, %s EUR, %sx", minutes, eur, lev)
    return 202, {"started": True, "minutes": minutes, "eur": eur, "lev": lev}


def stop_perps() -> tuple[int, dict]:
    if not perp_running():
        return 409, {"error": "Não há sessão a correr"}
    (PERP_DIR / "stop").write_text("stop", encoding="utf-8")
    log.info("Futures session stop requested from the app")
    return 202, {"stopping": True}


# ------------------------------------------------------------------ backtests (cached a day)
_bt_lock = threading.Lock()
BT_FILE = DESKS_DIR / "backtests.json"


def backtests(refresh: bool = False, wait: bool = False) -> dict:
    """Cached for a day. A stale cache is returned at once and rebuilt in the background
    (the backtests take about a minute), unless wait=True."""
    try:
        cached = json.loads(BT_FILE.read_text(encoding="utf-8"))
        if not refresh and time.time() - cached.get("_at", 0) < 86400:
            return cached
    except (OSError, ValueError):
        cached = {}
    if not wait and cached:
        threading.Thread(target=backtests, kwargs={"refresh": True, "wait": True}, daemon=True).start()
        return cached
    if not _bt_lock.acquire(blocking=False):
        return cached
    try:
        from . import futures, stocks
        out = {"_at": time.time()}
        fdata = {s: (futures.klines(s, refresh=False), futures.funding(s, refresh=False)) for s in futures.SYMBOLS}
        res = futures.backtest(fdata)
        eq, tr = res["equity"], res["trades"]
        out["futuros"] = {"note": "BTC, ETH, SOL, BNB e XRP desde 2020, com taxas, slippage e funding reais. "
                                  "As moedas foram escolhidas hoje (já sabemos que sobreviveram).",
                          "rows": _rows(eq, tr, futures.stats, [("2020-01-01", None), ("2020-01-01", "2022-12-31"),
                                                                  ("2023-01-01", None)]),
                          "bench": _bench(fdata["BTCUSDT"][0]["close"], futures.stats, "BTC comprar e manter")}
        sdata = {s: stocks.history(s, refresh=False) for s in stocks.NAMES}
        res = stocks.backtest(sdata)
        out["acoes"] = {"note": "ETFs com dividendos desde 2008, custos de 0,05% por lado. Escolhida entre 5 "
                                "variantes conhecidas por ser a mais estável nos dois períodos.",
                        "rows": _rows(res["equity"], None, stocks.stats, [("2008-01-01", None),
                                                                          ("2008-01-01", "2016-12-31"),
                                                                          ("2017-01-01", None)]),
                        "bench": _bench(sdata["SPY"]["close"], stocks.stats, "SPY comprar e manter", "2008-01-01")}
        out["memes"] = {"note": "Não há histórico grátis de memecoins ao minuto, por isso esta mesa não tem "
                                "backtest: só sessões em papel. Um resultado de 30 min é sobretudo sorte.", "rows": []}
        DESKS_DIR.mkdir(exist_ok=True)
        BT_FILE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return out
    except Exception:
        log.exception("desk backtests failed")
        return cached
    finally:
        _bt_lock.release()


def _rows(eq, trades, stats, periods):
    rows = []
    for a, b in periods:
        e = eq[(eq.index >= pd.Timestamp(a)) & (eq.index <= pd.Timestamp(b or "2100-01-01"))]
        st = stats(e)
        rows.append({"period": f"{a[:4]}–{b[:4]}" if b else f"desde {a[:4]}", "cagr": st["cagr"],
                     "max_drawdown": st["max_drawdown"], "sharpe": st["sharpe"]})
    return rows


def _bench(close, stats, label, start="2020-01-01"):
    c = close[close.index >= pd.Timestamp(start)]
    st = stats(c)
    return {"label": label, "cagr": st["cagr"], "max_drawdown": st["max_drawdown"], "sharpe": st["sharpe"]}


# ------------------------------------------------------------------ normalised views
def _mult(eq, start):
    return eq / start if start else 1.0


def desk(name: str, bt: dict | None = None) -> dict:
    meta = META[name]
    out = {"name": name, **meta, "backtest": (bt or {}).get(name)}
    if name == "memes":
        return {**out, **_memes()}
    if name == "perps":
        return {**out, **_perps()}
    st = read_state(name)
    if not st:
        return {**out, "exists": False, "status": "idle", "rules": [], "positions": [], "trades": [], "log": [],
                "curve": [], "ticker": [], "start_eur": 1000.0, "equity_eur": 1000.0}
    rate = st["eur_usd"]
    positions = []
    for q in st.get("positions", []):
        side = q.get("side", 1)
        last = q.get("last", q["entry"])
        if name == "futuros":
            pnl = q["qty"] * side * (last - q["entry"]) - q.get("fees_in", 0) - q.get("funding", 0)
            value = q["qty"] * last
        else:
            pnl = q["qty"] * (last - q["entry"])
            value = q["qty"] * last
        positions.append({"symbol": q["symbol"].replace("USDT", ""), "name": q.get("name", ""),
                          "side": ("long" if side == 1 else "short") if name == "futuros" else "",
                          "lev": q.get("lev"), "value_eur": value / rate, "pnl_eur": pnl / rate,
                          "pct": side * (last / q["entry"] - 1), "entry": q["entry"], "last": last,
                          "stop": q.get("stop"), "liq": q.get("liq"), "opened": q.get("opened")})
    paused = bool(controls().get(name, {}).get("paused"))
    trades = [{"symbol": t["symbol"].replace("USDT", ""), "side": "long" if t.get("side", 1) == 1 else "short",
               "pnl_eur": t["pnl_eur"], "pct": t.get("pct"), "why": t.get("why"), "closed": t.get("closed")}
              for t in st.get("trades", [])][::-1]
    return {**out, "exists": True, "status": "paused" if paused else "live", "paused": paused,
            "started": st["started"], "updated": st.get("updated"), "start_eur": st["start_eur"],
            "equity_eur": st.get("equity_eur", st["start_eur"]), "positions": positions, "trades": trades[:50],
            "log": st.get("events", [])[-80:][::-1], "curve": st.get("curve", [])[-1500:],
            "ticker": st.get("ticker", []), "rules": st.get("extra", {}).get("rules", []),
            "extra": {k: v for k, v in st.get("extra", {}).items() if k in ("ranking", "month")}}


def _memes() -> dict:
    from .memetest import MemeParams
    try:
        st = json.loads((MEME_DIR / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = None
    running = meme_running()
    p = MemeParams()
    rules = [f"Scanner: pools novos da PumpSwap e em alta; corta liquidez < $40 000, +80% numa hora, pool < 10 min",
             "Auditor: mint e freeze revogados, top 10 holders < 25%, não é honeypot, há vendas, liquidez ≥ $40 000",
             "Analyst: ainda a negociar agora, compradores reais e diferentes, não persegue a vela; nota ≥ 70",
             f"Executor: a melhor nota primeiro, 1 compra a cada 2 min, {p.stack_pct:.0%} do saldo, no máx. 2% da liquidez e 5 posições",
             "Risk: preço a cada 15 s; stop -6%, trailing a partir de +20% que vende 12% abaixo do máximo",
             "Custos de 1% por lado mais o impacto no pool; no fim da sessão vende tudo ao último preço"]
    from . import memedesk
    rules = ["Scanner: pools em alta e moedas que saíram do pump.fun; corta liquidez < $40 000, +80% numa hora e pools com menos de 10 min. Depois as baleias: 3 compras de $500+ em 30 min e mais compras do que vendas",
             "Analista: 5 votos bull/bear, a equipa de risco (mint e freeze revogados, top 10 < 25%, sem honeypot, há vendas) e a rota Jupiter (até 2% de impacto). Nota de 0 a 100",
             "Compounder: cada compra é 40% do saldo livre, no máximo 2 abertas, nunca reforça uma moeda",
             "Executor: a melhor nota primeiro, 1 compra a cada 2 min, ao preço de uma cotação real da Jupiter mais 0,5% de custos",
             "Risco, a cada 5 s: stop −8% enquanto não chega a 2×; a 2× vende metade (a entrada volta); o resto sai com trailing de 10% abaixo do máximo",
             "Tudo em papel: nenhuma carteira, nenhuma ordem real"]
    repos = [{"agent": a, "repo": memedesk.REPOS[a][0], "role": memedesk.REPOS[a][1]} for a in memedesk.AGENTS]
    if os.getenv("GMGN_API_KEY") or _env_has("GMGN_API_KEY"):
        rules.insert(2, "GMGN (chave tua): moedas em alta da GMGN, smart money e KOLs no SMARTS, rug score, wash trading e bots na equipa de risco")
    base = {"durations": list(DURATIONS), "stakes": list(STAKES), "rules": rules, "running": running, "repos": repos,
            "venues": list(VENUES), "gmgn_key": bool(os.getenv("GMGN_API_KEY") or _env_has("GMGN_API_KEY")),
            "cards": CARDS, "params": {"stop_pct": 0.08, "half_at": memedesk.HALF_AT, "trail_pct": 0.10,
                                       "stack_pct": 0.40, "max_open": 2, "goal_mult": 10}}
    if not st:
        return {**base, "exists": False, "status": "idle", "positions": [], "trades": [], "log": [], "curve": [],
                "ticker": [], "start_eur": 1000.0, "equity_eur": 1000.0}
    rate = st.get("eur_usd") or 1.0
    cost = st["p"]["cost_per_side"]
    trail = st["p"].get("trail_pct", 0.10)
    positions = []
    for q in st.get("positions", []):
        value = q["qty"] * q["last"] * (1 - cost)
        half = q.get("half_done", False)
        positions.append({"id": q["id"], "symbol": q["coin"], "name": q["name"].split(" / ")[0], "side": "",
                          "value_eur": value / rate, "cost_eur": q["cost_usd"] / rate,
                          "pnl_eur": (value - q["cost_usd"]) / rate, "pct": value / q["cost_usd"] - 1 if q["cost_usd"] else 0,
                          "mult": q["last"] / q["raw_entry"], "entry": q["raw_entry"], "last": q["last"], "peak": q["peak"],
                          "stop": None if half else q["raw_entry"] * (1 - st["p"]["stop_pct"]),
                          "trail": q["peak"] * (1 - trail) if half else None, "trail_on": q["trail_on"],
                          "half_done": half, "half_px": q.get("half_px"), "half_ts": q.get("half_ts"),
                          "opened": q["opened"], "mint": q.get("mint"), "img": q.get("img", ""), "mcap": q.get("mcap", 0),
                          "created": q.get("created", ""), "score": q.get("score"), "series": _series(q.get("series"))})
    trades = [{"id": t.get("id"), "symbol": t["coin"], "side": "", "pnl_eur": t["pnl_eur"], "pct": t["pct"],
               "why": t["reason"], "kind": t.get("kind", "end"), "mult": t.get("mult"), "closed": t["closed"],
               "opened": t.get("opened"), "peak": t.get("peak_move"), "mint": t.get("mint"), "img": t.get("img", ""),
               "entry": t.get("entry"), "exit": t.get("exit"), "half_px": t.get("half_px"), "half_ts": t.get("half_ts"),
               "mcap": t.get("mcap"), "created": t.get("created"), "score": t.get("score"),
               "series": _series(t.get("series"), 80)}
              for t in st.get("trades", [])][::-1]
    events = st.get("events", [])
    seen, ticker = set(), []
    for e in reversed(events):
        if e.get("agent") == "Scanner" and "h1_pct" in e and e["coin"] not in seen:
            seen.add(e["coin"])
            ticker.append({"symbol": e["coin"], "pct": (e.get("h1_pct") or 0) / 100})
        if e.get("agent") == "TRENDS" and e.get("verdict") in ("pass", "kill") and e["coin"] not in seen:
            import re as _re
            m = _re.search(r"([+-]?\d+)% 1h|\+(\d+)% em 1h", e.get("reason", ""))
            if m:
                seen.add(e["coin"])
                ticker.append({"symbol": e["coin"], "pct": float(m.group(1) or m.group(2)) / 100})
        if len(ticker) >= 12:
            break
    eq = st.get("equity_eur", st.get("start_eur"))
    finished = bool(st.get("finished")) or not running
    counts = st.get("counts", {})
    recap = None
    if finished and st.get("started"):
        mins = (datetime.fromisoformat(st["ends"]) - datetime.fromisoformat(st["started"])).total_seconds() / 60
        wins = [t for t in trades if t["pnl_eur"] > 0]
        best = max(trades, key=lambda t: t["pnl_eur"], default=None)
        worst = min(trades, key=lambda t: t["pnl_eur"], default=None)
        recap = {"minutes": round(mins), "start_eur": st["start_eur"], "end_eur": eq, "trades": len(trades),
                 "wins": len(wins), "coins_seen": st.get("pools_seen", 0),
                 "best": best and {"symbol": best["symbol"], "pnl_eur": best["pnl_eur"]},
                 "worst": worst and {"symbol": worst["symbol"], "pnl_eur": worst["pnl_eur"]},
                 "started": st["started"], "ended": st["ends"],
                 "win_rate": len(wins) / len(trades) if trades else None, "stop_pct": st["p"]["stop_pct"]}
    return {**base, "exists": True, "status": "live" if running else "finished", "started": st["started"],
            "ends": st["ends"], "start_eur": st["start_eur"], "equity_eur": eq, "positions": positions,
            "trades": trades, "log": events[-80:][::-1], "ticker": ticker, "counts": counts,
            "ready": st.get("ready", []), "pools_seen": st.get("pools_seen", 0),
            "curve": [[t, v / rate] for t, v in st.get("curve", [])][-1500:], "recap": recap,
            "mode": st.get("mode", "classic"), "agents": st.get("agents", {}),
            "venue": st.get("venue") or "gmgn", "feeds": st.get("feeds", {}), "cash_eur": st.get("cash", 0) / rate,
            "wins": sum(1 for t in trades if t["pnl_eur"] > 0), "losses": sum(1 for t in trades if t["pnl_eur"] <= 0)}


CARDS = [{"key": "SCANNER", "name": "Scanner"}, {"key": "ANALYST", "name": "Analista"},
         {"key": "COMPOUNDER", "name": "Compounder"}, {"key": "EXECUTOR", "name": "Executor"},
         {"key": "RISK", "name": "Risco"}]


def _series(points, n: int = 150) -> list:
    points = points or []
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n)] + [points[-1]]


def _env_has(key: str) -> bool:
    try:
        return any(line.split("=", 1)[0].strip() == key and line.split("=", 1)[1].strip()
                   for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines() if "=" in line)
    except OSError:
        return False


def _perps() -> dict:
    from . import perpdesk
    p = perpdesk.PerpParams()
    rules = [f"Scanner: os {p.universe} perpétuos mais negociados da Hyperliquid, velas de 15 min com EMA 20 e 50 no mesmo sentido; depois os {p.traders} melhores traders do leaderboard público ({p.min_traders}+ do nosso lado e mais dinheiro do nosso lado)",
             "Analista: 5 votos bull/bear (tendência de 1 h, RSI, funding, volume, VWAP do dia), a equipa de risco (ATR entre 0,15% e 3%) e o livro de ordens (spread até 0,05%). Nota de 0 a 100",
             "Compounder: margem de 40% do saldo livre à alavancagem da sessão (máx. 10x), no máximo 2 abertas",
             "Executor: a melhor nota primeiro, 1 abertura a cada 2 min, preenche no livro de ordens com taxa de 0,045%",
             "Risco, a cada 5 s: stop 1,5 ATR; a 2R (o dobro do risco) fecha metade e o stop vai para a entrada; o resto sai com trailing de 1,5 ATR; liquidação e funding à hora",
             "Tudo em papel: a INVO não tem API pública, nenhuma ordem real"]
    repos = [{"agent": a, "repo": perpdesk.REPOS[a][0], "role": perpdesk.REPOS[a][1]} for a in perpdesk.AGENTS]
    running = perp_running()
    rules.append("Dinheiro real (Hyperliquid): só dry-run. Cada trade em papel prepara a ordem real exata, com os limites, e regista-a; nada é assinado nem enviado")
    base = {"durations": list(DURATIONS), "stakes": list(STAKES), "levs": list(LEVERAGES), "rules": rules,
            "running": running, "repos": repos, "venues": ["invo"], "venue": "invo", "cards": CARDS,
            "params": {"stop_atr": p.stop_atr, "half_r": p.half_r, "trail_atr": p.trail_atr, "stack_pct": p.margin_pct,
                       "max_open": p.max_positions, "goal_mult": 10}}
    try:
        st = json.loads((PERP_DIR / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = None
    if not st:
        return {**base, "exists": False, "status": "idle", "positions": [], "trades": [], "log": [], "curve": [],
                "ticker": [], "start_eur": 1000.0, "equity_eur": 1000.0}
    rate = st.get("eur_usd") or 1.0
    positions = []
    for q in st.get("positions", []):
        pnl = q["side"] * q["qty"] * (q["last"] - q["entry"]) - q.get("funding", 0)
        trail = q["peak"] - q["side"] * p.trail_atr * q["atr"] if q.get("half_done") else None
        positions.append({"id": q["id"], "symbol": q["coin"], "name": q["coin"], "side": "long" if q["side"] == 1 else "short",
                          "lev": q["lev"], "value_eur": max(0.0, q["margin"] + pnl) / rate, "cost_eur": q["margin"] / rate,
                          "pnl_eur": pnl / rate, "pct": pnl / q["margin"] if q["margin"] else 0,
                          "mult": (q["margin"] + pnl) / q["margin"] if q["margin"] else 1, "entry": q["entry"],
                          "last": q["last"], "peak": q["peak"], "stop": q["stop"], "liq": q["liq"], "trail": trail,
                          "trail_on": q.get("trail_on"), "half_done": q.get("half_done", False),
                          "half_px": q.get("half_px"), "half_ts": q.get("half_ts"), "opened": q["opened"],
                          "atr": q["atr"], "score": q.get("score"), "series": _series(q.get("series"))})
    trades = [{"id": t.get("id"), "symbol": t["coin"], "side": t["side"], "lev": t.get("lev"), "pnl_eur": t["pnl_eur"],
               "pct": t["pct"], "why": t["reason"], "kind": t.get("kind", "end"), "mult": t.get("mult"),
               "closed": t["closed"], "opened": t.get("opened"), "entry": t.get("entry"), "exit": t.get("exit"),
               "half_px": t.get("half_px"), "half_ts": t.get("half_ts"), "score": t.get("score"),
               "series": _series(t.get("series"), 80)} for t in st.get("trades", [])][::-1]
    eq = st.get("equity_eur", st.get("start_eur"))
    recap = None
    if (bool(st.get("finished")) or not running) and st.get("started"):
        mins = (datetime.fromisoformat(st["ends"]) - datetime.fromisoformat(st["started"])).total_seconds() / 60
        wins = [t for t in trades if t["pnl_eur"] > 0]
        best = max(trades, key=lambda t: t["pnl_eur"], default=None)
        worst = min(trades, key=lambda t: t["pnl_eur"], default=None)
        recap = {"minutes": round(mins), "start_eur": st["start_eur"], "end_eur": eq, "trades": len(trades),
                 "wins": len(wins), "coins_seen": st.get("coins_seen", 0),
                 "best": best and {"symbol": best["symbol"], "pnl_eur": best["pnl_eur"]},
                 "worst": worst and {"symbol": worst["symbol"], "pnl_eur": worst["pnl_eur"]},
                 "started": st["started"], "ended": st["ends"], "win_rate": len(wins) / len(trades) if trades else None,
                 "stop_txt": f"Compounder 40% · metade a 2R · stop {p.stop_atr:g} ATR · {st.get('lev', 5):g}x".replace(".", ",")}
    return {**base, "exists": True, "status": "live" if running else "finished", "started": st["started"],
            "ends": st["ends"], "start_eur": st["start_eur"], "equity_eur": eq, "positions": positions,
            "trades": trades, "log": st.get("events", [])[-80:][::-1], "ticker": st.get("ticker", []),
            "counts": st.get("counts", {}), "ready": st.get("ready", []), "pools_seen": st.get("coins_seen", 0),
            "curve": [[t, v / rate] for t, v in st.get("curve", [])][-1500:], "recap": recap, "mode": "futuros",
            "agents": st.get("agents", {}), "feeds": st.get("feeds", {}), "lev": st.get("lev", 5),
            "cash_eur": st.get("cash", 0) / rate, "live": st.get("live") or {},
            "wins": sum(1 for t in trades if t["pnl_eur"] > 0), "losses": sum(1 for t in trades if t["pnl_eur"] <= 0)}


def overview(bt: dict | None = None) -> dict:
    items = []
    for name in ("memes", "futuros", "acoes"):
        d = desk(name, bt)
        if name == "futuros" and (perp_running() or (PERP_DIR / "state.json").exists()):
            d = {**desk("perps", bt), "name": "futuros"}
        curve = d.get("curve") or []
        step = max(1, len(curve) // 60)
        items.append({k: d.get(k) for k in ("name", "title", "subtitle", "blurb", "color", "mode", "status",
                                            "start_eur", "equity_eur", "started", "ends")}
                     | {"positions": len(d.get("positions") or []), "trades": len(d.get("trades") or []),
                        "spark": [v for _, v in curve[::step]][-60:]})
    return {"desks": items, "now": datetime.now(timezone.utc).isoformat()}


def pause(name: str, paused: bool) -> tuple[int, dict]:
    if name not in ("futuros", "acoes"):
        return 400, {"error": "só Futuros e Ações têm pausa (Memecoins para-se pela sessão)"}
    return 200, set_paused(name, paused)
