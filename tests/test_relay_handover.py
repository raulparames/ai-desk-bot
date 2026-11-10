"""The hand-over between the PC and GitHub (relay.py), and what the phone is told about a computer gone quiet.

The day's loss travels with the other desk files, a file pulled and not changed is not pushed back, the memes
minimum holds on GitHub and by hand, a session the computer left unfinished is told once, and the phone pages
get the age of the bot's last sign by the server's clock.

Offline: the relay's table is a dictionary, the push library a recorder and every folder a temp one.
"""

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pywebpush
import requests

from deskbot import cli, desks_api, guard, memedesk, memetest, perpdesk, push, relay

REPO = Path(__file__).resolve().parent.parent
SUB = {"endpoint": "https://web.push.apple.com/a", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}
STALL = "Sessão de %s parou de responder"
GH_SESSION = relay._gh_session       # the real one: the rig puts a recorder in its place


def stamp(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class Kv:
    """The relay's table and its functions, in memory: a push dates the row, as the database does."""

    def __init__(self):
        self.rows, self.pushes, self.queue, self.answers = {}, [], [], {}

    def put(self, key, value, age: float = 0.0):
        self.rows[key] = {"value": json.loads(json.dumps(value)), "updated_at": stamp(time.time() - age)}

    def rpc(self, name, body):
        if name == "runner_push":
            for k, v in body["p_items"].items():
                self.put(k, v)
            self.pushes.append(sorted(body["p_items"]))
        elif name == "runner_get":
            return {k: json.loads(json.dumps(v)) for k, v in self.rows.items() if k.startswith(body["p_prefix"])}
        elif name == "runner_take":
            out, self.queue = self.queue, []
            return out
        elif name == "runner_done":
            self.answers[body["p_id"]] = body["p_result"]
        else:
            raise AssertionError(f"unexpected relay function {name}")

    def value(self, key):
        return (self.rows.get(key) or {}).get("value")

    def files_pushed(self, since: int = 0) -> list[str]:
        return [k for batch in self.pushes[since:] for k in batch if k.startswith("file:")]


def _no_network(*a, **kw):
    raise AssertionError("a test tried to use the network")


def _no_process(*a, **kw):
    raise AssertionError("a test tried to start a session")


class Rig:
    """One temp folder that is the PC's or GitHub's disk, and one relay between them."""

    def __init__(self, tmp_path, monkeypatch):
        self.root, self.kv = tmp_path, Kv()
        self.sent, self.fail, self.cycles, self.sessions, self.during = [], None, 0, [], None
        monkeypatch.setenv("RELAY_SECRET", "x")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "test")
        monkeypatch.delenv("PUSH_CONTACT", raising=False)
        meme, perp = tmp_path / "memetest", tmp_path / "perpdesk"
        for mod, name, value in ((relay, "ROOT", tmp_path), (cli, "ROOT", tmp_path),
                                 (desks_api, "MEME_DIR", meme), (desks_api, "PERP_DIR", perp),
                                 (memetest, "OUT", meme), (memetest, "STOP_FILE", meme / "stop"),
                                 (perpdesk, "OUT", perp), (perpdesk, "STOP_FILE", perp / "stop"),
                                 (push, "DESKS", {"memes": meme / "state.json", "perps": perp / "state.json"}),
                                 (relay, "_GH_SESSION", threading.Event()), (relay, "_GH_KIND", {})):
            monkeypatch.setattr(mod, name, value)
        monkeypatch.setattr(relay.Relay, "rpc", lambda _, name, body, timeout=20: self.kv.rpc(name, body))
        monkeypatch.setattr(requests.Session, "request", _no_network)
        monkeypatch.setattr(desks_api, "_spawn", _no_process)
        monkeypatch.setattr(relay, "app_items", lambda owner: {"app:runner": {"owner": owner, "host": owner,
                                                                               "heartbeat": stamp(time.time())}})
        monkeypatch.setattr(relay, "_gh_session", lambda _, session: self.sessions.append(session))
        monkeypatch.setattr(cli, "run_desks", self._run_desks)
        monkeypatch.setattr(pywebpush, "webpush", self._webpush)

    def _run_desks(self):
        self.cycles += 1
        if self.during:
            self.during(self.cycles)

    def _webpush(self, **kw):
        if self.fail:
            raise self.fail
        self.sent.append(json.loads(kw["data"]))

    def file(self, rel: str, value=None, age: float = 0.0) -> Path:
        p = self.root / rel
        if value is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(value), encoding="utf-8")
            if age:
                os.utime(p, (time.time() - age,) * 2)
        return p

    def read(self, rel: str):
        return json.loads(self.file(rel).read_text(encoding="utf-8"))

    def pc_quiet(self, age: float = 600):
        self.kv.put("app:runner", {"owner": "pc", "host": "pc", "heartbeat": stamp(time.time() - age)}, age)

    def pc_left(self, desk: str = "memes", age: float = 600, saved: bool = True, subs: int = 1, **kw) -> dict:
        """The PC pushed this session's state `age` seconds ago and has not been heard of since."""
        wrote = time.time() - age
        st = {"started": "2026-11-10T09:00:00+00:00", "ends": stamp(time.time() + 3600), "start_eur": 110.0,
              "equity_eur": 108.5, "events": [], "positions": [], "trades": [], **kw}
        if saved:        # the old code, still running, writes no "saved"
            st["saved"] = datetime.fromtimestamp(wrote, timezone.utc).isoformat(timespec="seconds")
        self.kv.put(f"file:{relay.SESSION_DIRS[desk]}/state.json", st, age)
        self.pc_quiet(age)
        for i in range(subs):
            self.kv.put(f"push:sub:{i}", dict(SUB))
        return st

    def titles(self) -> list[str]:
        return [m["title"] for m in self.sent]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


