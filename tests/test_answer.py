"""Retrieval/synthesis split and failure alerting."""

from __future__ import annotations

import os
import time

import pytest

from pipeline import alert, answer, graph_sync, index, llm
from pipeline.llm import LLMError

# ── retrieval + synthesis ─────────────────────────────────────────────

def test_local_model_retrieves_and_subscription_writes(monkeypatch):
    """The whole point of the split: each job on the right model."""
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda *a, **k: "RETRIEVED CONTEXT")
    monkeypatch.setattr(answer, "complete", lambda prompt: f"answered from: {prompt[-30:]}")
    monkeypatch.setattr(llm, "last_provider", "gemini")

    result = answer.ask("why did we defer Atlas?")

    assert result.provider == "gemini", "must report which provider actually answered"
    assert result.synthesized is True
    assert result.context_chars == len("RETRIEVED CONTEXT")
    assert "answered from" in result.text


def test_context_is_passed_to_the_synthesizer(monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda *a, **k: "Atlas was deferred to Q1")
    monkeypatch.setattr(answer, "complete", lambda prompt: captured.append(prompt) or "ok")

    answer.ask("when is Atlas?")
    assert "Atlas was deferred to Q1" in captured[0]
    assert "when is Atlas?" in captured[0]


def test_empty_retrieval_does_not_invite_invention(monkeypatch):
    """Asking a model to answer from nothing is how a knowledge base starts lying."""
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda *a, **k: "")
    monkeypatch.setattr(answer, "fallback_local_context", lambda *a, **k: "   ")
    called: list[int] = []
    monkeypatch.setattr(answer, "complete", lambda prompt: called.append(1) or "x")

    result = answer.ask("anything?")

    assert called == [], "synthesis must not run without context"
    assert result.synthesized is False
    assert "doctor" in result.text, "should point at the diagnostic"


def test_synthesis_failure_returns_retrieved_context_unsynthesized(monkeypatch):
    """LightRAG's own /query is confirmed broken (HTTP 500 after 242s), so it is
    no longer the fallback when synthesis fails - the raw retrieved context is,
    since it is real and immediately available."""
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda *a, **k: "the graph says Atlas was deferred")
    called: list[int] = []
    monkeypatch.setattr(index, "query", lambda *a, **k: called.append(1) or "should never run")

    def boom(prompt):
        raise LLMError("all providers failed")

    monkeypatch.setattr(answer, "complete", boom)

    result = answer.ask("q")

    assert called == [], "must never fall through to LightRAG's own /query"
    assert result.synthesized is False
    assert "all providers failed" in result.text
    assert "the graph says Atlas was deferred" in result.text


def test_synthesize_false_uses_the_bounded_raw_query(monkeypatch):
    monkeypatch.setattr(answer, "_bounded_raw_query", lambda *a, **k: "lightrag answer")
    called: list[int] = []
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda *a, **k: called.append(1) or "")
    monkeypatch.setattr(answer, "fallback_local_context", lambda *a, **k: called.append(1) or "")

    result = answer.ask("q", synthesize=False)

    assert result.text == "lightrag answer"
    assert result.synthesized is False
    assert called == [], "synthesize=False must not also pay for graph/minutes retrieval it does not use"


def test_synthesize_false_reports_plainly_when_the_bounded_query_gives_up(monkeypatch):
    monkeypatch.setattr(answer, "_bounded_raw_query", lambda *a, **k: None)

    result = answer.ask("q", synthesize=False)

    assert result.synthesized is False
    assert "not usable" in result.text


def test_timing_is_reported_per_phase(monkeypatch):
    """Latency at scale must be attributable: traversal or generation?"""
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda *a, **k: "context")
    monkeypatch.setattr(answer, "complete", lambda prompt: "answer")
    monkeypatch.setattr(llm, "last_provider", "codex")

    result = answer.ask("q")
    line = result.timing_line()

    assert "retrieval" in line
    assert "synthesis" in line
    assert "codex" in line
    assert result.total_sec >= 0


# ── retrieval combination (graph + minutes) ───────────────────────────

def test_retrieve_context_combines_graph_and_minutes_when_both_match(monkeypatch):
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda q: "## Knowledge graph (matched: Atlas)")
    monkeypatch.setattr(answer, "fallback_local_context", lambda q: "Atlas kickoff notes")

    context = answer._retrieve_context("what is Atlas?")

    assert "Knowledge graph" in context
    assert "## Minutes excerpts" in context
    assert "Atlas kickoff notes" in context
    # Graph section comes first - it gives the shape, minutes the detail.
    assert context.index("Knowledge graph") < context.index("Minutes excerpts")


