"""The phone hears it when the computer (or GitHub) running a session goes quiet: a dead man's switch.

While a memes or futures session runs, whoever runs the bot keeps its own switch in the relay
(`watch:arm:pc` or `watch:arm:github`), rewritten once a minute with the notification the phone should get
if it goes quiet: already encrypted for each phone and signed with a VAPID token valid for 12 h. The row's
date is the runner's sign of life. Supabase checks every minute (pg_cron, `watch_tick` in
deploy/supabase_watch.sql): an active switch not rewritten for 3 minutes is a silence, and the function
`watch` posts a copy of those requests to the push services. The VAPID private key never leaves this
machine or GitHub Secrets: Supabase only holds requests it can send as they are, and only to those phones.

A session that dies while the computer is awake is the notifications' own business (push.Pusher): the
runner is then still alive, and with no session left alive here the switch is turned off at once.

Who claims a silence first tells it: Supabase, or GitHub when its 10-minute cycle takes over
(relay._tell_dead_sessions asks `runner_watch_claim`). It counts as told only once a push service took it;
the other one tells it otherwise.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from urllib.parse import urlparse

from . import push
from .config import env

log = logging.getLogger("watch")
ARM_EVERY_S = 60          # how stale the prepared notification may get
VALID_S = 12 * 3600       # VAPID token lifetime (push services accept up to 24 h)
QUIET_S = 180             # Supabase waits this long for a rewrite of the switch (watch_tick says the same)
KEY = "watch:arm:"        # + "pc" or "github": each runner owns its own
FIRED = "watch:fired:"    # + runner: who claimed its last silence and whether a push service took it
MAX_SENDS = 20            # as many phones as the `push` function accepts


def live_sessions(only: str = "") -> list[dict]:
    """Sessions running here now: started, not finished, not past their end, and writing their state."""
    now, out = time.time(), []
    for desk, path in push.DESKS.items():
        if only and desk != only:
            continue
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
            age = now - path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if (not isinstance(st, dict) or st.get("finished") or not st.get("started")
                or not push._future(st.get("ends", "")) or age >= push.STALL_S):
            continue
        out.append({"desk": desk, "started": str(st["started"]), "open": len(st.get("positions") or [])})
    return out


def message(runner: str, sessions: list[dict], gen: str) -> dict:
    """What the phone reads if this runner goes quiet. Only what is known: when it was last heard of."""
    names = {"memes": "memes", "perps": "futuros"}
    parts = []
    for s in sessions:
        n = s["open"]
        held = ("sem posições abertas" if not n else
                f"com {n} {'posição aberta' if n == 1 else 'posições abertas'}")
        parts.append(f"{names[s['desk']]} {held}")
    held_any = any(s["open"] for s in sessions)
    who = "computador" if runner == "pc" else "GitHub"
    body = (f"Deixou de enviar dados há mais de 2 min. Na última vez: sessão de {' e '.join(parts)}. "
            + (f"Se o {who} estiver a dormir ou sem rede, ficam sem stop até voltar. " if held_any else "")
            + "Papel.")
    first = next((s for s in sessions if s["open"]), sessions[0])
    return {"title": f"Sem sinal do {who}", "body": body, "url": push.PAGES[first["desk"]], "tag": f"watch-{gen}"}


def prepared(sub: dict, msg: dict, private_key: str, exp: int) -> dict:
    """One push request, ready to send as it is: encrypted body (aes128gcm) and VAPID headers."""
    from py_vapid import Vapid
    from pywebpush import WebPusher

    u = urlparse(sub["endpoint"])
    data = WebPusher({"endpoint": sub["endpoint"], "keys": sub["keys"]})._prepare_send_data(
        json.dumps(msg, ensure_ascii=False), {"Urgency": "high"}, ttl=6 * 3600, content_encoding="aes128gcm")
    headers = {k.lower(): str(v) for k, v in data["headers"].items()}
    vapid = Vapid.from_string(private_key=private_key).sign(
        {"aud": f"{u.scheme}://{u.netloc}", "exp": exp, "sub": push.contact()})
    headers.update({k.lower(): v.decode() if isinstance(v, bytes) else str(v) for k, v in vapid.items()})
    return {"endpoint": sub["endpoint"], "headers": headers, "body": base64.b64encode(data["data"]).decode()}


class Arm:
    """Kept by the runner's loop: tick() every few seconds, off() when the runner stops on purpose."""

    def __init__(self, relay, runner: str, only: str = ""):
        """Each runner writes only its own switch (KEY + runner). The PC turns off what its previous run
        left, unless a session is live. A GitHub session (`only` = its desk) touches its switch only once
        it set it: the one a previous job left when it died may still have a silence to tell."""
        self.relay, self.runner, self.only = relay, runner, only
        self.key = env("VAPID_PRIVATE_KEY")
        self.at = 0.0
        self.shown: list = []                 # the sessions the switch now in the relay names
        self.armed: bool | None = False if only else None    # None: not known what the relay holds

    @property
    def enabled(self) -> bool:
        return bool(self.key) and self.relay.enabled

    def _subs(self) -> dict[str, dict]:
        items = self.relay.get("push:sub:")
        out = {}
        for k, v in items.items():
            sub = push.clean_sub(v.get("value") if isinstance(v, dict) else None)
            if sub and not (v.get("value") or {}).get("gone"):
                out[k] = sub
        return dict(list(out.items())[:MAX_SENDS])

    def off(self):
        """No session alive here: nothing to tell if this runner goes quiet. A failed write is tried again
        on the next tick (armed stays as it was)."""
        if self.enabled and self.armed is not False:
            self.relay.push({KEY + self.runner: {"active": False, "runner": self.runner, "at": time.time()}}, force=True)
            self.armed, self.shown = False, []

    def tick(self, force: bool = False):
        """Every few seconds: off as soon as no session is alive, set again at once when the sessions or
        what they hold change, and rewritten every ARM_EVERY_S (that rewrite is the sign of life)."""
        if not self.enabled:
            return
        sessions = live_sessions(self.only)
        if not sessions:
            self.off()
            return
        shown = [(s["desk"], s["started"], s["open"]) for s in sessions]
        if not force and shown == self.shown and time.time() - self.at < ARM_EVERY_S:
            return
        self.at, self.shown = time.time(), shown          # also when it fails: tried again a minute later
        subs = self._subs()
        if not subs:
            self.off()
            return
        now = int(time.time())
        self.gen_ms = max(getattr(self, "gen_ms", 0) + 1, int(time.time() * 1000))   # never the same name twice
        gen = f"{self.runner}-{self.gen_ms}"
        msg = message(self.runner, sessions, gen)
        sends = []
        for k, sub in subs.items():
            try:
                sends.append({"key": k, **prepared(sub, msg, self.key, now + VALID_S)})
            except Exception as exc:     # one odd subscription never disarms the others
                log.warning("watch: could not prepare %s…: %s", k[-6:], type(exc).__name__)
        if not sends:
            self.off()
            return
        self.relay.push({KEY + self.runner: {
            "active": True, "gen": gen, "runner": self.runner, "at": now, "until": now + VALID_S - 600,
            "desks": [{"desk": s["desk"], "started": s["started"]} for s in sessions], "sends": sends}}, force=True)
        self.armed = True
