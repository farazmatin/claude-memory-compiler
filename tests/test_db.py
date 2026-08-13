"""Manifest state machine, recompile selection, and prior-context windowing.

Several of these cover defects found in adversarial review; each such test names
the failure it prevents.
"""

from __future__ import annotations

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
