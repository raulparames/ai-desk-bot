"""The switch Supabase pulls when the runner of a session goes quiet (deskbot/watch.py).

The prepared notification must open on the phone (decrypted here with the phone's own keys), carry a VAPID
token for the right push service, hold no private key, and exist only while a session is alive. GitHub's
cycle and Supabase never both tell the same silence.

Offline: the relay is a dictionary, the folders are temp ones and the keys are made for the test.
"""

import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone

import http_ece
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid

from deskbot import push, relay, watch


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def now_iso(ago: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() - ago, timezone.utc).isoformat()


class Kv:
    """The relay's table and the switch's functions as deploy/supabase_watch.sql writes them, in memory."""

    def __init__(self):
        self.rows, self.claims, self.results = {}, [], []

    def rpc(self, name, body):
        if name == "runner_push":
            for k, v in body["p_items"].items():
                self.rows[k] = {"value": json.loads(json.dumps(v)), "updated_at": now_iso()}
        elif name == "runner_get":
            return {k: json.loads(json.dumps(v)) for k, v in self.rows.items() if k.startswith(body["p_prefix"])}
        elif name == "runner_watch_claim":
            self.claims.append(body)
            runner, gen = body["p_runner"], body["p_gen"]
            arm = self.value("watch:arm:" + runner) or {}
            fired = self.value("watch:fired:" + runner) or {}
            if arm.get("gen") == gen and fired.get("gen") != gen:
                self.rows["watch:fired:" + runner] = {"value": {"gen": gen, "by": "github", "at": now_iso(), "tries": 1,
                                                                "desks": arm.get("desks", [])}, "updated_at": ""}
            fired = self.value("watch:fired:" + runner) or {}
            return {k: v for k, v in fired.items() if k != "sends"} if fired.get("gen") == gen else None
        elif name == "runner_watch_result":
            self.results.append(body)
            key = "watch:fired:" + body["p_runner"]
            fired = self.value(key) or {}
            if fired.get("gen") == body["p_gen"] and fired.get("by") == "github":
                if body["p_delivered"]:
                    fired["delivered"] = True
                else:
                    self.rows.pop(key)
        else:
            raise AssertionError(f"unexpected relay function {name}")

    def value(self, key):
        return (self.rows.get(key) or {}).get("value")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A relay in memory, the desks' state files in a temp folder, a test VAPID key and one phone."""
    vapid = Vapid()
    vapid.generate_keys()
    private = b64u(vapid.private_key.private_numbers().private_value.to_bytes(32, "big"))
    phone = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    p256dh = b64u(phone.public_key().public_bytes(serialization.Encoding.X962,
                                                  serialization.PublicFormat.UncompressedPoint))
    monkeypatch.setenv("RELAY_SECRET", "x")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", private)
    monkeypatch.delenv("PUSH_CONTACT", raising=False)
    monkeypatch.setattr(push, "DESKS", {"memes": tmp_path / "memetest" / "state.json",
                                        "perps": tmp_path / "perpdesk" / "state.json"})
    kv = Kv()
    r = relay.Relay()
    monkeypatch.setattr(r, "rpc", kv.rpc)
    endpoint = "https://web.push.apple.com/QAbc123"
    kv.rows[push.sub_key(endpoint)] = {"value": {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": b64u(auth)}},
                                       "updated_at": ""}

    class R:
        pass

    out = R()
    out.kv, out.relay, out.phone, out.auth, out.private, out.vapid, out.endpoint = kv, r, phone, auth, private, vapid, endpoint

    def session(desk="memes", positions=2, finished=False, age=0.0, ends_in_h=2.0, started="2026-11-10T10:00:00+00:00"):
        path = push.DESKS[desk]
        path.parent.mkdir(parents=True, exist_ok=True)
        ends = (datetime.now(timezone.utc) + timedelta(hours=ends_in_h)).isoformat()
        path.write_text(json.dumps({"started": started, "ends": ends, "finished": finished,
                                    "positions": [{"coin": f"C{i}"} for i in range(positions)]}), encoding="utf-8")
        t = time.time() - age
        os.utime(path, (t, t))

    out.session = session
    return out


def opened(rig, send: dict) -> dict:
    """What the phone shows: the body decrypted with the phone's own keys."""
    raw = http_ece.decrypt(base64.b64decode(send["body"]), private_key=rig.phone, auth_secret=rig.auth,
                           version="aes128gcm")
    return json.loads(raw)




PC = watch.KEY + "pc"
GH = watch.KEY + "github"
STARTED = "2026-11-10T10:00:00+00:00"


def test_a_live_session_arms_a_notification_the_phone_can_open(rig):
    rig.session("memes", positions=2)
    watch.Arm(rig.relay, "pc").tick()
    arm = rig.kv.value(PC)
    assert arm["active"] and arm["runner"] == "pc" and arm["gen"].startswith("pc-")
    assert arm["desks"] == [{"desk": "memes", "started": STARTED}]
    (send,) = arm["sends"]
    assert send["endpoint"] == rig.endpoint and send["key"] == push.sub_key(rig.endpoint)
    msg = opened(rig, send)
    assert msg["title"] == "Sem sinal do computador"
    assert "memes com 2 posições abertas" in msg["body"] and "ficam sem stop" in msg["body"]
    assert msg["url"] == "memes.html" and msg["tag"] == "watch-" + arm["gen"]
    assert send["headers"]["content-encoding"] == "aes128gcm" and send["headers"]["ttl"] == str(6 * 3600)
    assert send["headers"]["urgency"] == "high"


def test_the_token_is_for_that_push_service_and_signed_by_our_key(rig):
    rig.session("perps", positions=0)
    watch.Arm(rig.relay, "pc").tick()
    (send,) = rig.kv.value(PC)["sends"]
    auth = send["headers"]["authorization"]
    assert auth.startswith("vapid t=")
    token = auth.split("t=", 1)[1].split(",", 1)[0]
    key = auth.split("k=", 1)[1]
    head, claims, sig = token.split(".")
    claims = json.loads(unb64u(claims))
    assert claims["aud"] == "https://web.push.apple.com" and claims["sub"] == push.CONTACT_URL
    assert time.time() + 11 * 3600 < claims["exp"] <= time.time() + 12 * 3600 + 5
    assert unb64u(key) == rig.vapid.public_key.public_bytes(serialization.Encoding.X962,
                                                            serialization.PublicFormat.UncompressedPoint)
    msg = opened(rig, send)
    assert "futuros sem posições abertas" in msg["body"] and "sem stop" not in msg["body"]


def test_the_private_key_never_reaches_the_relay(rig):
    rig.session("memes")
    watch.Arm(rig.relay, "pc").tick()
    assert rig.private not in json.dumps(rig.kv.rows)


@pytest.mark.parametrize("how", ["finished", "stale", "over", "none"])
def test_no_live_session_turns_the_switch_off(rig, how):
    if how == "finished":
        rig.session(finished=True)
    elif how == "stale":
        rig.session(age=push.STALL_S + 5)        # the session died while this runner is awake: Pusher tells that
    elif how == "over":
        rig.session(ends_in_h=-0.1)
    rig.kv.rows[PC] = {"value": {"active": True, "gen": "pc-1", "runner": "pc"}, "updated_at": ""}   # last run's
    watch.Arm(rig.relay, "pc").tick()
    assert rig.kv.value(PC)["active"] is False


def test_a_session_that_ends_turns_it_off_on_the_next_tick_not_a_minute_later(rig):
    rig.session(positions=2)
    arm = watch.Arm(rig.relay, "pc")
    arm.tick()
    assert rig.kv.value(PC)["active"]
    rig.session(positions=0, finished=True)
    arm.tick()                                   # seconds later: the laptop may sleep right after the end
    assert rig.kv.value(PC)["active"] is False


def test_no_phone_no_arm(rig):
    rig.kv.rows.pop(push.sub_key(rig.endpoint))
    rig.session()
    watch.Arm(rig.relay, "pc").tick()
    assert rig.kv.value(PC)["active"] is False


def test_a_phone_that_turned_them_off_is_left_out(rig):
    rig.kv.rows[push.sub_key(rig.endpoint)]["value"]["gone"] = True
    rig.session()
    watch.Arm(rig.relay, "pc").tick()
    assert rig.kv.value(PC)["active"] is False


def test_rearmed_at_once_when_positions_change_and_each_minute_anyway(rig):
    rig.session(positions=1)
    arm = watch.Arm(rig.relay, "pc")
    arm.tick()
    first = rig.kv.value(PC)
    arm.tick()                                   # seconds later, nothing changed: not again
    assert rig.kv.value(PC) == first
    rig.session(positions=3)
    arm.tick()
    second = rig.kv.value(PC)
    assert second["gen"] != first["gen"] and "3 posições abertas" in opened(rig, second["sends"][0])["body"]
    arm.at -= watch.ARM_EVERY_S                  # a minute on: rewritten (the sign of life Supabase reads)
    arm.tick()
    assert rig.kv.value(PC)["gen"] != second["gen"]


def test_a_failed_write_is_tried_again(rig):
    rig.session()
    arm = watch.Arm(rig.relay, "pc")
    real = rig.kv.rpc
    calls = {"n": 0}

    def flaky(name, body):
        if name == "runner_push" and not calls["n"]:
            calls["n"] += 1
            raise RuntimeError("runner_push: 503")
        return real(name, body)

    rig.relay.rpc = flaky
    with pytest.raises(RuntimeError):
        arm.tick()
    assert arm.armed is None
    rig.session(finished=True)
    arm.tick()                                   # off is written although the arm never was
    assert rig.kv.value(PC)["active"] is False and arm.armed is False


def test_each_runner_writes_only_its_own_switch(rig):
    pcs = {"active": True, "gen": "pc-5", "runner": "pc", "desks": [{"desk": "memes", "started": "s"}]}
    rig.kv.rows[PC] = {"value": pcs, "updated_at": ""}
    rig.session("memes")                         # the PC's session, pulled from the relay
    arm = watch.Arm(rig.relay, "github", only="perps")
    arm.tick()                                   # its own session has not written yet
    arm.off()
    assert rig.kv.value(PC) == pcs and GH not in rig.kv.rows
    rig.session("perps", positions=1)
    arm.tick()
    mine = rig.kv.value(GH)
    assert mine["runner"] == "github" and [d["desk"] for d in mine["desks"]] == ["perps"]
    assert opened(rig, mine["sends"][0])["title"] == "Sem sinal do GitHub"
    arm.off()
    assert rig.kv.value(GH)["active"] is False and rig.kv.value(PC) == pcs
    rig.kv.rows[GH]["value"] = {"active": True, "gen": "github-9", "runner": "github"}   # a dead job's switch
    watch.Arm(rig.relay, "pc").off()
    assert rig.kv.value(GH)["active"] is True


def test_without_a_key_nothing_is_armed(rig, monkeypatch):
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "")
    rig.session()
    watch.Arm(rig.relay, "pc").tick()
    assert PC not in rig.kv.rows