OLD_BUY = {"ts": "2026-11-10T09:05:00+00:00", "agent": "EXECUTOR", "coin": "WIF", "verdict": "buy", "eur": 10, "score": 70}


# ------------------------------------------------------------ A. the day's loss travels
def test_the_days_loss_travels_with_the_other_desk_files(rig):
    assert {"memetest/day.json", "perpdesk/day.json"} <= set(relay.FILES)
    now = time.time()
    day = {"date": guard._today(now), "realised_usd": -7.5}
    rig.file("memetest/day.json", day)
    assert relay.Relay().push_files() == 1 and rig.kv.value("file:memetest/day.json") == day
    rig.file("memetest/day.json").unlink()                       # GitHub starts from an empty folder
    assert relay.Relay().pull_files(only_newer=False) == ["memetest/day.json"]
    assert guard.Guard(rig.root / "memetest", 100.0, now=now).prior == -7.5     # the day-loss brake still knows


def test_an_older_copy_in_the_relay_never_replaces_the_days_loss_here(rig):
    now = time.time()
    today, yesterday = guard._today(now), guard._today(now - 86400)
    rig.kv.put("file:perpdesk/day.json", {"date": today, "realised_usd": -2.0}, age=300)
    rig.file("perpdesk/day.json", {"date": today, "realised_usd": -9.0})        # written here after that push
    pc = relay.Relay()
    assert pc.pull_files() == [] and rig.read("perpdesk/day.json")["realised_usd"] == -9.0
    assert pc.push_files() == 1 and rig.kv.value("file:perpdesk/day.json")["realised_usd"] == -9.0   # and it goes up

    # yesterday's copy, however recently pushed, against today's here: never, not even on GitHub's pull of everything
    rig.kv.put("file:perpdesk/day.json", {"date": yesterday, "realised_usd": -50.0})
    os.utime(rig.file("perpdesk/day.json"), (now - 3600,) * 2)
    assert relay.Relay().pull_files() == [] and relay.Relay().pull_files(only_newer=False) == []
    rig.kv.put("file:perpdesk/day.json", {"realised_usd": 0.0})                 # nor one with no date at all
    assert relay.Relay().pull_files(only_newer=False) == []
    assert rig.read("perpdesk/day.json") == {"date": today, "realised_usd": -9.0}

    rig.kv.put("file:perpdesk/day.json", {"date": today, "realised_usd": -12.0})    # the same day, written later: taken
    assert relay.Relay().pull_files() == ["perpdesk/day.json"] and rig.read("perpdesk/day.json")["realised_usd"] == -12.0
    rig.file("perpdesk/day.json").write_text("not json", encoding="utf-8")         # nothing usable here: the relay's
    os.utime(rig.file("perpdesk/day.json"), (now - 3600,) * 2)
    assert relay.Relay().pull_files() == ["perpdesk/day.json"]


class Ticks:
    """pc_loop's stop event: instead of sleeping between two ticks it runs the test's next step."""

    def __init__(self, *steps):
        self.steps, self.n = steps, 0

    def is_set(self) -> bool:
        return self.n > len(self.steps)

    def wait(self, _seconds):
        if self.n < len(self.steps):
            self.steps[self.n]()
        self.n += 1


