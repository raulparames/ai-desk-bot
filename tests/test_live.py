"""Hyperliquid dry-run: the real orders are built like the official SDK does, limits apply, nothing is sent."""

import json

from deskbot import hl_live

META = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                     {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
                     {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10}]}


def bridge(tmp_path, monkeypatch, **envs):
    for k in ("HL_MODE", "HL_ACCOUNT_ADDRESS", "HL_MAX_ORDER_USD", "HL_MAX_DAILY_LOSS_USD", "HL_MAX_LEVERAGE", "HL_MAX_OPEN"):
        monkeypatch.delenv(k, raising=False)
    for k, v in envs.items():
        monkeypatch.setenv(k, v)
    calls = []

    def info(body, session=None):
        calls.append(body["type"])
        return META if body["type"] == "meta" else None
    b = hl_live.Bridge(info, tmp_path)
    b.calls = calls
    return b


def test_prices_and_sizes_follow_hyperliquid_rules():
    assert hl_live.round_px(74785.37, 5) == 74785          # 5 significant figures
    assert hl_live.wire(hl_live.round_px(74785.37, 5)) == "74785"
    assert hl_live.round_px(86.63129, 2) == 86.631          # 5 sig figs, at most 6 - 2 decimals
    assert hl_live.round_px(0.00522237, 0) == 0.005222      # 6 decimals for a 0-decimal size coin
    assert hl_live.round_sz(0.578999, 2) == 0.57            # sizes round down, never above the limit
    assert hl_live.wire(0.57) == "0.57" and hl_live.wire(1.0) == "1"


def test_open_builds_leverage_entry_and_exchange_stop_but_sends_nothing(tmp_path, monkeypatch):
    b = bridge(tmp_path, monkeypatch, HL_MAX_ORDER_USD="50", HL_MAX_LEVERAGE="5")
    b.on_open("SOL", 1, 10, 220.0, 86.20, 84.90, 86.20)     # session at 10x, $220 wanted
    rec = b.orders[-1]
    assert rec["sent"] is False and not rec["blocked"] and b.calls == ["meta"]   # read-only, nothing posted
    lev, order = rec["actions"]
    assert lev == {"type": "updateLeverage", "asset": 1, "isCross": False, "leverage": 5}
    entry, stop = order["orders"]
    assert order["grouping"] == "normalTpsl"
    assert entry["b"] is True and entry["r"] is False and entry["t"] == {"limit": {"tif": "Ioc"}}
    assert float(entry["p"]) == hl_live.round_px(86.20 * 1.01, 2)                 # 1% worst price for the buy
    assert entry["s"] == "0.58"                                                    # $50 cap / 86.20, rounded down
    assert stop["b"] is False and stop["r"] is True and stop["t"]["trigger"]["tpsl"] == "sl"
    assert stop["t"]["trigger"]["triggerPx"] == "84.9"
    assert any("limite de $50" in c for c in rec["checks"])
    lines = (tmp_path / "live_orders.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["sent"] is False


def test_limits_block_small_orders_too_many_positions_and_a_bad_day(tmp_path, monkeypatch):
    b = bridge(tmp_path, monkeypatch, HL_MAX_OPEN="1", HL_MAX_DAILY_LOSS_USD="20")
    b.on_open("SOL", 1, 5, 8.0, 86.2, 85.0, 86.2)
    assert any("mínimo" in x for x in b.orders[-1]["blocked"])       # $8 is under Hyperliquid's $10
    b.on_open("SOL", 1, 5, 40.0, 86.2, 85.0, 86.2)
    b.on_open("BTC", -1, 5, 40.0, 74785, 75500, 74785)
    assert any("posições" in x for x in b.orders[-1]["blocked"])
    b.on_close("SOL", 1, 84.0, "stop", -25.0)                        # a $25 loss today
    b.on_open("BTC", -1, 5, 40.0, 74785, 75500, 74785)
    assert any("perda do dia" in x for x in b.orders[-1]["blocked"])
    assert b.summary()["sent"] == 0 and b.blocked == 3


def test_half_moves_stop_to_entry_and_close_is_reduce_only(tmp_path, monkeypatch):
    b = bridge(tmp_path, monkeypatch)
    b.on_open("SOL", -1, 5, 45.0, 86.2, 87.5, 86.2)                  # a short
    b.on_half("SOL", -1, 83.6, 86.2)
    half = b.orders[-1]
    close_half, _cancel, new_stop = half["actions"]
    assert close_half["orders"][0]["b"] is True and close_half["orders"][0]["r"] is True   # buy back, reduce-only
    assert new_stop["orders"][0]["t"]["trigger"]["triggerPx"] == "86.2"
    b.on_close("SOL", -1, 83.0, "trail", 3.0)
    last = b.orders[-1]["actions"][0]["orders"][0]
    assert last["r"] is True and last["b"] is True and "SOL" not in b.open


def test_live_mode_is_refused_and_kill_switch_stops_everything(tmp_path, monkeypatch):
    b = bridge(tmp_path, monkeypatch, HL_MODE="live")
    assert b.mode == "dryrun" and "não existe" in b.note
    (tmp_path / "live_off").write_text("off", encoding="utf-8")
    b.on_open("SOL", 1, 5, 45.0, 86.2, 85.0, 86.2)
    assert not b.orders and b.calls == []
    off = bridge(tmp_path / "x", monkeypatch, HL_MODE="off") if (tmp_path / "x").mkdir() is None else None
    off.on_open("SOL", 1, 5, 45.0, 86.2, 85.0, 86.2)
    assert not off.orders
