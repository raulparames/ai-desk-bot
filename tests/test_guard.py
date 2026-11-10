"""The brakes on new entries (guard.py): session loss, day loss, losing streak. They never touch open positions."""

import json
from datetime import datetime, timezone

from deskbot import guard

NOON = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()


def test_durations_read_like_a_person_says_them():
    assert guard.dur(95) == "95 s" and guard.dur(600) == "10 min" and guard.dur(10400) == "2 h 53 min"
    assert guard.dur(-5) == "0 s"


def test_session_loss_stops_entries_until_the_session_ends(tmp_path):
    g = guard.Guard(tmp_path, 100.0, now=NOON)
    assert g.check(95.0, NOON) == [] and g.ok
    lines = g.check(90.0, NOON + 60)
    assert len(lines) == 1 and "perda da sessão" in lines[0] and not g.ok
    assert g.check(99.0, NOON + 120) == [] and not g.ok              # said once; winning back does not lift it
    assert g.check(99.0, NOON + 86400) == [] and not g.ok            # nor does a new day
    assert g.state()["on"] is True and g.state()["kind"] == "session"


def test_day_loss_counts_what_earlier_sessions_lost_today_and_lifts_at_midnight(tmp_path):
    (tmp_path / "day.json").write_text(json.dumps({"date": "2026-03-10", "realised_usd": -4.0}), encoding="utf-8")
    g = guard.Guard(tmp_path, 100.0, now=NOON)
    assert g.check(99.0, NOON) == [] and g.ok                        # -4 before, -1 now: -5%
    lines = g.check(97.9, NOON + 60)                                 # -6.1%
    assert "perda do dia" in lines[0] and g.state()["kind"] == "day"
    assert g.state()["day_pnl_usd"] == -6.1
    lines = g.check(97.9, NOON + 13 * 3600)                          # 01:00 UTC of the next day
    assert lines == ["novo dia (UTC): entradas de volta"] and g.ok
    assert g.state()["day_pnl_usd"] == 0.0                           # the new day starts from the equity it found


def test_a_day_file_from_another_day_is_ignored(tmp_path):
    (tmp_path / "day.json").write_text(json.dumps({"date": "2026-03-09", "realised_usd": -50.0}), encoding="utf-8")
    g = guard.Guard(tmp_path, 100.0, now=NOON)
    assert g.check(100.0, NOON) == [] and g.ok
    (tmp_path / "day.json").write_text("not json", encoding="utf-8")
    assert guard.Guard(tmp_path, 100.0, now=NOON).prior == 0.0


def test_three_losses_in_a_row_pause_entries_for_an_hour_then_they_come_back(tmp_path):
    g = guard.Guard(tmp_path, 100.0, now=NOON)
    assert g.closed("a", -1.0, True) == -1.0
    assert g.closed("b", -1.0, True) == -1.0
    assert g.check(98.0, NOON) == [] and g.ok
    g.closed("c", -1.0, True)
    lines = g.check(97.0, NOON)
    assert lines == ["3 perdas seguidas: pausa de 60 min nas entradas"] and not g.ok
    assert g.state()["until"] == "2026-03-10T13:00:00+00:00"
    assert g.check(97.0, NOON + 3599) == [] and not g.ok
    assert g.check(97.0, NOON + 3600) == ["pausa terminada: entradas de volta"] and g.ok and g.streak == 0


def test_a_win_resets_the_streak_and_a_half_counts_with_its_position(tmp_path):
    g = guard.Guard(tmp_path, 100.0, now=NOON)
    g.closed("a", -1.0, True)
    g.closed("b", -1.0, True)
    assert g.closed("c", 3.0, False) is None and g.streak == 2       # a half closed: the position is still open
    assert g.closed("c", -1.0, True) == 2.0 and g.streak == 0        # stopped at the entry after the half: a win
    assert json.loads((tmp_path / "day.json").read_text(encoding="utf-8"))["realised_usd"] == 0.0
    g.closed("d", 1.0, False)
    assert g.closed("d", -2.5, True) == -1.5 and g.streak == 1       # the half did not pay for the rest: a loss
