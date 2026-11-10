"""Lock-screen notifications (Web Push) for the phone app.

The app added to the iPhone home screen (iOS 16.4 or later) asks for permission and subscribes; the
Supabase function `push` keeps the subscription in the relay under push:sub:*. Whoever runs the bot
(the PC, or GitHub when the PC is off) reads them and sends a notification for every buy and sell of
a memes or futures session, and when a session ends.

Keys: the public key is in the app (public by design); the private key is VAPID_PRIVATE_KEY in .env
and in GitHub Secrets. Notifications only go to the browsers' own push services (Apple, Google,
Mozilla, Microsoft), never to any other address.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from urllib.parse import quote, urlparse

import requests

from .config import ROOT, env

log = logging.getLogger("push")
VAPID_PUBLIC = "BA2OTHuaQ8SfVrER8l_18Emkeg9s9g9e6rR9pgvH9KR5AS4A4MwONdlVAjJkWdUuO7XeJq0fn4wWqw6rEK-0p2U"
CONTACT = "https://raulparames.github.io/ai-desk-bot/"     # VAPID "sub": who sends these (a URL, no e-mail)
PUSH_HOSTS = ("web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com")
PUSH_SUFFIXES = (".push.apple.com", ".notify.windows.com", ".push.services.mozilla.com")
DESKS = {"memes": ROOT / "memetest" / "state.json", "perps": ROOT / "perpdesk" / "state.json"}
PAGES = {"memes": "memes.html", "perps": "perps.html"}


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
    """Title and text for one buy or sell, in the app's words."""
    coin, perps = e.get("coin", ""), desk == "perps"
    page = f"{PAGES[desk]}#/moeda/{quote(coin)}"
    if e.get("verdict") == "buy":
        if perps:
            title = f"Abriu {e.get('side', 'long')} {coin} {e.get('lev', '')}x · margem {_eur(e.get('eur') or 0)}"
        else:
            title = f"Comprou {coin} · {_eur(e.get('eur') or 0)}"
        body = f"40% do saldo livre · nota {e.get('score', '—')} · na {venue} (papel)"
    elif e.get("verdict") == "sell":
        pnl, kind = e.get("pnl_eur") or 0.0, e.get("kind")
        if kind == "half":
            title = f"{'Fechou' if perps else 'Vendeu'} metade de {coin} · {_eur(pnl, True)}"
            body = ("O stop passou para a entrada; o resto segue com trailing." if perps
                    else "A entrada voltou; o resto segue com trailing de 10%.")
        elif kind == "stop":
            title = f"Parou {coin} · {_eur(pnl, True)}"
            body = "Stop atingido. Posição pequena, segue em frente."
        elif kind == "liq":
            title = f"Liquidada: {coin} · {_eur(pnl, True)}"
            body = "A margem desta posição foi toda. Papel."
        else:
            title = f"Fechou {coin} · {_eur(pnl, True)}" + (f" ({_x(e.get('mult'))})" if not perps and e.get("mult") else "")
            body = "Trailing do máximo. Próxima compra: 40% do saldo livre." if kind == "trail" else "Fim da sessão."
    else:
        return None
    return {"title": title, "body": body, "url": page, "tag": f"{desk}-{e.get('ts')}-{coin}"}


class Pusher:
    SUBS_EVERY_S = 30
    MAX_PER_TICK = 4

    def __init__(self, relay):
        self.relay = relay
        self.key = env("VAPID_PRIVATE_KEY")
        self.subs: dict[str, dict] = {}
        self.subs_at = 0.0
        self.known: set[str] | None = None        # subscriptions already greeted
        self.seen: dict[str, str] = {}            # desk -> newest event time already handled
        self.ended: dict[str, str] = {}           # desk -> session start whose end was announced
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
            self._send_one(k, {"title": "Notificações ligadas ✓", "body": "Vais saber aqui de cada compra e venda das mesas (papel).",
                               "url": "./", "tag": "welcome"})

    def _send_one(self, key: str, msg: dict) -> bool:
        from pywebpush import WebPushException, webpush
        sub = self.subs.get(key)
        if not sub or not push_host_ok(sub.get("endpoint", "")):
            return False
        try:
            webpush(subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]}, data=json.dumps(msg, ensure_ascii=False),
                    vapid_private_key=self.key, vapid_claims={"sub": CONTACT}, ttl=6 * 3600, timeout=10,
                    requests_session=self.http, headers={"Urgency": "high"})
            return True
        except WebPushException as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (404, 410):   # the phone turned them off or removed the app
                self.relay.push({key: {"gone": True, "at": time.time()}})
                self.subs.pop(key, None)
            log.warning("push to %s… failed (%s)", key[-6:], code or exc)
        except requests.RequestException as exc:
            log.warning("push failed: %s", type(exc).__name__)
        return False

    def send(self, msg: dict) -> int:
        return sum(self._send_one(k, msg) for k in list(self.subs))

    # ------------------------------------------------------------ what to announce
    def tick(self):
        """Called every 5 s by whoever runs the bot."""
        if not self.enabled:
            return
        self.refresh()
        for desk, path in DESKS.items():
            try:
                st = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            events = [e for e in st.get("events", []) if e.get("verdict") in ("buy", "sell")]
            newest = max((e["ts"] for e in events), default="")
            if desk not in self.seen:          # first look: nothing old is announced
                self.seen[desk] = newest
                self.ended[desk] = st.get("started", "") if st.get("finished") else ""
                continue
            fresh = sorted((e for e in events if e["ts"] > self.seen[desk]), key=lambda e: e["ts"])
            started = st.get("started", "")
            ending = bool(st.get("finished")) and self.ended.get(desk) != started
            if not fresh and not ending:
                continue
            if fresh:
                self.seen[desk] = fresh[-1]["ts"]
            if ending:
                self.ended[desk] = started
            # the PC and GitHub hand sessions over: what the other one already announced is skipped
            done = self._announced().get(desk, {})
            fresh = [e for e in fresh if e["ts"] > done.get("ts", "")]
            ending = ending and done.get("end") != started
            venue = "INVO" if desk == "perps" else "FOMO"
            for e in fresh[-self.MAX_PER_TICK:]:
                msg = message(desk, e, venue)
                if msg and self.subs:
                    self.send(msg)
            start, end = st.get("start_eur") or 0, st.get("equity_eur") or 0
            if ending and self.subs and start:
                mult = f"{end / start:.2f}".replace(".", ",")
                self.send({"title": f"Sessão de {'futuros' if desk == 'perps' else 'memes'} terminou · {_eur(end)}",
                           "body": f"Começou com {_eur(start)}: {mult}×, {len(st.get('trades', []))} trades. Papel.",
                           "url": PAGES[desk], "tag": f"{desk}-end-{started}"})
            self.relay.push({"push:seen:" + desk: {"ts": max([done.get("ts", "")] + [e["ts"] for e in fresh]),
                                                   "end": started if ending else done.get("end", "")}})

    def _announced(self) -> dict:
        items = self.relay.get("push:seen:")
        return {k.rsplit(":", 1)[-1]: (v.get("value") or {}) for k, v in items.items()}
