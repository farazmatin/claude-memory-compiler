"""Manifest state machine, recompile selection, and prior-context windowing.

Several of these cover defects found in adversarial review; each such test names
the failure it prevents.
"""

from __future__ import annotations

import sqlite3

import pytest

from pipeline import db

from .conftest import make_meeting


def test_advance_and_fail_and_retry(manifest):
    make_meeting(manifest, "m1", "2026-08-10")

    db.advance(manifest, "m1", db.TRANSCRIBED, transcript_path="/t.json")
    assert db.get_meeting(manifest, "m1").status == db.TRANSCRIBED

    db.mark_failed(manifest, "m1", "boom")
    assert db.get_meeting(manifest, "m1").status == db.FAILED
    assert db.get_meeting(manifest, "m1").error == "boom"

    db.reset_to(manifest, "m1", db.DISCOVERED)
    reloaded = db.get_meeting(manifest, "m1")
    assert reloaded.status == db.DISCOVERED
    assert reloaded.error is None, "a retried meeting must not keep a stale error"


def test_advance_rejects_unknown_columns(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    try:
        db.advance(manifest, "m1", db.TRANSCRIBED, nonsense="x")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown column should raise")


def test_stale_template_requires_speakers_resolved(manifest):
    """Regression: --recompile must not skip speaker resolution.

    A meeting at `transcribed` also has a transcript and a NULL template_version.
    Including it here let --recompile jump it straight to minutes_compiled,
    bypassing stage 3 and producing minutes whose action items are owned by
    "SPEAKER_01".
    """
    make_meeting(manifest, "early", "2026-08-10")
    db.advance(manifest, "early", db.TRANSCRIBED, transcript_path="/t.json")

    make_meeting(manifest, "ready", "2026-08-11")
    db.advance(manifest, "ready", db.SPEAKERS_RESOLVED, transcript_path="/t.json")

    stale_ids = {m.id for m in db.stale_template(manifest, "2")}
    assert "ready" in stale_ids
    assert "early" not in stale_ids, "pre-speaker-resolution meeting must be excluded"


def test_stale_template_matches_only_old_versions(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.advance(
        manifest, "m1", db.INDEXED,
        transcript_path="/t.json", minutes_path="/m.md", template_version="1",
    )
    assert db.stale_template(manifest, "1") == []
    assert len(db.stale_template(manifest, "2")) == 1


def test_prior_context_sees_earlier_same_day_meetings(manifest):
    """Regression: same-day blindness.

    With five meetings a day, a date-only comparison made every meeting blind to
    the other four, so a decision reversed after lunch was never flagged.
    """
    for slot, time in enumerate(["09:00", "11:00", "14:00"]):
        mid = f"m{slot}"
        make_meeting(manifest, mid, "2026-08-10", time)
        db.advance(manifest, mid, db.INDEXED, minutes_path=f"/{mid}.md")

    priors = db.recent_indexed_before(manifest, "2026-08-10", 10, meeting_time="14:00")
    assert {m.id for m in priors} == {"m0", "m1"}, "must see earlier meetings same day"

    first = db.recent_indexed_before(manifest, "2026-08-10", 10, meeting_time="09:00")
    assert first == [], "the day's first meeting has no same-day priors"


def test_prior_context_excludes_self(manifest):
    """A recompile must not feed a meeting its own previous minutes as history."""
    make_meeting(manifest, "self", "2026-08-10", "09:00")
    db.advance(manifest, "self", db.INDEXED, minutes_path="/self.md")

    priors = db.recent_indexed_before(
        manifest, "2026-08-10", 10, meeting_time="09:00", exclude_id="self"
    )
    assert priors == []


def test_prior_context_spans_days_newest_first(manifest):
    for day in ["2026-08-08", "2026-08-09", "2026-08-10"]:
        make_meeting(manifest, day, day, "09:00")
        db.advance(manifest, day, db.INDEXED, minutes_path=f"/{day}.md")

    priors = db.recent_indexed_before(manifest, "2026-08-11", 2, meeting_time="09:00")
    assert [m.id for m in priors] == ["2026-08-10", "2026-08-09"]


def test_lightrag_doc_id_roundtrips(manifest):
    """The doc id must persist: it is what makes replace-on-recompile possible."""
    make_meeting(manifest, "m1", "2026-08-10")
    db.advance(manifest, "m1", db.INDEXED, lightrag_doc_id="doc-abc123")
    assert db.get_meeting(manifest, "m1").lightrag_doc_id == "doc-abc123"


def test_minutes_refresh_requeues_completed_meeting_and_keeps_old_index_id(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.advance(manifest, "m1", db.INDEXED, lightrag_doc_id="doc-old")

    assert db.queue_minutes_refresh(manifest, ["m1", "m1"]) == 1

    meeting = db.get_meeting(manifest, "m1")
    assert meeting.status == db.SPEAKERS_RESOLVED
    assert meeting.lightrag_doc_id == "doc-old"


# ── Stage-run failure visibility ────────────────────────────────────

def test_recent_stage_failures_reports_failed_runs_with_meeting_context(manifest):
    make_meeting(manifest, "m1", "2026-08-10", title_hint="Roadmap")
    run_id = db.start_stage(manifest, "m1", "transcribe")
    db.finish_stage(manifest, run_id, False, "OSError: ffmpeg not found")

    failures = db.recent_stage_failures(manifest)
    assert len(failures) == 1
    row = failures[0]
    assert row["stage"] == "transcribe"
    assert row["detail"] == "OSError: ffmpeg not found"
    assert row["label"] == db.get_meeting(manifest, "m1").label


def test_recent_stage_failures_excludes_successful_runs(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    ok_run = db.start_stage(manifest, "m1", "transcribe")
    db.finish_stage(manifest, ok_run, True, "12 segments")

    assert db.recent_stage_failures(manifest) == []


def test_recent_stage_failures_survives_a_later_successful_retry(manifest):
    """The whole point of this query: a meeting that failed once and was fixed
    on retry must still show up here, even though its CURRENT status is
    healthy and pending(conn, FAILED) no longer sees it anywhere."""
    make_meeting(manifest, "m1", "2026-08-10")
    failed_run = db.start_stage(manifest, "m1", "transcribe")
    db.finish_stage(manifest, failed_run, False, "TimeoutError: replicate")
    db.mark_failed(manifest, "m1", "TimeoutError: replicate")

    db.reset_to(manifest, "m1", db.DISCOVERED)
    ok_run = db.start_stage(manifest, "m1", "transcribe")
    db.finish_stage(manifest, ok_run, True, "12 segments")
    db.advance(manifest, "m1", db.TRANSCRIBED, transcript_path="/t.json")

    assert db.get_meeting(manifest, "m1").status == db.TRANSCRIBED
    assert db.pending(manifest, db.FAILED) == [], "not visible via current status"

    failures = db.recent_stage_failures(manifest)
    assert len(failures) == 1
    assert failures[0]["detail"] == "TimeoutError: replicate"


def test_recent_stage_failures_orders_newest_first_and_respects_limit(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    for i in range(3):
        run_id = db.start_stage(manifest, "m1", "transcribe")
        db.finish_stage(manifest, run_id, False, f"failure {i}")

    failures = db.recent_stage_failures(manifest, limit=2)
    assert [f["detail"] for f in failures] == ["failure 2", "failure 1"]


# ── Delete ────────────────────────────────────────────────────────────

def test_delete_meeting_clears_seen_files(manifest):
    """Without this, ingest.file_unchanged matches the deleted meeting's source
    path/size/mtime forever, and a file still sitting in inbox/ can never be
    re-ingested - silently and permanently."""
    make_meeting(manifest, "m1", "2026-08-10")
    manifest.execute(
        "INSERT INTO seen_files (path, size, mtime, meeting_id, seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("/inbox/m1.m4a", 123, 456, "m1", db.now_iso()),
    )
    manifest.commit()

    assert db.delete_meeting(manifest, "m1") is True

    row = manifest.execute(
        "SELECT 1 FROM seen_files WHERE meeting_id = ?", ("m1",)
    ).fetchone()
    assert row is None


def test_migration_adds_column_to_existing_manifest(tmp_path):
    """init_db must upgrade a manifest created before lightrag_doc_id existed.

    CREATE TABLE IF NOT EXISTS silently skips an existing table, so without an
    explicit migration an upgraded install keeps the old shape and fails on the
    first write.
    """
    db_path = tmp_path / "old.db"
    legacy = db.SCHEMA.replace("    lightrag_doc_id TEXT,\n", "")
    with db.connect(db_path) as conn:
        conn.executescript(legacy)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(meetings)")}
        assert "lightrag_doc_id" not in cols

    db.init_db(db_path)
    with db.connect(db_path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(meetings)")}
        assert "lightrag_doc_id" in cols

    db.init_db(db_path)  # must be idempotent


def test_speaker_names_persist_and_rank(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    make_meeting(manifest, "m2", "2026-08-11")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Faraz", "confirmed")
    db.set_speaker(manifest, "m1", "SPEAKER_01", None, "unknown")
    db.set_speaker(manifest, "m2", "SPEAKER_00", "Faraz", "confirmed")
    db.set_speaker(manifest, "m2", "SPEAKER_01", "Ali", "inferred")

    assert db.get_speakers(manifest, "m1") == {"SPEAKER_00": "Faraz"}
    assert db.known_speaker_names(manifest)[0] == "Faraz"


def test_set_speaker_upserts(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.set_speaker(manifest, "m1", "SPEAKER_00", None, "unknown")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Faraz", "confirmed")
    assert db.get_speakers(manifest, "m1") == {"SPEAKER_00": "Faraz"}


# ── Ask AI conversation history ──────────────────────────────────────

def _turn(conn, session_id, question="q", answer="a", **overrides):
    fields = dict(
        mode="hybrid", provider="gemini", synthesized=True,
        context_chars=100, retrieval_sec=1.0, synthesis_sec=2.0,
    )
    fields.update(overrides)
    return db.append_chat_turn(conn, session_id, question=question, answer=answer, **fields)


def test_append_chat_turn_indexes_from_zero_per_session(manifest):
    assert _turn(manifest, "s1", question="first") == 0
    assert _turn(manifest, "s1", question="second") == 1
    # A second session starts its own sequence rather than continuing s1's.
    assert _turn(manifest, "s2", question="other session") == 0


def test_recent_chat_turns_oldest_first(manifest):
    _turn(manifest, "s1", question="q0", answer="a0")
    _turn(manifest, "s1", question="q1", answer="a1")
    _turn(manifest, "s1", question="q2", answer="a2")

    turns = db.recent_chat_turns(manifest, "s1", limit=6)
    assert [t["question"] for t in turns] == ["q0", "q1", "q2"]


def test_recent_chat_turns_respects_limit_and_keeps_the_newest(manifest):
    for i in range(5):
        _turn(manifest, "s1", question=f"q{i}", answer=f"a{i}")

    turns = db.recent_chat_turns(manifest, "s1", limit=2)
    assert [t["question"] for t in turns] == ["q3", "q4"], "must be the newest, oldest-first"


def test_recent_chat_turns_does_not_leak_between_sessions(manifest):
    _turn(manifest, "s1", question="mine")
    _turn(manifest, "s2", question="not mine")
    assert [t["question"] for t in db.recent_chat_turns(manifest, "s1")] == ["mine"]


def test_recent_chat_turns_empty_session_returns_empty_list(manifest):
    assert db.recent_chat_turns(manifest, "never-seen") == []


def test_clear_chat_session_removes_only_that_session(manifest):
    _turn(manifest, "s1", question="keep-gone")
    _turn(manifest, "s2", question="keep-me")

    removed = db.clear_chat_session(manifest, "s1")

    assert removed == 1
    assert db.recent_chat_turns(manifest, "s1") == []
    assert [t["question"] for t in db.recent_chat_turns(manifest, "s2")] == ["keep-me"]


def test_append_chat_turn_rejects_duplicate_index_for_same_session(manifest):
    """UNIQUE(session_id, turn_index) is the race-safety net behind the
    COALESCE(MAX...) read in append_chat_turn."""
    _turn(manifest, "s1")
    with pytest.raises(sqlite3.IntegrityError):
        manifest.execute(
            "INSERT INTO chat_turns "
            "(session_id, turn_index, question, answer, synthesized, created_at) "
            "VALUES ('s1', 0, 'dup', 'dup', 1, ?)",
            (db.now_iso(),),
        )
