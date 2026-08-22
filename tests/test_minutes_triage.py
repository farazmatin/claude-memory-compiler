"""Junk-recording triage in `pipeline minutes`: park short/empty meetings
before they cost an LLM compile, and let --force override deliberately."""

from __future__ import annotations

import argparse

from pipeline import asr, cli, compile_minutes, db
from pipeline.asr import Segment, Transcript

from .conftest import make_meeting


def make_transcript(meeting_id: str, duration_sec: float, word_count: int) -> Transcript:
    """A one-segment transcript with an exact, controllable word count."""
    text = " ".join(["word"] * word_count) if word_count else ""
    return Transcript(
        meeting_id=meeting_id,
        model="test",
        language="en",
        duration_sec=duration_sec,
        segments=[Segment(start=0.0, end=duration_sec, text=text, speaker="SPEAKER_00")]
        if text
        else [],
    )


def minutes_args(**overrides) -> argparse.Namespace:
    defaults = dict(limit=None, recompile=False, traceback=False, force=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def setup_meeting(
    manifest, monkeypatch, tmp_path, meeting_id: str, *, duration_sec: float, word_count: int
) -> db.Meeting:
    """A meeting at speakers_resolved with a real, retained transcript on disk.

    Mirrors what the transcribe/speakers stages actually leave behind, so
    cmd_minutes's asr.load_transcript(meeting.id) call reads real bytes rather
    than being stubbed out from under it.
    """
    monkeypatch.setattr(asr, "TRANSCRIPTS_DIR", tmp_path)
    transcript = make_transcript(meeting_id, duration_sec, word_count)
    asr.save_transcript(transcript)

    meeting = make_meeting(
        manifest,
        meeting_id,
        "2026-08-10",
        duration_sec=duration_sec,
        status=db.SPEAKERS_RESOLVED,
        transcript_path=str(asr.transcript_paths(meeting_id)[0]),
    )
    # cmd_minutes opens its own connections; the insert above must be durable
    # before it can see it (same reasoning as test_dashboard.py).
    manifest.commit()
    return meeting


def install_fake_complete(monkeypatch) -> list[str]:
    """Stand in for the LLM so a "normal"/"forced" compile never calls it for
    real - the constraint on this task is stricter than usual: no LLM calls."""
    calls: list[str] = []

    def fake_complete(prompt, order=None):
        calls.append(prompt)
        return "---\ndate: 2026-08-10\ntitle: Test\n---\n# Test\n\nbody"

    monkeypatch.setattr(compile_minutes, "complete", fake_complete)
    return calls


def test_short_duration_meeting_is_skipped(manifest, monkeypatch, tmp_path):
    """Under the duration floor alone (plenty of words) still parks - duration
    and word count are independent gates, either one is enough."""
    calls = install_fake_complete(monkeypatch)
    meeting_id = "a" * 64
    setup_meeting(manifest, monkeypatch, tmp_path, meeting_id, duration_sec=5.0, word_count=200)

    assert cli.cmd_minutes(minutes_args()) == 0
    assert calls == [], "no LLM call for a junk recording"

    with db.connect() as conn:
        updated = db.get_meeting(conn, meeting_id)
        run = conn.execute(
            "SELECT detail, ok FROM stage_runs WHERE meeting_id = ? AND stage = 'minutes'",
            (meeting_id,),
        ).fetchone()

    assert updated.status == db.FAILED, "parked, not silently dropped"
    assert "5s" in updated.error
    assert "--force" in updated.error

    assert run["ok"] == 0
    assert "junk recording" in run["detail"], "pipeline status can see why"


def test_low_word_count_meeting_is_skipped(manifest, monkeypatch, tmp_path):
    """Under the word-count floor alone (duration is fine) still parks."""
    calls = install_fake_complete(monkeypatch)
    meeting_id = "b" * 64
    setup_meeting(manifest, monkeypatch, tmp_path, meeting_id, duration_sec=300.0, word_count=5)

    assert cli.cmd_minutes(minutes_args()) == 0
    assert calls == [], "no LLM call for a near-empty transcript"

    with db.connect() as conn:
        updated = db.get_meeting(conn, meeting_id)
    assert updated.status == db.FAILED
    assert "5 words" in updated.error


def test_normal_meeting_is_unaffected(manifest, monkeypatch, tmp_path):
    """Clears both floors: compiles exactly as it did before this feature."""
    calls = install_fake_complete(monkeypatch)
    monkeypatch.setattr(compile_minutes, "MINUTES_DIR", tmp_path)
    meeting_id = "c" * 64
    setup_meeting(manifest, monkeypatch, tmp_path, meeting_id, duration_sec=600.0, word_count=300)

    assert cli.cmd_minutes(minutes_args()) == 0
    assert len(calls) == 1, "a real meeting still spends exactly one LLM call"

    with db.connect() as conn:
        updated = db.get_meeting(conn, meeting_id)
    assert updated.status == db.MINUTES_COMPILED
    assert updated.minutes_path and updated.error is None


def test_force_overrides_the_floor(manifest, monkeypatch, tmp_path):
    """--force compiles a short meeting deliberately, e.g. a genuine 90-second
    decision that would otherwise be parked."""
    calls = install_fake_complete(monkeypatch)
    monkeypatch.setattr(compile_minutes, "MINUTES_DIR", tmp_path)
    meeting_id = "d" * 64
    setup_meeting(manifest, monkeypatch, tmp_path, meeting_id, duration_sec=5.0, word_count=3)

    assert cli.cmd_minutes(minutes_args(force=True)) == 0
    assert len(calls) == 1, "--force must still reach the LLM"

    with db.connect() as conn:
        updated = db.get_meeting(conn, meeting_id)
    assert updated.status == db.MINUTES_COMPILED, "compiled despite being under the floor"
