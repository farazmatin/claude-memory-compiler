"""Provider chain fallthrough and output normalization."""

from __future__ import annotations

import json
import shutil

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

    def unavailable_reason(self):
        return f"{self.name} unavailable"

    def complete(self, prompt):
        self.calls += 1
        if not self.ok:
            raise llm.LLMError(f"{self.name} refused")
        return f"{self.name}-response"


def test_default_order_prefers_antigravity():
    """Antigravity leads: it is the CLI that holds a live Google session here.

    The standalone `gemini` CLI is last because it has no credentials on this
    machine and each attempt costs ~12s before falling through.
    """
    assert [p.name for p in llm.build_chain()] == ["antigravity", "codex", "claude", "gemini"]


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


def test_gemini_provider_no_longer_passes_bogus_dash_p(monkeypatch):
    """Regression: `-p -` made the CLI merge stdin and the `-p` value as
    `${stdinData}\\n${input}`, appending a stray "-" line to every prompt.
    Piped stdin alone already triggers headless mode."""
    monkeypatch.setattr(llm, "GEMINI_MODEL", "gemini-3.5-flash")
    args = llm.gemini_provider().args
    assert args == ["-m", "gemini-3.5-flash"]
    assert "-p" not in args


def test_gemini_provider_skips_the_auth_gate_when_the_binary_is_substituted(monkeypatch, tmp_path):
    """Regression: the e2e harness points MMC_GEMINI_BIN at a local stub that
    needs no OAuth at all. Gating that stub on ~/.gemini/oauth_creds.json in the
    real user's home directory disabled the only provider those tests
    configure, failing the minutes stage of every one of them."""
    stub = tmp_path / "fake-gemini"
    stub.write_text("", encoding="utf-8")
    monkeypatch.setenv("MMC_GEMINI_BIN", str(stub))

    provider = llm.gemini_provider()

    assert provider.auth_gate is None, "a substituted binary owns its own auth"


def test_gemini_provider_keeps_the_auth_gate_for_the_real_default_binary(monkeypatch):
    monkeypatch.delenv("MMC_GEMINI_BIN", raising=False)
    assert llm.gemini_provider().auth_gate is llm._gemini_auth_gate


# ── Gemini auth gate ────────────────────────────────────────────────────
# Regression coverage for the unauthenticated-CLI hang: every headless call
# used to burn ~12s on an interactive OAuth prompt that swallowed the piped
# prompt as its answer and exited 130, with the failure visible only as a
# server-log line.

def test_auth_gate_blocks_when_no_credential_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(llm.Path, "home", classmethod(lambda cls: tmp_path))
    reason = llm._gemini_auth_gate()
    assert "not authenticated" in reason
    assert "gemini" in reason


def test_auth_gate_clears_once_credential_file_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(llm.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "oauth_creds.json").write_text("{}", encoding="utf-8")
    assert llm._gemini_auth_gate() == ""


def test_auth_gate_clears_with_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(llm.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert llm._gemini_auth_gate() == ""


def test_cli_provider_auth_gate_skips_the_call_and_says_why():
    """A blocked gate must fail fast with the reason, not spend ~12s attempting
    a call that cannot succeed - the binary itself is present and fine."""
    provider = llm.CLIProvider(
        name="cat", binary="cat", args=[], auth_gate=lambda: "not authenticated",
    )
    if not shutil.which("cat"):
        pytest.skip("cat unavailable")

    assert provider.available() is False
    assert provider.unavailable_reason() == "not authenticated"
    with pytest.raises(llm.LLMError, match="not authenticated"):
        provider.complete("prompt")


def test_cli_provider_unblocked_auth_gate_does_not_interfere():
    provider = llm.CLIProvider(name="cat", binary="cat", args=[], auth_gate=lambda: "")
    if not shutil.which("cat"):
        pytest.skip("cat unavailable")
    assert provider.available() is True
    assert provider.complete("hello") == "hello"


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


# ── Antigravity provider ──────────────────────────────────────────────

def test_antigravity_reads_the_terminal_result_event():
    stream = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "hi\n"}})
    assert llm.AntigravityProvider._response_from_stream(stream) == "hi"


