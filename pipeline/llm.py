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
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

from pipeline.config import (
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

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def complete(self, prompt: str) -> str:
        if not self.available():
            raise LLMError(f"{self.binary} not found on PATH")
        try:
            # `input` implies stdin=PIPE; passing both raises ValueError. Piping
            # stdin also stops a CLI that wants a TTY from blocking on one and
            # hanging the batch, which the timeout then catches.
            result = subprocess.run(
                [self.binary, *self.args],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=LLM_TIMEOUT_SEC,
                cwd=str(ROOT_DIR),
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


def gemini_provider() -> CLIProvider:
    """Gemini CLI. Highest priority: Flash is fast and cheap against quota."""
    args = ["-p", "-"]
    if GEMINI_MODEL:
        args = ["-m", GEMINI_MODEL, *args]
    return CLIProvider(
        name="gemini",
        binary=os.environ.get("MMC_GEMINI_BIN", "gemini"),
        args=_split_override("MMC_GEMINI_ARGS", args),
    )


def codex_provider() -> CLIProvider:
    """Codex CLI, non-interactive exec mode."""
    return CLIProvider(
        name="codex",
        binary=os.environ.get("MMC_CODEX_BIN", "codex"),
        args=_split_override("MMC_CODEX_ARGS", ["exec", "-"]),
    )


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
        except Exception as exc:  # noqa: BLE001 - SDK raises varied transport errors
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        if not response.strip():
            raise LLMError("claude returned an empty response")
        return response


# ── Chain ─────────────────────────────────────────────────────────────

PROVIDER_FACTORIES = {
    "gemini": gemini_provider,
    "codex": codex_provider,
    "claude": ClaudeSDKProvider,
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
            errors.append(f"{provider.name}: unavailable")
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
