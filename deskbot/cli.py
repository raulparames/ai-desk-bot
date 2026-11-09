"""Command line: python -m deskbot <command>"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

import pandas as pd

from . import data
from .config import REPORTS_DIR, ROOT, load_settings
from .desk import px


def _setup_logging(verbose: bool):
    (ROOT / "logs").mkdir(exist_ok=True)
    handlers = [logging.FileHandler(ROOT / "logs" / "bot.log", encoding="utf-8")]
    if sys.stdout is not None:  # None under pythonw.exe (Task Scheduler, no window)
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers)


def _frames(s, refresh=True):
    m = s.market
    return data.load_universe(m.universe, m.timeframe, m.history_start, refresh=refresh)


# ------------------------------------------------------------------ backtest

def cmd_backtest(args):
    from .backtest import (buy_and_hold, equal_weight, metrics, run_backtest, walk_forward)
    from .report import render

    s = load_settings()
    d = _frames(s, refresh=not args.offline)
    reg = s.market.regime_symbol
    n = len(s.sleeves)
    initial = 10_000.0

    def sleeves_run(risk):
        runs = {sl.name: run_backtest(d, sl.params, risk, reg, initial / n) for sl in s.sleeves}
        return runs, sum(r.equity for r in runs.values()), [t for r in runs.values() for t in r.trades]

    def cut(eq, trades, start):
        eq = eq[eq.index >= pd.Timestamp(start)]
        return metrics(eq, [t for t in trades if t.opened >= pd.Timestamp(start)])

    print("Running the 4 sleeves, walk-forward and benchmarks...")
    runs, eq, trades = sleeves_run(s.risk)
    wf = walk_forward(d, s.strategy, s.risk, regime_symbol=reg)
    oos = str(wf.oos_equity.index[1].date())
    ens = initial * (1 + pd.DataFrame({i: r.equity.pct_change() for i, r in enumerate(wf.runs.values())})
                     .fillna(0).mean(axis=1)).cumprod()
    btc, ew = buy_and_hold(d[reg]), equal_weight(d)
    md_params = replace(s.strategy, breakout_lookback=20, volume_mult=1.0, rsi_max=70, stop_pct=0.04,
                        take_profit_pct=0.08, trail_atr=99.0)
    md = run_backtest(d, md_params, s.risk, reg, initial)

    bot_full, bot_oos = metrics(eq, trades), cut(eq, trades, oos)
    btc_oos = metrics(btc[btc.index >= oos])
    main_rows = {
        "Bot, 2019-2026 completo": bot_full,
        f"Bot, fora da amostra ({oos}+)": bot_oos,
        "Bot, 2024+": cut(eq, trades, "2024-01-01"),
        f"BTC comprar e manter ({oos}+)": btc_oos,
        f"Cesto das 10 moedas ({oos}+)": metrics(ew[ew.index >= oos]),
        f"Regras do guia .md: stop 4% / alvo 8% ({oos}+)": cut(md.equity, md.trades, oos),
    }
    selection_rows = {
        "Walk-forward: reotimiza a cada 6 meses": metrics(wf.oos_equity, wf.oos_trades),
        "48 combinações em paralelo, sem escolher": metrics(ens[ens.index >= oos]),
        "4 sleeves fixas (o bot)": bot_oos,
    }
    risk_rows = {}
    for rp in (0.01, 0.015, 0.02, 0.03):
        _, e2, t2 = sleeves_run(replace(s.risk, risk_per_trade=rp))
        risk_rows[f"{rp:.1%} por trade ({oos}+)" + (" (atual)" if rp == s.risk.risk_per_trade else "")] = cut(e2, t2, oos)
    sleeve_rows = {f"{name} ({oos}+)": cut(r.equity, r.trades, oos) for name, r in runs.items()}

    def yearly(series):
        y = series.resample("YE").last()
        y = pd.concat([pd.Series([series.iloc[0]], index=[series.index[0]]), y])
        return (y.pct_change().dropna() * 100).set_axis(y.index[1:].year)

    ytab = pd.DataFrame({"Bot": yearly(eq), "BTC": yearly(btc), "Cesto 10 moedas": yearly(ew)}).dropna()
    caveats = [
        "Backtest não é garantia: os resultados passados não se repetem necessariamente.",
        "Viés de sobrevivência: as 10 moedas foram escolhidas hoje, já sabendo que sobreviveram.",
        "Taxas de 0.1% e slippage de 0.05% por lado incluídas. Funding, spreads em crash e falhas de execução não.",
        "A camada de IA (News, Sentimento, Cético, Chefe) não entra no backtest: um LLM conhece o futuro destes "
        "anos, por isso testá-la no passado seria batota. O efeito dela mede-se em paper trading com `review`.",
        "O bot está fora do mercado na maior parte do tempo (exposição média baixa); a força dele é cortar quedas, "
        "não bater o BTC em anos de euforia.",
        "Os stops no live são verificados a cada 15 minutos ao preço atual; no backtest usam a mínima do dia.",
    ]
    ctx = {
        "subtitle": f"{len(s.market.universe)} moedas, velas diárias, {d[reg].index[0].date()} a "
                    f"{d[reg].index[-1].date()}. Risco {s.risk.risk_per_trade:.1%} por trade, 4 sleeves.",
        "bot_full": bot_full, "bot_oos": bot_oos, "btc_oos": btc_oos,
        "curves": {"Bot (4 sleeves)": eq[eq.index >= btc.index[200]], "BTC": btc[btc.index >= btc.index[200]],
                   "Cesto 10 moedas": ew[ew.index >= btc.index[200]]},
        "main_rows": main_rows, "selection_rows": selection_rows, "risk_rows": risk_rows,
        "sleeve_rows": sleeve_rows, "yearly": ytab, "caveats": caveats,
    }
    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / "backtest.html"
    out.write_text(render(ctx), encoding="utf-8")

    def line(name, m):
        return (f"{name:<52} CAGR {m.get('cagr', 0):+7.1%}  maxDD {m.get('max_drawdown', 0):+7.1%}  "
                f"Sharpe {m.get('sharpe', 0):5.2f}  trades {m.get('trades', '-')}")
    for name, m in {**main_rows, **selection_rows}.items():
        print(line(name, m))
    print(ytab.round(1).to_string())
    print(f"\nReport: {out}")


# ------------------------------------------------------------------ scan / desk

def cmd_scan(args):
    from .strategy import compute_signals, regime_series

    s = load_settings()
    d = _frames(s)
    reg = s.market.regime_symbol
    last = d[reg].index[-1]
    print(f"Last completed daily candle: {last.date()}")
    rows = []
    for sym, df in d.items():
        row = {"symbol": sym}
        for sl in s.sleeves:
            f = compute_signals(df, regime_series(d[reg], sl.params), sl.params)
            if last not in f.index:
                continue
            r = f.loc[last]
            row.update({"close": r["close"], "vol_x": round(r["vol_ratio"], 2), "rsi": round(r["rsi"]),
                        "> EMA50": bool(r["close"] > r["ema_trend"]), "BTC risk-on": bool(r["regime_on"])})
            row[sl.name] = "SINAL" if r["signal"] else f"{r['close'] / r['breakout_level'] - 1:+.1%}"
        rows.append(row)
    print(pd.DataFrame(rows).set_index("symbol").to_string())
    print("\nSleeve columns: SINAL, or how far the close is from that sleeve's breakout level.")


def _make_bot(s, need_broker=True):
    from .brokers import make_broker
    from .journal import Journal
    from .live import Bot
    from .llm import LLM

    return Bot(s, make_broker(s) if need_broker else None, LLM(s.ai), Journal())


def cmd_desk(args):
    from .desk import Desk
    from .llm import LLM
    from .strategy import compute_signals, make_plan, regime_series

    s = load_settings()
    if args.backend:
        s = replace(s, ai=replace(s.ai, backend=args.backend))
    d = _frames(s)
    reg = s.market.regime_symbol
    sym = args.symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    sl = s.sleeves[0]
    f = compute_signals(d[sym], regime_series(d[reg], sl.params), sl.params)
    row = f.iloc[-1]
    price = data.last_prices([sym]).get(sym, float(row["close"]))
    plan = make_plan(sym, row, sl.params, entry=price)
    llm = LLM(s.ai)
    desk = Desk(llm, s.risk, s.ai.news_web_search, s.ai.boss_effort)
    print(f"Desk demo for {sym} on candle {row.name.date()} (signal today: {bool(row['signal'])}). No orders are sent.\n")
    v = desk.discuss(plan, [x.name for x in s.sleeves], [], force_ai=True)
    from .journal import Journal
    Journal().demo(str(row.name.date()), v)  # shows up in the dashboard, ignored by `review`
    print(v.chat_text())
    if v.news and v.news.get("sources"):
        print("\nFontes do News:")
        for src in v.news["sources"]:
            print(f"  - {src['title']} {src['url']}")
    print(f"\nDecisão final: {v.decision}  |  chamadas IA: {llm.calls}  custo estimado: ${llm.cost_usd:.3f}")


# ------------------------------------------------------------------ live

def cmd_run(args):
    s = load_settings()
    bot = _make_bot(s)
    if args.loop:
        bot.loop()
    else:
        verdicts = bot.daily(force=True) if args.force else bot.run_once()
        for v in verdicts:
            print(f"\n=== {v.symbol}: {v.decision} ===\n{v.chat_text()}")
        run_desks()
        cmd_status(args, bot)


def run_desks():
    """The paper desks for futures and stocks ride on the bot's 15-minute cycle; a failure is only logged."""
    import time as _time
    from . import futures, relay, stocks
    pulled = relay.pull_before_cycle()   # what GitHub did while this PC was off
    if pulled:
        logging.getLogger(__name__).info("Took over from GitHub: %s", ", ".join(pulled))
    futures.run_cycle()
    yahoo = stocks.CACHE / "SPY.csv"
    stale = not yahoo.exists() or _time.time() - yahoo.stat().st_mtime > 3 * 3600   # daily data: every 3 h
    stocks.run_cycle(refresh=stale)


