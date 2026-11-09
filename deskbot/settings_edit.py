"""Editable parameters for the dashboard's Parameters tab.

Each parameter has a range; values outside it are refused. Saving rewrites only the value on
that key's line in config.toml (comments and layout stay), checks the file still loads, and
logs the change in the journal. Sleeve-specific keys ([[sleeves]]) are not editable here.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from .config import ROOT, Settings, load_settings

CONFIG = ROOT / "config.toml"

# (agent, section, key, label, help, min, max, step, kind)  kind: x | atr | r | pct | int | num
PARAMS = [
    ("Scout", "strategy", "volume_mult", "Volume mínimo do breakout",
     "O volume do dia do breakout tem de ser pelo menos isto vezes a média de 20 dias.", 1.0, 3.0, 0.1, "x"),
    ("Scout", "strategy", "trend_ema", "EMA de tendência da moeda",
     "A moeda tem de fechar acima desta média móvel.", 20, 200, 5, "int"),
    ("Scout", "strategy", "regime_ema", "EMA de regime do BTC",
     "Só compra enquanto o BTC fecha acima desta média (mercado risk-on).", 100, 300, 10, "int"),
    ("Scout", "strategy", "rsi_max", "RSI máximo",
     "Não compra moedas já muito sobrecompradas.", 60, 100, 1, "num"),
    ("Auditor", "strategy", "min_room_r", "Espaço mínimo até à resistência",
     "O máximo do último ano tem de estar pelo menos a isto, em R, da entrada. 0 desliga.", 0.0, 5.0, 0.5, "r"),
    ("Analyst", "strategy", "max_extension_atr", "Não perseguir a vela",
     "Não compra se o fecho já está mais de X ATR acima do nível de breakout. 0 desliga.", 0.0, 4.0, 0.25, "atr"),
    ("Analyst", "strategy", "min_quality", "Nota mínima do Analyst",
     "Média das 4 perguntas (tendência, volume, espaço, perseguição), de 0 a 100. 0 desliga.", 0, 90, 5, "int"),
    ("Charts", "strategy", "stop_atr", "Stop inicial",
     "Distância do stop à entrada, em ATR. Só vale para posições novas.", 1.0, 4.0, 0.25, "atr"),
    ("Executor", "risk", "risk_per_trade", "Risco por trade",
     "Quanto da sleeve se perde se o stop for tocado.", 0.0025, 0.03, 0.0025, "pct"),
    ("Executor", "risk", "max_position_pct", "Tamanho máximo de uma posição",
     "Teto do valor de uma posição, em % da sleeve.", 0.05, 0.5, 0.05, "pct"),
    ("Executor", "risk", "max_positions", "Posições por sleeve",
     "Máximo de posições abertas ao mesmo tempo em cada sleeve.", 1, 10, 1, "int"),
    ("Executor", "strategy", "max_entry_gap_pct", "Preço fugiu antes da compra",
     "Não compra se o preço subiu mais do que isto entre o fecho do sinal e a compra. 0 desliga.", 0.0, 0.2, 0.01, "pct"),
    ("Risk", "strategy", "trail_atr", "Trailing stop",
     "O stop sobe para X ATR abaixo do fecho mais alto. Nunca desce.", 1.5, 6.0, 0.25, "atr"),
    ("Risk", "strategy", "trail_start_r", "Trailing só depois de",
     "O trailing só começa quando o fecho passa entrada + X R. 0 = desde o início.", 0.0, 3.0, 0.25, "r"),
    ("Risk", "strategy", "timeout_days", "Dinheiro parado: dias",
     "Sai se ao fim de N dias a posição ainda não subiu o mínimo abaixo. 0 desliga.", 0, 60, 1, "int"),
    ("Risk", "strategy", "timeout_min_r", "Dinheiro parado: mínimo",
     "O ganho mínimo, em R, para a posição não ser fechada por tempo.", 0.0, 2.0, 0.25, "r"),
    ("Risk", "risk", "max_drawdown_halt", "Kill switch",
     "Sem novas entradas se a conta cair isto desde o pico.", 0.05, 0.5, 0.05, "pct"),
    ("Cético", "risk", "min_rr", "Reward:risk mínimo",
     "Regra dura do Cético na hora da compra. Não entra no backtest.", 1.0, 4.0, 0.25, "num"),
]
INT_KEYS = {"trend_ema", "regime_ema", "max_positions", "timeout_days"}
BY_KEY = {(sec, key): (sec, key, lo, hi) for _, sec, key, _, _, lo, hi, _, _ in PARAMS}


def current(s: Settings) -> dict[str, float]:
    return {f"{sec}.{key}": getattr(getattr(s, sec), key) for _, sec, key, *_ in PARAMS}


def schema(s: Settings) -> list[dict]:
    vals = current(s)
    return [{"agent": a, "section": sec, "key": key, "id": f"{sec}.{key}", "label": label, "help": hlp,
             "min": lo, "max": hi, "step": st, "kind": kind, "value": vals[f"{sec}.{key}"]}
            for a, sec, key, label, hlp, lo, hi, st, kind in PARAMS]


def validate(changes: dict) -> dict[tuple[str, str], float]:
    """{"strategy.timeout_days": 15} -> {("strategy", "timeout_days"): 15}. Raises ValueError."""
    out = {}
    for ident, value in changes.items():
        sec, _, key = str(ident).partition(".")
        if (sec, key) not in BY_KEY:
            raise ValueError(f"{ident} não é editável")
        _, _, lo, hi = BY_KEY[(sec, key)]
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: valor inválido") from None
        if not lo <= v <= hi:
            raise ValueError(f"{key}: tem de estar entre {lo:g} e {hi:g}")
        out[(sec, key)] = int(round(v)) if key in INT_KEYS else round(v, 6)
    return out


def apply(s: Settings, changes: dict[tuple[str, str], float]) -> Settings:
    """Settings with the changes applied to the base strategy, every sleeve and the risk block."""
    strat = {k: v for (sec, k), v in changes.items() if sec == "strategy"}
    risk = {k: v for (sec, k), v in changes.items() if sec == "risk"}
    sleeves = tuple(replace(sl, params=replace(sl.params, **strat)) for sl in s.sleeves)
    return replace(s, strategy=replace(s.strategy, **strat), sleeves=sleeves, risk=replace(s.risk, **risk))


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return repr(float(v))


def write(changes: dict[tuple[str, str], float], path: Path = CONFIG) -> None:
    """Rewrites the values in place; a key missing from its section is appended to it."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    todo = dict(changes)
    section, last_key_line = None, {}
    for i, line in enumerate(lines):
        h = re.match(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*$", line)
        if h:
            section = None if line.lstrip().startswith("[[") else h.group(1)
            continue
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)([^#\r\n]*?)(\s*#.*)?(\r?\n)?$", line)
        if not m or section is None:
            continue
        last_key_line[section] = i
        key = m.group(2)
        if (section, key) in todo:
            value = _fmt(todo.pop((section, key)))
            head, comment, nl = f"{m.group(1)}{key}{m.group(3)}", m.group(5) or "", m.group(6) or ""
            if comment:  # keep the comment in the same column
                width = len(m.group(4)) + len(comment) - len(comment.lstrip())
                lines[i] = f"{head}{value}{' ' * max(1, width - len(value))}{comment.lstrip()}{nl}"
            else:
                lines[i] = f"{head}{value}{nl}"
    for (sec, key), v in todo.items():
        if sec not in last_key_line:
            raise ValueError(f"secção [{sec}] não existe no config.toml")
        i = last_key_line[sec]
        lines.insert(i + 1, f"{key} = {_fmt(v)}\n")
        last_key_line = {k: (j + 1 if j > i else j) for k, j in last_key_line.items()}
    backup = text
    path.write_text("".join(lines), encoding="utf-8")
    try:
        load_settings(path)
    except Exception:
        path.write_text(backup, encoding="utf-8")
        raise
