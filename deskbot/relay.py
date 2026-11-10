"""Relay between whoever runs the bot (this PC or GitHub) and the phone app, through Supabase.

  PC on:  the dashboard process runs `pc_loop` on a thread. Every 5 s it pushes the three desks as the
          app shows them and a heartbeat, every 30 s the raw desk files (so GitHub can carry on from
          them), and it carries out the app's commands (pause a desk, start or stop a memes session).
  PC off: GitHub Actions runs `python -m deskbot gh-cycle` every 10 minutes. When the PC's heartbeat is
          older than 2 minutes, it takes over: pulls the raw files, carries out commands (a memes
          session runs inside that job, pushing every 5 s), runs the futures and stocks cycle, pushes.
          A session the PC left unfinished is told to the phone once ("parou de responder").
  Back on: before the PC runs its desks it pulls any raw file GitHub wrote after its own copy.

Supabase keys: RELAY_URL and RELAY_KEY (publishable, public by design) and RELAY_SECRET (the bot's
secret; only its hash is in the database). The tables are closed; everything goes through functions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from .config import ROOT, env

log = logging.getLogger("relay")
DEFAULT_URL = "https://kkawnjfowemyqkplkrma.supabase.co"
DEFAULT_KEY = "sb_publishable_oL1nYjpN8GKzuG_RuhwGgQ_DAxM06qa"
FILES = ["desks/futuros.json", "desks/acoes.json", "desks/controls.json", "desks/backtests.json",
         "memetest/state.json", "perpdesk/state.json",
         "memetest/day.json", "perpdesk/day.json"]   # what the sessions lost today: the day-loss brake survives a hand-over
SESSION_DIRS = {"memes": "memetest", "perps": "perpdesk"}
PC_FRESH_S = 120
POINTS = 400   # chart points sent to the phone per desk


def _hash(value) -> str:
    """How push() knows a value is the one the relay already has."""
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _older_day(remote, local: Path) -> bool:
    """day.json keeps what the sessions lost today (UTC). A copy from an earlier day must never replace the
    one here: the day-loss brake would forget today's loss."""
    try:
        mine = json.loads(local.read_text(encoding="utf-8"))["date"]
    except (OSError, ValueError, KeyError, TypeError):
        return False                     # nothing usable here: the relay's copy is all there is
    theirs = remote.get("date") if isinstance(remote, dict) else None
    return not theirs or str(theirs) < str(mine)


