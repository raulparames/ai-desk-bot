"""Self-contained HTML backtest report (inline SVG, no external files)."""

from __future__ import annotations

import html
import math

import pandas as pd

COLORS = ["#2f6fdb", "#e0703a", "#8a8f98", "#2e9e6a"]


def _pct(x, digits=1):
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{digits}f}%"


def _num(x, digits=2):
    if x is None or (isinstance(x, float) and (math.isnan(x))):
        return "-"
    return "inf" if x == float("inf") else f"{x:.{digits}f}"


def equity_svg(series: dict[str, pd.Series], width=920, height=340) -> str:
    """Log-scale line chart, all series rebased to 1 at their first point."""
    pad_l, pad_r, pad_t, pad_b = 56, 16, 16, 28
    clean = {k: (v / v.iloc[0]).resample("W").last().dropna() for k, v in series.items() if len(v) > 1}
    t0 = min(s.index[0] for s in clean.values())
    t1 = max(s.index[-1] for s in clean.values())
    lo = min(s.min() for s in clean.values())
    hi = max(s.max() for s in clean.values())
    llo, lhi = math.log10(lo), math.log10(hi)
    span = (t1 - t0).total_seconds() or 1

    def xy(t, v):
        x = pad_l + (t - t0).total_seconds() / span * (width - pad_l - pad_r)
        y = pad_t + (lhi - math.log10(v)) / (lhi - llo or 1) * (height - pad_t - pad_b)
        return f"{x:.1f},{y:.1f}"

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Curva de capital">']
    for k in range(math.floor(llo), math.ceil(lhi) + 1):
        for m in (1, 2, 5):
            v = m * 10 ** k
            if lo <= v <= hi:
                y = xy(t0, v).split(",")[1]
                parts.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y}" y2="{y}" class="grid"/>'
                             f'<text x="{pad_l - 6}" y="{float(y) + 4}" class="tick" text-anchor="end">{v:g}x</text>')
    for year in range(t0.year + 1, t1.year + 1):
        x = xy(pd.Timestamp(year=year, month=1, day=1), lo).split(",")[0]
        parts.append(f'<text x="{x}" y="{height - 8}" class="tick" text-anchor="middle">{year}</text>')
    for i, (name, s) in enumerate(clean.items()):
        pts = " ".join(xy(t, v) for t, v in s.items())
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{COLORS[i % len(COLORS)]}" '
                     f'stroke-width="{2.2 if i == 0 else 1.4}"/>')
    parts.append("</svg>")
    legend = "".join(f'<span class="key"><i style="background:{COLORS[i % len(COLORS)]}"></i>{html.escape(n)}</span>'
                     for i, n in enumerate(clean))
    return f'<div class="legend">{legend}</div>' + "".join(parts)


def metrics_table(rows: dict[str, dict], cols: list[tuple[str, str, str]]) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label, _ in cols)
    body = []
    for name, m in rows.items():
        cells = []
        for key, _, kind in cols:
            v = m.get(key)
            cells.append(f"<td>{_pct(v) if kind == 'pct' else _num(v, 0 if kind == 'int' else 2)}</td>")
        body.append(f"<tr><th scope='row'>{html.escape(name)}</th>{''.join(cells)}</tr>")
    return f"<table><thead><tr><th></th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


MAIN_COLS = [("cagr", "CAGR", "pct"), ("total_return", "Retorno total", "pct"),
             ("max_drawdown", "Queda máx.", "pct"), ("sharpe", "Sharpe", "num"),
             ("calmar", "Calmar", "num"), ("trades", "Trades", "int"),
             ("win_rate", "Acerto", "pct"), ("profit_factor", "Profit factor", "num")]


def render(ctx: dict) -> str:
    css = """
:root{--bg:#fbfbfa;--fg:#1d1f23;--muted:#666b73;--line:#e3e4e6;--card:#fff;--accent:#2f6fdb}
@media (prefers-color-scheme:dark){:root{--bg:#141518;--fg:#e8e9eb;--muted:#9a9fa8;--line:#2b2d32;--card:#1b1d21;--accent:#6f9ef0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 10px}
.sub{color:var(--muted);margin:0 0 24px}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.tile span{color:var(--muted);font-size:13px}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:14px}th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th[scope=row],thead th:first-child{text-align:left}thead th{color:var(--muted);font-weight:600}
svg{width:100%;height:auto;display:block}.grid{stroke:var(--line)}.tick{fill:var(--muted);font-size:11px}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:6px;font-size:13px}.key i{display:inline-block;width:14px;height:3px;margin-right:6px;vertical-align:middle}
ul{padding-left:20px}li{margin:4px 0}.note{color:var(--muted);font-size:13px}
"""
    m = ctx["bot_oos"]
    tiles = [("CAGR desde 2021-07", _pct(m["cagr"])), ("Queda máxima", _pct(m["max_drawdown"])),
             ("Sharpe", _num(m["sharpe"])), ("BTC no mesmo período", _pct(ctx["btc_oos"]["cagr"]) + " /ano"),
             ("Queda máx. do BTC", _pct(ctx["btc_oos"]["max_drawdown"])), ("Trades (total)", str(ctx["bot_full"]["trades"]))]
    tiles_html = "".join(f'<div class="tile"><b>{v}</b><span>{html.escape(k)}</span></div>' for k, v in tiles)
    yearly = ctx["yearly"]
    ytab = "<table><thead><tr><th>Ano</th>" + "".join(f"<th>{html.escape(c)}</th>" for c in yearly.columns) + \
           "</tr></thead><tbody>" + "".join(
        f"<tr><th scope='row'>{y}</th>" + "".join(f"<td>{v:+.1f}%</td>" for v in row) + "</tr>"
        for y, row in yearly.iterrows()) + "</tbody></table>"
    caveats = "".join(f"<li>{html.escape(c)}</li>" for c in ctx["caveats"])
    return f"""<!doctype html><html lang="pt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Desk Bot backtest</title><style>{css}</style></head>
<body><main>
<h1>AI Desk Bot: backtest</h1>
<p class="sub">{html.escape(ctx['subtitle'])}</p>
<div class="tiles">{tiles_html}</div>
<h2>Curva de capital (escala log)</h2>
<div class="card">{equity_svg(ctx['curves'])}</div>
<h2>Bot vs. comprar e manter</h2>
<div class="card">{metrics_table(ctx['main_rows'], MAIN_COLS)}</div>
<p class="note">"Fora da amostra" começa em 2021-07-18, o primeiro dia que o walk-forward podia testar depois de 2 anos de treino.</p>
<h2>Retorno por ano</h2>
<div class="card">{ytab}</div>
<h2>Escolher parâmetros vs. não escolher</h2>
<div class="card">{metrics_table(ctx['selection_rows'], MAIN_COLS)}</div>
<p class="note">Reotimizar os parâmetros a cada 6 meses rendeu menos fora da amostra do que correr conjuntos fixos em paralelo. Por isso o bot usa 4 sleeves fixas.</p>
<h2>Nível de risco por trade</h2>
<div class="card">{metrics_table(ctx['risk_rows'], MAIN_COLS)}</div>
<h2>Cada sleeve</h2>
<div class="card">{metrics_table(ctx['sleeve_rows'], MAIN_COLS)}</div>
<h2>Limitações</h2><ul>{caveats}</ul>
</main></body></html>"""
