"""Phone notifications (push.py): nothing is lost when a send fails, alarms reach the phone, a dead session is noticed.

Offline: the push library is replaced by a recorder and the relay by a dictionary.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
import pywebpush

from deskbot import push

SUB = {"endpoint": "https://web.push.apple.com/a", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}
T = "2026-11-09T10:%02d:%02d+00:00"


class FakeRelay:
    enabled = True

    def __init__(self, subs=1):
        self.kv = {f"push:sub:{i}": {"value": dict(SUB)} for i in range(subs)}

    def get(self, prefix):
        return {k: v for k, v in self.kv.items() if k.startswith(prefix)}

    def push(self, items):
        self.kv.update({k: {"value": v} for k, v in items.items()})


def buy(m, s, coin, **kw):
    return {"ts": T % (m, s), "agent": "EXECUTOR", "coin": coin, "verdict": "buy", "eur": 10, "score": 70, **kw}


def alarm(m, s, text, coin="—"):
    return {"ts": T % (m, s), "agent": "RISK", "coin": coin, "verdict": "alert", "reason": text}


class Rig:
    """One pusher looking at one memes state file and one futures state file."""

    def __init__(self, tmp_path, monkeypatch, subs=1):
        self.sent, self.fail = [], None
        self.files = {"memes": tmp_path / "memes.json", "perps": tmp_path / "perps.json"}
        monkeypatch.setattr(push, "DESKS", dict(self.files))
        monkeypatch.setattr(pywebpush, "webpush", self._webpush)
        monkeypatch.delenv("PUSH_CONTACT", raising=False)
        self.relay = FakeRelay(subs)
        self.p = self.pusher()

    def _webpush(self, **kw):
        if self.fail:
            raise self.fail
        self.sent.append(json.loads(kw["data"]))

    def pusher(self):
        p = push.Pusher(self.relay)
        p.key = "test"
        return p

    def write(self, desk, events, age=0, **kw):
        ends = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
        self.files[desk].write_text(json.dumps({"events": events, "started": "s1", "ends": ends, **kw}), encoding="utf-8")
        if age:
            os.utime(self.files[desk], (time.time() - age,) * 2)

    def tick(self, p=None):
        p = p or self.p
        p._tick_at = time.time() - 5         # the runner itself ticked five seconds ago
        p.tick()
        return [m["title"] for m in self.sent]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_the_sender_identity_is_one_the_push_library_accepts(monkeypatch):
    from py_vapid import _check_sub
    monkeypatch.delenv("PUSH_CONTACT", raising=False)
    assert push.contact() == "https://raulparames.github.io" and _check_sub(push.contact())
    assert not _check_sub("https://raulparames.github.io/ai-desk-bot/")      # what silenced every notification before
    monkeypatch.setenv("PUSH_CONTACT", "someone@example.com")
    assert push.contact() == "mailto:someone@example.com" and _check_sub(push.contact())
    monkeypatch.setenv("PUSH_CONTACT", "mailto:someone@example.com")
    assert push.contact() == "mailto:someone@example.com"
    monkeypatch.setenv("PUSH_CONTACT", "not an address")
    assert push.contact() == "https://raulparames.github.io"


def test_texts_say_what_the_desks_do_now():
    b = push.message("perps", buy(0, 0, "SOL", side="long", lev=5, eur=20, risk_eur=1.02), "INVO")
    assert b["title"] == "Abriu long SOL 5x · margem 20,00 €"
    assert b["body"] == "Arrisca 1,02 € no stop · nota 70 · na INVO (papel)"
    m = push.message("memes", buy(0, 0, "$A"), "FOMO")
    assert m["body"] == "nota 70 · na FOMO (papel)"
    trail = push.message("memes", {"verdict": "sell", "kind": "trail", "coin": "$A", "pnl_eur": 3, "mult": 1.3, "ts": "t"}, "FOMO")
    assert trail["title"] == "Fechou $A · +3,00 € (x1,30)" and trail["body"] == "Trailing do máximo."
    liq = push.message("memes", {"verdict": "sell", "kind": "liq", "coin": "$A", "pnl_eur": -2, "ts": "t"}, "FOMO")
    assert liq["title"] == "Saiu de $A · −2,00 €" and "liquidez" in liq["body"]
    gone = push.message("perps", {"verdict": "sell", "kind": "liq", "coin": "SOL", "pnl_eur": -20, "ts": "t"}, "INVO")
    assert gone["title"] == "Liquidada: SOL · −20,00 €"
    for msg in (b, m, trail, liq, gone):
        assert "40%" not in msg["body"]
    a = push.message("perps", alarm(1, 0, "sem preços há 45 s: posições sem vigia"), "INVO")
    assert a == {"title": "Alerta · Futuros", "body": "Sem preços há 45 s: posições sem vigia", "url": "perps.html", "tag": a["tag"]}
    c = push.message("memes", alarm(1, 0, "liquidez caiu 60%", "$A"), "FOMO")
    assert c["title"] == "Alerta · Memecoins · $A" and c["url"] == "memes.html#/moeda/%24A"
    assert a["tag"] != push.message("perps", alarm(1, 0, "outro alarme"), "INVO")["tag"]      # two alarms in one second stay two
    assert push.message("memes", {"verdict": "wait", "coin": "$A", "ts": "t"}, "FOMO") is None


def test_a_send_that_reached_nobody_is_tried_again_and_given_up_after_three(rig):
    rig.write("memes", [])
    assert rig.tick() == []
    rig.write("memes", [buy(1, 0, "$A")])
    rig.fail = ValueError("Missing 'sub' from claims")       # any error at all, even one the library does not name
    assert rig.tick() == [] and rig.p.seen["memes"][0] == ""
    rig.fail = None
    assert rig.tick() == ["Comprou $A · 10,00 €"]            # it was not lost
    assert rig.tick() == ["Comprou $A · 10,00 €"]            # and not sent twice
    rig.write("memes", [buy(1, 0, "$A"), buy(2, 0, "$B")])
    rig.fail = ValueError("still broken")
    for _ in range(3):
        rig.tick()
    assert rig.p.seen["memes"] == (T % (2, 0), 0)            # three ticks, then it lets go
    rig.fail = None
    rig.write("memes", [buy(1, 0, "$A"), buy(2, 0, "$B"), buy(3, 0, "$C")])
    assert rig.tick() == ["Comprou $A · 10,00 €", "Comprou $C · 10,00 €"]


def test_one_phone_failing_does_not_hold_back_the_others(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, subs=2)
    rig.write("memes", [])
    rig.tick()
    calls = []

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("this phone only")
        rig.sent.append(json.loads(kw["data"]))
    monkeypatch.setattr(pywebpush, "webpush", flaky)
    rig.write("memes", [buy(1, 0, "$A")])
    assert rig.tick() == ["Comprou $A · 10,00 €"] and len(calls) == 2
    assert rig.tick() == ["Comprou $A · 10,00 €"]            # one phone got it: that is delivered


def test_with_no_phone_subscribed_nothing_piles_up(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, subs=0)
    rig.write("memes", [])
    rig.tick()
    rig.write("memes", [buy(1, 0, "$A")])
    rig.tick()
    assert rig.p.seen["memes"] == (T % (1, 0), 0) and rig.sent == []


def test_two_events_in_the_same_second_are_both_announced(rig):
    rig.write("perps", [buy(1, 0, "AAA", side="long", lev=5)])
    assert rig.tick() == []
    rig.write("perps", [buy(1, 0, "AAA", side="long", lev=5), buy(1, 0, "BBB", side="short", lev=5)])
    assert rig.tick() == ["Abriu short BBB 5x · margem 10,00 €"]          # same second as one already seen
    rig.write("perps", [buy(1, 0, "AAA", side="long", lev=5), buy(1, 0, "BBB", side="short", lev=5),
                        alarm(2, 0, "x"), alarm(2, 0, "y")])
    assert rig.tick()[1:] == ["Alerta · Futuros", "Alerta · Futuros"]
    assert rig.relay.kv["push:seen:perps"]["value"]["ts"] == T % (2, 0) and rig.relay.kv["push:seen:perps"]["value"]["n"] == 1
    other = rig.pusher()                                     # the other runner picks up exactly there
    other.seen = {"perps": ("", 0)}
    assert len(rig.tick(other)) == 3


def test_a_record_left_by_the_old_code_means_that_whole_second_was_told(rig):
    rig.write("memes", [])
    rig.tick()
    rig.relay.kv["push:seen:memes"] = {"value": {"ts": T % (1, 0), "end": ""}}
    rig.write("memes", [buy(1, 0, "$A"), buy(1, 0, "$B"), buy(1, 1, "$C")])
    assert rig.tick() == ["Comprou $C · 10,00 €"]


def test_alarms_reach_the_phone_and_are_never_dropped_by_a_burst(rig):
    rig.write("perps", [])
    rig.tick()
    events = [buy(1, i, f"C{i}", side="long", lev=5) for i in range(6)]
    events.insert(1, alarm(1, 0, "perda do dia de 6%: entradas paradas até amanhã"))
    events.append(alarm(1, 9, "sem preços há 45 s"))
    titles = rig.tick(rig.write("perps", events))
    assert [t for t in titles if t.startswith("Alerta")] == ["Alerta · Futuros"] * 2
    assert [t.split()[2] for t in titles if t.startswith("Abriu")] == ["C2", "C3", "C4", "C5"]     # the latest four trades
    assert rig.sent[0]["body"].startswith("Perda do dia de 6%")
    assert rig.tick() == titles


def test_a_session_that_stops_answering_is_announced_once_and_again_if_it_recurs(rig):
    pos = [{"coin": "SOL"}]
    rig.write("perps", [], positions=pos)
    rig.tick()
    rig.write("perps", [], age=400, positions=pos)
    assert rig.tick() == []                                  # once could be a slow disk
    titles = rig.tick()
    assert titles == ["Sessão de futuros parou de responder"]
    assert "1 posição aberta fica sem stop" in rig.sent[0]["body"] and "6 min" in rig.sent[0]["body"]
    assert rig.tick() == titles and rig.tick() == titles     # said once
    other = rig.pusher()
    other.seen, other.stalled = {"perps": ("", 0)}, {"perps": ""}
    rig.tick(other)
    assert rig.tick(other) == titles                         # the other runner knows it was said
    rig.write("perps", [], positions=pos)                    # it came back
    assert rig.tick() == titles
    rig.write("perps", [], age=500)                          # and stopped again, at another moment
    rig.tick()
    assert rig.tick() == titles * 2 and "Sem posições abertas" in rig.sent[1]["body"]


def test_no_false_alarm_for_a_finished_session_an_overdue_one_or_a_computer_that_slept(rig):
    rig.write("memes", [])
    rig.tick()
    rig.write("memes", [], age=400, finished=True)
    rig.tick()
    assert all("parou" not in t for t in rig.tick())
    rig.files["memes"].write_text(json.dumps({"events": [], "started": "s2", "ends": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
    os.utime(rig.files["memes"], (time.time() - 400,) * 2)
    rig.tick()
    assert all("parou" not in t for t in rig.tick())
    rig.write("memes", [], age=400, started="s3")
    for _ in range(3):                                       # this runner was itself away: the file is old for that reason
        rig.p._tick_at = time.time() - 600
        rig.p.tick()
    assert all("parou" not in m["title"] for m in rig.sent)


def test_a_session_already_silent_at_the_first_look_is_still_announced_once(rig):
    rig.write("perps", [], age=400, positions=[{"coin": "SOL"}])     # the runner starts and finds it already dead
    assert rig.tick() == [] and rig.tick() == []             # the first look, then once could be a slow disk
    titles = rig.tick()
    assert titles == ["Sessão de futuros parou de responder"] and "1 posição aberta fica sem stop" in rig.sent[0]["body"]
    assert rig.tick() == titles and rig.tick() == titles     # said once
    other = rig.pusher()                                     # another runner starts later: the relay says it was told
    for _ in range(4):
        assert rig.tick(other) == titles


def test_trouble_in_one_desk_does_not_silence_the_other(rig):
    rig.write("memes", [])
    rig.write("perps", [])
    rig.tick()
    rig.files["memes"].write_text(json.dumps({"events": "not a list", "started": "s1"}), encoding="utf-8")
    rig.write("perps", [buy(1, 0, "SOL", side="long", lev=5)])
    assert rig.tick() == ["Abriu long SOL 5x · margem 10,00 €"]


def test_the_end_of_a_session_waits_for_a_working_send(rig):
    rig.write("memes", [])
    rig.tick()
    rig.fail = ValueError("down")
    rig.write("memes", [], finished=True, start_eur=100, equity_eur=97.5, trades=[{}])
    assert rig.tick() == [] and rig.p.ended["memes"] == ""
    rig.fail = None
    assert rig.tick() == ["Sessão de memes terminou · 97,50 €"]
    assert rig.sent[0]["body"] == "Começou com 100,00 €: 0,97×, 1 trades. Papel."
    assert rig.tick() == ["Sessão de memes terminou · 97,50 €"]


def test_the_end_of_a_session_counts_a_half_and_its_rest_as_one_trade(rig):
    rig.write("perps", [])
    rig.tick()
    sales = [{"id": "a", "kind": "half"}, {"id": "a", "kind": "trail"}, {"id": "b", "kind": "stop"}]
    rig.write("perps", [], finished=True, start_eur=100, equity_eur=103, trades=sales)
    assert rig.tick() == ["Sessão de futuros terminou · 103,00 €"]
    assert rig.sent[0]["body"] == "Começou com 100,00 €: 1,03×, 2 trades. Papel."      # it said 3