def test_a_pc_that_takes_the_lead_back_first_takes_what_github_lost_today(rig, monkeypatch):
    """The dashboard lived through a sleep: nothing restarted, so nothing pulled the day's loss GitHub wrote."""
    now, clock = time.time(), time.time
    today = guard._today(now)
    rig.file("memetest/day.json", {"date": today, "realised_usd": -1.0}, age=3600)
    rig.kv.put("file:memetest/day.json", {"date": today, "realised_usd": -1.0}, age=3590)
    mine = {"started": "2026-11-10T09:00:00+00:00", "finished": True, "equity_eur": 99.0}
    rig.file("perpdesk/state.json", mine, age=3600)
    rig.kv.put("app:runner", {"owner": "pc", "host": "pc", "heartbeat": stamp(now)})

    def github_ran():        # the PC sleeps; a memes session on GitHub loses 6 more and GitHub goes idle
        rig.kv.put("file:memetest/day.json", {"date": today, "realised_usd": -7.0})
        rig.kv.put("file:perpdesk/state.json", {**mine, "equity_eur": 50.0})
        rig.kv.put("app:runner", {"owner": "github", "host": "github", "heartbeat": stamp(time.time() - 60)})

    def a_session_here():    # the PC has the lead again: a session started now from the phone, and its first close
        assert rig.kv.value("app:runner")["owner"] == "pc"
        assert rig.read("memetest/day.json")["realised_usd"] == -7.0
        assert rig.read("perpdesk/state.json") == mine           # only the day's loss: a session's state is left alone
        g = guard.Guard(rig.root / "memetest", 100.0, now=now)
        assert g.prior == -7.0                                   # it was -1.0, and the day brake let entries through
        g.closed("p1", -0.5, True)
        monkeypatch.setattr(time, "time", lambda: clock() + 40)  # the next tick is one that pushes the files

    relay.pc_loop(Ticks(github_ran, a_session_here))
    assert rig.kv.value("file:memetest/day.json")["realised_usd"] == -7.5     # GitHub's 6 are still in the relay's total


def test_a_pc_that_cannot_read_the_days_loss_does_not_take_the_lead_back_yet(rig, monkeypatch):
    today = guard._today(time.time())
    rig.file("memetest/day.json", {"date": today, "realised_usd": -1.0}, age=3600)
    rig.kv.put("file:memetest/day.json", {"date": today, "realised_usd": -7.0})
    rig.kv.put("app:runner", {"owner": "github", "host": "github", "heartbeat": stamp(time.time() - 60)})
    pull, down = relay.Relay.pull_files, [True]

    def flaky(self, only_newer=True, only=""):
        if down[0]:
            raise RuntimeError("the files cannot be read")
        return pull(self, only_newer, only)

    def back():              # after the first tick: it led nothing and pushed nothing over GitHub's total
        assert rig.kv.value("app:runner")["owner"] == "github" and rig.kv.pushes == []
        assert rig.read("memetest/day.json")["realised_usd"] == -1.0
        down[0] = False

    monkeypatch.setattr(relay.Relay, "pull_files", flaky)
    relay.pc_loop(Ticks(back))
    assert rig.kv.value("app:runner")["owner"] == "pc" and rig.read("memetest/day.json")["realised_usd"] == -7.0
    assert rig.kv.value("file:memetest/day.json")["realised_usd"] == -7.0


def test_a_session_started_from_the_phone_starts_from_the_days_loss_in_the_relay(rig, monkeypatch):
    today = guard._today(time.time())
    rig.file("perpdesk/day.json", {"date": today, "realised_usd": -1.0}, age=3600)
    rig.file("perpdesk/state.json", {"started": "s1", "finished": True}, age=3600)
    rig.kv.put("file:perpdesk/day.json", {"date": today, "realised_usd": -7.0})
    rig.kv.put("file:perpdesk/state.json", {"started": "s2", "finished": True})
    rig.kv.put("file:memetest/day.json", {"date": today, "realised_usd": -3.0})
    seen = []

    def spawn(desk):
        return lambda *a: seen.append((desk, rig.read(f"{relay.SESSION_DIRS[desk]}/day.json")["realised_usd"])) or (202, {"started": True})

    monkeypatch.setattr(desks_api, "start_perps", spawn("perps"))
    monkeypatch.setattr(desks_api, "start_memes", spawn("memes"))
    pc = relay.Relay()
    assert relay.run_command(start("perps", 20), "pc", pc) == {"code": 202, "started": True}
    assert seen == [("perps", -7.0)]                             # read before the session was started
    assert rig.read("perpdesk/state.json")["started"] == "s1" and not rig.file("memetest/day.json").exists()
    assert relay.run_command(start("memes", 50), "pc", pc)["code"] == 202 and seen[-1] == ("memes", -3.0)

    rig.kv.put("file:perpdesk/day.json", {"date": today, "realised_usd": -9.0})
    monkeypatch.setattr(pc, "get", lambda prefix: 1 / 0)         # the relay fails: the session still starts
    assert relay.run_command(start("perps", 20), "pc", pc)["code"] == 202 and seen[-1] == ("perps", -7.0)
    assert relay.run_command(start("perps", 20), "pc")["code"] == 202         # and with no relay at hand


