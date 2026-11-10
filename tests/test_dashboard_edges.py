"""The dashboard's door (deskbot/dashboard.py): what a request is refused for before any of it is read or served.

A Content-Length that is not a size is refused with nothing read (a negative one would read until the peer
closes, past the body cap), and a request that arrives from this PC under a foreign Host is a web page whose DNS
name was pointed at 127.0.0.1: it gets nothing, while every normal way in (127.0.0.1, localhost, no Host, the
HTTPS proxy, the phone on the Wi-Fi) still passes.

Offline: the handler is built without a socket, the app is a stand-in, the chart desk's folder a temp one and its
start a recorder. No analysis is ever started.
"""

import io
import json
import types

import pytest

from deskbot import chartdesk, dashboard, phone

ANALYZE, LOGIN, STATE = "/api/desk/grafico/analyze", "/api/login", "/api/desk/grafico"
BAD_LENGTH = (400, {"error": "pedido inválido"})
REBOUND = (403, {"error": "host not allowed"})


def _no_real_llm():
    raise AssertionError("a test tried to build the real LLM")


@pytest.fixture(autouse=True)
def desk(tmp_path, monkeypatch):
    """The chart desk is a temp folder with an empty memory, and its start only records what reached it."""
    started = []
    monkeypatch.setattr(chartdesk, "OUT", tmp_path / "chartdesk")
    monkeypatch.setattr(chartdesk, "backend_name", lambda: "claude-cli")
    monkeypatch.setattr(chartdesk, "make_llm", _no_real_llm)
    monkeypatch.setattr(chartdesk, "start", lambda b: (started.append(b), (202, {"id": "abc", "status": "analysing"}))[1])
    monkeypatch.setattr(phone, "TOKEN_FILE", tmp_path / "phone_token.txt")
    chartdesk._reset()
    yield started
    chartdesk._reset()
    assert not (tmp_path / "chartdesk").exists()         # nothing here saves a job


class AnyApp:
    """Stands for dashboard.App: the handler only needs its methods to exist."""

    def __getattr__(self, name):
        return lambda *a, **kw: (200, {"called": name})


class Unread:
    def read(self, n=-1):
        raise AssertionError("the body of a refused request was read")


def handler(path: str, headers=None, raw=None, address="127.0.0.1"):
    """A dashboard Handler with no socket: what it answers is kept in .sent, and a body it was not given
    (raw=None) fails the test if it is read."""
    h = dashboard.Handler.__new__(dashboard.Handler)
    h.path, h.client_address = path, (address, 50000)
    h.headers = dict(headers or {})
    h.server = types.SimpleNamespace(server_port=8899)
    h.rfile = io.BytesIO(raw) if raw is not None else Unread()
    h.app, h.sent = AnyApp(), []
    h._json = lambda code, obj: h.sent.append((code, obj))
    h._send = lambda code, body, ctype: h.sent.append((code, ctype))
    return h


def post(path: str, headers=None, raw=None, address="127.0.0.1"):
    h = handler(path, headers, raw, address)
    h.do_POST()
    return h.sent


def get(path: str, headers=None, address="127.0.0.1"):
    h = handler(path, headers, None, address)
    h.do_GET()
    return h.sent


# ------------------------------------------------------------ Content-Length
@pytest.mark.parametrize("path", [ANALYZE, LOGIN])
@pytest.mark.parametrize("length", ["-1", "-2000000", "abc", "1e3", "12 34", "0x10"])
def test_a_content_length_that_is_not_a_size_is_refused_with_nothing_read(desk, path, length):
    assert post(path, {"Content-Length": length}) == [BAD_LENGTH]        # Unread: reading the body fails the test
    assert desk == []


def test_a_body_over_the_cap_is_still_refused_before_it_is_read(desk):
    too_large = (413, {"error": "pedido demasiado grande"})
    assert post(ANALYZE, {"Content-Length": "2000001"}) == [too_large]
    assert post(LOGIN, {"Content-Length": "65537"}) == [too_large]
    assert post("/api/controls", {"Content-Length": str(10 ** 30)}) == [too_large]
    assert desk == []


def test_a_proper_length_and_no_length_at_all_still_get_through(desk):
    raw = json.dumps({"shots": [{"tf": "5m", "image": "AAAA"}]}).encode()
    assert post(ANALYZE, {"Content-Length": str(len(raw))}, raw) == [(202, {"id": "abc", "status": "analysing"})]
    assert desk == [{"shots": [{"tf": "5m", "image": "AAAA"}]}]          # the recorder, never the real desk
    assert post(LOGIN, {"Content-Length": "2"}, b"{}") == [(200, {"called": "login"})]
    assert post("/api/controls", {}, b"") == [(200, {"called": "set_controls"})]             # no header: an empty body
    assert post("/api/controls", {"Content-Length": ""}, b"") == [(200, {"called": "set_controls"})]
    assert post("/api/controls", {"Content-Length": "0"}, b"") == [(200, {"called": "set_controls"})]