def test_retrieve_context_uses_graph_only_when_minutes_empty(monkeypatch):
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda q: "graph stuff")
    monkeypatch.setattr(answer, "fallback_local_context", lambda q: "")
    assert answer._retrieve_context("q") == "graph stuff"


def test_retrieve_context_uses_minutes_only_when_graph_empty(monkeypatch):
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda q: "")
    monkeypatch.setattr(answer, "fallback_local_context", lambda q: "minutes stuff")
    assert answer._retrieve_context("q") == "## Minutes excerpts\n\nminutes stuff"


def test_retrieve_context_empty_when_both_empty(monkeypatch):
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda q: "")
    monkeypatch.setattr(answer, "fallback_local_context", lambda q: "")
    assert answer._retrieve_context("q") == ""


# ── bounded raw query (LightRAG's own /query, --local only) ───────────
# Regression coverage: LightRAG's /query is confirmed to return HTTP 500 after
# 242s on this deployment. Callers must never wait anywhere near that long.

def test_bounded_raw_query_gives_up_at_the_cap_not_the_real_delay(monkeypatch):
    monkeypatch.setattr(answer, "RAW_QUERY_CAP_SEC", 0.05)

    def slow_query(*a, **k):
        time.sleep(0.3)
        return "too late"

    monkeypatch.setattr(index, "query", slow_query)

    started = time.monotonic()
    result = answer._bounded_raw_query("q", None, None)
    elapsed = time.monotonic() - started

    assert result is None
    assert elapsed < 0.2, "must give up at the cap, not wait out the slow call"


def test_bounded_raw_query_returns_the_result_when_fast_enough(monkeypatch):
    monkeypatch.setattr(index, "query", lambda *a, **k: "fast answer")
    assert answer._bounded_raw_query("q", None, None) == "fast answer"


def test_bounded_raw_query_swallows_index_errors(monkeypatch):
    def boom(*a, **k):
        raise index.IndexError_("HTTP 500")

    monkeypatch.setattr(index, "query", boom)
    assert answer._bounded_raw_query("q", None, None) is None


def test_synthesis_prompt_forbids_filling_gaps():
    prompt = answer.build_synthesis_prompt("q", "ctx")
    assert "say so plainly" in prompt
    assert "Cite the meetings" in prompt
    # A reversed decision is usually the most useful thing to surface.
    assert "position changed" in prompt


# ── conversation history ─────────────────────────────────────────────

def test_history_block_appears_before_retrieved_records():
    prompt = answer.build_synthesis_prompt(
        "why?", "RECORD TEXT", history=[("what happened at the roadmap meeting?", "We shipped in Q1.")]
    )
    assert "## Earlier in this conversation" in prompt
    assert "We shipped in Q1." in prompt
    assert prompt.index("## Earlier in this conversation") < prompt.index("## Retrieved records")
    assert prompt.index("## Retrieved records") < prompt.index("RECORD TEXT")


def test_history_is_not_evidence_rule_is_present():
    prompt = answer.build_synthesis_prompt("why?", "ctx", history=[("q", "a")])
    assert "not evidence" in prompt


def test_no_history_omits_the_block_entirely():
    prompt = answer.build_synthesis_prompt("q", "ctx", history=None)
    assert "Earlier in this conversation" not in prompt
    prompt_empty = answer.build_synthesis_prompt("q", "ctx", history=[])
    assert "Earlier in this conversation" not in prompt_empty


def test_retrieval_query_folds_in_the_last_two_questions():
    history = [("what did we decide about Atlas?", "a1"), ("who owns it?", "a2")]
    assert answer._retrieval_query("why?", history) == "what did we decide about Atlas? who owns it? why?"


def test_retrieval_query_uses_only_the_last_two_turns():
    history = [(f"q{i}", f"a{i}") for i in range(5)]
    query = answer._retrieval_query("why?", history)
    assert query == "q3 q4 why?"


def test_retrieval_query_is_unchanged_with_no_history():
    assert answer._retrieval_query("q", []) == "q"


def test_history_reaches_retrieval_for_a_pronoun_follow_up(monkeypatch):
    """The whole point of item 7's retrieval fix: "why?" alone retrieves
    nothing, but folded with the prior question it does."""
    captured: list[str] = []
    monkeypatch.setattr(
        graph_sync, "retrieve_context", lambda q, *a, **k: captured.append(q) or "context"
    )
    monkeypatch.setattr(answer, "complete", lambda prompt: "ok")

    answer.ask("why?", history=[("what did we decide about Atlas?", "We deferred it.")])

    assert "Atlas" in captured[0]
    assert "why?" in captured[0]