class Relay:
    def __init__(self):
        load_dotenv(ROOT / ".env")   # also when used outside the dashboard (never overrides real env vars)
        self.url = (env("RELAY_URL") or DEFAULT_URL).rstrip("/")
        self.key = env("RELAY_KEY") or DEFAULT_KEY
        self.secret = env("RELAY_SECRET")
        self.http = requests.Session()
        self.http.headers.update({"apikey": self.key, "Content-Type": "application/json"})
        self._sent: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.secret)

    def rpc(self, name: str, body: dict, timeout: float = 20):
        r = self.http.post(f"{self.url}/rest/v1/rpc/{name}", json={"p_secret": self.secret, **body}, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{name}: {r.status_code} {r.text[:200]}")
        return r.json() if r.text else None

    # ------------------------------------------------------------ push / pull
    def push(self, items: dict, force: bool = False) -> int:
        """Send only what changed since the last push (the heartbeat always goes)."""
        out = {}
        for k, v in items.items():
            h = _hash(v)
            if force or k == "app:runner" or self._sent.get(k) != h:
                out[k] = v
                self._sent[k] = h
        if out:
            self.rpc("runner_push", {"p_items": out})
        return len(out)

    def get(self, prefix: str) -> dict:
        return self.rpc("runner_get", {"p_prefix": prefix}) or {}

    def take_commands(self, who: str) -> list[dict]:
        return self.rpc("runner_take", {"p_runner": who}) or []

    def done(self, cid: int, result: dict):
        self.rpc("runner_done", {"p_id": cid, "p_result": result})

    def set_password_from_file(self):
        path = ROOT / "phone_password.json"
        if not path.exists():
            return False
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.rpc("runner_set_password", {"p_salt": raw["salt"], "p_iterations": int(raw["iterations"]),
                                         "p_hash": raw["hash"]})
        return True

    # ------------------------------------------------------------ raw desk files (handover PC <-> GitHub)
    def push_files(self):
        items = {}
        for rel in FILES:
            p = ROOT / rel
            if p.exists():
                try:
                    items["file:" + rel] = json.loads(p.read_text(encoding="utf-8"))
                except ValueError:
                    continue
        return self.push(items)

    def pull_files(self, only_newer: bool = True, only: str = "") -> list[str]:
        """Write remote raw files locally when they are newer than the local copy. `only` keeps it to the
        files whose path ends so ("/day.json": the day's loss, and no session's state).

        What the relay holds is remembered the way push() remembers what it sent, so push_files only sends
        back a file that changed here. A file pushed again unchanged would get a new date in the relay, and
        the next runner to pull it would take that date for the session's last sign of life."""
        remote = self.get("file:")
        got = []
        for key, item in remote.items():
            rel = key[len("file:"):]
            if rel not in FILES or not rel.endswith(only):
                continue
            self._sent[key] = _hash(item["value"])
            p = ROOT / rel
            ts = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).timestamp()
            if only_newer and p.exists() and p.stat().st_mtime >= ts - 5:
                continue
            if rel.endswith("/day.json") and _older_day(item["value"], p):
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(item["value"], ensure_ascii=False), encoding="utf-8")
            os.utime(p, (ts, ts))
            got.append(rel)
        return got

    def runner(self) -> dict:
        item = self.get("app:runner").get("app:runner") or {}
        return item.get("value") or {}


# ------------------------------------------------------------ what the app shows
def _thin(payload: dict) -> dict:
    curve = payload.get("curve") or []
    if len(curve) > POINTS:
        step = len(curve) / POINTS
        payload["curve"] = [curve[int(i * step)] for i in range(POINTS)] + [curve[-1]]
    payload["log"] = (payload.get("log") or [])[:60]
    payload["trades"] = (payload.get("trades") or [])[:40]
    return payload


def app_items(owner: str) -> dict:
    from . import desks_api
    bt = desks_api.backtests()
    items = {"app:overview": desks_api.overview(bt)}
    for name in ("memes", "perps"):
        d = desks_api.desk(name, bt)
        if owner == "github" and _GH_KIND.get("desk") == name:
            d["running"] = bool(d.get("running") or _GH_SESSION.is_set())
        items[f"app:desk:{name}"] = _thin(d)
    items["app:runner"] = {"owner": owner, "host": socket.gethostname() if owner == "pc" else "github",
                           "heartbeat": datetime.now(timezone.utc).isoformat()}
    return json.loads(json.dumps(items, default=str))   # numpy/pandas values -> plain JSON


def memes_min_error() -> str:
    """The words desks_api.start_memes refuses a balance under the memes desk's minimum with."""
    from . import desks_api
    return (f"na mesa de memes o saldo mínimo é {desks_api.MEME_STAKES[0]} €: "
            "com menos, nenhuma compra chega ao mínimo de 5 $")


def memes_stake_error(eur: float) -> str:
    """Why the memes desk does not take this balance ("" when it does), as desks_api.start_memes says it.
    The PC checks it there; GitHub starts its sessions without going through start_memes, so it asks here."""
    from . import desks_api
    if eur in desks_api.MEME_STAKES:
        return ""
    if eur in desks_api.STAKES:
        return memes_min_error()
    return f"o saldo tem de ser {', '.join(map(str, desks_api.MEME_STAKES))} €"


