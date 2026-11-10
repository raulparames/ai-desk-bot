"""Lock-screen notifications (Web Push) for the phone app.

The app added to the iPhone home screen (iOS 16.4 or later) asks for permission and subscribes; the
Supabase function `push` keeps the subscription in the relay under push:sub:*. Whoever runs the bot
(the PC, or GitHub when the PC is off) reads them and sends a notification for every buy and sell of
a memes or futures session, for every alarm a desk raises (a brake on entries, prices that stopped
arriving, time without watching), when a session stops answering and when it ends.

A notification counts as handled only after a push service took it: one that nobody received is tried
again on the next ticks, three times at most.

Keys: the public key is in the app (public by design); the private key is VAPID_PRIVATE_KEY in .env
and in GitHub Secrets. Notifications only go to the browsers' own push services (Apple, Google,
Mozilla, Microsoft), never to any other address.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests

from .config import ROOT, env
from .guard import dur

log = logging.getLogger("push")
VAPID_PUBLIC = "BA2OTHuaQ8SfVrER8l_18Emkeg9s9g9e6rR9pgvH9KR5AS4A4MwONdlVAjJkWdUuO7XeJq0fn4wWqw6rEK-0p2U"
CONTACT_URL = "https://raulparames.github.io"     # VAPID "sub": a bare https origin (one with a path is refused)
PUSH_HOSTS = ("web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com")
PUSH_SUFFIXES = (".push.apple.com", ".notify.windows.com", ".push.services.mozilla.com")
DESKS = {"memes": ROOT / "memetest" / "state.json", "perps": ROOT / "perpdesk" / "state.json"}
PAGES = {"memes": "memes.html", "perps": "perps.html"}
STALL_S = 180          # a running session writes its state every few seconds
LAST = 10 ** 6         # "every event of that second"


def contact() -> str:
    """VAPID "sub": who sends these. The e-mail in PUSH_CONTACT (.env) when there is one, else the app's origin."""
    mail = (env("PUSH_CONTACT") or "").strip()
    if mail.lower().startswith("mailto:"):
        mail = mail[7:]
    return "mailto:" + mail if "@" in mail else CONTACT_URL


def push_host_ok(endpoint: str) -> bool:
    try:
        u = urlparse(endpoint)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host in PUSH_HOSTS or host.endswith(PUSH_SUFFIXES))


def sub_key(endpoint: str) -> str:
    return "push:sub:" + hashlib.sha256(endpoint.encode()).hexdigest()[:24]


def clean_sub(raw: dict) -> dict | None:
    """Only what Web Push needs, and only for a real push service."""
    if not isinstance(raw, dict):
        return None
    endpoint, keys = str(raw.get("endpoint") or ""), raw.get("keys") or {}
    p256dh, auth = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
    if not push_host_ok(endpoint) or len(endpoint) > 1000 or not (40 < len(p256dh) < 200) or not (10 < len(auth) < 60):
        return None
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}, "at": time.time()}


def _eur(v: float, sign: bool = False) -> str:
    s = f"{abs(v):,.2f}".replace(",", " ").replace(".", ",") + " €"
    return (("+" if v >= 0 else "−") + s) if sign else s


def _x(v) -> str:
    return f"x{v:.2f}".replace(".", ",") if v else ""


def message(desk: str, e: dict, venue: str) -> dict | None:
    """Title and text for one buy, sell or alarm, in the app's words."""
    coin, perps = e.get("coin", ""), desk == "perps"
    page = f"{PAGES[desk]}#/moeda/{quote(coin)}"
    if e.get("verdict") == "alert":
        text, named = str(e.get("reason") or ""), coin not in ("", "—")
        return {"title": f"Alerta · {'Futuros' if perps else 'Memecoins'}" + (f" · {coin}" if named else ""),
                "body": text[:1].upper() + text[1:], "url": page if named else PAGES[desk],
                "tag": f"{desk}-alert-{e.get('ts')}-{hashlib.sha256(text.encode()).hexdigest()[:6]}"}
    if e.get("verdict") == "buy":
        if perps:
            title = f"Abriu {e.get('side', 'long')} {coin} {e.get('lev', '')}x · margem {_eur(e.get('eur') or 0)}"
        else:
            title = f"Comprou {coin} · {_eur(e.get('eur') or 0)}"
        risk = e.get("risk_eur")
        body = (f"Arrisca {_eur(risk)} no stop · " if risk else "") + f"nota {e.get('score', '—')} · na {venue} (papel)"
    elif e.get("verdict") == "sell":
        pnl, kind = e.get("pnl_eur") or 0.0, e.get("kind")
        if kind == "half":
            title = f"{'Fechou' if perps else 'Vendeu'} metade de {coin} · {_eur(pnl, True)}"
            body = ("O stop passou para a entrada; o resto segue com trailing." if perps
                    else "A entrada voltou; o resto segue com trailing de 10%.")
        elif kind == "stop":
            title = f"Parou {coin} · {_eur(pnl, True)}"
            body = "Stop atingido. Posição pequena, segue em frente."
        elif kind == "liq" and perps:
            title = f"Liquidada: {coin} · {_eur(pnl, True)}"
            body = "A margem desta posição foi toda. Papel."
        elif kind == "liq":
            title = f"Saiu de {coin} · {_eur(pnl, True)}"
            body = "A liquidez da pool caiu: saiu antes de ficar preso. Papel."
        else:
            title = f"Fechou {coin} · {_eur(pnl, True)}" + (f" ({_x(e.get('mult'))})" if not perps and e.get("mult") else "")
            body = "Trailing do máximo." if kind == "trail" else "Fim da sessão."
    else:
        return None
    return {"title": title, "body": body, "url": page, "tag": f"{desk}-{e.get('ts')}-{coin}"}