def test_history_reaches_the_synthesis_prompt(monkeypatch):
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda *a, **k: "context")
    captured: list[str] = []
    monkeypatch.setattr(answer, "complete", lambda prompt: captured.append(prompt) or "ok")

    answer.ask("why?", history=[("what did we decide about Atlas?", "We deferred it.")])

    assert "We deferred it." in captured[0]


def test_clamp_history_answer_marks_truncation():
    long_answer = "x" * 5000
    clamped = answer._clamp_history_answer(long_answer)
    assert len(clamped) < len(long_answer)
    assert clamped.endswith("... [truncated]")
    assert clamped.startswith("x" * answer.HISTORY_ANSWER_CHAR_CAP)


def test_clamp_history_answer_leaves_short_answers_alone():
    assert answer._clamp_history_answer("short") == "short"


def test_fit_history_to_budget_keeps_newest_when_over_budget():
    """Walks newest -> oldest so an old turn is what gets dropped, not the
    most recent one a follow-up question is actually about.

    Answers are kept under HISTORY_ANSWER_CHAR_CAP so the per-answer clamp
    does not confound the token-budget math this test is checking."""
    history = [(f"q{i}", "x" * 1000) for i in range(4)]  # ~1000 chars/turn, unclamped
    fitted = answer._fit_history_to_budget(history, token_budget=375)  # ~1500 char budget

    assert [q for q, _ in fitted] == ["q3"], "only the newest turn fits, and it must survive"


def test_fit_history_to_budget_keeps_the_single_newest_turn_even_if_oversized():
    """A turn on its own larger than the budget must still not be dropped
    entirely - some context beats none."""
    history = [("q0", "x" * 100_000)]
    fitted = answer._fit_history_to_budget(history, token_budget=10)
    assert len(fitted) == 1


def test_fit_history_to_budget_reverses_back_to_chronological_order():
    history = [("q0", "a"), ("q1", "a"), ("q2", "a")]
    fitted = answer._fit_history_to_budget(history, token_budget=answer.HISTORY_TOKEN_BUDGET)
    assert [q for q, _ in fitted] == ["q0", "q1", "q2"]


def test_ask_hard_caps_history_to_configured_turn_count(monkeypatch):
    monkeypatch.setattr(answer, "CHAT_HISTORY_TURNS", 2)
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda *a, **k: "context")
    captured: list[str] = []
    monkeypatch.setattr(answer, "complete", lambda prompt: captured.append(prompt) or "ok")

    history = [(f"q{i}", f"a{i}") for i in range(6)]
    answer.ask("latest?", history=history)

    prompt = captured[0]
    assert "q5" in prompt and "q4" in prompt, "the most recent turns must survive the cap"
    assert "q0" not in prompt, "the hard cap must drop the oldest turns first"


def test_pipeline_query_cli_path_passes_no_history(monkeypatch):
    """`pipeline query` has no session and must keep working exactly as before."""
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda *a, **k: "context")
    captured: list[str] = []
    monkeypatch.setattr(answer, "complete", lambda prompt: captured.append(prompt) or "ok")

    answer.ask("plain question, no history")

    assert "Earlier in this conversation" not in captured[0]


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


# ── the decision/commitment registers as a retrieval source ───────────

def _register_rows(manifest):
    """One meeting with a decision, a commitment and an open question."""
    from tests.conftest import make_meeting

    meeting_id = "e" * 64
    make_meeting(manifest, meeting_id, "2026-08-12", title_hint="CRUD access review")
    manifest.execute(
        "INSERT INTO decisions (meeting_id, text, decided_by, rationale, timestamp_cite, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            meeting_id,
            "Use a test NCID for non-production CRUD access",
            "Yuliya",
            "Faraz lacked development-environment access",
            "[0:01:17]",
            "2026-08-12T10:00:00-04:00",
        ),
    )
    manifest.execute(
        "INSERT INTO commitments (meeting_id, owner, text, due_date, due_date_iso,"
        " timestamp_cite, state, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            meeting_id,
            "Ali",
            "Send the vault correction change request",
            "Friday",
            None,
            "[0:25:01]",
            "open",
            "2026-08-12T10:00:00-04:00",
        ),
    )
    manifest.execute(
        "INSERT INTO open_questions (meeting_id, text, owner, created_at) VALUES (?,?,?,?)",
        (
            meeting_id,
            "Does the Canada-associated NCID block production access?",
            "Jay",
            "2026-08-12T10:00:00-04:00",
        ),
    )
    manifest.commit()
    return meeting_id