# ------------------------------------------------------------ B. what was pulled is not pushed back
def test_a_file_pulled_and_not_changed_is_not_pushed_back(rig):
    rig.pc_left("memes", age=600)
    rig.kv.put("file:desks/controls.json", {"paused": {"futuros": False}}, age=900)
    rig.kv.put("file:memetest/day.json", {"date": "2026-11-10", "realised_usd": -1.25}, age=700)
    before = {k: v["updated_at"] for k, v in rig.kv.rows.items() if k.startswith("file:")}

    def desks(n):        # GitHub's cycle of the desks: it writes on the first and the third, nothing on the second
        if n != 2:
            rig.file("desks/futuros.json", {"equity": 100.0 + n, "positions": []})

    rig.during, pushed = desks, []
    for _ in range(3):
        mark = len(rig.kv.pushes)
        assert relay.gh_cycle().startswith("GitHub fez o ciclo")
        pushed.append(rig.kv.files_pushed(mark))
    assert pushed == [["file:desks/futuros.json"], [], ["file:desks/futuros.json"]]
    assert rig.kv.value("file:desks/futuros.json") == {"equity": 103.0, "positions": []}
    after = {k: rig.kv.rows[k]["updated_at"] for k in before}
    assert after == before                                       # the session's last sign of life is still the PC's
    wrote = datetime.fromisoformat(before["file:memetest/state.json"]).timestamp()
    assert abs(rig.file("memetest/state.json").stat().st_mtime - wrote) < 2


def test_a_file_changed_here_after_the_pull_is_still_pushed(rig):
    st = rig.pc_left("perps", age=60)
    pc = relay.Relay()
    assert pc.pull_files() == ["perpdesk/state.json"]
    key = "file:perpdesk/state.json"
    assert pc._sent[key] == relay._hash(st) and pc.push({key: st}) == 0     # hashed exactly as push() hashes
    assert pc.push_files() == 0 and rig.kv.files_pushed() == []
    rig.file("perpdesk/state.json", {**st, "equity_eur": 111.0})
    assert pc.push_files() == 1 and rig.kv.value(key)["equity_eur"] == 111.0
    assert pc.push_files() == 0                                  # and only once


# ------------------------------------------------------------ C. the memes minimum, everywhere
def start(desk, eur=None, cid=1):
    payload = {"desk": desk, "minutes": 30} | ({} if eur is None else {"eur": eur})
    return {"id": cid, "kind": "start_session", "payload": payload}


def test_github_refuses_a_memes_balance_under_the_minimum_in_the_apps_words(rig):
    code, said = desks_api.start_memes(30, 20)
    assert code == 400 and str(desks_api.MEME_STAKES[0]) in said["error"]
    assert relay.run_command(start("memes", 20), "github") == {"code": 400, "error": said["error"]}
    assert relay.run_command(start("memes", 75), "github") == {"code": 400, "error": desks_api.start_memes(30, 75)[1]["error"]}
    for eur in desks_api.MEME_STAKES:
        assert relay.run_command(start("memes", eur), "github") == {"code": 202, "deferred": True}
    assert relay.run_command(start("memes"), "github") == {"code": 202, "deferred": True}       # no balance given: 110
    assert relay.run_command(start("perps", 20), "github") == {"code": 202, "deferred": True}   # futures take 20 €


def test_github_starts_no_session_for_a_refused_start(rig):
    rig.pc_quiet()
    rig.kv.queue = [start("memes", 20, cid=7)]
    out = relay.gh_cycle()
    assert rig.sessions == [] and "sessão" not in out and rig.cycles == 1
    assert rig.kv.answers[7] == {"code": 400, "error": desks_api.start_memes(30, 20)[1]["error"]}
    rig.kv.queue = [start("memes", 50, cid=8)]
    assert "sessão de memes" in relay.gh_cycle()
    assert rig.sessions == [{"desk": "memes", "minutes": 30, "eur": 50}]
    assert rig.kv.answers[8] == {"code": 202, "started": True, "on": "github"}
    rig.kv.queue = [start("perps", 20, cid=9)]
    assert "sessão de futuros" in relay.gh_cycle() and rig.sessions[-1] == {"desk": "perps", "minutes": 30, "eur": 20}


