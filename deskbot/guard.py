"""Brakes shared by the two paper desks. They only ever stop NEW entries: open positions keep their exits.

  session   the equity fell 10% below the session's start: no new entries until the session ends
  day       today's result (UTC, across the sessions of this desk) reached -6% of the stake: none until
            midnight UTC. What earlier sessions lost today is kept in <desk>/day.json
  streak    3 positions in a row closed at a loss: a 60-minute pause, then entries come back by themselves

The numbers are judgment values, not fitted to anything. They cut the size of a bad day; they do not make
a strategy profitable.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Brakes:
    max_session_loss: float = 0.10
    max_day_loss: float = 0.06
    max_losses: int = 3
    pause_s: float = 3600


def _today(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


def dur(seconds: float) -> str:
    """95 -> '95 s', 600 -> '10 min', 10400 -> '2 h 53 min'."""
    s = int(max(0, seconds))
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


class Guard:
    def __init__(self, folder: Path, start_usd: float, brakes: Brakes | None = None, now: float | None = None):
        now = time.time() if now is None else now
        self.file = Path(folder) / "day.json"
        self.b = brakes or Brakes()
        self.start = float(start_usd)
        self.kind = ""              # "", "session", "day" or "streak"
        self.reason = ""
        self.until = 0.0            # only the streak pause has an end time
        self.streak = 0
        self.by_pos: dict[str, float] = {}
        self.day = _today(now)
        self.prior = self._load()   # what earlier sessions of this desk made or lost today
        self.realised = 0.0         # what this session closed today
        self.anchor = self.start    # equity at the start of the UTC day, or of the session
        self.day_pnl = self.prior

    # ------------------------------------------------------------ the day file
    def _load(self) -> float:
        try:
            d = json.loads(self.file.read_text(encoding="utf-8"))
            return float(d["realised_usd"]) if d.get("date") == self.day else 0.0
        except (OSError, ValueError, KeyError, TypeError):
            return 0.0

    def _store(self):
        try:
            self.file.write_text(json.dumps({"date": self.day, "realised_usd": round(self.prior + self.realised, 6)}),
                                 encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ what the desk tells it
    def closed(self, pos_id: str, pnl_usd: float, full: bool) -> float | None:
        """A half or a full close. Returns the whole position's result when it is fully closed."""
        self.by_pos[pos_id] = self.by_pos.get(pos_id, 0.0) + pnl_usd
        self.realised += pnl_usd
        self._store()
        if not full:
            return None
        total = self.by_pos.pop(pos_id)
        self.streak = self.streak + 1 if total < 0 else 0
        return total

    def check(self, equity: float, now: float | None = None) -> list[str]:
        """Called before every possible entry. Returns the lines to announce (a brake went on or came off)."""
        now = time.time() if now is None else now
        out = []
        today = _today(now)
        if today != self.day:
            self.day, self.prior, self.realised, self.anchor = today, 0.0, 0.0, equity
            self._store()
            if self.kind == "day":
                out.append(self._set("", "novo dia (UTC): entradas de volta"))
        if self.kind == "streak" and now >= self.until:
            self.streak = 0
            out.append(self._set("", "pausa terminada: entradas de volta"))
        self.day_pnl = self.prior + equity - self.anchor
        if self.kind == "session":
            return out
        if equity <= self.start * (1 - self.b.max_session_loss):
            out.append(self._set("session", f"perda da sessão chegou a {self.b.max_session_loss:.0%}: "
                                            "sem novas entradas até ao fim da sessão"))
        elif self.kind != "day":
            if self.day_pnl <= -self.b.max_day_loss * self.start:
                out.append(self._set("day", f"perda do dia chegou a {self.b.max_day_loss:.0%}: "
                                            "sem novas entradas até à meia-noite UTC"))
            elif not self.kind and self.streak >= self.b.max_losses:
                self.until = now + self.b.pause_s
                out.append(self._set("streak", f"{self.streak} perdas seguidas: pausa de {dur(self.b.pause_s)} "
                                               "nas entradas"))
        return out

    def _set(self, kind: str, text: str) -> str:
        self.kind, self.reason = kind, text if kind else ""
        if kind != "streak":
            self.until = 0.0
        return text

    @property
    def ok(self) -> bool:
        return not self.kind

    def state(self) -> dict:
        return {"on": bool(self.kind), "kind": self.kind, "reason": self.reason, "streak": self.streak,
                "until": datetime.fromtimestamp(self.until, timezone.utc).isoformat(timespec="seconds") if self.until else "",
                "day_pnl_usd": round(self.day_pnl, 4)}


def keep_awake(on: bool) -> bool:
    """Ask Windows not to go to sleep by itself while a session runs (the request ends with the process).
    It does not stop the sleep of a closed lid or of the power button: that is a setting only the owner changes."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        continuous, system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(continuous | (system_required if on else 0))
        return True
    except Exception:
        return False
