"""What the app is told about a session's health: silent, stopped early, braking, or running the old rules."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from deskbot import desks_api as api


def iso(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def state(folder, age_s: float = 0.0, **kw) -> dict:
    st = {"started": iso(-60), "ends": iso(60), "start_eur": 110.0, "equity_eur": 110.0, "events": [], **kw}
    path = folder / "state.json"
    path.write_text(json.dumps(st), encoding="utf-8")
    then = time.time() - age_s
    os.utime(path, (then, then))
    return st


def test_a_fresh_running_session_is_healthy(tmp_path):
    h = api._health(state(tmp_path, 5, guard={}), tmp_path, True)
    assert not h["stalled"] and h["stale_s"] < 60 and not h["legacy"] and h["guard"] == {} and h["alerts"] == []


def test_a_running_session_that_stopped_writing_is_stalled(tmp_path):
    h = api._health(state(tmp_path, api.STALL_S + 60, guard={}), tmp_path, True)
    assert h["stalled"] and h["stale_s"] >= api.STALL_S


def test_a_dead_session_before_its_end_is_stalled_and_ended_at_its_last_write(tmp_path):
    st = state(tmp_path, 600, guard={})
    h = api._health(st, tmp_path, False)
    assert h["stalled"]
    ended = datetime.fromisoformat(h["ended"])
    assert ended < datetime.fromisoformat(st["ends"])
    assert abs((datetime.now(timezone.utc) - ended).total_seconds() - 600) < 30


def test_a_finished_session_is_never_stalled(tmp_path):
    assert not api._health(state(tmp_path, 9000, guard={}, finished=True), tmp_path, False)["stalled"]
    assert not api._health(state(tmp_path, 9000, guard={}, finished=True), tmp_path, True)["stalled"]


def test_a_session_that_died_is_stalled_even_after_its_hour_has_come(tmp_path):
    st = state(tmp_path, 9000, guard={}, ends=iso(-30))                        # not running, never wrote "finished"
    assert api._health(st, tmp_path, False)["stalled"]
    assert not api._health({**st, "finished": True}, tmp_path, False)["stalled"]


def test_the_last_sign_of_life_is_the_sessions_own_clock_not_the_files_date(tmp_path):
    st = state(tmp_path, 0, guard={}, saved=iso(-10))                          # the file is new: copied a moment ago
    h = api._health(st, tmp_path, True)
    assert h["stalled"] and abs(h["stale_s"] - 600) < 30 and h["last_write"] == st["saved"]
    assert abs(api._health(state(tmp_path, 600, guard={}), tmp_path, True)["stale_s"] - 600) < 30      # old states
    h = api._health(state(tmp_path, 600, guard={}, saved="not a date"), tmp_path, True)
    assert abs(h["stale_s"] - 600) < 30                                        # a broken one falls back to the file


def test_brakes_alerts_and_feed_fields_pass_through(tmp_path):
    guard = {"on": True, "kind": "pause", "reason": "3 perdas seguidas", "until": iso(30)}
    events = [{"ts": iso(-9), "agent": "RISK", "verdict": "alert", "coin": "BTC", "reason": "primeiro"},
              {"ts": iso(-5), "agent": "RISK", "verdict": "sell", "coin": "BTC", "reason": "stop"},
              {"ts": iso(-1), "agent": "RISK", "verdict": "alert", "coin": "—", "reason": "último"}]
    h = api._health(state(tmp_path, 1, guard=guard, events=events, feed_stale=True, max_gap_s=95.0), tmp_path, True)
    assert h["guard"] == guard and h["feed_stale"] is True and h["max_gap_s"] == 95.0
    assert [a["reason"] for a in h["alerts"]] == ["último", "primeiro"]        # newest first, alerts only
    assert "quote_fail" not in h                                               # absent in the state: absent here


def test_a_session_that_ended_with_the_feed_down_no_longer_says_no_prices(tmp_path, monkeypatch):
    """The old code leaves feed_stale: true in the state of a session that ended during an outage."""
    done = state(tmp_path, 9000, cash=110.0, feed_stale=True, max_gap_s=95.0, finished=True)   # no "guard": the old code
    h = api._health(done, tmp_path, False)
    assert "feed_stale" not in h and h["max_gap_s"] == 95.0 and not h["stalled"]
    monkeypatch.setattr(api, "PERP_DIR", tmp_path)
    monkeypatch.setattr(api, "perp_running", lambda: False)
    assert not api._perps().get("feed_stale")                                  # what the futures page is given
    running = state(tmp_path, 2, cash=110.0, feed_stale=True)                  # still running: it is said
    assert api._health(running, tmp_path, True)["feed_stale"] is True


def test_a_state_without_brakes_is_an_old_session(tmp_path):
    assert api._health(state(tmp_path), tmp_path, True)["legacy"]


def test_missing_or_broken_dates_do_not_break_the_page(tmp_path):
    h = api._health({"ends": "not a date"}, tmp_path, False)                   # no state file either
    assert h["stale_s"] == 0 and h["ended"] == h["last_write"]
    h = api._health({"ends": "not a date", "saved": 7}, tmp_path, True)
    assert not h["stalled"] and h["stale_s"] == 0
    assert api._when(None) is None and api._when("2026-11-10T00:00:00").tzinfo is not None


def test_the_futures_page_says_when_a_session_runs_the_old_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "PERP_DIR", tmp_path)
    monkeypatch.setattr(api, "perp_running", lambda: True)
    state(tmp_path, 2, cash=110.0)                                             # no "guard": started before the brakes
    old = api._perps()
    assert old["legacy"] and old["rules"][0].startswith("Esta sessão começou antes das regras novas")
    state(tmp_path, 2, cash=110.0, guard={})
    new = api._perps()
    assert not new["legacy"] and new["rules"][0].startswith("Scanner")
    assert any(r.startswith("Travões") for r in new["rules"]) and new["params"]["risk_pct"] == 0.01
    assert not any("40% do saldo livre de margem à" in r for r in new["rules"])


MEME_P = {"cost_per_side": 0.005, "stop_pct": 0.08, "trail_pct": 0.10}


def memes(tmp_path, monkeypatch, running=True, **kw) -> dict:
    monkeypatch.setattr(api, "MEME_DIR", tmp_path)
    monkeypatch.setattr(api, "meme_running", lambda: running)
    state(tmp_path, 2, cash=110.0, p=MEME_P, **kw)
    return api._memes()


def test_the_memes_page_states_the_rules_the_desk_really_runs(tmp_path, monkeypatch):
    d = memes(tmp_path, monkeypatch, guard={})
    text = " | ".join(d["rules"])
    assert not d["legacy"] and d["rules"][0].startswith("Scanner")
    assert "cada compra é 15% da mesa" in text and "35% da mesa investido" in text and "2 abertas" in text
    assert "40% do saldo livre" not in text and "liquidez da pool cair para metade" in text
    assert any(r.startswith("Travões") and "não volta a entrar" in r for r in d["rules"])
    p = d["params"]
    assert (p["max_trade_pct"], p["max_deployed_pct"], p["stack_pct"], p["max_open"]) == (0.15, 0.35, 0.40, 2)
    assert p["brakes"] == {"session": 0.10, "day": 0.06, "losses": 3} and p["stop_pct"] == 0.08
    assert "40%" not in api.META["memes"]["blurb"]


def test_the_memes_page_says_when_a_session_runs_the_old_rules(tmp_path, monkeypatch):
    old = memes(tmp_path, monkeypatch)                                         # no "guard" in the state
    assert old["legacy"] and old["rules"][0].startswith("Esta sessão começou antes das regras novas")
    done = memes(tmp_path, monkeypatch, running=False)                         # over: the note is for a running session only
    assert done["rules"][0].startswith("Scanner")


def test_the_more_entries_style_still_changes_its_rules(tmp_path, monkeypatch):
    text = " | ".join(memes(tmp_path, monkeypatch, guard={}, style="mais")["rules"])
    assert "2 compras de $300+" in text and "top 10 < 40%" in text and "cada compra é 15% da mesa" in text


def test_a_trade_tells_how_its_sale_was_priced(tmp_path, monkeypatch):
    trade = {"coin": "$A", "pnl_eur": -1.0, "pct": -0.08, "reason": "stop −8%", "closed": iso(-5),
             "fill_src": "model", "blind": True}
    d = memes(tmp_path, monkeypatch, guard={}, trades=[trade, {**trade, "fill_src": None, "blind": False}])
    assert [(t["fill_src"], t["blind"]) for t in d["trades"]] == [(None, False), ("model", True)]


def test_a_memes_desk_too_small_to_ever_buy_is_not_started(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(api, "MEME_DIR", tmp_path)
    monkeypatch.setattr(api, "meme_running", lambda: False)
    monkeypatch.setattr(api, "PERP_DIR", tmp_path / "perps")                   # never the live desk's folder
    monkeypatch.setattr(api, "perp_running", lambda: False)
    monkeypatch.setattr(api, "_spawn", lambda folder, args: started.append(args))
    code, body = api.start_memes(5, 20)
    assert code == 400 and "50 €" in body["error"] and not started
    code, body = api.start_memes(5, 7)                                         # not a stake of any desk
    assert code == 400 and body["error"] == "o saldo tem de ser 50, 110, 500, 1000 €" and not started
    assert api.start_memes(5, 50)[0] == 202 and len(started) == 1
    assert 20 not in api._memes()["stakes"] and 20 in api._perps()["stakes"]
    assert api.MEME_STAKES[0] * 0.15 * 1.0 > 5                                 # the smallest desk can make a buy


def test_the_home_list_carries_the_stalled_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "PERP_DIR", tmp_path)
    monkeypatch.setattr(api, "perp_running", lambda: True)
    state(tmp_path, api.STALL_S + 120, cash=110.0, guard={})
    assert api._perps()["stalled"] is True


# ---------------------------------------------------------------- what the second review found
def sale(pid: str, kind: str, pnl: float, **kw) -> dict:
    return {"id": pid, "coin": "$" + pid.upper(), "side": "long", "kind": kind, "pnl_eur": pnl, "pct": pnl / 10,
            "reason": kind, "closed": iso(-5), **kw}


# position a: half sold at a gain, the rest at a small loss (it won as a whole); position b: stopped out
SALES = [sale("a", "half", 5.0), sale("a", "trail", -1.0, blind=True), sale("b", "stop", -2.0)]


def perps(tmp_path, monkeypatch, running=False, **kw) -> dict:
    folder = tmp_path / "perps"
    folder.mkdir(exist_ok=True)
    monkeypatch.setattr(api, "PERP_DIR", folder)
    monkeypatch.setattr(api, "perp_running", lambda: running)
    state(folder, 2, cash=110.0, **kw)
    return api._perps()


def test_the_memes_recap_counts_positions_not_sales(tmp_path, monkeypatch):
    d = memes(tmp_path, monkeypatch, running=False, guard={}, finished=True, trades=SALES)
    r = d["recap"]
    assert len(d["trades"]) == 3 and (d["wins"], d["losses"]) == (1, 1)        # the list still shows every sale
    assert (r["trades"], r["wins"], r["win_rate"]) == (2, 1, 0.5)              # it said 3 trades, 33%
    assert memes(tmp_path, monkeypatch, running=False, guard={}, finished=True)["recap"]["win_rate"] is None


def test_the_futures_recap_counts_positions_not_sales_and_says_which_went_unwatched(tmp_path, monkeypatch):
    d = perps(tmp_path, monkeypatch, guard={}, finished=True, trades=SALES)
    r = d["recap"]
    assert (r["trades"], r["wins"], r["win_rate"]) == (2, 1, 0.5) and (d["wins"], d["losses"]) == (1, 1)
    assert [t["blind"] for t in d["trades"]] == [False, True, False]           # newest first; absent means watched


def test_the_home_list_counts_positions_not_sales(tmp_path, monkeypatch):
    memes(tmp_path, monkeypatch, running=False, guard={}, finished=True, trades=SALES)
    perps(tmp_path, monkeypatch, guard={}, finished=True, trades=SALES[:2])
    assert {d["name"]: d["trades"] for d in api.overview()["desks"]} == {"memes": 2, "futuros": 1}


def test_the_memes_recap_states_the_size_rule_the_session_ran(tmp_path, monkeypatch):
    new = memes(tmp_path, monkeypatch, running=False, guard={}, finished=True)["recap"]
    assert new["stop_txt"] == "15% da mesa por compra · metade a 2× · trailing 10% · stop −8%"
    old = memes(tmp_path, monkeypatch, running=False, finished=True)["recap"]  # no "guard": the old rules
    assert old["stop_txt"] == "Compounder 40% · metade a 2× · trailing 10% · stop −8%"


def test_a_long_balance_curve_keeps_its_whole_span(tmp_path, monkeypatch):
    points = [[n, 100.0 + n] for n in range(6000)]                             # 12 hours at a point every 5 s was more
    assert api._series(points, 1500)[0] == points[0] and api._series(points, 1500)[-1] == points[-1]
    assert len(api._series(points, 1500)) == 1501 and api._series(points[:1500], 1500) == points[:1500]
    for d in (memes(tmp_path, monkeypatch, guard={}, curve=points), perps(tmp_path, monkeypatch, guard={}, curve=points)):
        assert len(d["curve"]) == 1501 and d["curve"][0] == points[0] and d["curve"][-1] == points[-1]


def test_the_stop_button_writes_the_file_the_running_session_looks_for(tmp_path, monkeypatch):
    from deskbot import memetest, perpdesk
    assert api.MEME_DIR == memetest.OUT == memetest.STOP_FILE.parent           # the app and the desk agree on the place
    assert api.PERP_DIR == perpdesk.OUT == perpdesk.STOP_FILE.parent
    m, p = tmp_path / "memes", tmp_path / "perps"
    m.mkdir()
    p.mkdir()
    monkeypatch.setattr(api, "MEME_DIR", m)
    monkeypatch.setattr(api, "PERP_DIR", p)
    monkeypatch.setattr(api, "meme_running", lambda: False)
    monkeypatch.setattr(api, "perp_running", lambda: False)
    assert api.stop_memes() == (409, {"error": "Não há sessão a correr"})
    assert api.stop_perps() == (409, {"error": "Não há sessão a correr"})
    assert not list(m.iterdir()) and not list(p.iterdir())                     # nothing to stop: nothing written
    monkeypatch.setattr(api, "meme_running", lambda: True)
    assert api.stop_memes() == (202, {"stopping": True})
    assert [f.name for f in m.iterdir()] == [memetest.STOP_FILE.name] and not list(p.iterdir())
    monkeypatch.setattr(api, "perp_running", lambda: True)
    assert api.stop_perps() == (202, {"stopping": True})
    assert [f.name for f in p.iterdir()] == [perpdesk.STOP_FILE.name]