# ---------------------------------------------------------------- GitHub's cycle and Supabase: one telling
@pytest.fixture
def dead(rig, monkeypatch):
    """The PC armed for its memes session and went quiet 10 min ago; GitHub's cycle reads the pulled files."""
    rig.session("memes", positions=2, age=600)
    sent = []
    monkeypatch.setattr(push.Pusher, "refresh", lambda self: setattr(self, "subs", {"k": {}}))
    monkeypatch.setattr(push.Pusher, "send", lambda self, msg: sent.append(msg) or 1)
    rig.kv.rows[PC] = {"value": {"active": True, "gen": "pc-7", "runner": "pc", "desks": [{"desk": "memes", "started": STARTED}]},
                       "updated_at": now_iso(600)}
    rig.sent = sent
    return rig


def fired(dead, **kw):
    dead.kv.rows["watch:fired:pc"] = {"value": {"gen": "pc-7", "by": "supabase", "at": now_iso(400), "tries": 1,
                                                "desks": [{"desk": "memes", "started": STARTED}], **kw}, "updated_at": ""}


def told(dead) -> bool:
    return str((dead.kv.value("push:seen:memes") or {}).get("stall", "")).startswith(STARTED + "@")


def test_supabase_got_it_through_so_github_only_records_it(dead):
    fired(dead, delivered=True)
    assert relay._tell_dead_sessions(dead.relay, {"owner": "pc"}) == []
    assert dead.sent == [] and told(dead)
    assert relay._tell_dead_sessions(dead.relay, {"owner": "pc"}) == [] and dead.sent == []


