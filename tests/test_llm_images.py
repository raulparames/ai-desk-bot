"""Screenshots to the model (llm.ask_json_images): the pictures go in first, the model gets no tools, and every
failure comes back as None with a reason a person can read.

Offline: subprocess.run and the Anthropic client are replaced by recorders. The real Claude is never called.
"""

import base64
import json
import logging
import subprocess
from types import SimpleNamespace

import anthropic
import pytest

from deskbot import llm as llm_mod
from deskbot.config import AIConfig
from deskbot.llm import LLM

SYSTEM = "És um trader discricionário."
PROMPT = "Lê estes gráficos."
SCHEMA = {"type": "object", "properties": {"legivel": {"type": "boolean"}}, "required": ["legivel"]}
PNG = b"\x89PNG\r\n\x1a\n" + b"one" * 40
JPG = b"\xff\xd8\xff\xe0" + b"two" * 40
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"three" * 40
SHOTS = [(PNG, "image/png"), (JPG, "image/jpeg"), (WEBP, "image/webp")]
ANSWER = {"legivel": True}
UNEXPECTED = "Falha inesperada ao falar com a IA"


def stream(*events):
    """What `claude -p --output-format stream-json` prints: one JSON event per line (strings go in as they are)."""
    return "\n".join(e if isinstance(e, str) else json.dumps(e) for e in events) + "\n"


def result(**kw):
    return {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
            "structured_output": ANSWER, "total_cost_usd": 0.0, **kw}


GOOD = stream({"type": "system", "subtype": "init", "tools": ["StructuredOutput"]},
              {"type": "assistant", "message": {"content": []}},
              result(total_cost_usd=0.0421))


class FakeRun:
    """Stands in for subprocess.run and keeps what it was called with."""

    def __init__(self, stdout="", stderr="", returncode=0, raises=None):
        self.stdout, self.stderr, self.returncode, self.raises = stdout, stderr, returncode, raises
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.raises:
            raise self.raises
        return SimpleNamespace(stdout=self.stdout, stderr=self.stderr, returncode=self.returncode)

    @property
    def cmd(self):
        return self.calls[-1][0]

    @property
    def kw(self):
        return self.calls[-1][1]


def cli(monkeypatch, run, **cfg):
    monkeypatch.setattr(llm_mod, "_find_claude", lambda: "claude-test.exe")
    monkeypatch.setattr(subprocess, "run", run)
    return LLM(AIConfig(backend="claude-cli", model="claude-test-model", effort="low", timeout_s=77, **cfg))


class FakeMessages:
    def __init__(self, reply=None, raises=None):
        self.reply, self.raises, self.calls = reply, raises, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.reply


def reply(text=json.dumps(ANSWER), stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="thinking", thinking="…"),
                                                             SimpleNamespace(type="text", text=text)])


def api(monkeypatch, messages):
    """An LLM on the anthropic backend whose client is a recorder; any subprocess is a test failure."""
    monkeypatch.setattr(anthropic, "Anthropic", lambda: SimpleNamespace(beta=SimpleNamespace(messages=messages)))
    monkeypatch.setattr(subprocess, "run", forbidden)
    return LLM(AIConfig(backend="anthropic", model="claude-test-model", effort="low"))


def forbidden(*a, **kw):
    raise AssertionError("nothing may be called here")


def decoded(blocks):
    return [(base64.b64decode(b["source"]["data"]), b["source"]["media_type"]) for b in blocks]


# ---------------------------------------------------------------- claude-cli

def test_the_cli_call_is_stream_json_with_no_tools_at_all(monkeypatch):
    run = FakeRun(GOOD)
    bot = cli(monkeypatch, run)
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) == ANSWER
    assert run.cmd == ["claude-test.exe", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                       "--verbose", "--json-schema", json.dumps(SCHEMA), "--tools", "",
                       "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
                       "--no-session-persistence", "--model", "claude-test-model", "--effort", "low",
                       "--system-prompt", SYSTEM]
    assert run.cmd[run.cmd.index("--tools") + 1] == ""
    for never in ("Read", "--allowedTools", "--allowed-tools", "--bare", "WebSearch"):
        assert never not in run.cmd
    assert not any(base64.b64encode(PNG).decode() in arg for arg in run.cmd)     # pictures travel on stdin only
    assert run.kw["timeout"] == 77 and run.kw["encoding"] == "utf-8" and run.kw["text"] is True
    assert run.kw["capture_output"] is True
    assert run.kw["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_stdin_is_one_json_line_with_the_pictures_first_and_in_order(monkeypatch):
    run = FakeRun(GOOD)
    cli(monkeypatch, run).ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS)
    sent = run.kw["input"]
    assert sent.endswith("\n") and sent.count("\n") == 1
    msg = json.loads(sent)
    assert msg["type"] == "user" and msg["message"]["role"] == "user"
    content = msg["message"]["content"]
    assert [b["type"] for b in content] == ["image", "image", "image", "text"]
    assert all(b["source"]["type"] == "base64" for b in content[:3])
    assert decoded(content[:3]) == SHOTS
    assert content[3] == {"type": "text", "text": PROMPT}