def test_by_hand_the_memes_desk_refuses_a_balance_under_the_minimum(rig, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli, "_setup_logging", lambda verbose: None)
    monkeypatch.setattr(memedesk, "run", lambda *a: ran.append(("memes", *a)) or "ok")
    monkeypatch.setattr(perpdesk, "run", lambda *a: ran.append(("perps", *a)) or "ok")
    (rig.root / "logs").mkdir()
    loggers = [logging.getLogger("memetest"), logging.getLogger("perpdesk")]
    kept = [list(lg.handlers) for lg in loggers]
    try:
        for eur in ("20", "49.99", "nan", "-50"):
            with pytest.raises(SystemExit) as stop:
                cli.main(["memedesk", "--minutes", "30", "--eur", eur])
            assert stop.value.code not in (0, None)
            assert desks_api.start_memes(30, 20)[1]["error"] in capsys.readouterr().out
        assert ran == [] and not (rig.root / "logs" / "memetest.log").exists()
        cli.main(["memedesk", "--minutes", "30", "--eur", "50"])
        cli.main(["perpdesk", "--minutes", "30", "--eur", "20"])       # the futures desk keeps its own stakes
        assert ran == [("memes", 30.0, 50.0, "fomo", "poucas"), ("perps", 30.0, 20.0, 5.0)]
    finally:
        for lg, was in zip(loggers, kept):
            for h in [h for h in lg.handlers if h not in was]:
                lg.removeHandler(h)
                h.close()


# ------------------------------------------------------------ D. a session the computer left behind
@pytest.mark.parametrize("saved", [True, False])       # False: a state written by the old code
def test_a_session_the_computer_left_unfinished_is_told_once(rig, saved):
    st = rig.pc_left("memes", age=600, saved=saved, positions=[{"coin": "WIF"}], events=[OLD_BUY])
    for _ in range(3):
        assert relay.gh_cycle().startswith("GitHub fez o ciclo")
    assert rig.titles() == [STALL % "memes"] and rig.cycles == 3      # once, and the old buy is not news
    msg = rig.sent[0]
    assert msg["body"] == ("O computador deixou de dar sinal há 10 min; a sessão tinha 1 posição aberta na última vez. "
                           "Vê o computador. Papel.")
    assert msg["url"] == "memes.html" and msg["tag"].startswith("memes-stall-")
    assert rig.kv.value("push:seen:memes")["stall"].startswith(st["started"] + "@")
    assert rig.kv.value("push:seen:perps") is None


def test_both_desks_left_running_are_each_told_once(rig):
    rig.pc_left("memes", age=400)
    rig.pc_left("perps", age=400, saved=False, positions=[{"coin": "SOL"}, {"coin": "ETH"}])
    for _ in range(3):
        relay.gh_cycle()
    assert sorted(rig.titles()) == sorted([STALL % "memes", STALL % "futuros"])
    assert "a sessão tinha 2 posições abertas na última vez" in next(m["body"] for m in rig.sent if "futuros" in m["title"])
    assert "a sessão não tinha posições abertas na última vez" in next(m["body"] for m in rig.sent if "memes" in m["title"])


def test_a_computer_gone_quiet_is_not_said_to_have_left_positions_without_a_stop(rig):
    """With only the dashboard down the session still runs and watches its stops: GitHub cannot tell which it is."""
    rig.pc_left("memes", age=200, positions=[{"coin": "WIF"}, {"coin": "BONK"}])
    relay.gh_cycle()
    assert rig.titles() == [STALL % "memes"]
    body = rig.sent[0]["body"]
    assert "sem stop" not in body and "tinha 2 posições abertas na última vez" in body
    assert body.startswith("O computador deixou de dar sinal há 3 min;")


def test_a_finished_session_or_no_session_is_not_news(rig):
    rig.pc_left("memes", age=600, finished=True, events=[OLD_BUY])
    rig.kv.put("file:perpdesk/state.json", {"events": []}, age=600)         # never started
    for _ in range(3):
        relay.gh_cycle()
    assert rig.sent == [] and rig.cycles == 3
    assert rig.kv.value("push:seen:memes") is None and rig.kv.value("push:seen:perps") is None


def test_a_computer_that_is_answering_keeps_the_lead(rig):
    rig.pc_left("memes", age=30)
    assert relay.gh_cycle().startswith("PC ativo")
    assert rig.sent == [] and rig.cycles == 0 and not rig.file("memetest/state.json").exists()


