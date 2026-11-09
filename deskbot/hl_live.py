"""Real money on Hyperliquid, step 1 of 3: DRY-RUN. Nothing is signed and nothing is sent.

For every trade of the futures sessions (perpdesk) this builds the exact request Hyperliquid would
receive, with the same format and rounding as the official SDK (hyperliquid-dex/hyperliquid-python-sdk):

  open   updateLeverage (isolated, so a liquidation can only take that position's margin) and one
         "order" action, grouping "normalTpsl": the entry as an IOC limit order (a market order is an
         aggressive IOC limit, bounded here at 1% from the reference price) plus a stop-loss trigger
         order that lives on the exchange, so it still works if the PC and GitHub are both down.
  half   reduce-only IOC for half the size, and the stop moved to the entry for the rest.
  close  reduce-only IOC for the rest (a stop or liquidation would have been filled by the exchange).

Rules checked like the exchange does: prices with at most 5 significant figures and 6 - szDecimals
decimals, sizes rounded down to szDecimals, minimum order value $10, the coin's maximum leverage.
Our own limits are checked too, so the log already shows what they would block:

  HL_MAX_ORDER_USD       largest position value                      (default 50)
  HL_MAX_DAILY_LOSS_USD  loss in a day after which nothing new opens (default 20)
  HL_MAX_LEVERAGE        never above 10                              (default 5)
  HL_MAX_OPEN            positions at the same time                  (default 2)
  HL_ACCOUNT_ADDRESS     optional, your public address: the real balance is read (read-only)
                         and the orders are sized as they would be on it

HL_MODE in .env: "dryrun" (default) or "off". "live" does not exist yet and is refused. Kill switch:
a file perpdesk/live_off stops it at once. Everything is written to perpdesk/live_orders.jsonl.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .config import env

log = logging.getLogger("hl_live")
MIN_ORDER_USD = 10.0
SLIPPAGE = 0.01            # worst price accepted by the IOC "market" orders
KEEP = 60                  # orders kept in the state for the app


def wire(x: float) -> str:
    """The SDK's float_to_wire: at most 8 decimals, no trailing zeros, refuses hidden rounding."""
    rounded = f"{x:.8f}"
    if abs(float(rounded) - x) >= 1e-12:
        raise ValueError(f"rounding would change {x}")
    if rounded in ("-0", "-0.00000000"):
        rounded = "0"
    return f"{Decimal(rounded).normalize():f}"


def round_px(px: float, sz_decimals: int) -> float:
    """5 significant figures and at most 6 - szDecimals decimals (perps), like the SDK."""
    return round(float(f"{px:.5g}"), max(0, 6 - sz_decimals))


def round_sz(sz: float, sz_decimals: int) -> float:
    """Rounded down, so the order is never bigger than the limit allows."""
    f = 10 ** sz_decimals
    return math.floor(sz * f + 1e-9) / f


class Limits:
    def __init__(self):
        self.max_order_usd = _num("HL_MAX_ORDER_USD", 50.0)
        self.max_daily_loss_usd = _num("HL_MAX_DAILY_LOSS_USD", 20.0)
        self.max_leverage = min(10.0, _num("HL_MAX_LEVERAGE", 5.0))
        self.max_open = int(_num("HL_MAX_OPEN", 2))

    def as_dict(self) -> dict:
        return {"max_order_usd": self.max_order_usd, "max_daily_loss_usd": self.max_daily_loss_usd,
                "max_leverage": self.max_leverage, "max_open": self.max_open}