def test_one_picture_is_enough(monkeypatch):
    run = FakeRun(GOOD)
    assert cli(monkeypatch, run).ask_json_images(SYSTEM, PROMPT, SCHEMA, [(JPG, "image/jpeg")]) == ANSWER
    content = json.loads(run.kw["input"])["message"]["content"]
    assert decoded(content[:-1]) == [(JPG, "image/jpeg")] and content[-1]["type"] == "text"


def test_the_last_result_line_wins_and_lines_that_are_not_json_are_skipped(monkeypatch):
    out = stream("Warning: something chatty", "", result(structured_output={"legivel": False}, total_cost_usd=9),
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": '{"type": "result"}'}]}},
                 "[1, 2, 3]", '"result"', "{broken", result(structured_output={"legivel": True, "n": 2},
                                                             total_cost_usd=0.25))
    bot = cli(monkeypatch, FakeRun(out))
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) == {"legivel": True, "n": 2}
    assert bot.last_error == "" and bot.calls == 1
    assert bot.cost_usd == pytest.approx(0.25)


def test_a_line_separator_inside_the_models_text_does_not_cut_the_answer(monkeypatch):
    """Node prints U+2028 raw inside JSON strings; str.splitlines() would break the line there."""
    line = json.dumps(result(structured_output={"legivel": True, "resumo": "a b\x85c"}), ensure_ascii=False)
    bot = cli(monkeypatch, FakeRun(line + "\r\n"))
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) == {"legivel": True, "resumo": "a b\x85c"}


def test_the_cost_of_each_call_is_added_up(monkeypatch):
    bot = cli(monkeypatch, FakeRun(GOOD))
    bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS)
    bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS)
    assert bot.calls == 2
    assert bot.cost_usd == pytest.approx(0.0842)


def test_effort_falls_back_to_the_configured_one_and_can_be_overridden(monkeypatch):
    run = FakeRun(GOOD)
    bot = cli(monkeypatch, run)
    bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS)
    assert run.cmd[run.cmd.index("--effort") + 1] == "low"
    bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS, effort="high")
    assert run.cmd[run.cmd.index("--effort") + 1] == "high"
    bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS, "medium")
    assert run.cmd[run.cmd.index("--effort") + 1] == "medium"


@pytest.mark.parametrize("stdout", [
    stream(result(is_error=True, result="Credit balance is too low", structured_output=None)),
    stream(result(is_error=True)),                                  # an error flag beats a payload that looks fine
    stream(result(structured_output=None)),
    stream({"type": "result", "is_error": False, "result": "no payload key at all"}),
    stream(result(structured_output="not an object")),
    stream(result(structured_output=[ANSWER])),
    stream({"type": "system", "subtype": "init"}, {"type": "assistant", "message": {"content": []}}),
    "Error: not logged in\n",
    "<html>garbage</html>",
    "",
], ids=["is_error", "is_error_with_payload", "null_output", "missing_output", "string_output", "list_output",
        "no_result_line", "plain_text", "garbage", "empty"])
def test_a_bad_cli_answer_is_none_with_a_reason(monkeypatch, stdout):
    bot = cli(monkeypatch, FakeRun(stdout, stderr="boom", returncode=1))
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) is None
    assert bot.last_error.strip()


def test_the_reason_quotes_what_the_cli_said_went_wrong(monkeypatch):
    out = stream(result(is_error=True, result="Credit balance is too low", structured_output=None,
                        total_cost_usd=0.01))
    bot = cli(monkeypatch, FakeRun(out))
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) is None
    assert "Credit balance is too low" in bot.last_error
    assert bot.cost_usd == pytest.approx(0.01)       # a failed call that was billed still counts


@pytest.mark.parametrize("boom", [
    subprocess.TimeoutExpired(cmd="claude", timeout=77),
    FileNotFoundError("claude-test.exe"),
    RuntimeError("something nobody planned for"),
], ids=["timeout", "cannot_start", "unexpected"])
def test_a_cli_that_hangs_or_cannot_start_is_none_with_a_reason(monkeypatch, boom):
    bot = cli(monkeypatch, FakeRun(raises=boom))
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) is None
    assert bot.last_error.strip()
    assert bot.calls == 0


