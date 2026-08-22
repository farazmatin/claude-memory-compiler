"""LLM providers for the speaker resolver and the minutes compiler.

All three providers are **subscription-backed**, not metered API keys. That is a
deliberate constraint and it shapes the design:

- Gemini and Codex are CLI tools, driven here as subprocesses.
- Claude is driven through the Agent SDK.
- None of them can serve LightRAG, which needs an HTTP endpoint. Graph and entity
  extraction therefore runs on local Ollama - see AGENTS.md.

Providers are tried in priority order and fall through on failure, so a quota
limit or a transient CLI error on the preferred provider does not stall the
nightly batch.

Prompts reach the CLIs on **stdin**, never as argv. A one-hour transcript is tens
of thousands of tokens and would risk ARG_MAX as a command-line argument.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pipeline.config import (
    ANTIGRAVITY_BIN,
    ANTIGRAVITY_MODEL,
    GEMINI_MODEL,
    LLM_PROVIDER_ORDER,
    LLM_TIMEOUT_SEC,
    ROOT_DIR,
)


class LLMError(RuntimeError):
    """A model call failed. Callers decide whether that is fatal for a stage."""


class Provider(Protocol):
    name: str

    def available(self) -> bool:
        """Cheap check for whether this provider can run at all."""

    def unavailable_reason(self) -> str:
        """Why `available()` returned False, for the fall-through log line.

        Without this, an unauthenticated CLI and a missing binary both print as
        the same generic "unavailable" and there is nothing to act on. See
        `_gemini_auth_gate` for the case this exists to catch.
        """

    def complete(self, prompt: str) -> str:
        ...


# ── CLI-backed providers ──────────────────────────────────────────────

@dataclass
class CLIProvider:
    """Drives a non-interactive CLI, feeding the prompt on stdin.

    The exact invocation for these tools changes between releases, so both the
    binary and its arguments are overridable by environment variable rather than
    hardcoded - see `.env.example`.
    """

    name: str
    binary: str
    args: list[str]
    # Optional extra gate beyond "is the binary on PATH". Returns a non-empty
    # reason when the CLI cannot possibly succeed right now (e.g. Gemini's is on
    # PATH long before it is authenticated) - see `_gemini_auth_gate`. Checked
    # cheaply, with no subprocess, so `available()` stays a stat() rather than a
    # ~12s doomed call.
    auth_gate: Callable[[], str] | None = None

    def available(self) -> bool:
        if shutil.which(self.binary) is None:
            return False
        return not (self.auth_gate and self.auth_gate())

    def unavailable_reason(self) -> str:
        if shutil.which(self.binary) is None:
            return f"{self.binary} not found on PATH"
        if self.auth_gate:
            reason = self.auth_gate()
            if reason:
                return reason
        return "unavailable"

    def complete(self, prompt: str) -> str:
        resolved = shutil.which(self.binary)
        if not resolved:
            raise LLMError(f"{self.binary} not found on PATH")
        if self.auth_gate:
            reason = self.auth_gate()
            if reason:
                # Fail fast rather than spend ~12s on a call that cannot
                # succeed - see `_gemini_auth_gate`.
                raise LLMError(reason)
        try:
            # `input` implies stdin=PIPE; passing both raises ValueError. Piping
            # stdin also stops a CLI that wants a TTY from blocking on one and
            # hanging the batch, which the timeout then catches.
            use_shell = os.name == "nt" and resolved.lower().endswith((".cmd", ".bat", ".ps1"))
            result = subprocess.run(
                [resolved, *self.args],
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=LLM_TIMEOUT_SEC,
                cwd=str(ROOT_DIR),
                shell=use_shell,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"{self.name} timed out after {LLM_TIMEOUT_SEC}s") from exc
        except OSError as exc:
            raise LLMError(f"{self.name} could not be launched: {exc}") from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:500]
            raise LLMError(f"{self.name} exited {result.returncode}: {detail}")

        output = (result.stdout or "").strip()
        if not output:
            raise LLMError(f"{self.name} returned no output")
        return output


def _gemini_auth_gate() -> str:
    """Non-empty when the Gemini CLI cannot authenticate right now.

    Piped stdin alone already puts the CLI into headless mode, so `-p` is not
    needed to trigger it - and passing `-p -` was actively harmful: the CLI
    merges stdin and the `-p` value as `${stdinData}\\n${input}`, so `-p -`
    appended a stray "-" line to every single prompt sent to Gemini.

    Authentication is the bigger issue. This CLI does not validate credentials
    before running: every headless call while unauthenticated spends ~12s
    hitting an interactive OAuth prompt, which then consumes the piped prompt
    as its own "yes/no" answer and exits 130 - and the chain silently falls
    through to the next provider with nothing but a server-log line to show
    for it. Checking the credential file directly costs one stat(), so both
    the fall-through chain and `pipeline doctor` (via `available()`) can skip
    straight past a CLI that is guaranteed to fail, and say why.
    """
    if os.environ.get("GEMINI_API_KEY"):
        return ""
    if (Path.home() / ".gemini" / "oauth_creds.json").is_file():
        return ""
    return (
        "gemini CLI is not authenticated (no ~/.gemini/oauth_creds.json). "
        "Run `gemini` in a terminal and complete the browser sign-in it opens "
        "- once, interactively, as yourself - or set GEMINI_API_KEY."
    )


def gemini_provider() -> CLIProvider:
    """Gemini CLI. Highest priority: Flash is fast and cheap against quota."""
    args = ["-m", GEMINI_MODEL] if GEMINI_MODEL else []
    binary = os.environ.get("MMC_GEMINI_BIN", "gemini")
    return CLIProvider(
        name="gemini",
        binary=binary,
        args=_split_override("MMC_GEMINI_ARGS", args),
        # The gate reasons about the real CLI's credential files, so it only
        # applies to the real CLI. Anyone who has substituted the binary owns
        # its authentication - the e2e harness points MMC_GEMINI_BIN at a local
        # stub that needs no OAuth at all, and gating that stub on a file in the
        # developer's home directory disabled the only provider those tests
        # configure, failing the minutes stage of all 16 of them.
        auth_gate=_gemini_auth_gate if binary == "gemini" else None,
    )


def codex_provider() -> CLIProvider:
    """Codex CLI, non-interactive exec mode."""
    return CLIProvider(
        name="codex",
        binary=os.environ.get("MMC_CODEX_BIN", "codex"),
        args=_split_override("MMC_CODEX_ARGS", ["exec", "-"]),
    )


@dataclass
class AntigravityProvider:
    """Google Antigravity's `agy` CLI, driven in stream-json mode.

    Kept separate from CLIProvider because neither end is plain text. `agy
    --print` takes the prompt as an ARGUMENT and rejects stdin, which is unusable
    here - a one-hour transcript is far past the ~32 KB Windows command-line
    limit, which is exactly why every other provider is fed on stdin. The
    stream-json input format is the only route that accepts an arbitrarily large
    prompt: one NDJSON message on stdin, and a stream of NDJSON events back whose
    final `result` event carries the answer.

    This is the CLI that actually holds a live Google session on this machine,
    and its model list includes Flash versions the standalone `gemini` CLI has
    never heard of.
    """

    name: str = "antigravity"
    binary: str = ANTIGRAVITY_BIN
    model: str = ANTIGRAVITY_MODEL

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def unavailable_reason(self) -> str:
        if shutil.which(self.binary) is None:
            return (
                f"{self.binary} not found on PATH - install the Antigravity CLI "
                "or set MMC_ANTIGRAVITY_BIN"
            )
        return "unavailable"

    def complete(self, prompt: str) -> str:
        resolved = shutil.which(self.binary)
        if not resolved:
            raise LLMError(self.unavailable_reason())

        # Keyed on "event", not "type". agy's stream-json input is Claude Code's
        # message shape under a different envelope key, and the mismatch fails
        # closed in the worst way: agy prints the complaint to STDOUT and leaves
        # stderr empty, so it surfaces as a bare "exited 1" and the chain quietly
        # falls through to codex at 4x the latency.
        message = json.dumps(
            {
                "event": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            }
        )
        args = [
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--model", self.model,
            # agy's own default is 5m; the batch ceiling is the real bound.
            "--print-timeout", f"{int(LLM_TIMEOUT_SEC)}s",
        ]
        try:
            use_shell = os.name == "nt" and resolved.lower().endswith((".cmd", ".bat", ".ps1"))
            result = subprocess.run(
                [resolved, *args],
                input=message,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=LLM_TIMEOUT_SEC,
                cwd=str(ROOT_DIR),
                shell=use_shell,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"{self.name} timed out after {LLM_TIMEOUT_SEC}s") from exc
        except OSError as exc:
            raise LLMError(f"{self.name} could not be launched: {exc}") from exc

        if result.returncode != 0:
            raise LLMError(
                f"{self.name} exited {result.returncode}: "
                f"{self._error_detail(result.stdout or '', result.stderr or '')}"
            )

        return self._response_from_stream(result.stdout or "")

    @staticmethod
    def _error_detail(stdout: str, stderr: str) -> str:
        """agy's own error text, not the head of the stream.

        agy reports failures as a `result` event on STDOUT and leaves stderr
        empty, behind an init event that lists every tool it has. Slicing the
        head of stdout therefore shows a tool list and hides the one line that
        says what actually went wrong.
        """
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") != "result":
                continue
            payload = event.get("result") or {}
            reported = (payload.get("error") or "").strip()
            if reported:
                return reported[:500]
            status = payload.get("status")
            if status and status != "SUCCESS":
                return f"status {status}"
        # No parseable result event: fall back to the TAIL, where a crash lands,
        # rather than the head, which is only ever the init banner.
        raw = (stderr or stdout).strip()
        return raw[-500:] if raw else "no output"

    @staticmethod
    def _response_from_stream(stdout: str) -> str:
        """Pull the answer out of the NDJSON event stream.

        Only the terminal `result` event is authoritative. The `agent_response`
        steps arrive as `text_delta` fragments, so reassembling from those is a
        second, redundant parser - but it is the fallback when a run is cut off
        before its result event, which otherwise reads as "returned no output".
        """
        deltas: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "result":
                payload = event.get("result") or {}
                status = payload.get("status")
                response = (payload.get("response") or "").strip()
                if status and status != "SUCCESS" and not response:
                    raise LLMError(f"antigravity returned status {status}")
                if response:
                    return response
            step = event.get("step_update") or {}
            if step.get("step_type") == "agent_response":
                deltas.append(step.get("text_delta") or "")

        joined = "".join(deltas).strip()
        if joined:
            return joined
        raise LLMError("antigravity returned no result event")


def antigravity_provider() -> AntigravityProvider:
    return AntigravityProvider()


def _split_override(env_var: str, default: list[str]) -> list[str]:
    raw = os.environ.get(env_var)
    return raw.split() if raw else default


# ── Claude Agent SDK provider ─────────────────────────────────────────

class ClaudeSDKProvider:
    """Claude via the Agent SDK. Runs with no tools, so it behaves as a plain
    completion rather than an agent."""

    name = "claude"

    def available(self) -> bool:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            return False
        return True

    def unavailable_reason(self) -> str:
        return "claude-agent-sdk not installed"

    def complete(self, prompt: str) -> str:
        return asyncio.run(self._acomplete(prompt))

    async def _acomplete(self, prompt: str) -> str:
        try:
            from claude_agent_sdk import (  # type: ignore[import-not-found]
                AssistantMessage,
                ClaudeAgentOptions,
                TextBlock,
                query,
            )
        except ImportError as exc:
            raise LLMError(f"claude-agent-sdk not installed: {exc}") from exc

        response = ""
        try:
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    cwd=str(ROOT_DIR),
                    # No tools: prevents the model wandering into the filesystem
                    # mid-extraction.
                    allowed_tools=[],
                    max_turns=3,
                ),
            ):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            response += block.text
        except Exception as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        if not response.strip():
            raise LLMError("claude returned an empty response")
        return response


# ── Chain ─────────────────────────────────────────────────────────────

PROVIDER_FACTORIES = {
    "gemini": gemini_provider,
    "codex": codex_provider,
    "claude": ClaudeSDKProvider,
    "antigravity": antigravity_provider,
}


def build_chain(order: list[str] | None = None) -> list[Provider]:
    """Instantiate providers in priority order, skipping unknown names."""
    names = order or LLM_PROVIDER_ORDER
    chain: list[Provider] = []
    for name in names:
        factory = PROVIDER_FACTORIES.get(name.strip().lower())
        if factory:
            chain.append(factory())  # type: ignore[arg-type]
    return chain


# The provider that served the most recent call, for logging into stage_runs.
last_provider: str | None = None


def complete(prompt: str, order: list[str] | None = None) -> str:
    """Send a prompt to the first provider that succeeds.

    Falls through on any failure - a quota limit or CLI hiccup on the preferred
    provider must not stall a nightly batch that has already paid for
    transcription.
    """
    global last_provider

    chain = build_chain(order)
    if not chain:
        raise LLMError("no LLM providers configured (see MMC_LLM_PROVIDERS)")

    errors: list[str] = []
    for provider in chain:
        if not provider.available():
            errors.append(f"{provider.name}: {provider.unavailable_reason()}")
            continue
        try:
            response = provider.complete(prompt)
            last_provider = provider.name
            if errors:
                print(f"    (fell through to {provider.name}: {'; '.join(errors)})")
            return response
        except LLMError as exc:
            errors.append(f"{provider.name}: {exc}")

    last_provider = None
    raise LLMError("all providers failed -> " + " | ".join(errors))


def extract_fenced_block(text: str, language: str = "") -> str:
    """Pull the contents of the first fenced code block, or return text as-is.

    Models wrap structured output in fences even when told not to, so this
    normalizes both shapes instead of failing on the common case.
    """
    fence = f"```{language}" if language else "```"
    start = text.find(fence)
    if start == -1 and language:
        start = text.find("```")
        fence = "```"
    if start == -1:
        return text.strip()

    body_start = text.find("\n", start)
    if body_start == -1:
        return text.strip()
    end = text.find("```", body_start)
    if end == -1:
        return text[body_start:].strip()
    return text[body_start:end].strip()
