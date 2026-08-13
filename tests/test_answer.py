"""Retrieval/synthesis split and failure alerting."""

from __future__ import annotations

import os

import pytest

from pipeline import alert, answer, index, llm
from pipeline.llm import LLMError

# ── retrieval + synthesis ─────────────────────────────────────────────

def test_local_model_retrieves_and_subscription_writes(monkeypatch):
    """The whole point of the split: each job on the right model."""
    monkeypatch.setattr(index, "query_context", lambda *a, **k: "RETRIEVED CONTEXT")
    monkeypatch.setattr(answer, "complete", lambda prompt: f"answered from: {prompt[-30:]}")
    monkeypatch.setattr(llm, "last_provider", "gemini")

    result = answer.ask("why did we defer Atlas?")

    assert result.provider == "gemini", "must report which provider actually answered"
    assert result.synthesized is True
    assert result.context_chars == len("RETRIEVED CONTEXT")
    assert "answered from" in result.text


def test_context_is_passed_to_the_synthesizer(monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(index, "query_context", lambda *a, **k: "Atlas was deferred to Q1")
    monkeypatch.setattr(answer, "complete", lambda prompt: captured.append(prompt) or "ok")

    answer.ask("when is Atlas?")
    assert "Atlas was deferred to Q1" in captured[0]
    assert "when is Atlas?" in captured[0]


def test_empty_retrieval_does_not_invite_invention(monkeypatch):
    """Asking a model to answer from nothing is how a knowledge base starts lying."""
    monkeypatch.setattr(index, "query_context", lambda *a, **k: "   ")
    called: list[int] = []
    monkeypatch.setattr(answer, "complete", lambda prompt: called.append(1) or "x")

    result = answer.ask("anything?")

    assert called == [], "synthesis must not run without context"
    assert result.synthesized is False
    assert "doctor" in result.text, "should point at the diagnostic"


def test_falls_back_to_local_generation_when_providers_fail(monkeypatch):
    """An answer from the small local model beats no answer."""
    monkeypatch.setattr(index, "query_context", lambda *a, **k: "context")
    monkeypatch.setattr(index, "query", lambda *a, **k: "local answer")

    def boom(prompt):
        raise LLMError("all providers failed")

    monkeypatch.setattr(answer, "complete", boom)

    result = answer.ask("q")
    assert result.text == "local answer"
    assert result.synthesized is False


def test_local_flag_skips_retrieval_split(monkeypatch):
    monkeypatch.setattr(index, "query", lambda *a, **k: "lightrag answer")
    called: list[int] = []
    monkeypatch.setattr(index, "query_context", lambda *a, **k: called.append(1) or "x")

    result = answer.ask("q", synthesize=False)
    assert result.text == "lightrag answer"
    assert called == []
    assert result.synthesized is False


def test_timing_is_reported_per_phase(monkeypatch):
    """Latency at scale must be attributable: traversal or generation?"""
    monkeypatch.setattr(index, "query_context", lambda *a, **k: "context")
    monkeypatch.setattr(answer, "complete", lambda prompt: "answer")
    monkeypatch.setattr(llm, "last_provider", "codex")

    result = answer.ask("q")
    line = result.timing_line()

    assert "retrieval" in line
    assert "synthesis" in line
    assert "codex" in line
    assert result.total_sec >= 0


def test_synthesis_prompt_forbids_filling_gaps():
    prompt = answer.build_synthesis_prompt("q", "ctx")
    assert "say so plainly" in prompt
    assert "Cite the meetings" in prompt
    # A reversed decision is usually the most useful thing to surface.
    assert "position changed" in prompt


# ── alerting ──────────────────────────────────────────────────────────

def test_summary_names_the_failed_stages_and_next_steps():
    subject, body = alert.build_summary(["transcribe", "index"], "boom")

    assert "transcribe, index" in subject
    assert "boom" in body
    assert "pipeline status" in body
    assert "pipeline doctor" in body
    # Reassurance matters: a 3am alert should not imply data loss.
    assert "Nothing is lost" in body


def test_no_alert_configured_is_not_an_error(monkeypatch):
    monkeypatch.setattr(alert, "ALERT_COMMAND", "")
    assert alert.send(["transcribe"]) is False


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell")
def test_alert_delivers_summary_on_stdin(tmp_path, monkeypatch):
    """The summary goes on stdin so it is not limited by argv length.

    Runs from inside tmp_path: an earlier version passed an absolute path into the
    command, and on Windows shlex mangled it into a relative filename that landed
    in whatever directory pytest happened to be in - which is how two junk files
    ended up committed to a checkout.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(alert, "ALERT_COMMAND", "tee alert.txt")

    assert alert.send(["transcribe"], detail="detail here") is True
    written = (tmp_path / "alert.txt").read_text(encoding="utf-8")
    assert "transcribe" in written
    assert "detail here" in written


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell")
def test_subject_is_substituted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(alert, "ALERT_COMMAND", 'sh -c "echo {subject} > out.txt"')
    alert.send(["minutes"])
    assert "minutes" in (tmp_path / "out.txt").read_text(encoding="utf-8")


def test_windows_paths_survive_command_splitting():
    """Regression: shlex defaults to POSIX mode, where backslashes are escapes.

    On Windows that turned `tee C:\\Users\\me\\alert.txt` into
    `tee C:Usersmealert.txt`, writing to a garbage filename instead of failing.
    """
    argv = alert.split_command(r'tee C:\Users\me\alert.txt') if os.name == "nt" else None
    if argv is not None:
        assert argv[1] == r"C:\Users\me\alert.txt"

    # The POSIX path must keep working wherever the tests actually run.
    assert alert.split_command("mail -s subject me@example.com") == [
        "mail", "-s", "subject", "me@example.com"
    ]


def test_failing_alert_command_does_not_raise(monkeypatch):
    """An alerting failure must never mask the pipeline failure it reports."""
    monkeypatch.setattr(alert, "ALERT_COMMAND", "false")
    assert alert.send(["transcribe"]) is False


def test_missing_alert_binary_does_not_raise(monkeypatch):
    monkeypatch.setattr(alert, "ALERT_COMMAND", "definitely-not-a-real-binary-xyz")
    assert alert.send(["transcribe"]) is False


def test_unparseable_alert_command_does_not_raise(monkeypatch):
    monkeypatch.setattr(alert, "ALERT_COMMAND", 'mail -s "unclosed')
    assert alert.send(["transcribe"]) is False