def run_command(cmd: dict, owner: str, relay: Relay | None = None) -> dict:
    from . import desks_api
    kind, p = cmd.get("kind"), cmd.get("payload") or {}
    try:
        if kind == "start_session" and owner == "pc" and relay:    # GitHub may have lost money today since this PC looked:
            day = SESSION_DIRS["perps" if p.get("desk") == "perps" else "memes"] + "/day.json"
            _safe(lambda: relay.pull_files(only=day))               # the new session's day-loss brake starts from it
        if kind == "pause":
            code, res = desks_api.pause(str(p.get("name")), bool(p.get("paused")))
        elif kind == "start_session" and owner == "github":
            err = "" if p.get("desk") == "perps" else memes_stake_error(float(p.get("eur") or 110))
            if not err:
                return {"code": 202, "deferred": True}
            code, res = 400, {"error": err}      # not deferred: gh_cycle starts no session
        elif kind == "start_session":
            if p.get("desk") == "perps":
                code, res = desks_api.start_perps(int(p.get("minutes") or 0), float(p.get("eur") or 110),
                                                  float(p.get("lev") or 5))
            else:
                code, res = desks_api.start_memes(int(p.get("minutes") or 0), float(p.get("eur") or 110),
                                                  str(p.get("venue") or "fomo"), str(p.get("style") or "poucas"))
        elif kind == "stop_session":
            perps = p.get("desk") == "perps"
            if owner == "github":
                folder = ROOT / SESSION_DIRS["perps" if perps else "memes"]
                folder.mkdir(exist_ok=True)
                (folder / "stop").write_text("stop", encoding="utf-8")
                code, res = 202, {"stopping": True}
            else:
                code, res = desks_api.stop_perps() if perps else desks_api.stop_memes()
        else:
            code, res = 400, {"error": "unknown command"}
    except Exception as exc:  # a bad command must never stop the loop
        code, res = 500, {"error": str(exc)}
    log.info("command %s %s -> %s", kind, p, code)
    return {"code": code, **(res if isinstance(res, dict) else {})}


# ------------------------------------------------------------ the PC side
def pc_loop(stop: threading.Event | None = None):
    """Runs on a thread inside the dashboard process."""
    relay = Relay()
    if not relay.enabled:
        log.info("Relay off: no RELAY_SECRET in .env")
        return
    stop = stop or threading.Event()
    try:
        relay.set_password_from_file()
        pulled = relay.pull_files()
        if pulled:
            log.info("Relay: took over from GitHub, pulled %s", ", ".join(pulled))
    except Exception:
        log.exception("Relay start failed (will keep trying)")
    _safe(lambda: _adopt_silences(relay))   # what GitHub already said about a dead session is not said again
    pw_mtime = _mtime(ROOT / "phone_password.json")
    last_files = 0.0
    from .push import Pusher
    pusher = Pusher(relay)   # lock-screen notifications for the memes and futures sessions
    while not stop.is_set():
        tick = time.time()
        try:
            rn = relay.runner()
            gh_busy = rn.get("owner") == "github" and _age(rn.get("heartbeat")) < 30 and rn.get("session")
            if not gh_busy:   # a memes session still running on GitHub keeps the lead until it ends
                if rn.get("owner") == "github":   # back from sleep: GitHub had the lead and may have told of a dead session
                    # what its sessions lost today comes first: the total here is from before the sleep, and pushed
                    # as it is it would erase GitHub's. Only day.json: a session that woke with this PC keeps its state.
                    relay.pull_files(only="/day.json")
                    _safe(lambda: _adopt_silences(relay))
                relay.push(app_items("pc"))
                for cmd in relay.take_commands("pc"):
                    relay.done(cmd["id"], run_command(cmd, "pc", relay))
                if time.time() - last_files > 30:
                    relay.push_files()
                    last_files = time.time()
                try:
                    pusher.tick()
                except Exception as exc:   # a failed notification must never stop the relay
                    log.warning("Push tick failed: %s", exc)
            m = _mtime(ROOT / "phone_password.json")
            if m != pw_mtime:
                relay.set_password_from_file()
                pw_mtime = m
        except Exception as exc:
            log.warning("Relay tick failed: %s", exc)
        stop.wait(max(1.0, 5 - (time.time() - tick)))


