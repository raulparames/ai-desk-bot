"""The Auditor: the Scout's entry rules as ordered layers. The first red layer cuts the coin.

Adapted from the "5 agents" desk guide: fixed order, 1 red kills, the layers after it read
"not checked", and every cut names the number that failed. Passing every layer is the same
thing as `signal` in strategy.compute_signals (a test checks it).
"""

from __future__ import annotations

import math

import pandas as pd

from .config import StrategyParams

OK, FAIL, SKIP, OFF = "ok", "fail", "skip", "off"
OPTIONAL = "filtro opcional, liga-se em Parâmetros"


def _f(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return math.nan
    return v


def _pct(v: float) -> str:
    return f"{v * 100:+.1f}%".replace(".", ",")


def layers(row: pd.Series, p: StrategyParams, btc_gap: float | None = None) -> list[dict]:
    """Each layer: key, name, rule, value (text), ok (bool or None when the rule is off)."""
    close, ema, level = _f(row["close"]), _f(row["ema_trend"]), _f(row["breakout_level"])
    vol, rsi, room = _f(row["vol_ratio"]), _f(row["rsi"]), _f(row.get("room_r"))
    ext, quality = _f(row.get("extension_atr")), _f(row.get("quality"))
    regime = bool(row["regime_on"])
    out = [
        {"key": "regime", "name": "Regime do mercado", "rule": f"BTC acima da EMA{p.regime_ema}",
         "value": (f"BTC {_pct(btc_gap)} vs EMA{p.regime_ema}" if btc_gap is not None else
                   ("risk-on" if regime else "risk-off")), "ok": regime},
        {"key": "trend", "name": "Tendência da moeda", "rule": f"fecho acima da EMA{p.trend_ema}",
         "value": f"{_pct(close / ema - 1)} vs EMA{p.trend_ema}", "ok": close > ema},
        {"key": "breakout", "name": "Breakout", "rule": f"fecho acima do máximo de {p.breakout_lookback} dias",
         "value": f"{_pct(close / level - 1)} vs máximo", "ok": close > level},
        {"key": "volume", "name": "Volume", "rule": f"≥ {p.volume_mult:g}x a média de {p.volume_window} dias",
         "value": f"{vol:.2f}x".replace(".", ","), "ok": vol >= p.volume_mult},
        {"key": "rsi", "name": "Sobrecompra", "rule": f"RSI abaixo de {p.rsi_max:g}",
         "value": f"RSI {rsi:.0f}", "ok": rsi < p.rsi_max},
        {"key": "room", "name": "Espaço até à resistência", "rule": (f"≥ {p.min_room_r:g}R até ao máximo do último ano" if p.min_room_r > 0 else OPTIONAL),
         "value": "sem resistência acima" if not math.isfinite(room) else f"{room:.1f}R".replace(".", ","),
         "ok": None if p.min_room_r <= 0 else (not math.isfinite(room) or room >= p.min_room_r)},
        {"key": "chase", "name": "Não persegue a vela", "rule": (f"fecho ≤ {p.max_extension_atr:g} ATR acima do nível" if p.max_extension_atr > 0 else OPTIONAL),
         "value": (f"{abs(ext):.1f} ATR {'acima' if ext >= 0 else 'abaixo'}".replace(".", ",")
                   if math.isfinite(ext) else "—"),
         "ok": None if p.max_extension_atr <= 0 else (math.isfinite(ext) and ext <= p.max_extension_atr)},
        {"key": "quality", "name": "Nota do Analyst", "rule": (f"nota ≥ {p.min_quality:g}/100" if p.min_quality > 0 else OPTIONAL),
         "value": f"{quality:.0f}/100" if math.isfinite(quality) else "—",
         "ok": None if p.min_quality <= 0 else (math.isfinite(quality) and quality >= p.min_quality)},
    ]
    return out


def audit(row: pd.Series, p: StrategyParams, btc_gap: float | None = None) -> dict:
    """Runs the layers in order. Returns the layers with a status and the first one that failed."""
    result, failed = [], None
    for layer in layers(row, p, btc_gap):
        if layer["ok"] is None:
            status = OFF
        elif failed is not None:
            status = SKIP
        elif layer["ok"]:
            status = OK
        else:
            status = FAIL
            failed = layer
        result.append({**layer, "status": status})
    active = [x for x in result if x["status"] != OFF]
    return {"layers": result, "passed": failed is None, "failed": failed,
            "checked": sum(1 for x in active if x["status"] in (OK, FAIL)), "total": len(active)}