class Bridge:
    """Called by the futures desk after each paper trade. Never raises into the desk."""

    def __init__(self, info, out_dir: Path):
        self.info = info                      # perpdesk's read-only Hyperliquid /info call
        self.out = out_dir
        self.journal = out_dir / "live_orders.jsonl"
        self.kill_file = out_dir / "live_off"
        raw = (env("HL_MODE") or "dryrun").lower()
        self.note = ""
        if raw == "live":
            self.note = "HL_MODE=live ainda não existe: continua em dry-run, nada é enviado"
            raw = "dryrun"
        self.mode = raw if raw in ("dryrun", "off") else "dryrun"
        self.address = env("HL_ACCOUNT_ADDRESS")
        self.limits = Limits()
        self.assets: dict[str, tuple[int, int, int]] = {}     # coin -> (asset index, szDecimals, maxLeverage)
        self.orders: list[dict] = []
        self.open: dict[str, dict] = {}       # coin -> what the real position would be
        self.day, self.day_pnl = "", 0.0
        self.prepared = self.blocked = 0
        self.block_reasons: dict[str, int] = {}
        self.account: dict = {}

    @property
    def on(self) -> bool:
        return self.mode == "dryrun" and not self.kill_file.exists()

    # ------------------------------------------------------------ exchange facts (read-only)
    def asset(self, coin: str) -> tuple[int, int, int] | None:
        if not self.assets:
            meta = self.info({"type": "meta"}) or {}
            for i, a in enumerate(meta.get("universe", [])):
                self.assets[a["name"]] = (i, int(a.get("szDecimals", 0)), int(a.get("maxLeverage", 1)))
        return self.assets.get(coin)

    def real_balance(self) -> float | None:
        if not self.address:
            return None
        st = self.info({"type": "clearinghouseState", "user": self.address}) or {}
        try:
            self.account = {"value": float(st["marginSummary"]["accountValue"]), "free": float(st["withdrawable"]),
                            "at": _now()}
            return self.account["free"]
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------ building the requests
    def _order(self, a: int, is_buy: bool, px: float, sz: float, reduce_only: bool, kind: dict) -> dict:
        return {"a": a, "b": is_buy, "p": wire(px), "s": wire(sz), "r": reduce_only, "t": kind}

    def _ioc(self, a: int, szd: int, is_buy: bool, ref: float, sz: float, reduce_only: bool) -> dict:
        bound = round_px(ref * (1 + SLIPPAGE if is_buy else 1 - SLIPPAGE), szd)
        return self._order(a, is_buy, bound, sz, reduce_only, {"limit": {"tif": "Ioc"}})

    def _stop(self, a: int, szd: int, side: int, stop: float, sz: float) -> dict:
        trig = round_px(stop, szd)
        bound = round_px(stop * (1 - SLIPPAGE if side == 1 else 1 + SLIPPAGE), szd)
        return self._order(a, side != 1, bound, sz, True,
                           {"trigger": {"isMarket": True, "triggerPx": wire(trig), "tpsl": "sl"}})

    # ------------------------------------------------------------ what the desk tells us
    def on_open(self, coin: str, side: int, lev: float, notional: float, ref: float, stop: float, paper_fill: float):
        if not self.on:
            return
        try:
            self._roll_day()
            found = self.asset(coin)
            checks, actions = [], []
            if not found:
                return self._log("open", coin, side, actions, ["moeda não existe na Hyperliquid"], paper_fill)
            a, szd, max_lev = found
            real_lev = int(min(lev, self.limits.max_leverage, max_lev))
            free = self.real_balance()
            want = notional if free is None else free * 0.40 * real_lev   # same Compounder rule on the real balance
            if want > self.limits.max_order_usd:
                checks.append(f"reduzida ao limite de ${self.limits.max_order_usd:g}")
                want = self.limits.max_order_usd
            sz = round_sz(want / ref, szd)
            value = sz * ref
            blocks = []
            if value < MIN_ORDER_USD:
                blocks.append(f"abaixo do mínimo de ${MIN_ORDER_USD:g} da Hyperliquid")
            if len(self.open) >= self.limits.max_open:
                blocks.append(f"já há {self.limits.max_open} posições")
            if -self.day_pnl >= self.limits.max_daily_loss_usd:
                blocks.append(f"perda do dia chegou a ${self.limits.max_daily_loss_usd:g}")
            if lev > real_lev:
                checks.append(f"alavancagem {lev:g}x → {real_lev}x (limite)")
            actions.append({"type": "updateLeverage", "asset": a, "isCross": False, "leverage": real_lev})
            if sz > 0:
                actions.append({"type": "order", "grouping": "normalTpsl",
                                "orders": [self._ioc(a, szd, side == 1, ref, sz, False), self._stop(a, szd, side, stop, sz)]})
            if not blocks:
                self.open[coin] = {"side": side, "sz": sz, "a": a, "szd": szd, "entry": ref}
            self._log("open", coin, side, actions, blocks, paper_fill, checks=checks, value=value, lev=real_lev,
                      size=sz, stop=round_px(stop, szd))
        except Exception as exc:   # the dry-run must never stop the paper desk
            log.exception("dry-run open failed")
            self._log("open", coin, side, [], [f"erro: {type(exc).__name__}"], paper_fill)

    def on_half(self, coin: str, side: int, ref: float, new_stop: float):
        if not self.on or coin not in self.open:
            return
        try:
            p = self.open[coin]
            half = round_sz(p["sz"] / 2, p["szd"])
            rest = round_sz(p["sz"] - half, p["szd"])
            actions = [{"type": "order", "grouping": "na", "orders": [self._ioc(p["a"], p["szd"], side != 1, ref, half, True)]},
                       {"type": "cancel", "note": "o stop anterior (o id só existe em modo real)"},
                       {"type": "order", "grouping": "na", "orders": [self._stop(p["a"], p["szd"], side, new_stop, rest)]}]
            blocks = [] if half * ref >= MIN_ORDER_USD else [f"metade abaixo do mínimo de ${MIN_ORDER_USD:g}: fecharia tudo"]
            p["sz"] = rest
            self._log("half", coin, side, actions, blocks, ref, size=half, stop=round_px(new_stop, p["szd"]))
        except Exception as exc:
            log.exception("dry-run half failed")
            self._log("half", coin, side, [], [f"erro: {type(exc).__name__}"], ref)

    def on_close(self, coin: str, side: int, ref: float, kind: str, pnl_usd: float):
        if coin not in self.open:
            return
        p = self.open.pop(coin)
        self._roll_day()
        self.day_pnl += pnl_usd
        if not self.on:
            return
        try:
            if kind in ("stop", "liq"):
                actions = [{"type": "none", "note": "o stop está na exchange: fecharia sozinho, sem ordem nova"}]
            else:
                actions = [{"type": "order", "grouping": "na", "orders": [self._ioc(p["a"], p["szd"], side != 1, ref, p["sz"], True)]},
                           {"type": "cancel", "note": "o stop que sobra"}]
            self._log("close", coin, side, actions, [], ref, size=p["sz"], reason=kind)
        except Exception as exc:
            log.exception("dry-run close failed")
            self._log("close", coin, side, [], [f"erro: {type(exc).__name__}"], ref)

    # ------------------------------------------------------------ journal
    def _roll_day(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self.day:
            self.day, self.day_pnl = today, 0.0

    def _log(self, what: str, coin: str, side: int, actions: list, blocks: list, ref: float, **extra):
        rec = {"ts": _now(), "what": what, "coin": coin, "side": "long" if side == 1 else "short", "ref": ref,
               "sent": False, "mode": self.mode, "blocked": blocks, "actions": actions, **extra}
        if blocks:
            self.blocked += 1
            for b in blocks:
                key = b.split(":")[0]
                self.block_reasons[key] = self.block_reasons.get(key, 0) + 1
        else:
            self.prepared += 1
        self.orders.append(rec)
        del self.orders[:-KEEP]
        try:
            with self.journal.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        log.info("DRY-RUN %s %s %s: %s", what, coin, rec["side"], "; ".join(blocks) or "pronta (não enviada)")

    def summary(self) -> dict:
        return {"mode": self.mode if self.on or self.mode == "off" else "parado (live_off)", "note": self.note,
                "exchange": "Hyperliquid", "sent": 0, "prepared": self.prepared, "blocked": self.blocked,
                "block_reasons": self.block_reasons, "limits": self.limits.as_dict(), "day_pnl_usd": self.day_pnl,
                "account": self.account, "address_set": bool(self.address), "open": list(self.open),
                "orders": self.orders[-20:]}


def _num(name: str, default: float) -> float:
    try:
        v = float(env(name) or default)
        return v if math.isfinite(v) and v > 0 else default
    except ValueError:
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
