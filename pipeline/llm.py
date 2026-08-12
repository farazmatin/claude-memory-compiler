"""Claude Agent SDK wrapper for pure text-in/text-out prompting.

Used by the speaker resolver and the minutes compiler. Runs with no tools and a
tight turn limit, so it behaves as a plain completion call rather than an agent.

This path is covered by the user's existing Claude subscription, which is why
minutes generation - the highest-value artifact in the system - uses it, while
LightRAG's bulk entity extraction runs on a local Ollama model instead.
"""

from __future__ import annotations

import asyncio

from pipeline.config import ROOT_DIR


class LLMError(RuntimeError):
    """The model call failed. Callers decide whether that is fatal for a stage."""


async def acomplete(prompt: str, max_turns: int = 2) -> str:
    """Send a prompt, return the concatenated text response."""
    try:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            AssistantMessage,
            ClaudeAgentOptions,
            TextBlock,
            query,
        )
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise LLMError(f"claude-agent-sdk not installed: {exc}") from exc

    response = ""
    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                # No tools: this is a completion, not an agent. Prevents the model
                # from wandering into the filesystem mid-extraction.
                allowed_tools=[],
                max_turns=max_turns,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
    except Exception as exc:  # noqa: BLE001 - SDK raises a wide range of transport errors
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc

    if not response.strip():
        raise LLMError("model returned an empty response")
    return response


def complete(prompt: str, max_turns: int = 2) -> str:
    """Synchronous wrapper around `acomplete`."""
    return asyncio.run(acomplete(prompt, max_turns=max_turns))


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
