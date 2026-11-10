"""The paper desks as the phone app sees them. The app shows two: Memecoins (FOMO) and Futuros (INVO).

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
from .guard import Brakes, dur, results
from .paperdesk import DESKS_DIR, controls, read_state, set_paused

log = logging.getLogger(__name__)
MEME_DIR = ROOT / "memetest"
PERP_DIR = ROOT / "perpdesk"
DURATIONS = (5, 10, 15, 30, 120, 720)

META = {
    "memes": {"title": "Memecoins", "mode": "memecoins", "color": "#ff4fa3",
              "subtitle": "memecoins da Solana · FOMO · sessões de 5 min a 12 h",
              "blurb": "5 agentes (Scanner, Analista, Compounder, Executor, Risco). Compras pequenas, metade a 2×, trailing 10%, stop −8%."},
    "futuros": {"title": "Futuros", "mode": "futuros", "color": "#7b61ff",
                "subtitle": "perpétuos de cripto · long e short · velas de 4 h",
                "blurb": "BTC, ETH, SOL, BNB e XRP. Long e short, alavancagem máxima 10x, 1% de risco por trade."},
    "perps": {"title": "Futuros", "mode": "futuros", "color": "#7b61ff",
              "subtitle": "perpétuos da Hyperliquid · INVO · sessões de 5 min a 12 h",
              "blurb": "5 agentes na Hyperliquid (a da INVO). Long e short até 10x, arrisca 1% do saldo por trade, metade a 2R, trailing."},
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


STALL_S = 180                   # a running session writes its state every few seconds
HEALTH = ("feed_stale", "blind_s", "max_gap_s", "tick_ms", "max_tick_ms", "risk_errors", "rate_limited", "quote_fail")


def _when(iso) -> datetime | None:
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _health(st: dict, folder: Path, running: bool) -> dict:
    """What tells a healthy session from one that only looks alive: when it last wrote its state, whether it
    stopped without finishing (a crash, or the computer asleep), the brakes and the alarms it raised."""
    now = datetime.now(timezone.utc)
    try:
        wrote = datetime.fromtimestamp((folder / "state.json").stat().st_mtime, timezone.utc)
    except OSError:
        wrote = now
    wrote = _when(st.get("saved")) or wrote           # the session's own clock: copying the file changes its date
    age = max(0.0, (now - wrote).total_seconds())
    ends = _when(st.get("ends"))
    # a session that is not running and never wrote "finished" died on the way, whether its hour has come or not
    stalled = not st.get("finished") and (age > STALL_S if running else True)
    ended = min(ends, wrote) if ends else wrote       # a session that died early ended when it last wrote
    alerts = [e for e in st.get("events", []) if e.get("verdict") == "alert"][-10:][::-1]
    return {"stalled": stalled, "stale_s": round(age), "last_write": wrote.isoformat(timespec="seconds"),
            "ended": ended.isoformat(timespec="seconds"), "guard": st.get("guard") or {}, "alerts": alerts,
            "legacy": "guard" not in st,          # a session started before the brakes existed: it runs the old rules
            # a finished session watches nothing: "sem preços" from an outage it ended in is not said for ever
            **{k: st[k] for k in HEALTH if k in st and not (k == "feed_stale" and st.get("finished"))}}


STAKES = (20, 50, 110, 500, 1000)
MEME_STAKES = tuple(s for s in STAKES if s >= 50)   # 15% of a smaller desk is under the $5 minimum buy: it would never trade
VENUES = ("fomo",)              # where the memes app opens each coin (orders stay in paper)
LEVERAGES = (2, 5, 10)


def _check(minutes: int, eur: float, stakes: tuple = STAKES) -> str:
    if minutes not in DURATIONS:
        return f"duração tem de ser {', '.join(map(str, DURATIONS))} minutos"
    if eur not in stakes:
        return f"o saldo tem de ser {', '.join(map(str, stakes))} €"
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


def start_memes(minutes: int, eur: float = 110, venue: str = "fomo", style: str = "poucas") -> tuple[int, dict]:
    if eur in STAKES and eur not in MEME_STAKES:
        return 400, {"error": f"na mesa de memes o saldo mínimo é {MEME_STAKES[0]} €: com menos, nenhuma compra chega ao mínimo de 5 $"}
    if err := _check(minutes, eur, MEME_STAKES):      # the stakes this desk takes, not the futures desk's list
        return 400, {"error": err}
    venue = venue if venue in VENUES else "fomo"
    style = style if style in ("poucas", "mais") else "poucas"
    if meme_running():
        return 409, {"error": "Já há uma sessão de memes a correr"}
    _spawn(MEME_DIR, ["memedesk", "--minutes", str(minutes), "--eur", str(eur), "--venue", venue, "--style", style])
    log.info("Memes session started from the app: %d min, %s EUR, %s, %s", minutes, eur, venue, style)
    return 202, {"started": True, "minutes": minutes, "eur": eur, "venue": venue, "style": style}


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
    p, b = memedesk.memes_params(), Brakes()
    rules = ["Scanner: a cada minuto lê cerca de 300 tokens da Solana (listas da Jupiter, pools em alta e moedas que saíram do pump.fun); corta liquidez < $40 000, +80% numa hora, mint ou freeze ativos e moedas com menos de 10 min. As 2 melhores seguem; depois as baleias: 3 compras de $500+ em 30 min e mais compras do que vendas",
             "Analista: 5 votos bull/bear, a equipa de risco (mint e freeze revogados, top 10 < 25%, sem honeypot, há vendas) e a rota Jupiter (até 2% de impacto). Nota de 0 a 100",
             f"Compounder: cada compra é {p.max_trade_pct:.0%} da mesa, nunca mais de {p.stack_pct:.0%} do dinheiro livre nem de {p.max_pool_pct:.0%} da liquidez da pool; no máximo {p.max_deployed_pct:.0%} da mesa investido e {p.max_positions} abertas; não compra abaixo de {memedesk.MIN_TRADE_USD:g} $; nunca reforça uma moeda",
             "Executor: a melhor nota primeiro, 1 compra a cada 2 min, ao preço de uma cotação real da Jupiter mais 0,5% de custos",
             f"Risco, a cada 5 s: stop −{p.stop_pct:.0%} enquanto não chega a 2×; a 2× vende metade (a entrada volta); o resto sai com trailing de {p.trail_pct:.0%} abaixo do máximo; sai também se a liquidez da pool cair para metade ou for retirada",
             f"Travões: sem novas compras se a sessão perder {b.max_session_loss:.0%} ou o dia {b.max_day_loss:.0%}; {b.max_losses} perdas seguidas dão uma pausa de {dur(b.pause_s)}; a moeda que fechou a perder não volta a entrar nesta sessão; se o computador esteve parado, {dur(memedesk.PAUSE_AFTER_GAP_S)} sem compras. As saídas nunca param",
             "Contas: a venda conta ao preço de uma cotação real da Jupiter; sem cotação, é estimada pela liquidez da pool; com a pool vazia conta zero",
             "Tudo em papel: nenhuma carteira, nenhuma ordem real"]
    repos = [{"agent": a, "repo": memedesk.REPOS[a][0], "role": memedesk.REPOS[a][1]} for a in memedesk.AGENTS]
    if os.getenv("GMGN_API_KEY") or _env_has("GMGN_API_KEY"):
        rules.insert(2, "GMGN (chave tua): moedas em alta da GMGN, smart money e KOLs no SMARTS, rug score, wash trading e bots na equipa de risco")
    base = {"durations": list(DURATIONS), "stakes": list(MEME_STAKES), "rules": rules, "running": running, "repos": repos,
            "venues": list(VENUES), "venue": "fomo", "gmgn_key": bool(os.getenv("GMGN_API_KEY") or _env_has("GMGN_API_KEY")),
            "cards": CARDS,
            "params": {"stop_pct": p.stop_pct, "half_at": memedesk.HALF_AT, "trail_pct": p.trail_pct,
                       "stack_pct": p.stack_pct, "max_trade_pct": p.max_trade_pct, "max_deployed_pct": p.max_deployed_pct,
                       "max_open": p.max_positions, "min_trade_usd": memedesk.MIN_TRADE_USD, "goal_mult": 10,
                       "brakes": {"session": b.max_session_loss, "day": b.max_day_loss, "losses": b.max_losses}}}
    if not st:
        return {**base, "exists": False, "status": "idle", "positions": [], "trades": [], "log": [], "curve": [],
                "ticker": [], "start_eur": 1000.0, "equity_eur": 1000.0}
    rate = st.get("eur_usd") or 1.0
    if st.get("style") == "mais":   # the rules as this session runs them
        base["rules"] = [r.replace("3 compras de $500+", "2 compras de $300+").replace("top 10 < 25%", "top 10 < 40%")
                         .replace("Nota de 0 a 100", "Precisa de 3 votos em 5 (modo mais entradas). Nota de 0 a 100")
                         for r in base["rules"]]
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
               "fill_src": t.get("fill_src"), "blind": bool(t.get("blind")),
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
    health = _health(st, MEME_DIR, running)
    if running and health["legacy"]:
        base["rules"] = ["Esta sessão começou antes das regras novas: ainda compra com 40% do saldo livre e não tem travões. As regras abaixo valem a partir da próxima sessão", *base["rules"]]
    finished = bool(st.get("finished")) or not running
    counts = st.get("counts", {})
    closed = results(trades, "pnl_eur")               # one result per position: its half is not a trade of its own
    wins = sum(1 for v in closed if v > 0)
    recap = None
    if finished and st.get("started"):
        mins = (_when(health["ended"]) - _when(st["started"])).total_seconds() / 60
        best = max(trades, key=lambda t: t["pnl_eur"], default=None)
        worst = min(trades, key=lambda t: t["pnl_eur"], default=None)
        size = "Compounder 40%" if health["legacy"] else f"{p.max_trade_pct:.0%} da mesa por compra"
        recap = {"minutes": round(mins), "start_eur": st["start_eur"], "end_eur": eq, "trades": len(closed),
                 "wins": wins, "coins_seen": st.get("pools_seen", 0),
                 "best": best and {"symbol": best["symbol"], "pnl_eur": best["pnl_eur"]},
                 "worst": worst and {"symbol": worst["symbol"], "pnl_eur": worst["pnl_eur"]},
                 "started": st["started"], "ended": health["ended"],
                 "win_rate": wins / len(closed) if closed else None, "stop_pct": st["p"]["stop_pct"],
                 "stop_txt": f"{size} · metade a {memedesk.HALF_AT:g}× · trailing {trail:.0%} · stop −{st['p']['stop_pct']:.0%}"}
    return {**base, "exists": True, "status": "live" if running else "finished", "started": st["started"],
            "ends": st["ends"], "start_eur": st["start_eur"], "equity_eur": eq, "positions": positions,
            "trades": trades, "log": events[-80:][::-1], **health, "ticker": ticker, "counts": counts,
            "ready": st.get("ready", []), "pools_seen": st.get("pools_seen", 0),
            "curve": _series([[t, v / rate] for t, v in st.get("curve", [])], 1500), "recap": recap,
            "mode": st.get("mode", "classic"), "agents": st.get("agents", {}),
            "venue": "fomo", "feeds": st.get("feeds", {}), "cash_eur": st.get("cash", 0) / rate,
            "style": st.get("style") or "poucas", "wins": wins, "losses": len(closed) - wins}


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
    p, b = perpdesk.PerpParams(), Brakes()
    rules = [f"Scanner: os {p.universe} perpétuos mais negociados da Hyperliquid, velas de 15 min com EMA 20 e 50 no mesmo sentido; depois os {p.traders} melhores traders do leaderboard público ({p.min_traders}+ do nosso lado e mais dinheiro do nosso lado)",
             "Analista: 5 votos bull/bear (tendência de 1 h, RSI, funding, volume, VWAP do dia), a equipa de risco (ATR entre 0,15% e 3%) e o livro de ordens (spread até 0,05%). Nota de 0 a 100",
             f"Compounder: cada trade arrisca {p.risk_pct:.0%} do saldo no stop (metade se já há uma posição no mesmo sentido), no máximo {p.max_heat:.0%} em risco ao mesmo tempo e {p.margin_pct:.0%} do saldo livre de margem; alavancagem até à da sessão (máx. 10x), no máximo {p.max_positions} abertas",
             "Executor: a melhor nota primeiro, 1 abertura a cada 2 min, preenche no livro de ordens com taxa de 0,045%",
             "Risco, a cada 5 s: stop 1,5 ATR; a 2R (o dobro do risco) fecha metade e o stop vai para a entrada; o resto sai com trailing de 1,5 ATR; liquidação e funding à hora",
             f"Travões: sem novas entradas se a sessão perder {b.max_session_loss:.0%} ou o dia {b.max_day_loss:.0%}; {b.max_losses} perdas seguidas dão uma pausa de {dur(b.pause_s)}; a moeda que fechou a perder descansa {dur(p.loss_cooldown_min * 60)}. As saídas nunca param",
             "Tudo em papel: a INVO não tem API pública, nenhuma ordem real"]
    repos = [{"agent": a, "repo": perpdesk.REPOS[a][0], "role": perpdesk.REPOS[a][1]} for a in perpdesk.AGENTS]
    running = perp_running()
    rules.append("Dinheiro real (Hyperliquid): só dry-run. Cada trade em papel prepara a ordem real exata, com os limites, e regista-a; nada é assinado nem enviado")
    base = {"durations": list(DURATIONS), "stakes": list(STAKES), "levs": list(LEVERAGES), "rules": rules,
            "running": running, "repos": repos, "venues": ["invo"], "venue": "invo", "cards": CARDS,
            "params": {"stop_atr": p.stop_atr, "half_r": p.half_r, "trail_atr": p.trail_atr, "stack_pct": p.margin_pct,
                       "risk_pct": p.risk_pct, "max_heat": p.max_heat, "max_open": p.max_positions, "goal_mult": 10,
                       "brakes": {"session": b.max_session_loss, "day": b.max_day_loss, "losses": b.max_losses}}}
    try:
        st = json.loads((PERP_DIR / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = None
    if not st:
        return {**base, "exists": False, "status": "idle", "positions": [], "trades": [], "log": [], "curve": [],
                "ticker": [], "start_eur": 1000.0, "equity_eur": 1000.0}
    rate = st.get("eur_usd") or 1.0
    if running and "guard" not in st:
        base["rules"] = ["Esta sessão começou antes das regras novas: ainda usa 40% do saldo livre de margem por trade e não tem travões. As regras abaixo valem a partir da próxima sessão", *rules]
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
               "blind": bool(t.get("blind")),
               "series": _series(t.get("series"), 80)} for t in st.get("trades", [])][::-1]
    eq = st.get("equity_eur", st.get("start_eur"))
    health = _health(st, PERP_DIR, running)
    closed = results(trades, "pnl_eur")               # one result per position: its half is not a trade of its own
    wins = sum(1 for v in closed if v > 0)
    recap = None
    if (bool(st.get("finished")) or not running) and st.get("started"):
        mins = (_when(health["ended"]) - _when(st["started"])).total_seconds() / 60
        best = max(trades, key=lambda t: t["pnl_eur"], default=None)
        worst = min(trades, key=lambda t: t["pnl_eur"], default=None)
        recap = {"minutes": round(mins), "start_eur": st["start_eur"], "end_eur": eq, "trades": len(closed),
                 "wins": wins, "coins_seen": st.get("coins_seen", 0),
                 "best": best and {"symbol": best["symbol"], "pnl_eur": best["pnl_eur"]},
                 "worst": worst and {"symbol": worst["symbol"], "pnl_eur": worst["pnl_eur"]},
                 "started": st["started"], "ended": health["ended"], "win_rate": wins / len(closed) if closed else None,
                 "stop_txt": f"{'Compounder 40%' if health['legacy'] else f'Risco {p.risk_pct:.0%} por trade'} · metade a {p.half_r:g}R · stop {p.stop_atr:g} ATR · {st.get('lev', 5):g}x".replace(".", ",")}
    return {**base, "exists": True, "status": "live" if running else "finished", "started": st["started"],
            "ends": st["ends"], "start_eur": st["start_eur"], "equity_eur": eq, "positions": positions,
            "trades": trades, "log": st.get("events", [])[-80:][::-1], **health, "ticker": st.get("ticker", []),
            "counts": st.get("counts", {}), "ready": st.get("ready", []), "pools_seen": st.get("coins_seen", 0),
            "curve": _series([[t, v / rate] for t, v in st.get("curve", [])], 1500), "recap": recap, "mode": "futuros",
            "agents": st.get("agents", {}), "feeds": st.get("feeds", {}), "lev": st.get("lev", 5),
            "cash_eur": st.get("cash", 0) / rate, "live": st.get("live") or {},
            "wins": wins, "losses": len(closed) - wins}


def overview(bt: dict | None = None) -> dict:
    items = []
    for name in ("memes", "futuros"):   # the app's two desks: Memecoins on FOMO, Futuros on INVO (Hyperliquid)
        d = desk(name, bt) if name == "memes" else {**desk("perps", bt), "name": "futuros"}
        curve = d.get("curve") or []
        step = max(1, len(curve) // 60)
        items.append({k: d.get(k) for k in ("name", "title", "subtitle", "blurb", "color", "mode", "status",
                                            "start_eur", "equity_eur", "started", "ends", "stalled")}
                     | {"positions": len(d.get("positions") or []),
                        "trades": sum(1 for t in d.get("trades") or [] if t.get("kind") != "half"),
                        "spark": [v for _, v in curve[::step]][-60:]})
    return {"desks": items, "now": datetime.now(timezone.utc).isoformat()}


def pause(name: str, paused: bool) -> tuple[int, dict]:
    if name not in ("futuros", "acoes"):
        return 400, {"error": "só Futuros e Ações têm pausa (Memecoins para-se pela sessão)"}
    return 200, set_paused(name, paused)
