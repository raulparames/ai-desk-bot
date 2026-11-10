"""One call shape for every agent: system + prompt + JSON schema -> dict (or None on failure).

ask_json_images is the same call with screenshots in front of the prompt (the "Gráfico" desk).

Backends:
  claude-cli  uses your Claude Code login (no API key). Runs `claude -p` with plugins, MCP
              servers, skills and settings switched off so each call stays small.
  anthropic   uses the Anthropic SDK with ANTHROPIC_API_KEY (or an `ant auth login` profile).
  none        no AI: the desk falls back to the hard rules only.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .config import AIConfig, env

log = logging.getLogger(__name__)


IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")


class LLMError(RuntimeError):
    """str() stays English for the log; `pt` is the wording a person may be shown in the app."""

    def __init__(self, msg: str, pt: str = ""):
        super().__init__(msg)
        self.pt = pt or msg


def _image_content(images, prompt: str) -> list[dict]:
    """The user turn for both backends: every screenshot first, in the order given, then the text."""
    if not images:
        raise LLMError("no images given", pt="Falta pelo menos uma imagem do gráfico")
    blocks = []
    for item in images:
        try:
            raw, media_type = item
        except (TypeError, ValueError) as exc:
            raise LLMError("image must be a (bytes, media_type) pair", pt="Imagem inválida") from exc
        if media_type not in IMAGE_TYPES:
            # refused here so nothing unexpected is ever sent to the model
            raise LLMError(f"unsupported image type {str(media_type)[:40]!r}",
                           pt="Formato de imagem não suportado: usa PNG, JPEG ou WebP")
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            raise LLMError("empty image", pt="Uma das imagens está vazia")
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                   "data": base64.b64encode(raw).decode("ascii")}})
    return blocks + [{"type": "text", "text": prompt}]


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
        self.last_error = ""    # why the last ask_json_images returned None (pt-PT), "" after a success
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

    def ask_json_images(self, system: str, prompt: str, schema: dict, images: list[tuple[bytes, str]],
                        effort: str | None = None) -> dict | None:
        """ask_json with screenshots in front of the prompt. images = [(raw_bytes, media_type)].

        The model gets no tools on either backend: it only looks and answers. Never raises, because the
        caller is a background job: any failure returns None and leaves the reason in self.last_error.
        The images themselves never reach a log line or an error message.
        """
        self.last_error = ""
        if not self.enabled:
            self.last_error = "A IA está desligada"
            return None
        try:
            content = _image_content(images, prompt)
            effort = effort or self.cfg.effort
            if self.backend == "claude-cli":
                out = self._ask_cli_images(system, content, schema, effort)
            else:
                out = self._ask_api(system, content, schema, effort, False)
            if not isinstance(out, dict):
                raise LLMError("answer was not a JSON object", pt="A resposta da IA veio num formato inesperado")
            return out
        except LLMError as exc:
            log.warning("LLM image call failed: %s", exc)
            self.last_error = exc.pt
        except Exception as exc:  # noqa: BLE001 - a crash here would leave the job "a analisar" for ever
            log.warning("LLM image call crashed: %s: %s", type(exc).__name__, str(exc)[:300])
            self.last_error = "Falha inesperada ao falar com a IA"
        return None

    def _ask_cli_images(self, system, content, schema, effort) -> dict:
        """`claude -p` only takes an image through stream-json on stdin; the other way would be handing the
        model a file-reading tool, and this call must stay tool-less."""
        cmd = [self._claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--json-schema", json.dumps(schema), "--tools", "",
               "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
               "--no-session-persistence", "--model", self.cfg.model, "--effort", effort,
               "--system-prompt", system]
        line = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
        try:
            proc = subprocess.run(cmd, input=line, capture_output=True, text=True,
                                  encoding="utf-8", timeout=self.cfg.timeout_s,
                                  # no console window flashing up when run from Task Scheduler
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude CLI timed out after {self.cfg.timeout_s}s",
                           pt=f"O Claude não respondeu em {self.cfg.timeout_s} s") from exc
        except OSError as exc:
            raise LLMError(f"claude CLI could not start: {exc}", pt="Não consegui arrancar o Claude neste PC") from exc
        out = None
        # split on "\n" only: splitlines() would also cut a JSON line at a U+2028 inside the model's text
        for raw in (proc.stdout or "").split("\n"):
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                out = event     # the last one wins
        if out is None:
            # stderr only: stdout is the event stream and is not worth the risk of quoting
            raise LLMError(f"claude CLI gave no result line (exit {proc.returncode}): {(proc.stderr or '')[:300]}",
                           pt="O Claude não devolveu resposta")
        self.calls += 1
        self.cost_usd += float(out.get("total_cost_usd") or 0)
        why = str(out.get("result"))[:300]
        if out.get("is_error"):
            raise LLMError(f"claude CLI error: {why}", pt=f"O Claude devolveu um erro: {why[:160]}")
        if not isinstance(out.get("structured_output"), dict):
            raise LLMError(f"claude CLI error: {why}", pt="O Claude não devolveu a análise no formato pedido")
        return out["structured_output"]

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
        """`prompt` is the user turn: plain text from ask_json, a list of content blocks from ask_json_images."""
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
            raise LLMError("rate limited", pt="Limite de pedidos à IA atingido; tenta daqui a pouco") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}: {exc.message}",
                           pt=f"Erro da API da Anthropic ({exc.status_code})") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("network error reaching the Anthropic API",
                           pt="Sem ligação à API da Anthropic") from exc
        self.calls += 1
        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request", pt="A IA recusou o pedido")
        if resp.stop_reason == "max_tokens":
            raise LLMError("response hit max_tokens", pt="A resposta da IA ficou cortada a meio")
        texts = [b.text for b in resp.content if b.type == "text"]
        if not texts:
            raise LLMError("no text block in response", pt="A IA não devolveu texto")
        try:
            return json.loads(texts[-1])
        except json.JSONDecodeError as exc:
            raise LLMError("response was not valid JSON", pt="A resposta da IA não veio em JSON válido") from exc