def _keyed(events: list[dict]) -> list[tuple[tuple, dict]]:
    """Each event with (time, place among the events of that same second): two in one second stay two."""
    out, n = [], {}
    for e in events:
        n[e["ts"]] = n.get(e["ts"], -1) + 1
        out.append(((e["ts"], n[e["ts"]]), e))
    return out


def _key(v) -> tuple:
    return tuple(v) if isinstance(v, (tuple, list)) else (v, LAST)


def _future(iso: str) -> bool:
    try:
        return datetime.fromisoformat(iso) > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


class Pusher:
    SUBS_EVERY_S = 30
    MAX_PER_TICK = 4
    TRIES = 3

    def __init__(self, relay):
        self.relay = relay
        self.key = env("VAPID_PRIVATE_KEY")
        self.subs: dict[str, dict] = {}
        self.subs_at = 0.0
        self.known: set[str] | None = None        # subscriptions already greeted
        self.seen: dict[str, tuple] = {}          # desk -> newest event already handled (see _keyed)
        self.ended: dict[str, str] = {}           # desk -> session start whose end was announced
        self.stalled: dict[str, str] = {}         # desk -> the silence already announced (see _desk)
        self.tries: dict[str, int] = {}           # desk -> ticks its next notification reached nobody
        self._quiet: dict[str, int] = {}          # desk -> ticks in a row its state file looked dead
        self._tick_at = 0.0
        self._woke = 0.0                          # when this runner last came back from a sleep
        self.http = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.key) and self.relay.enabled

    # ------------------------------------------------------------ subscriptions
    def refresh(self):
        if time.time() - self.subs_at < self.SUBS_EVERY_S:
            return
        self.subs_at = time.time()
        items = self.relay.get("push:sub:")
        subs = {k: v["value"] for k, v in items.items() if isinstance(v.get("value"), dict) and not v["value"].get("gone")}
        new = set(subs) - (self.known or set(subs))
        self.subs = subs
        self.known = set(subs) | (self.known or set())
        for k in new:   # a phone that just turned notifications on gets a first one, so it knows it works
            self._send_one(k, {"title": "Notificações ligadas ✓", "body": "Vais saber aqui de cada compra, venda e alerta das mesas (papel).",
                               "url": "./", "tag": "welcome"})

    def _send_one(self, key: str, msg: dict) -> bool:
        from pywebpush import WebPushException, webpush
        sub = self.subs.get(key)
        if not sub or not push_host_ok(sub.get("endpoint", "")):
            return False
        try:
            webpush(subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]}, data=json.dumps(msg, ensure_ascii=False),
                    vapid_private_key=self.key, vapid_claims={"sub": contact()}, ttl=6 * 3600, timeout=10,
                    requests_session=self.http, headers={"Urgency": "high"})
            return True
        except WebPushException as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (404, 410):   # the phone turned them off or removed the app
                self.relay.push({key: {"gone": True, "at": time.time()}})
                self.subs.pop(key, None)
            log.warning("push to %s… failed (%s)", key[-6:], code or type(exc).__name__)
        except requests.RequestException as exc:
            log.warning("push failed: %s", type(exc).__name__)
        except Exception as exc:     # e.g. a claim the library refuses: one bad send never stops the others
            log.warning("push failed: %s: %s", type(exc).__name__, str(exc).replace(str(self.key), "…")[:160])
        return False

    def send(self, msg: dict) -> int:
        return sum(self._send_one(k, msg) for k in list(self.subs))

    def _deliver(self, desk: str, msg: dict | None) -> bool:
        """True when the message is dealt with: someone got it, there is nobody to send it to, or it was
        given up after TRIES ticks. False: it reached nobody and goes again on the next tick."""
        if not msg or not self.subs or self.send(msg) or not self.subs:
            self.tries.pop(desk, None)
            return True
        self.tries[desk] = self.tries.get(desk, 0) + 1
        if self.tries[desk] < self.TRIES:
            return False
        log.warning("push: gave up on a %s notification after %d tries", desk, self.TRIES)
        self.tries.pop(desk, None)
        return True

    # ------------------------------------------------------------ what to announce
    def tick(self):
        """Called every 5 s by whoever runs the bot."""
        if not self.enabled:
            return
        self.refresh()
        now = time.time()
        if self._tick_at and now - self._tick_at > 60:
            self._woke = now      # this runner was itself asleep: the sessions slept too and need a moment to write
        # files look old for no reason on the first tick and for STALL_S after a wake (that silence was the
        # whole computer's, which Supabase's switch tells: deskbot/watch.py)
        away = now - self._tick_at > 60 or now - self._woke < STALL_S
        self._tick_at = now
        for desk, path in DESKS.items():
            try:
                st = json.loads(path.read_text(encoding="utf-8"))
                wrote = path.stat().st_mtime
            except (OSError, ValueError):
                continue
            try:
                self._desk(desk, st, now - wrote, away, f"{st.get('started', '')}@{int(wrote)}")
            except Exception:        # one desk's trouble never silences the other
                log.exception("push for the %s desk failed", desk)

    def _silent(self, desk: str, st: dict, age: float, away: bool, silence: str) -> bool:
        """A session that should be running stopped writing its state (it crashed, or the computer sleeps)."""
        if st.get("finished") or age < STALL_S or away or not st.get("started") or not _future(st.get("ends", "")):
            self._quiet[desk] = 0
            return False
        self._quiet[desk] = self._quiet.get(desk, 0) + 1
        return self._quiet[desk] >= 2 and self.stalled.get(desk) != silence

    def _desk(self, desk: str, st: dict, age: float, away: bool, silence: str):
        """`silence` names one silence: the session and the moment it last wrote. If it comes back and stops
        again, that is a new one."""
        events = _keyed([e for e in st.get("events", []) if e.get("verdict") in ("buy", "sell", "alert") and e.get("ts")])
        started = st.get("started", "")
        if desk not in self.seen:          # first look: nothing old is announced
            self.seen[desk] = max((k for k, _ in events), default=("", 0))
            self.ended[desk] = started if st.get("finished") else ""
            self.stalled[desk] = ""        # a session found already silent is still news: the relay record stops a repeat
            return
        seen = _key(self.seen[desk])
        fresh = sorted(((k, e) for k, e in events if k > seen), key=lambda x: x[0])
        ending = bool(st.get("finished")) and self.ended.get(desk) != started
        silent = self._silent(desk, st, age, away, silence)
        if not fresh and not ending and not silent:
            return
        # the PC and GitHub hand sessions over: what the other one already announced is skipped
        done = self._announced().get(desk, {})
        told = (done.get("ts", ""), done.get("n", LAST))
        fresh = [(k, e) for k, e in fresh if k > told]
        venue = "INVO" if desk == "perps" else "FOMO"
        trades = [k for k, e in fresh if e["verdict"] != "alert"]
        burst = set(trades[:-self.MAX_PER_TICK])                 # only the latest trades of a burst; alarms always go
        last, whole = max(seen, told), True
        for k, e in fresh:
            if k not in burst and not self._deliver(desk, message(desk, e, venue)):
                whole = False
                break
            last = k
        self.seen[desk] = last
        rec = {"ts": last[0], "n": last[1], "end": done.get("end", ""), "stall": done.get("stall", "")}
        if whole and silent:
            n = len(st.get("positions", []))
            what = ("Sem posições abertas." if not n else
                    f"{n} {'posição aberta fica' if n == 1 else 'posições abertas ficam'} sem stop até voltar.")
            msg = {"title": f"Sessão de {'futuros' if desk == 'perps' else 'memes'} parou de responder",
                   "body": f"Sem sinal há {dur(age)}. {what} Vê o computador. Papel.",
                   "url": PAGES[desk], "tag": f"{desk}-stall-{silence}"}
            whole = self._deliver(desk, msg if done.get("stall") != silence else None)
            if whole:
                self.stalled[desk] = rec["stall"] = silence
        if whole and ending:
            start, end = st.get("start_eur") or 0, st.get("equity_eur") or 0
            msg = None
            if start and done.get("end") != started:
                mult = f"{end / start:.2f}".replace(".", ",")
                n = sum(1 for t in st.get("trades", []) if t.get("kind") != "half")    # the half is part of its position
                msg = {"title": f"Sessão de {'futuros' if desk == 'perps' else 'memes'} terminou · {_eur(end)}",
                       "body": f"Começou com {_eur(start)}: {mult}×, {n} trades. Papel.",
                       "url": PAGES[desk], "tag": f"{desk}-end-{started}"}
            if self._deliver(desk, msg):
                self.ended[desk] = rec["end"] = started
        if rec != {"ts": done.get("ts", ""), "n": done.get("n"), "end": done.get("end", ""), "stall": done.get("stall", "")}:
            self.relay.push({"push:seen:" + desk: rec})

    def _announced(self) -> dict:
        items = self.relay.get("push:seen:")
        return {k.rsplit(":", 1)[-1]: (v.get("value") or {}) for k, v in items.items()}
