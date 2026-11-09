"""SQLite journal: every desk discussion, order and equity snapshot, with the reasoning."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .config import JOURNAL_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, ts TEXT, bar_date TEXT, symbol TEXT, sleeves TEXT,
    decision TEXT, size_multiplier REAL, entry REAL, stop REAL, verdict TEXT);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, sleeve TEXT, side TEXT,
    qty REAL, price REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS equity (ts TEXT, equity REAL, cash REAL);
CREATE TABLE IF NOT EXISTS demos (
    id INTEGER PRIMARY KEY, ts TEXT, bar_date TEXT, symbol TEXT, decision TEXT, verdict TEXT);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY, ts TEXT, bar_date TEXT, sleeve TEXT, symbol TEXT, passed INTEGER,
    layer TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY, ts TEXT, key TEXT, old TEXT, new TEXT, source TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path=JOURNAL_DB):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def decision(self, bar_date: str, verdict, sleeves: list[str]):
        p = verdict.plan
        self.db.execute(
            "INSERT INTO decisions (ts, bar_date, symbol, sleeves, decision, size_multiplier, entry, stop, verdict)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (_now(), bar_date, verdict.symbol, ",".join(sleeves), verdict.decision,
             verdict.size_multiplier, p["entry"], p["stop"],
             json.dumps(verdict.as_dict(), ensure_ascii=False, default=str)))
        self.db.commit()

    def demo(self, bar_date: str, verdict):
        """Demo discussions (dashboard / `desk` command) are kept apart so `review` ignores them."""
        self.db.execute("INSERT INTO demos (ts, bar_date, symbol, decision, verdict) VALUES (?,?,?,?,?)",
                        (_now(), bar_date, verdict.symbol, verdict.decision,
                         json.dumps(verdict.as_dict(), ensure_ascii=False, default=str)))
        self.db.commit()

    def order(self, symbol: str, sleeve: str, side: str, qty: float, price: float, reason: str):
        self.db.execute("INSERT INTO orders (ts, symbol, sleeve, side, qty, price, reason) VALUES (?,?,?,?,?,?,?)",
                        (_now(), symbol, sleeve, side, qty, price, reason))
        self.db.commit()

    def equity(self, equity: float, cash: float):
        self.db.execute("INSERT INTO equity VALUES (?,?,?)", (_now(), equity, cash))
        self.db.commit()

    def scans(self, bar_date: str, rows: list[tuple]):
        """The Auditor's daily cut: (sleeve, symbol, passed, first failed layer, its value) per coin."""
        self.db.execute("DELETE FROM scans WHERE bar_date = ?", (bar_date,))
        self.db.executemany("INSERT INTO scans (ts, bar_date, sleeve, symbol, passed, layer, value) "
                            "VALUES (?,?,?,?,?,?,?)", [(_now(), bar_date, *r) for r in rows])
        self.db.commit()

    def scan_history(self, days: int = 30) -> list[dict]:
        return self._rows("SELECT bar_date, sleeve, symbol, passed, layer, value FROM scans "
                          "WHERE bar_date >= date('now', ?) ORDER BY bar_date", (f"-{days} days",))

    def change(self, key: str, old, new, source: str = "painel"):
        self.db.execute("INSERT INTO changes (ts, key, old, new, source) VALUES (?,?,?,?,?)",
                        (_now(), key, str(old), str(new), source))
        self.db.commit()

    def changes(self, n: int = 50) -> list[dict]:
        return self._rows("SELECT ts, key, old, new, source FROM changes ORDER BY id DESC LIMIT ?", (n,))

    def _rows(self, sql: str, params=()) -> list[dict]:
        cur = self.db.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def decisions(self) -> list[dict]:
        return self._rows("SELECT id, ts, bar_date, symbol, sleeves, decision, size_multiplier, entry, stop, verdict "
                          "FROM decisions ORDER BY id")

    def demos(self, n: int = 30) -> list[dict]:
        return self._rows("SELECT id, ts, bar_date, symbol, decision, verdict FROM demos ORDER BY id DESC LIMIT ?", (n,))

    def orders(self, n: int = 200) -> list[dict]:
        return self._rows("SELECT ts, symbol, sleeve, side, qty, price, reason FROM orders ORDER BY id DESC LIMIT ?", (n,))

    def equity_history(self) -> list[dict]:
        return self._rows("SELECT ts, equity, cash FROM equity ORDER BY ts")

    def recent_orders(self, n: int = 20) -> list[tuple]:
        return self.db.execute("SELECT ts, symbol, sleeve, side, qty, price, reason FROM orders "
                               "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