def test_structured_context_answers_a_decision_question_with_its_rationale(manifest):
    """The register is the precise answer; the prose is the fallback.

    "What did we decide about X" is a lookup against 216 parsed rows that carry
    who decided, why, and a timestamp - and it was being answered by keyword
    scoring whole minutes files instead. Rationale in particular is the thing
    minutes are long in order to preserve, and it never reached the model.
    """
    _register_rows(manifest)
    context = answer.structured_context("What did we decide about CRUD access?")

    assert "test NCID" in context
    assert "Yuliya" in context, "who decided must survive"
    assert "lacked development-environment access" in context, "rationale must survive"
    assert "2026-08-12" in context, "the answer has to be citable to a meeting"


def test_structured_context_finds_a_persons_commitments_by_name(manifest):
    """"What does Ali owe?" shares no keywords with the commitment's text.

    Owner is a column, so a question naming a known owner should retrieve their
    open commitments directly rather than hoping the wording overlaps.
    """
    _register_rows(manifest)
    context = answer.structured_context("What is Ali on the hook for?")

    assert "vault correction change request" in context
    assert "Ali" in context


def test_structured_context_is_empty_when_nothing_matches(manifest):
    """No headers with nothing under them - an empty section invites invention."""
    _register_rows(manifest)
    assert answer.structured_context("quarterly pricing for the Lisbon office") == ""


def test_retrieve_context_puts_the_register_ahead_of_prose(monkeypatch):
    """Ordering is the instruction: precise rows first, prose after."""
    monkeypatch.setattr(graph_sync, "retrieve_context", lambda *a, **k: "GRAPH")
    monkeypatch.setattr(answer, "structured_context", lambda q: "## Decision register\n\nREGISTER")
    monkeypatch.setattr(answer, "fallback_local_context", lambda q, top_n=5: "PROSE")

    context = answer._retrieve_context("what did we decide")
    assert context.index("REGISTER") < context.index("PROSE")
    assert "GRAPH" in context


def test_prompt_tells_the_model_the_registers_outrank_prose():
    """Retrieval ordering is a hint; the rules have to say it outright.

    The register sections are parsed rows with an owner, a rationale and a
    timestamp. The minutes excerpts under them are prose containing the same
    facts less precisely, so a model given both can answer from either - and
    the wrong pick loses the attribution.
    """
    prompt = answer.build_synthesis_prompt("who owns the vault fix", "CONTEXT")
    lowered = prompt.lower()
    assert "register" in lowered, "the rules must name the register sections"
    assert "authoritative" in lowered or "prefer" in lowered


def test_citation_prefers_the_minutes_title_over_the_drive_filename():
    """`title_hint` is a mangled Drive file id, not a title.

    Real values look like "1orzS fOYO8qQnBfGwVkEmJ6PWkoxdCse 8 Aug 12 at 4 00 p",
    which is unusable as a citation, while the minutes filename carries the
    title the compiler actually wrote. Every register citation was quoting the
    former.
    """
    row = {
        "meeting_date": "2026-08-12",
        "title_hint": "1orzS fOYO8qQnBfGwVkEmJ6PWkoxdCse 8 Aug 12 at 4 00 p",
        "source_name": "My_recording_10.mp4",
        "minutes_path": "minutes/2026-08-12-kafka-acl-production-change-order-creation-52494306.md",
        "timestamp_cite": "[0:04:11]",
    }
    cite = answer._cite(row)
    assert "Kafka Acl Production Change Order Creation" in cite
    assert "1orzS" not in cite, "the Drive id must never reach a citation"
    assert "[0:04:11]" in cite


def test_citation_falls_back_when_there_is_no_minutes_file():
    """A meeting still mid-pipeline has no minutes filename to read a title from."""
    cite = answer._cite({"meeting_date": "2026-08-12", "title_hint": "Roadmap review"})
    assert "Roadmap review" in cite


def test_register_retrieval_is_selective_not_everything(manifest):
    """Scoring on total occurrences let one common word return the whole table.

    "access" appears in most of a security team's decisions, so raw
    term-frequency scoring returned ~7k characters and all three registers for
    every question asked. Distinct-keyword matching is what makes it retrieval
    rather than a dump.
    """
    from tests.conftest import make_meeting

    meeting_id = "f" * 64
    make_meeting(manifest, meeting_id, "2026-08-12", title_hint="Access review")
    rows = [
        "Grant CRUD access to the pre-prod query service",   # 2 keywords
        "Review access badges for the Toronto office",       # 1 keyword
        "Access the shared drive for onboarding docs",       # 1 keyword
    ]
    for text in rows:
        manifest.execute(
            "INSERT INTO decisions (meeting_id, text, created_at) VALUES (?,?,?)",
            (meeting_id, text, "2026-08-12T10:00:00-04:00"),
        )
    manifest.commit()

    context = answer.structured_context("What did we decide about CRUD access?")
    assert "pre-prod query service" in context, "the two-keyword hit must survive"
    assert "Toronto office" not in context, "a single common word is not a match"