def test_the_checks_before_the_length_keep_their_own_answers(desk):
    """Not found, no phone key and a foreign Origin are told as before, whatever the length says."""
    assert post("/api/nothing", {"Content-Length": "-1"}) == [(404, {"error": "not found"})]
    assert post(ANALYZE, {"Content-Length": "-1"}, address="192.168.1.50") == [(401, {"error": "falta a chave do telemóvel"})]
    assert post(ANALYZE, {"Content-Length": "abc", "Origin": "https://evil.example"}) == [(403, {"error": "origin not allowed"})]
    assert desk == []


# ------------------------------------------------------------ a foreign Host on a request from this PC
@pytest.mark.parametrize("host", ["attacker.example:8899", "attacker.example", "ATTACKER.example:8899",
                                  "127.0.0.1.attacker.example:8899", "localhost.attacker.example:8899",
                                  "192.168.1.20:8899"])
def test_a_request_from_this_pc_under_a_foreign_host_gets_nothing(desk, host):
    assert get(STATE, {"Host": host}) == [REBOUND]
    assert get("/api/overview", {"Host": host}) == [REBOUND]
    assert get("/api/phone", {"Host": host}) == [REBOUND]                # the secret link and its QR code
    assert get("/", {"Host": host}) == [REBOUND]                         # nor the pages
    raw = json.dumps({"shots": []}).encode()
    assert post(ANALYZE, {"Host": host, "Content-Length": str(len(raw))}) == [REBOUND]       # and nothing is read
    assert post("/api/controls", {"Host": host, "Content-Length": "2"}) == [REBOUND]
    assert post("/api/phone/rotate", {"Host": host, "Content-Length": "2"}) == [REBOUND]
    assert post(LOGIN, {"Host": host, "Content-Length": "-1"}) == [REBOUND]
    assert desk == []


@pytest.mark.parametrize("host", ["127.0.0.1:8899", "localhost:8899", "LOCALHOST:8899", "127.0.0.1", "localhost",
                                  "[::1]:8899", "", None])
def test_this_pc_under_its_own_names_or_no_host_still_passes(desk, host):
    headers = {} if host is None else {"Host": host}
    (code, payload), = get(STATE, headers)
    assert code == 200 and payload["job"] is None and payload["history"] == [] and payload["ai"]["enabled"]
    assert get("/", headers) == [(200, "text/html; charset=utf-8")]
    assert get("/api/phone", headers)[0][0] == 200                       # the secret link: only on this PC, and it is
    raw = json.dumps({"shots": [{"tf": "5m", "image": "AAAA"}]}).encode()
    assert post(ANALYZE, {**headers, "Content-Length": str(len(raw))}, raw) == [(202, {"id": "abc", "status": "analysing"})]
    assert post(LOGIN, {**headers, "Content-Length": "2"}, b"{}") == [(200, {"called": "login"})]
    assert len(desk) == 1
    h = handler(STATE, headers)
    assert not h._rebound() and h._local() and h._authed()


def test_the_https_proxy_and_the_phone_on_the_wifi_are_not_taken_for_a_rebound_page(desk):
    """Neither is a request of this PC's own: the Host is whatever name they came by, and the phone key decides."""
    tok = phone.token()
    need_key = [(401, {"error": "falta a chave do telemóvel"})]
    proxy = {"Host": "desk.example.com", "X-Forwarded-For": "203.0.113.9"}       # arrives from 127.0.0.1, via Caddy
    wifi = {"Host": "192.168.1.20:8899"}
    for headers, address in ((proxy, "127.0.0.1"), (wifi, "192.168.1.50")):
        h = handler(STATE, headers, address=address)
        assert not h._rebound() and not h._local()
        assert get(STATE, headers, address) == need_key
        (code, payload), = get(STATE, {**headers, "X-Desk-Token": tok}, address)
        assert code == 200 and payload["history"] == []
        assert get("/", headers, address) == [(200, "text/html; charset=utf-8")]
        assert get("/api/phone", {**headers, "X-Desk-Token": tok}, address) == [(403, {"error": "só no PC"})]
        assert post(ANALYZE, {**headers, "Content-Length": "2"}, address=address) == need_key
        sent = post("/api/controls", {**headers, "X-Desk-Token": tok, "Content-Length": "2",
                                      "Origin": f"https://{headers['Host']}"}, b"{}", address)
        assert sent == [(200, {"called": "set_controls"})]
    assert desk == []
