"""Provider chain fallthrough and output normalization."""

from __future__ import annotations

import pytest

from pipeline import llm


class FakeProvider:
    def __init__(self, name, *, ok=True, up=True):
        self.name = name
        self.ok = ok
        self.up = up
        self.calls = 0

    def available(self):
        return self.up

    def complete(self, prompt):
        self.calls += 1
        if not self.ok:
            raise llm.LLMError(f"{self.name} refused")
        return f"{self.name}-response"


def test_default_order_is_gemini_codex_claude():
    assert [p.name for p in llm.build_chain()] == ["gemini", "codex", "claude"]


def test_unknown_provider_names_are_skipped():
    assert [p.name for p in llm.build_chain(["nope", "claude"])] == ["claude"]


def test_first_working_provider_wins(monkeypatch):
    first = FakeProvider("gemini")
    second = FakeProvider("codex")
    monkeypatch.setattr(llm, "build_chain", lambda order=None: [first, second])

    assert llm.complete("prompt") == "gemini-response"
    assert second.calls == 0, "later providers must not be called on success"
    assert llm.last_provider == "gemini"


def test_falls_through_on_failure(monkeypatch):
    """A quota limit on the preferred provider must not stall a batch that has
    already paid for transcription."""
    failing = FakeProvider("gemini", ok=False)
    working = FakeProvider("codex")
    monkeypatch.setattr(llm, "build_chain", lambda order=None: [failing, working])

    assert llm.complete("prompt") == "codex-response"
    assert failing.calls == 1
    assert llm.last_provider == "codex"


def test_skips_unavailable_without_calling(monkeypatch):
    absent = FakeProvider("gemini", up=False)
    working = FakeProvider("claude")
    monkeypatch.setattr(llm, "build_chain", lambda order=None: [absent, working])

    assert llm.complete("prompt") == "claude-response"
    assert absent.calls == 0


def test_all_failing_raises_with_every_reason(monkeypatch):
    monkeypatch.setattr(
        llm, "build_chain",
        lambda order=None: [FakeProvider("gemini", ok=False), FakeProvider("codex", ok=False)],
    )
    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete("prompt")
    assert "gemini" in str(excinfo.value)
    assert "codex" in str(excinfo.value)


def test_empty_chain_raises(monkeypatch):
    monkeypatch.setattr(llm, "build_chain", lambda order=None: [])
    with pytest.raises(llm.LLMError):
        llm.complete("prompt")


def test_gemini_model_pin_is_optional(monkeypatch):
    monkeypatch.setattr(llm, "GEMINI_MODEL", "")
    assert "-m" not in llm.gemini_provider().args
    monkeypatch.setattr(llm, "GEMINI_MODEL", "gemini-flash-latest")
    args = llm.gemini_provider().args
    assert args[:2] == ["-m", "gemini-flash-latest"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('{"a": 1}', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('here you go:\n```json\n{"a": 1}\n```\nhope that helps', '{"a": 1}'),
    ],
)
def test_extract_fenced_block(raw, expected):
    assert llm.extract_fenced_block(raw, "json") == expected


def test_cli_provider_reports_missing_binary():
    provider = llm.CLIProvider(name="nope", binary="definitely-not-a-real-binary", args=[])
    assert provider.available() is False
    with pytest.raises(llm.LLMError):
        provider.complete("prompt")


def test_cli_provider_pipes_prompt_on_stdin():
    """Prompts are tens of thousands of tokens; argv would risk ARG_MAX."""
    provider = llm.CLIProvider(name="cat", binary="cat", args=[])
    if not provider.available():
        pytest.skip("cat unavailable")
    assert provider.complete("hello from stdin") == "hello from stdin"


def test_cli_provider_raises_on_nonzero_exit():
    provider = llm.CLIProvider(name="false", binary="false", args=[])
    if not provider.available():
        pytest.skip("false unavailable")
    with pytest.raises(llm.LLMError, match="exited"):
        provider.complete("prompt")


def test_cli_provider_raises_on_empty_output():
    provider = llm.CLIProvider(name="true", binary="true", args=[])
    if not provider.available():
        pytest.skip("true unavailable")
    with pytest.raises(llm.LLMError, match="no output"):
        provider.complete("prompt")