def pull_before_cycle():
    """Called by the PC's 15-minute cycle: adopt what GitHub did while the PC was off."""
    relay = Relay()
    if not relay.enabled:
        return []
    try:
        return relay.pull_files()
    except Exception as exc:
        log.warning("Relay pull failed: %s", exc)
        return []


# ------------------------------------------------------------ the GitHub side
_GH_SESSION = threading.Event()
_GH_KIND: dict = {}


def gh_cycle() -> str:
    relay = Relay()
    if not relay.enabled:
        return "RELAY_SECRET em falta"
    rn = relay.runner()
    if rn.get("owner") == "pc" and _age(rn.get("heartbeat")) < PC_FRESH_S:
        return f"PC ativo (último sinal há {_age(rn.get('heartbeat')):.0f} s): nada a fazer"
    pulled = relay.pull_files(only_newer=False)
    log.info("GitHub takes over; pulled %s", pulled)
    _safe(lambda: _tell_dead_sessions(relay, rn))   # the computer went quiet with a session on: the phone hears it once
    session = None
    for cmd in relay.take_commands("github"):
        res = run_command(cmd, "github")
        if res.get("deferred"):
            session = cmd.get("payload") or {}
            res = {"code": 202, "started": True, "on": "github"}
        relay.done(cmd["id"], res)
    from .cli import run_desks
    run_desks()
    relay.push(app_items("github"), force=True)
    relay.push_files()
    if session:
        _gh_session(relay, session)
    return "GitHub fez o ciclo" + (f" e uma sessão de {'futuros' if session.get('desk') == 'perps' else 'memes'}"
                                   if session else "")


def _gh_session(relay: Relay, session: dict):
    """A memes or futures session inside the GitHub job (max ~5.5 h), pushing to the phone every 5 s."""
    minutes = min(int(session.get("minutes") or 30), 330)
    eur = float(session.get("eur") or 110)
    if session.get("desk") == "perps":
        from . import perpdesk
        desk = perpdesk.PerpDesk(minutes, eur, min(float(session.get("lev") or 5), 10))
    else:
        from . import memedesk
        desk = memedesk.MemesDesk(minutes, eur, str(session.get("venue") or "fomo"), str(session.get("style") or "poucas"))
    done = threading.Event()
    _GH_KIND["desk"] = "perps" if session.get("desk") == "perps" else "memes"
    _GH_SESSION.set()
    from .push import Pusher
    notes = Pusher(relay)
    _safe(lambda: _adopt_silences(relay))   # the other desk's dead session, already told: named as this disk reads it
    _safe(notes.tick)        # first look: whatever is already in the files is not announced

    def pusher():
        while not done.wait(5):
            try:
                items = app_items("github")
                items["app:runner"]["session"] = True
                relay.push(items)
                for cmd in relay.take_commands("github"):
                    relay.done(cmd["id"], run_command(cmd, "github"))
            except Exception as exc:
                log.warning("session push failed: %s", exc)
            _safe(notes.tick)

    t = threading.Thread(target=pusher, daemon=True)
    t.start()
    try:
        desk.run()
    finally:
        done.set()
        _GH_SESSION.clear()
        t.join(timeout=10)
        relay.push(app_items("github"), force=True)
        relay.push_files()
        _safe(notes.tick)    # the last sells and the end of the session


def _safe(fn):
    try:
        fn()
    except Exception as exc:   # notifications must never stop a session
        log.warning("Push tick failed: %s", exc)


# ------------------------------------------------------------ a session the computer left behind
def _stamp(iso) -> float | None:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def _same_silence(told, started: str, wrote: float) -> bool:
    """`told` is a silence the notifications already named: "<session start>@<second it last wrote>" (see
    push.Pusher). The PC reads that second from its own state file and GitHub from the copy in the relay,
    pushed up to 30 s later, so one silence can be named a little apart. Under two minutes it is the same
    one: a session that came back and stopped again wrote for longer than that in between."""
    head, _, at = str(told or "").rpartition("@")
    try:
        return bool(started) and head == started and abs(int(at) - wrote) < PC_FRESH_S
    except ValueError:
        return False