def test_supabase_still_trying_github_waits_without_recording(dead):
    fired(dead, at=now_iso(60))
    assert relay._tell_dead_sessions(dead.relay, {"owner": "pc"}) == []
    assert dead.sent == [] and not told(dead)


@pytest.mark.parametrize("how", ["failed", "too long"])
def test_supabase_could_not_deliver_so_github_tells_it(dead, how):
    fired(dead, failed=True) if how == "failed" else fired(dead, at=now_iso(relay.WATCH_WAIT_S + 60))
    assert relay._tell_dead_sessions(dead.relay, {"owner": "pc"}) == ["memes"]
    assert len(dead.sent) == 1 and dead.kv.results == []


def test_github_first_tells_it_and_reports_it(dead):
    assert relay._tell_dead_sessions(dead.relay, {"owner": "pc"}) == ["memes"]
    assert len(dead.sent) == 1 and dead.kv.value("watch:fired:pc")["by"] == "github"
    assert dead.kv.value("watch:fired:pc")["delivered"] is True
    assert dead.kv.claims == [{"p_runner": "pc", "p_gen": "pc-7"}]
    assert dead.kv.results == [{"p_runner": "pc", "p_gen": "pc-7", "p_delivered": True}]


def test_github_could_not_send_so_it_hands_the_claim_back(dead, monkeypatch):
    monkeypatch.setattr(push.Pusher, "send", lambda self, msg: 0)
    assert relay._tell_dead_sessions(dead.relay, {"owner": "pc"}) == []
    assert "watch:fired:pc" not in dead.kv.rows and not told(dead)      # Supabase can claim it next minute
    monkeypatch.setattr(push.Pusher, "send", lambda self, msg: dead.sent.append(msg) or 1)
    assert relay._tell_dead_sessions(dead.relay, {"owner": "pc"}) == ["memes"]