def cmd_desks(args):
    run_desks()
    from .paperdesk import read_state
    for name in ("futuros", "acoes"):
        st = read_state(name) or {}
        print(f"{name}: {st.get('equity_eur', 0):,.2f} EUR, {len(st.get('positions', []))} posição(ões), "
              f"{len(st.get('trades', []))} trade(s)")


def cmd_status(args, bot=None):
    s = load_settings()
    bot = bot or _make_bot(s)
    prices = data.last_prices(s.market.universe)
    acct = bot.account(prices)
    bot.state.save()
    print(f"\nBroker: {bot.broker.name} | equity {acct['equity']:,.2f} | cash {acct['cash']:,.2f} | "
          f"drawdown {acct['drawdown']:+.1%}{' | KILL SWITCH' if acct['halted'] else ''}")
    print(f"Last daily candle processed: {bot.state.last_bar or 'never'}")
    if bot.state.positions:
        print("\nOpen positions:")
        for p in bot.state.positions:
            now = prices.get(p.symbol, p.entry)
            print(f"  {p.symbol:<9} [{p.sleeve:<15}] qty {p.qty:.6g} entry {px(p.entry)} now {px(now)} "
                  f"({now / p.entry - 1:+.1%}) stop {px(p.stop)}" + (f" target {px(p.target)}" if p.target else ""))
    else:
        print("No open positions.")
    orders = bot.journal.recent_orders(10)
    if orders:
        print("\nRecent orders:")
        for o in orders:
            print(f"  {o[0]} {o[3]:<4} {o[1]:<9} [{o[2]}] {o[4]:.6g} @ {px(o[5])}  {o[6]}")