def test_the_timeout_reason_says_how_long_it_waited(monkeypatch):
    bot = cli(monkeypatch, FakeRun(raises=subprocess.TimeoutExpired(cmd="claude", timeout=77)))
    bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS)
    assert "77" in bot.last_error


def test_a_success_clears_the_reason_left_by_the_failure_before_it(monkeypatch):
    run = FakeRun("garbage")
    bot = cli(monkeypatch, run)
    assert bot.last_error == ""                       # set by __init__, before any call
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) is None
    assert bot.last_error
    run.stdout = GOOD
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) == ANSWER
    assert bot.last_error == ""


@pytest.mark.parametrize("images", [
    [],
    None,
    [(PNG, "image/gif")],
    [(PNG, "image/svg+xml")],
    [(PNG, "application/pdf")],
    [(PNG, "IMAGE/PNG")],
    [(PNG, "")],
    [(PNG, None)],
    [(PNG, "image/png"), (JPG, "image/bmp")],          # one bad picture stops the whole call
    [(b"", "image/png")],
    [("not bytes", "image/png")],
    [PNG],
], ids=["empty_list", "none", "gif", "svg", "pdf", "wrong_case", "blank_type", "no_type", "second_is_bad",
        "empty_bytes", "text_not_bytes", "not_a_pair"])
def test_no_pictures_or_a_type_that_is_not_png_jpeg_webp_is_never_sent(monkeypatch, images):
    run = FakeRun(GOOD)
    bot = cli(monkeypatch, run)
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, images) is None
    assert bot.last_error.strip()
    assert run.calls == []
    assert bot.calls == 0


def test_a_failure_never_puts_the_picture_in_the_log_or_in_the_reason(monkeypatch, caplog):
    b64 = base64.b64encode(PNG).decode()
    caplog.set_level(logging.DEBUG)
    for run in (FakeRun("garbage " + b64, stderr="stderr noise"),
                FakeRun(stream(result(structured_output=None))),
                FakeRun(raises=subprocess.TimeoutExpired(cmd="claude", timeout=77)),
                FakeRun(raises=RuntimeError("odd"))):
        bot = cli(monkeypatch, run)
        assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) is None
        assert b64 not in bot.last_error and b64[:24] not in bot.last_error
    assert caplog.records                               # the failures were logged...
    assert b64[:24] not in caplog.text                  # ...without the pictures


# ---------------------------------------------------------------- anthropic

def test_the_api_call_sends_the_pictures_before_the_text_and_no_tools(monkeypatch):
    messages = FakeMessages(reply())
    bot = api(monkeypatch, messages)
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS, effort="medium") == ANSWER
    assert bot.last_error == "" and bot.calls == 1
    (kw,) = messages.calls
    assert "tools" not in kw
    (turn,) = kw.pop("messages")
    assert turn["role"] == "user"
    assert [b["type"] for b in turn["content"]] == ["image", "image", "image", "text"]
    assert all(b["source"]["type"] == "base64" for b in turn["content"][:3])
    assert decoded(turn["content"][:3]) == SHOTS
    assert turn["content"][3] == {"type": "text", "text": PROMPT}
    assert kw == {"model": "claude-test-model", "max_tokens": 16000, "system": SYSTEM,
                  "thinking": {"type": "adaptive"},
                  "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
                  "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


@pytest.mark.parametrize("answer", [
    reply(stop_reason="refusal"),
    reply(stop_reason="max_tokens"),
    reply(text="not json at all"),
    reply(text="[1, 2]"),
    SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking", thinking="…")]),
], ids=["refusal", "max_tokens", "not_json", "json_but_not_an_object", "no_text_block"])
def test_a_refusal_or_a_cut_or_broken_api_answer_is_none_with_a_reason(monkeypatch, answer):
    bot = api(monkeypatch, FakeMessages(answer))
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) is None
    assert bot.last_error.strip()


def _request():
    """Stand-in for the HTTP request the SDK hangs on its errors: nothing is built that could reach the network."""
    return SimpleNamespace(method="POST", url="https://api.anthropic.com/v1/messages")


def _response(status):
    return SimpleNamespace(status_code=status, headers={}, request=_request())


@pytest.mark.parametrize("make", [
    lambda: anthropic.RateLimitError("slow down", response=_response(429), body=None),
    lambda: anthropic.InternalServerError("overloaded", response=_response(529), body=None),
    lambda: anthropic.BadRequestError("image too large", response=_response(400), body=None),
    lambda: anthropic.APIConnectionError(message="no route", request=_request()),
], ids=["rate_limit", "server_error", "bad_request", "no_network"])
def test_an_api_error_is_none_with_its_own_reason(monkeypatch, make):
    messages = FakeMessages(raises=make())
    bot = api(monkeypatch, messages)
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) is None
    assert bot.last_error.strip() and bot.last_error != UNEXPECTED      # mapped like ask_json maps it, not swallowed
    assert len(messages.calls) == 1 and bot.calls == 0