def test_a_told_silence_stays_told_when_the_switch_was_turned_off_since(dead):
    fired(dead, delivered=True)
    dead.kv.rows[PC]["value"] = {"active": False, "runner": "pc"}
    assert relay._tell_dead_sessions(dead.relay, {"owner": "pc"}) == [] and told(dead)


def test_a_silence_after_the_one_supabase_told_is_githubs_to_tell(dead):
    fired(dead, delivered=True, at=now_iso(900))   # told before the session last wrote: an older silence
    dead.kv.rows[PC]["value"] = {"active": False, "runner": "pc"}
    assert relay._tell_dead_sessions(dead.relay, {"owner": "pc"}) == ["memes"]


def test_a_session_no_switch_names_is_githubs_to_tell(dead):
    dead.kv.rows[PC]["value"]["desks"] = [{"desk": "memes", "started": "an older session"}]
    fired(dead, delivered=True, desks=[{"desk": "memes", "started": "an older session"}])
    assert relay._tell_dead_sessions(dead.relay, {"owner": "pc"}) == ["memes"]
    assert dead.kv.claims == []


def test_a_failed_claim_leaves_it_to_github(dead, monkeypatch):
    real = dead.kv.rpc

    def broken(name, body):
        if name == "runner_watch_claim":
            raise RuntimeError("runner_watch_claim: 404")
        return real(name, body)

    monkeypatch.setattr(dead.relay, "rpc", broken)
    assert relay._tell_dead_sessions(dead.relay, {"owner": "pc"}) == ["memes"]


def test_after_a_wake_the_pc_does_not_tell_the_computers_own_silence_again(rig, monkeypatch):
    rig.session("memes", positions=2)
    sent = []
    monkeypatch.setattr(push.Pusher, "refresh", lambda self: setattr(self, "subs", {"k": {}}))
    monkeypatch.setattr(push.Pusher, "send", lambda self, msg: sent.append(msg) or 1)
    p = push.Pusher(rig.relay)
    clock = {"t": time.time()}
    monkeypatch.setattr(push.time, "time", lambda: clock["t"])
    p.tick()
    clock["t"] += 600                            # the lid was closed for 10 minutes; the session slept too
    rig.session("memes", positions=2, age=600)
    for _ in range(6):                           # 30 s after the wake the session has still not written
        clock["t"] += 5
        p.tick()
    assert not [m for m in sent if "parou de responder" in m["title"]]
    for _ in range(push.STALL_S // 5):           # still silent 3 minutes after the wake: that is news
        clock["t"] += 5
        p.tick()
    assert [m for m in sent if "parou de responder" in m["title"]]


def test_the_sql_and_the_python_agree():
    sql = (relay.ROOT / "deploy" / "supabase_watch.sql").read_text(encoding="utf-8")
    assert f"interval '{watch.QUIET_S} seconds'" in sql and watch.QUIET_S == push.STALL_S
    assert "'watch:arm:' ||" in sql and "'watch:fired:' ||" in sql
    assert watch.KEY == "watch:arm:" and watch.FIRED == "watch:fired:"