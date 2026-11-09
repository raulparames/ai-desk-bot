"""One call shape for every agent: system + prompt + JSON schema -> dict (or None on failure).

Backends:
  claude-cli  uses your Claude Code login (no API key). Runs `claude -p` with plugins, MCP
              servers, skills and settings switched off so each call stays small.
  anthropic   uses the Anthropic SDK with ANTHROPIC_API_KEY (or an `ant auth login` profile).
  none        no AI: the desk falls back to the hard rules only.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .config import AIConfig, env

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def _find_claude() -> str:
    override = env("CLAUDE_BIN")
    if override:
        return override
    found = shutil.which("claude")
    if not found:
        # Shells that don't load the npm or native-installer folders onto PATH (e.g. some PowerShell setups)
        home = Path.home()
        for candidate in (
            Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "npm" / "node_modules"
            / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
            home / ".local" / "bin" / "claude.exe",
            home / ".local" / "bin" / "claude",
        ):
            if candidate.exists():
                return str(candidate)
        raise LLMError("claude CLI not found on PATH; set CLAUDE_BIN or use backend 'anthropic'/'none'")
    # The npm shim is a .cmd file; call the real executable so arguments are not re-parsed by cmd.exe
    if found.lower().endswith((".cmd", ".ps1")) or Path(found).suffix == "":
        exe = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)
    return found


class LLM:
    def __init__(self, cfg: AIConfig):
        self.cfg = cfg
        self.backend = cfg.backend
        self.calls = 0
        self.cost_usd = 0.0
        self._client = None
        if self.backend == "claude-cli":
            self._claude = _find_claude()
        elif self.backend == "anthropic":
            import anthropic
            self._client = anthropic.Anthropic()

    @property
    def enabled(self) -> bool:
        return self.backend in ("claude-cli", "anthropic")

    def ask_json(self, system: str, prompt: str, schema: dict, effort: str | None = None,
                 web_search: bool = False) -> dict | None:
        if not self.enabled:
            return None
        effort = effort or self.cfg.effort
        try:
            if self.backend == "claude-cli":
                return self._ask_cli(system, prompt, schema, effort, web_search)
            return self._ask_api(system, prompt, schema, effort, web_search)
        except LLMError as exc:
            log.warning("LLM call failed: %s", exc)
            return None

    def _ask_cli(self, system, prompt, schema, effort, web_search) -> dict:
        cmd = [self._claude, "-p", "--output-format", "json",
               "--json-schema", json.dumps(schema),
               "--tools", "WebSearch" if web_search else "",
               "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
               "--no-session-persistence", "--model", self.cfg.model, "--effort", effort,
               "--system-prompt", system]
        if web_search:
            cmd += ["--allowedTools", "WebSearch"]
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  encoding="utf-8", timeout=self.cfg.timeout_s,
                                  # no console window flashing up when run from Task Scheduler
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude CLI timed out after {self.cfg.timeout_s}s") from exc
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"claude CLI returned non-JSON (exit {proc.returncode}): "
                           f"{(proc.stderr or proc.stdout)[:300]}") from exc
        self.calls += 1
        self.cost_usd += float(out.get("total_cost_usd") or 0)
        if out.get("is_error") or out.get("structured_output") is None:
            raise LLMError(f"claude CLI error: {str(out.get('result'))[:300]}")
        return out["structured_output"]

    def _ask_api(self, system, prompt, schema, effort, web_search) -> dict:
        import anthropic

        kwargs = dict(
            model=self.cfg.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            # refusal fallback: the API re-runs a declined request on a fallback model
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if web_search:
            kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
        try:
            resp = self._client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise LLMError("rate limited") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("network error reaching the Anthropic API") from exc
        self.calls += 1
        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request")
        if resp.stop_reason == "max_tokens":
            raise LLMError("response hit max_tokens")
        texts = [b.text for b in resp.content if b.type == "text"]
        if not texts:
            raise LLMError("no text block in response")
        try:
            return json.loads(texts[-1])
        except json.JSONDecodeError as exc:
            raise LLMError("response was not valid JSON") from exc