def test_the_api_reason_carries_the_status_code(monkeypatch):
    boom = anthropic.InternalServerError("overloaded", response=_response(529), body=None)
    bot = api(monkeypatch, FakeMessages(raises=boom))
    bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS)
    assert "529" in bot.last_error


def test_an_error_nobody_planned_for_is_still_none_with_a_reason(monkeypatch):
    bot = api(monkeypatch, FakeMessages(raises=TypeError("create() got an unexpected keyword argument")))
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) is None
    assert bot.last_error == UNEXPECTED


def test_the_api_path_refuses_a_bad_picture_before_calling_anything(monkeypatch):
    messages = FakeMessages(reply())
    bot = api(monkeypatch, messages)
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, [(PNG, "image/gif")]) is None
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, []) is None
    assert bot.last_error.strip() and messages.calls == []


# ---------------------------------------------------------------- no AI

def test_with_the_ai_switched_off_nothing_is_called(monkeypatch):
    monkeypatch.setattr(llm_mod, "_find_claude", forbidden)
    monkeypatch.setattr(anthropic, "Anthropic", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    bot = LLM(AIConfig(backend="none"))
    assert bot.enabled is False
    assert bot.ask_json_images(SYSTEM, PROMPT, SCHEMA, SHOTS) is None
    assert bot.last_error.strip()
    assert bot.calls == 0 and bot.cost_usd == 0


# ---------------------------------------------------------------- the old text-only call is untouched

OLD = json.dumps({"type": "result", "is_error": False, "result": "ok", "structured_output": ANSWER,
                  "total_cost_usd": 0.5})


@pytest.mark.parametrize("web_search", [False, True])
def test_ask_json_still_builds_exactly_the_old_command(monkeypatch, web_search):
    run = FakeRun(OLD)
    bot = cli(monkeypatch, run)
    assert bot.ask_json(SYSTEM, PROMPT, SCHEMA, None, web_search) == ANSWER        # positional, as desk.py calls it
    old = ["claude-test.exe", "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
           "--tools", "WebSearch" if web_search else "",
           "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
           "--no-session-persistence", "--model", "claude-test-model", "--effort", "low",
           "--system-prompt", SYSTEM]
    assert run.cmd == old + (["--allowedTools", "WebSearch"] if web_search else [])
    assert run.kw == {"input": PROMPT, "capture_output": True, "text": True, "encoding": "utf-8", "timeout": 77,
                      "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    assert bot.calls == 1 and bot.cost_usd == pytest.approx(0.5)


def test_ask_json_still_takes_effort_by_position_and_fails_to_none(monkeypatch):
    run = FakeRun(OLD)
    bot = cli(monkeypatch, run)
    bot.ask_json(SYSTEM, PROMPT, SCHEMA, "medium")
    assert run.cmd[run.cmd.index("--effort") + 1] == "medium"
    for bad in (FakeRun("not json", stderr="x", returncode=1),
                FakeRun(json.dumps({"is_error": True, "result": "nope"})),
                FakeRun(json.dumps({"is_error": False, "structured_output": None})),
                FakeRun(raises=subprocess.TimeoutExpired(cmd="claude", timeout=77))):
        monkeypatch.setattr(subprocess, "run", bad)
        assert bot.ask_json(SYSTEM, PROMPT, SCHEMA) is None


def test_ask_json_does_not_read_the_stream_format(monkeypatch):
    """The old call parses stdout as one JSON document; several lines are still a failure there, as before."""
    bot = cli(monkeypatch, FakeRun(GOOD))
    assert bot.ask_json(SYSTEM, PROMPT, SCHEMA) is None


@pytest.mark.parametrize("web_search", [False, True])
def test_ask_json_on_the_api_still_sends_the_prompt_as_plain_text(monkeypatch, web_search):
    messages = FakeMessages(reply())
    bot = api(monkeypatch, messages)
    assert bot.ask_json(SYSTEM, PROMPT, SCHEMA, "high", web_search) == ANSWER
    expected = {"model": "claude-test-model", "max_tokens": 16000, "system": SYSTEM,
                "messages": [{"role": "user", "content": PROMPT}],
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
                "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    if web_search:
        expected["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    assert messages.calls == [expected]


def test_ask_json_on_the_api_still_fails_to_none(monkeypatch):
    for messages in (FakeMessages(reply(stop_reason="refusal")), FakeMessages(reply(text="nope")),
                     FakeMessages(raises=anthropic.APIConnectionError(message="no route", request=_request()))):
        assert api(monkeypatch, messages).ask_json(SYSTEM, PROMPT, SCHEMA) is None