def test_a_session_github_itself_runs_is_not_a_dead_one(rig):
    rig.pc_left("memes", age=600)
    r = relay.Relay()
    r.pull_files(only_newer=False)
    relay._GH_SESSION.set()
    assert relay._tell_dead_sessions(r, {}) == []
    relay._GH_SESSION.clear()
    assert relay._tell_dead_sessions(r, {"owner": "github", "session": True, "heartbeat": stamp(time.time())}) == []
    assert rig.sent == [] and rig.kv.value("push:seen:memes") is None
    assert relay._tell_dead_sessions(r, {"owner": "pc", "heartbeat": stamp(time.time() - 600)}) == ["memes"]
    assert rig.titles() == [STALL % "memes"]


def test_trouble_telling_it_never_stops_the_cycle(rig, monkeypatch):
    rig.pc_left("memes", age=600)

    def boom(*a, **kw):
        raise RuntimeError("no push today")

    for target, name in ((push.Pusher, "refresh"), (push.Pusher, "_announced"), (relay, "_tell_dead_sessions")):
        with monkeypatch.context() as m:
            m.setattr(target, name, boom)
            assert relay.gh_cycle().startswith("GitHub fez o ciclo")
        assert rig.sent == [] and rig.kv.value("push:seen:memes") is None
        assert rig.kv.value("app:runner")["owner"] == "github"       # the cycle went to its end
    assert rig.cycles == 3
    relay.gh_cycle()                                                 # and what was not said is said when it can be
    relay.gh_cycle()
    assert rig.titles() == [STALL % "memes"]


def test_a_notification_nobody_received_is_tried_on_the_next_cycle(rig):
    rig.pc_left("perps", age=600)
    rig.fail = RuntimeError("push service down")
    relay.gh_cycle()
    assert rig.sent == [] and rig.kv.value("push:seen:perps") is None
    rig.fail = None
    relay.gh_cycle()
    relay.gh_cycle()
    assert rig.titles() == [STALL % "futuros"]


def test_with_no_phone_or_no_key_nothing_is_sent(rig, monkeypatch):
    rig.pc_left("memes", age=600, subs=0)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "")
    relay.gh_cycle()
    assert rig.kv.value("push:seen:memes") is None                   # no key: this runner cannot notify at all
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test")
    relay.gh_cycle()
    relay.gh_cycle()
    assert rig.sent == [] and rig.kv.value("push:seen:memes")["stall"]   # nobody to tell: dealt with, as Pusher does


def test_a_pc_that_wakes_later_does_not_say_it_again(rig):
    st = rig.pc_left("perps", age=600, saved=False)
    relay.gh_cycle()
    assert rig.titles() == [STALL % "futuros"]
    told = rig.kv.value("push:seen:perps")["stall"]

    def ticks(p, n):
        for _ in range(n):
            p._tick_at = time.time() - 5         # the runner itself ticked five seconds ago
            p.tick()

    rig.file("perpdesk/state.json", st, age=625)     # the PC's own copy: written 25 s before the relay got it
    pc = relay.Relay()
    relay._adopt_silences(pc)
    mine = rig.kv.value("push:seen:perps")["stall"]
    assert mine != told and mine == f"{st['started']}@{int(rig.file('perpdesk/state.json').stat().st_mtime)}"
    notes = push.Pusher(pc)
    ticks(notes, 4)
    assert rig.titles() == [STALL % "futuros"]       # the PC's notifications find it in the record

    rig.file("perpdesk/state.json", st, age=200)     # it came back, wrote for minutes and died again: another silence
    relay._adopt_silences(pc)
    assert rig.kv.value("push:seen:perps")["stall"] == mine
    ticks(notes, 3)
    assert rig.titles() == [STALL % "futuros"] * 2


def test_what_counts_as_the_same_silence():
    assert relay._same_silence("s1@1000", "s1", 1000.9) and relay._same_silence("s1@1000", "s1", 1030)
    assert not relay._same_silence("s1@1000", "s1", 1000 + relay.PC_FRESH_S)     # it wrote again for two minutes
    assert not relay._same_silence("s1@1000", "s2", 1000)                        # another session
    for told in ("", None, "s1", "s1@soon", "@1000"):
        assert not relay._same_silence(told, "s1", 1000)
    assert not relay._same_silence("@1000", "", 1000)


def a_futures_session_on_github(monkeypatch):
    """The real _gh_session around a futures session that ends at once, after its notifications looked three times."""
    made = []

    class Notes(push.Pusher):
        def __init__(self, r):
            super().__init__(r)
            made.append(self)

    class Idle:
        def __init__(self, *a):
            pass

        def run(self):
            for _ in range(3):
                made[-1]._tick_at = time.time() - 5      # the session's own loop ticked five seconds ago
                made[-1].tick()

    monkeypatch.setattr(push, "Pusher", Notes)
    monkeypatch.setattr(perpdesk, "PerpDesk", Idle)
    monkeypatch.setattr(relay, "_gh_session", GH_SESSION)