def _tell_dead_sessions(relay: Relay, rn: dict) -> list[str]:
    """GitHub is taking over from a computer that went quiet. A memes or futures session that the files just
    pulled show unfinished is taken for dead (the computer sleeps, is off or lost the network; with only the
    dashboard down it still runs): the phone gets the notifications' own "parou de responder", once, with
    what the session had open when it was last heard of. Their record in the
    relay (push:seen:<desk>, "stall") is what keeps the next cycles from saying it again. Nothing else in the
    files is announced: buys and sells from before the silence are old news."""
    if _GH_SESSION.is_set() or (rn.get("owner") == "github" and rn.get("session") and _age(rn.get("heartbeat")) < 30):
        return []                        # a session GitHub itself is running is not a dead one
    from . import push
    from .guard import dur
    notes = push.Pusher(relay)
    if not notes.enabled:
        return []
    told, done = [], None
    for desk, path in push.DESKS.items():
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if not isinstance(st, dict) or st.get("finished") or not st.get("started"):
            continue
        started = str(st["started"])
        saved = _stamp(st.get("saved"))
        wrote = saved or mtime                       # the session's own clock when it has one, as the app reads it
        # named exactly as Pusher.tick names it on this disk: a session GitHub runs for the other desk finds it told
        silence = f"{started}@{int(mtime)}"
        done = notes._announced() if done is None else done
        rec = done.get(desk, {})
        told_it = str(rec.get("stall") or "")
        if told_it == silence or _same_silence(told_it, started, wrote):
            continue
        if saved is None and told_it.rpartition("@")[0] == started:
            # the old code writes no "saved", and the relay's date moves when the PC pushes its copy again: there
            continue                     # only the session start says that this silence was already told
        n = len(st.get("positions") or [])
        # the computer's silence is all that is known here (the session may still be watching its stops)
        what = ("não tinha posições abertas" if not n else
                f"tinha {n} {'posição aberta' if n == 1 else 'posições abertas'}")
        msg = {"title": f"Sessão de {'futuros' if desk == 'perps' else 'memes'} parou de responder",
               "body": (f"O computador deixou de dar sinal há {dur(max(0.0, time.time() - wrote))}; a sessão {what} "
                        "na última vez. Vê o computador. Papel."),
               "url": push.PAGES[desk], "tag": f"{desk}-stall-{silence}"}
        notes.refresh()
        if notes.subs and not notes.send(msg):
            continue                     # it reached nobody: the next cycle tries again
        relay.push({"push:seen:" + desk: {**rec, "stall": silence}})
        told.append(desk)
    return told


def _adopt_silences(relay: Relay):
    """The PC's side of _tell_dead_sessions. The notifications name a silence by the second this PC's own
    state file was last written, which is not to the second what GitHub read in the relay (nor what this PC
    read before a restart, when the pull above gave the file the relay's date). A silence already announced
    is renamed to this PC's reading, so Pusher finds it in the record and stays quiet about it."""
    if not env("VAPID_PRIVATE_KEY"):
        return
    from . import push
    items = relay.get("push:seen:")
    for desk, path in push.DESKS.items():
        rec = (items.get("push:seen:" + desk) or {}).get("value") or {}
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
            wrote = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if not isinstance(st, dict) or not rec.get("stall"):
            continue
        mine = f"{st.get('started', '')}@{int(wrote)}"       # exactly as Pusher.tick names it
        if rec["stall"] != mine and _same_silence(rec["stall"], str(st.get("started") or ""), wrote):
            relay.push({"push:seen:" + desk: {**rec, "stall": mine}})


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _age(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        return time.time() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 1e9