def cmd_review(args):
    """Shadow review: what happened after each desk decision, so you can see whether the AI vetoes helped."""
    s = load_settings()
    from .journal import Journal

    rows = Journal().decisions()
    if not rows:
        print("No desk decisions yet. Run the bot for a while first.")
        return
    d = _frames(s)
    out = []
    for r in rows:
        df = d.get(r["symbol"])
        if df is None:
            continue
        after = df[df.index > pd.Timestamp(r["bar_date"])].head(args.days)
        if after.empty:
            continue
        entry, stop = after["open"].iloc[0], r["stop"]
        risk = r["entry"] - stop
        hit = after[after["low"] <= stop]
        end = after.loc[:hit.index[0]] if len(hit) else after
        result = -1.0 if len(hit) else (end["close"].iloc[-1] - entry) / risk
        out.append({"date": r["bar_date"], "symbol": r["symbol"], "decision": r["decision"],
                    "best_R": round((end["high"].max() - entry) / risk, 2), "result_R": round(result, 2),
                    "stopped": bool(len(hit)), "days": len(end)})
    df = pd.DataFrame(out)
    print(df.to_string(index=False))
    print(f"\nAverage R after {args.days} days by desk decision (simplified: fixed stop, no trailing):")
    print(df.groupby("decision")["result_R"].agg(["count", "mean"]).round(2).to_string())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="deskbot", description="AI Desk Bot (paper trading only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest", help="backtest the 4 sleeves, walk-forward and benchmarks; writes reports/backtest.html")
    b.add_argument("--offline", action="store_true", help="use cached data only")
    sub.add_parser("scan", help="show today's breakout signals (no AI, no orders)")
    dk = sub.add_parser("desk", help="run the AI desk on one coin as a demo (no orders)")
    dk.add_argument("symbol")
    dk.add_argument("--backend", choices=["claude-cli", "anthropic", "none"])
    r = sub.add_parser("run", help="one live paper cycle (exits + daily desk if due)")
    r.add_argument("--loop", action="store_true", help="keep running every check_minutes")
    r.add_argument("--force", action="store_true", help="re-run the daily desk even if today's candle was processed")
    sub.add_parser("status", help="account, open positions, recent orders")
    rv = sub.add_parser("review", help="what happened after each desk decision")
    rv.add_argument("--days", type=int, default=20)
    sub.add_parser("desks", help="one cycle of the futures and stocks paper desks")
    sub.add_parser("gh-cycle", help="GitHub Actions: take over the desks when the PC is off (relay)")
    md = sub.add_parser("memedesk", help="memes mode: 5-agent Solana memecoin desk in paper (like the gmgn video)")
    md.add_argument("--minutes", type=float, default=720)
    md.add_argument("--eur", type=float, default=110)
    mt = sub.add_parser("memetest", help="paper test of a Solana memecoin desk (no wallet, no orders)")
    mt.add_argument("--minutes", type=float, default=30)
    mt.add_argument("--eur", type=float, default=1000)
    db = sub.add_parser("dashboard", help="local web dashboard on http://127.0.0.1:8899")
    db.add_argument("--port", type=int, default=None, help="default: [dashboard] port in config.toml")
    db.add_argument("--lan", action="store_true", default=None, help="also listen on the home network (phone)")
    db.add_argument("--local", dest="lan", action="store_false", help="only this PC")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _setup_logging(args.verbose)
    {"backtest": cmd_backtest, "scan": cmd_scan, "desk": cmd_desk, "run": cmd_run,
     "status": cmd_status, "review": cmd_review, "dashboard": cmd_dashboard, "memetest": cmd_memetest, "desks": cmd_desks, "memedesk": cmd_memedesk,
     "gh-cycle": cmd_gh_cycle}[args.cmd](args)


def cmd_dashboard(args):
    from .dashboard import serve
    serve(load_settings(), args.port, args.lan)


def cmd_memetest(args):
    from . import memetest
    fh = logging.FileHandler(ROOT / "logs" / "memetest.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("memetest").addHandler(fh)
    print(memetest.run(args.minutes, args.eur))


def cmd_memedesk(args):
    from . import memedesk
    fh = logging.FileHandler(ROOT / "logs" / "memetest.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("memetest").addHandler(fh)
    print(memedesk.run(args.minutes, args.eur))


def cmd_gh_cycle(args):
    from . import relay
    print(relay.gh_cycle())