@pytest.mark.parametrize("pc_woke", [False, True])
def test_a_session_github_runs_for_the_other_desk_does_not_say_the_stall_again(rig, monkeypatch, pc_woke):
    st = rig.pc_left("memes", age=600, positions=[{"coin": "WIF"}])
    st["saved"] = stamp(time.time() - 620)           # the session last wrote 20 s before the PC pushed its state
    rig.kv.put("file:memetest/state.json", st, age=600)
    relay.gh_cycle()
    assert rig.titles() == [STALL % "memes"] and "há 10 min" in rig.sent[0]["body"]      # counted from "saved"
    named = f"{st['started']}@{int(rig.file('memetest/state.json').stat().st_mtime)}"
    assert rig.kv.value("push:seen:memes")["stall"] == named     # as push.Pusher names it on this disk, not by "saved"
    if pc_woke:              # the PC woke for a moment, named the silence by its own file (15 s older) and went again
        rig.kv.put("push:seen:memes", {"stall": f"{st['started']}@{int(time.time() - 615)}"})
        rig.pc_quiet(300)
    a_futures_session_on_github(monkeypatch)
    rig.kv.queue = [start("perps", 20, cid=3)]
    assert "sessão de futuros" in relay.gh_cycle()               # its notifications look at the memes file too
    assert rig.titles() == [STALL % "memes"]
    assert rig.kv.value("push:seen:memes")["stall"] == named


def test_a_dead_session_of_the_old_code_is_not_told_again_after_the_pc_pushed_its_copy(rig):
    st = rig.pc_left("memes", age=600, saved=False)
    for _ in range(2):
        relay.gh_cycle()
    assert rig.titles() == [STALL % "memes"]
    # the PC boots: its own copy, written 20 s after the one it had pushed, goes up and gets a new date in the relay
    rig.file("memetest/state.json").unlink()                     # (that was GitHub's disk)
    rig.file("memetest/state.json", {**st, "equity_eur": 108.1}, age=580)
    pc = relay.Relay()
    assert pc.pull_files() == []
    relay._adopt_silences(pc)
    assert pc.push_files() == 1
    notes = push.Pusher(pc)
    for _ in range(4):
        notes._tick_at = time.time() - 5
        notes.tick()
    assert rig.titles() == [STALL % "memes"]                     # the PC finds it in the record
    rig.file("memetest/state.json").unlink()                     # the PC is off again; GitHub starts from an empty disk
    rig.pc_quiet(600)
    for _ in range(2):
        relay.gh_cycle()
    assert rig.titles() == [STALL % "memes"]                     # the same dead session, with no "saved" to tell it by

    rig.pc_left("memes", age=600, saved=False, started="2026-11-10T11:00:00+00:00")
    relay.gh_cycle()
    assert rig.titles() == [STALL % "memes"] * 2                 # another session that went quiet is news


