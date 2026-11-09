"""Relay between whoever runs the bot (this PC or GitHub) and the phone app, through Supabase.

  PC on:  the dashboard process runs `pc_loop` on a thread. Every 5 s it pushes the three desks as the
          app shows them and a heartbeat, every 30 s the raw desk files (so GitHub can carry on from
          them), and it carries out the app's commands (pause a desk, start or stop a memes session).
  PC off: GitHub Actions runs `python -m deskbot gh-cycle` every 10 minutes. When the PC's heartbeat is
          older than 2 minutes, it takes over: pulls the raw files, carries out commands (a memes
          session runs inside that job, pushing every 5 s), runs the futures and stocks cycle, pushes.
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
         "memetest/state.json", "perpdesk/state.json"]
SESSION_DIRS = {"memes": "memetest", "perps": "perpdesk"}
PC_FRESH_S = 120
POINTS = 400   # chart points sent to the phone per desk


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
            h = hashlib.sha1(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()
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

    def pull_files(self, only_newer: bool = True) -> list[str]:
        """Write remote raw files locally when they are newer than the local copy."""
        remote = self.get("file:")
        got = []
        for key, item in remote.items():
            rel = key[len("file:"):]
            if rel not in FILES:
                continue
            p = ROOT / rel
            ts = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).timestamp()
            if only_newer and p.exists() and p.stat().st_mtime >= ts - 5:
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
    for name in ("memes", "futuros", "acoes", "perps"):
        d = desks_api.desk(name, bt)
        if owner == "github" and _GH_KIND.get("desk") == name:
            d["running"] = bool(d.get("running") or _GH_SESSION.is_set())
        items[f"app:desk:{name}"] = _thin(d)
    items["app:runner"] = {"owner": owner, "host": socket.gethostname() if owner == "pc" else "github",
                           "heartbeat": datetime.now(timezone.utc).isoformat()}
    return json.loads(json.dumps(items, default=str))   # numpy/pandas values -> plain JSON


def run_command(cmd: dict, owner: str) -> dict:
    from . import desks_api
    kind, p = cmd.get("kind"), cmd.get("payload") or {}
    try:
        if kind == "pause":
            code, res = desks_api.pause(str(p.get("name")), bool(p.get("paused")))
        elif kind == "start_session":
            if owner == "github":
                return {"code": 202, "deferred": True}
            if p.get("desk") == "perps":
                code, res = desks_api.start_perps(int(p.get("minutes") or 0), float(p.get("eur") or 110),
                                                  float(p.get("lev") or 5))
            else:
                code, res = desks_api.start_memes(int(p.get("minutes") or 0), float(p.get("eur") or 110),
                                                  str(p.get("venue") or "gmgn"))
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
                relay.push(app_items("pc"))
                for cmd in relay.take_commands("pc"):
                    relay.done(cmd["id"], run_command(cmd, "pc"))
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
        desk = memedesk.MemesDesk(minutes, eur, str(session.get("venue") or "gmgn"))
    done = threading.Event()
    _GH_KIND["desk"] = "perps" if session.get("desk") == "perps" else "memes"
    _GH_SESSION.set()
    from .push import Pusher
    notes = Pusher(relay)
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