def test_antigravity_reassembles_deltas_when_the_run_is_cut_off():
    """A truncated run would otherwise read as "returned no output"."""
    stream = "\n".join(
        json.dumps(
            {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": t}}
        )
        for t in ("Hel", "lo ", "world")
    )
    assert llm.AntigravityProvider._response_from_stream(stream) == "Hello world"


def test_antigravity_raises_on_a_failed_status():
    stream = json.dumps({"event": "result", "result": {"status": "ERROR", "response": ""}})
    with pytest.raises(llm.LLMError, match="status ERROR"):
        llm.AntigravityProvider._response_from_stream(stream)


def test_antigravity_ignores_non_json_noise_in_the_stream():
    stream = "\n".join(
        [
            "Fetching available models...",
            "not json at all",
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}),
        ]
    )
    assert llm.AntigravityProvider._response_from_stream(stream) == "ok"


def test_antigravity_raises_when_the_stream_is_empty():
    with pytest.raises(llm.LLMError, match="no result event"):
        llm.AntigravityProvider._response_from_stream("")


def test_antigravity_sends_the_prompt_on_stdin_not_argv(monkeypatch):
    """A transcript is far past the Windows ~32 KB argv limit.

    `agy --print` takes the prompt as an argument and rejects stdin, so the
    stream-json input format is the only usable route for a real prompt.
    """
    captured = {}

    class Result:
        returncode = 0
        stdout = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "y"}})
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return Result()

    monkeypatch.setattr(llm.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(llm.subprocess, "run", fake_run)

    huge = "x" * 100_000
    assert llm.AntigravityProvider().complete(huge) == "y"

    assert huge not in " ".join(captured["cmd"]), "prompt must never reach argv"
    assert "--input-format" in captured["cmd"] and "stream-json" in captured["cmd"]
    payload = json.loads(captured["input"])
    assert payload["message"]["content"][0]["text"] == huge


def test_antigravity_is_first_in_the_default_chain():
    from pipeline.config import LLM_PROVIDER_ORDER

    assert LLM_PROVIDER_ORDER[0] == "antigravity"
    assert "antigravity" in llm.PROVIDER_FACTORIES


def test_antigravity_envelopes_the_prompt_under_event_not_type(monkeypatch):
    """`agy` keys stream-json input on "event", not Claude Code's "type".

    `--input-format stream-json` arrived in agy 1.1.15 and this provider was
    written against Claude Code's schema, which is the same shape under a
    different envelope key. agy answers a `{"type": "user"}` message with
    `stream input message is missing the "event" field` and exits 1 - on stdout,
    with an empty stderr, so the failure reads as an opaque "exited 1" and every
    minutes compile silently fell through to the next provider in the chain.
    """
    captured = {}

    class Result:
        returncode = 0
        stdout = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "y"}})
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["input"] = kwargs.get("input")
        return Result()

    monkeypatch.setattr(llm.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(llm.subprocess, "run", fake_run)

    llm.AntigravityProvider().complete("hello")

    payload = json.loads(captured["input"])
    assert payload["event"] == "user"
    assert "type" not in payload
    # The message sub-object was always right; only the envelope key was wrong.
    assert payload["message"] == {
        "role": "user",
        "content": [{"type": "text", "text": "hello"}],
    }


def test_antigravity_surfaces_the_stream_error_not_the_init_event(monkeypatch):
    """A non-zero exit must report agy's own error, not the first 500 bytes.

    agy writes its complaint to stdout as a `result` event and leaves stderr
    empty. The init event that precedes it lists 57 tool names, so a naive
    stdout[:500] shows nothing but that tool list - which is how a one-key
    schema mismatch stayed invisible while every compile fell through to codex.
    """
    init = json.dumps({"event": "init", "init": {"tools": [f"t{i}" for i in range(57)]}})
    err = json.dumps(
        {"event": "result", "result": {"status": "ERROR", "response": "", "error": "boom"}}
    )

    class Result:
        returncode = 1
        stdout = init + "\n" + err + "\n"
        stderr = ""

    monkeypatch.setattr(llm.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(llm.subprocess, "run", lambda cmd, **kw: Result())

    with pytest.raises(llm.LLMError) as excinfo:
        llm.AntigravityProvider().complete("hi")
    assert "boom" in str(excinfo.value)
    assert "t42" not in str(excinfo.value), "init tool list must not crowd out the error"