# ------------------------------------------------------------ E. the phone knows how old the last sign is
def pages():
    spec = importlib.util.spec_from_file_location("build_pages_under_test", REPO / "deploy" / "build_pages.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_phone_pages_are_given_the_age_of_the_last_sign():
    html = pages().rewrite("<html><script>var page = 1;</script></html>")
    assert html.count("<script>") == 2 and html.rstrip().endswith("<script>var page = 1;</script></html>")
    shim = html.split("<script>")[1]
    assert "runner.age_s = " in shim and "cache.since" in shim and "updated_at" in shim and "runner.heartbeat" in shim
    assert "Date.now" not in shim and "new Date" not in shim     # the server's clock, never the phone's


def test_the_pages_say_no_signal_only_for_a_desk_that_says_it_is_running():
    static = REPO / "deskbot" / "dashboard_static"
    memes = (static / "memes.html").read_text(encoding="utf-8")
    app = (static / "app.html").read_text(encoding="utf-8")
    assert "d.runner && d.runner.age_s > 120 && d.running" in memes and "Sem sinal do ${" in memes
    assert "Computador sem sinal" not in memes                   # it said as a fact that the positions had no stop
    assert "d.runner && d.runner.age_s > 120" in app and 'gone && x.status === "live"' in app
    assert 'lost(d) && d.status === "live"' in app
    for page in (memes, app):
        assert "age_s" in pages().rewrite(page).split("<script>")[1]


HARNESS = """
const ANSWERS = %s;
let asked = 0;
globalThis.window = globalThis;
globalThis.localStorage = {getItem: () => "token"};
globalThis.fetch = async () => new Response(JSON.stringify(ANSWERS[asked++]), {status: 200});
Date.now = () => 0;                                   // a phone whose clock is decades wrong
%s
(async () => {
  const out = [];
  for (let i = 0; i < ANSWERS.length; i++) out.push((await (await window.fetch("/api/desks")).json()).runner);
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_age_is_counted_by_the_servers_clock(tmp_path):
    shim = re.search(r"<script>(.*?)</script>", pages().rewrite("<script></script>"), re.S).group(1)
    over = {"value": {"desks": []}, "updated_at": "2026-11-10T12:05:00.654321+00:00"}
    answers = [
        # the relay dated the heartbeat 12:05:00.654; the server answers at 12:10:00.123
        {"now": "2026-11-10T12:10:00.123456+00:00",
         "items": {"app:overview": over, "app:runner": {"value": {"owner": "pc", "heartbeat": "2026-11-10T12:04:58+00:00"},
                                                       "updated_at": "2026-11-10T12:05:00.654321+00:00"}}},
        {"now": "2026-11-10T12:10:30+00:00", "items": {}},             # nothing new: the computer is still quiet
        # no date from the relay: the heartbeat the bot wrote
        {"now": "2026-11-10T12:11:00+00:00",
         "items": {"app:runner": {"value": {"owner": "github", "heartbeat": "2026-11-10T12:10:50Z"}}}},
        {"now": "2026-11-10T12:12:00+00:00", "items": {"app:runner": {"value": {"owner": "github", "heartbeat": "soon"}}}},
    ]
    script = tmp_path / "shim_check.js"
    script.write_text(HARNESS % (json.dumps(answers), shim), encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(tmp_path))
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert [r.get("age_s") for r in got] == [299, 329, 10, None]
    assert [r["owner"] for r in got] == ["pc", "pc", "github", "github"]


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_what_the_page_says_is_wrong_with_a_session(tmp_path):
    """The page's own healthRows, run on what the phone is given."""
    memes = (REPO / "deskbot" / "dashboard_static" / "memes.html").read_text(encoding="utf-8")
    code = [re.search(rf"^const {name} = .*$", memes, re.M).group(0) for name in ("esc", "hhmm", "capital", "durTxt", "braking", "lost")]
    code.append(re.search(r"^function healthRows\(d\) \{.*?^\}", memes, re.M | re.S).group(0))
    pc, gh = {"owner": "pc", "age_s": 300}, {"owner": "github", "age_s": 600}
    cases = [
        {"running": True, "positions": [{}, {}], "runner": pc},                       # 0 the computer went quiet
        {"running": True, "positions": [{}], "runner": gh},                           # 1 GitHub went quiet
        {"running": True, "positions": [], "runner": pc},                             # 2 quiet, nothing open
        {"running": True, "stalled": True, "stale_s": 400, "last_write": "2026-11-10T12:00:00+00:00",
         "positions": [{}], "runner": pc},                                            # 3 the session itself is silent
        {"running": False, "finished": True, "feed_stale": True, "positions": []},    # 4 ended during an outage (old code)
        {"running": True, "feed_stale": True, "positions": [{}], "runner": {"owner": "pc", "age_s": 4}},   # 5 no prices now
        {"running": False, "finished": True, "positions": [], "runner": pc},          # 6 over: a quiet computer is no news
    ]
    script = tmp_path / "health_check.js"
    script.write_text("\n".join(code) + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(healthRows)));\n", encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(tmp_path))
    assert run.returncode == 0, run.stderr
    rows = json.loads(run.stdout)
    assert "<b>Sem sinal do computador</b> · O telemóvel não recebe dados há 5 min. Se o computador estiver a dormir ou " \
           "desligado, as 2 posições abertas ficam sem stop até voltar. Vê o computador." in rows[0]
    assert "<b>Sem sinal do GitHub</b> · O telemóvel não recebe dados há 10 min. Se o GitHub tiver parado, a posição " \
           "aberta fica sem stop até voltar.</div>" in rows[1]
    assert "Sem sinal do computador" in rows[2] and "sem stop" not in rows[2]
    for row in rows[:3]:
        assert "estão sem stop" not in row and "está sem stop" not in row         # that is only for a session known silent
    assert "Sessão sem sinal" in rows[3] and "1 posição aberta está sem stop até voltar." in rows[3]
    assert "Sem sinal do" not in rows[3]
    assert rows[4] == "" and rows[6] == ""                                         # "Sem preços" does not outlive the session
    assert "<b>Sem preços</b>" in rows[5] and "Sem sinal" not in rows[5]
