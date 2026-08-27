"""Interface tests for the deep people-merge module."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from pipeline import config, db, people_merge

from .conftest import make_meeting


def _use_manifest(manifest, monkeypatch):
    db_path = manifest.execute("PRAGMA database_list").fetchone()["file"]
    manifest.commit()
    monkeypatch.setattr(config, "DB_PATH", db_path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_rewrite_job(
    manifest,
    *,
    job_id: str,
    meeting_id: str,
    minutes_path: Path | None,
    before: str | None,
    after: str | None,
    state: str = "pending",
):
    def digest(text: str | None) -> str | None:
        return (
            hashlib.sha256(text.encode("utf-8")).hexdigest()
            if text is not None
            else None
        )

    manifest.execute(
        """
        INSERT INTO minute_rewrite_jobs
            (id, operation_id, meeting_id, minutes_path, mappings_json,
             before_sha256, after_sha256, before_text, after_text,
             state, created_at)
        VALUES (?, 'operation', ?, ?, '{"Alice":"Bob"}', ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            meeting_id,
            str(minutes_path) if minutes_path else None,
            digest(before),
            digest(after),
            before,
            after,
            state,
            config.now_iso(),
        ),
    )


def test_preview_retains_a_selected_living_target(manifest, monkeypatch):
    db.add_person(manifest, "Mike", role="Engineer")
    db.add_person(manifest, "Michael", role="Lead")
    _use_manifest(manifest, monkeypatch)

    result = people_merge.preview(["Mike", "Michael"], "Michael")

    assert result.requested_target == "Michael"
    assert result.actual_target == "Michael"
    assert result.source_names == ("Mike", "Michael")
    assert len(result.digest) == 64


def test_preview_resolves_a_tombstoned_target(manifest, monkeypatch):
    db.add_person(manifest, "Mike")
    db.add_person(manifest, "Michael")
    db.flatten_and_record_merge(manifest, "Mikey", "Michael")
    _use_manifest(manifest, monkeypatch)

    result = people_merge.preview(["Mike"], "Mikey")

    assert result.requested_target == "Mikey"
    assert result.actual_target == "Michael"


def test_preview_supports_a_read_only_pre_migration_manifest(manifest, monkeypatch):
    db.add_person(manifest, "Mike")
    manifest.execute("DROP TABLE merged_names")
    _use_manifest(manifest, monkeypatch)
    manifest_path = Path(config.DB_PATH)
    manifest_before = _sha256(manifest_path)

    result = people_merge.preview(["Mike"], "Michael")

    assert result.actual_target == "Michael"
    assert len(result.digest) == 64
    assert _sha256(manifest_path) == manifest_before


def test_preview_rejects_a_missing_source(manifest, monkeypatch):
    db.add_person(manifest, "Michael")
    _use_manifest(manifest, monkeypatch)

    with pytest.raises(ValueError, match="source does not exist: 'Mike'"):
        people_merge.preview(["Mike"], "Michael")


def test_preview_rejects_a_blank_target(manifest, monkeypatch):
    db.add_person(manifest, "Mike")
    _use_manifest(manifest, monkeypatch)

    with pytest.raises(ValueError, match="target must not be blank"):
        people_merge.preview(["Mike"], "  ")


def test_preview_rejects_duplicate_sources(manifest, monkeypatch):
    db.add_person(manifest, "Mike")
    _use_manifest(manifest, monkeypatch)

    with pytest.raises(ValueError, match="sources must be unique"):
        people_merge.preview(["Mike", "Mike"], "Michael")


def test_preview_requires_at_least_one_source(manifest, monkeypatch):
    _use_manifest(manifest, monkeypatch)

    with pytest.raises(ValueError, match="at least one source is required"):
        people_merge.preview([], "Michael")


def test_preview_counts_the_full_affected_meeting_union(manifest, monkeypatch):
    for meeting_id in ("speaker", "entity", "relation", "commitment", "decision", "question"):
        make_meeting(manifest, meeting_id, "2026-08-26")
    db.add_person(manifest, "Mike")
    db.set_speaker(manifest, "speaker", "SPEAKER_00", "Mike", "confirmed")
    db.replace_entities(manifest, "entity", [{"name": "Mike"}], [])
    db.replace_entities(
        manifest,
        "relation",
        [],
        [{"subject": "Atlas", "predicate": "owned by", "object": "Mike"}],
    )
    db.replace_commitments(manifest, "commitment", [{"text": "Ship", "owner": "Mike"}])
    db.replace_decisions(manifest, "decision", [{"text": "Ship", "decided_by": "Mike"}])
    db.replace_open_questions(manifest, "question", [{"text": "Ship?", "owner": "Mike"}])
    _use_manifest(manifest, monkeypatch)

    result = people_merge.preview(["Mike"], "Michael")

    assert result.affected_meetings == 6


def test_preview_counts_only_speaker_rows_that_will_change(manifest, monkeypatch):
    for meeting_id, name in (("m1", "Mike"), ("m2", "Mike"), ("m3", "Michael")):
        make_meeting(manifest, meeting_id, "2026-08-26")
        db.set_speaker(manifest, meeting_id, "SPEAKER_00", name, "confirmed")
    db.add_person(manifest, "Mike")
    db.add_person(manifest, "Michael")
    _use_manifest(manifest, monkeypatch)

    result = people_merge.preview(["Mike", "Michael"], "Michael")

    assert result.speaker_rows == 2
    assert result.affected_meetings == 2


def test_preview_rejects_a_merge_with_no_absorbed_source(manifest, monkeypatch):
    db.add_person(manifest, "Michael")
    _use_manifest(manifest, monkeypatch)

    with pytest.raises(ValueError, match="merge would not change any source"):
        people_merge.preview(["Michael"], "Michael")


def test_preview_reports_literal_rewrites_and_missing_minutes(
    manifest, monkeypatch, tmp_path
):
    changed_path = tmp_path / "changed.md"
    changed_path.write_text("Mike and Michael agreed. Mike's action.", encoding="utf-8")
    missing_path = tmp_path / "missing.md"

    for meeting_id, minutes_path in (
        ("changed", changed_path),
        ("missing", missing_path),
        ("null", None),
    ):
        make_meeting(manifest, meeting_id, "2026-08-26")
        db.set_speaker(manifest, meeting_id, "SPEAKER_00", "Mike", "confirmed")
        db.advance(
            manifest,
            meeting_id,
            db.MINUTES_COMPILED,
            minutes_path=str(minutes_path) if minutes_path else None,
        )
    db.add_person(manifest, "Mike")
    _use_manifest(manifest, monkeypatch)

    result = people_merge.preview(["Mike"], "Michael")

    assert result.files_changed == 1
    assert result.literal_matches == 2
    assert result.missing_files == ("missing", "null")


def test_preview_flags_common_word_matches_for_human_review(
    manifest, monkeypatch, tmp_path
):
    minutes_path = tmp_path / "may.md"
    minutes_path.write_text("May may leave in May.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-26")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "May", "confirmed")
    db.advance(manifest, "m1", db.MINUTES_COMPILED, minutes_path=str(minutes_path))
    db.add_person(manifest, "May")
    _use_manifest(manifest, monkeypatch)

    result = people_merge.preview(["May"], "May Chen")

    assert result.literal_matches == 2
    assert result.conflicts == ("Common-word source 'May' matched 2 literals",)


def test_preview_counts_common_word_matches_without_other_sources(
    manifest, monkeypatch, tmp_path
):
    for meeting_id, name, text in (
        ("may", "May", "May approved."),
        ("john", "John", "John asked John, then John approved."),
    ):
        minutes_path = tmp_path / f"{meeting_id}.md"
        minutes_path.write_text(text, encoding="utf-8")
        make_meeting(manifest, meeting_id, "2026-08-26")
        db.set_speaker(manifest, meeting_id, "SPEAKER_00", name, "confirmed")
        db.advance(
            manifest,
            meeting_id,
            db.MINUTES_COMPILED,
            minutes_path=str(minutes_path),
        )
        db.add_person(manifest, name)
    _use_manifest(manifest, monkeypatch)

    result = people_merge.preview(["May", "John"], "Review Team")

    assert result.literal_matches == 4
    assert result.conflicts == ("Common-word source 'May' matched 1 literals",)


def test_preview_digest_changes_when_a_source_alias_changes(manifest, monkeypatch):
    db.add_person(manifest, "Mike")
    _use_manifest(manifest, monkeypatch)
    before = people_merge.preview(["Mike"], "Michael")

    db.add_person(manifest, "Mike", aliases=["Mikey"])
    manifest.commit()
    after = people_merge.preview(["Mike"], "Michael")

    assert after.digest != before.digest


def test_merge_rejects_source_row_drift_inside_an_already_affected_meeting(
    manifest, monkeypatch
):
    make_meeting(manifest, "m1", "2026-08-26")
    db.add_person(manifest, "Mike")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Mike", "confirmed")
    _use_manifest(manifest, monkeypatch)
    approved = people_merge.preview(["Mike"], "Michael")

    db.replace_commitments(
        manifest,
        "m1",
        [{"text": "Ship Atlas", "owner": "Mike"}],
    )
    manifest.commit()

    with pytest.raises(people_merge.PreviewDriftError):
        people_merge.merge(["Mike"], "Michael", expected_digest=approved.digest)

    assert [person["canonical"] for person in db.list_people(manifest)] == ["Mike"]


def test_preview_accepts_an_existing_target_outside_the_selection(manifest, monkeypatch):
    db.add_person(manifest, "Mike")
    db.add_person(manifest, "Michael")
    _use_manifest(manifest, monkeypatch)

    result = people_merge.preview(["Mike"], "Michael")

    assert result.actual_target == "Michael"
    assert result.source_names == ("Mike",)


def test_preview_is_stable_and_byte_for_byte_non_mutating(
    manifest, monkeypatch, tmp_path
):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_bytes(b"Mike owns Atlas.\r\n")
    make_meeting(manifest, "m1", "2026-08-26")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Mike", "confirmed")
    db.advance(manifest, "m1", db.INDEXED, minutes_path=str(minutes_path))
    db.add_person(manifest, "Mike")
    _use_manifest(manifest, monkeypatch)
    manifest_path = Path(config.DB_PATH)
    manifest_before = _sha256(manifest_path)
    minutes_before = _sha256(minutes_path)

    first = people_merge.preview(["Mike"], "Michael")
    second = people_merge.preview(["Mike"], "Michael")

    assert second.digest == first.digest
    assert _sha256(manifest_path) == manifest_before
    assert _sha256(minutes_path) == minutes_before
    assert [person["canonical"] for person in db.list_people(manifest)] == ["Mike"]


def test_preview_digest_changes_when_a_minutes_file_changes(
    manifest, monkeypatch, tmp_path
):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Mike owns Atlas.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-26")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Mike", "confirmed")
    db.advance(manifest, "m1", db.INDEXED, minutes_path=str(minutes_path))
    db.add_person(manifest, "Mike")
    _use_manifest(manifest, monkeypatch)
    before = people_merge.preview(["Mike"], "Michael")

    minutes_path.write_text("Mike owns Atlas and Beacon.", encoding="utf-8")
    after = people_merge.preview(["Mike"], "Michael")

    assert after.digest != before.digest


def test_merge_rejects_preview_drift_before_any_mutation(manifest, monkeypatch):
    db.add_person(manifest, "Mike")
    _use_manifest(manifest, monkeypatch)
    approved = people_merge.preview(["Mike"], "Michael")
    db.add_person(manifest, "Mike", aliases=["Mikey"])
    manifest.commit()
    manifest_path = Path(config.DB_PATH)
    before = _sha256(manifest_path)

    with pytest.raises(people_merge.PreviewDriftError):
        people_merge.merge(["Mike"], "Michael", expected_digest=approved.digest)

    assert _sha256(manifest_path) == before
    assert [person["canonical"] for person in db.list_people(manifest)] == ["Mike"]


def test_merge_creates_target_inherits_role_and_hides_the_source(
    manifest, monkeypatch
):
    db.add_person(manifest, "Mike", aliases=["Mikey"], role="Engineer")
    _use_manifest(manifest, monkeypatch)
    approved = people_merge.preview(["Mike"], "Michael")

    result = people_merge.merge(
        ["Mike"], "Michael", expected_digest=approved.digest
    )

    assert result.target == "Michael"
    assert db.list_people(manifest) == [
        {"canonical": "Michael", "role": "Engineer", "aliases": "mikey", "meetings": 0}
    ]
    assert db.resolve_merged_name(manifest, "Mike") == "Michael"
    assert db.canonical_name(manifest, "Mikey") == "Michael"
    assert manifest.execute(
        "SELECT 1 FROM person_aliases WHERE alias = 'mike'"
    ).fetchone() is None


def test_merge_rewrites_every_person_bearing_record_and_queues_minutes(
    manifest, monkeypatch, tmp_path
):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Alice owns Atlas.", encoding="utf-8")
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_bytes(b'{"speaker":"Alice"}')
    transcript_before = _sha256(transcript_path)

    make_meeting(manifest, "m1", "2026-08-26")
    db.advance(
        manifest,
        "m1",
        db.INDEXED,
        minutes_path=str(minutes_path),
        transcript_path=str(transcript_path),
        lightrag_doc_id="doc-old",
    )
    db.add_person(manifest, "Alice", aliases=["Ally"], role="Engineer")
    db.add_person(manifest, "Bob", role="Director")
    db.flatten_and_record_merge(manifest, "Old Alice", "Alice")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Alice", "confirmed")
    db.upsert_speaker_match(
        manifest,
        "m1",
        "SPEAKER_00",
        best_canonical="Alice",
        best_score=0.9,
        next_canonical="Bob",
        next_score=0.8,
        resolved_as="Alice",
    )
    db.replace_clusters(
        manifest,
        [
            {
                "id": "cluster-1",
                "size": 1,
                "total_speech": 30.0,
                "best_canonical": "Alice",
                "best_score": 0.9,
                "next_canonical": "Bob",
                "next_score": 0.8,
                "band": "review",
                "members": [("m1", "SPEAKER_00")],
            }
        ],
    )
    db.add_voice_sample(
        manifest,
        canonical="Alice",
        meeting_id="m1",
        label="SPEAKER_00",
        embedding=b"voice",
        dim=1,
        model="test",
        speech_sec=30.0,
    )
    db.replace_entities(
        manifest,
        "m1",
        [{"name": "Alice", "kind": "person"}],
        [{"subject": "Alice", "predicate": "mentors", "object": "Alice"}],
    )
    db.replace_commitments(manifest, "m1", [{"text": "Ship", "owner": "Alice"}])
    db.replace_decisions(manifest, "m1", [{"text": "Ship", "decided_by": "Alice"}])
    db.replace_open_questions(manifest, "m1", [{"text": "Ship?", "owner": "Alice"}])
    _use_manifest(manifest, monkeypatch)
    approved = people_merge.preview(["Alice"], "Bob")

    result = people_merge.merge(["Alice"], "Bob", expected_digest=approved.digest)

    match = db.get_speaker_match(manifest, "m1", "SPEAKER_00")
    cluster = manifest.execute("SELECT * FROM voice_clusters").fetchone()
    job = manifest.execute("SELECT * FROM minute_rewrite_jobs").fetchone()
    meeting = db.get_meeting(manifest, "m1")
    assert result.speaker_rows == 1
    assert db.get_speakers(manifest, "m1") == {"SPEAKER_00": "Bob"}
    assert (match["resolved_as"], match["best_canonical"], match["next_canonical"]) == (
        "Bob",
        "Bob",
        None,
    )
    assert (cluster["best_canonical"], cluster["next_canonical"]) == ("Bob", None)
    assert [row["canonical"] for row in db.person_samples(manifest, "Bob")] == ["Bob"]
    assert [entity["name"] for entity in db.get_entities(manifest, "m1")] == ["Bob"]
    assert db.get_relations(manifest, "m1")[0]["subject"] == "Bob"
    assert db.get_relations(manifest, "m1")[0]["object"] == "Bob"
    assert db.list_commitments(manifest)[0]["owner"] == "Bob"
    assert db.get_decisions(manifest, "m1")[0]["decided_by"] == "Bob"
    assert db.get_open_questions(manifest, "m1")[0]["owner"] == "Bob"
    assert db.resolve_merged_name(manifest, "Old Alice") == "Bob"
    assert db.canonical_name(manifest, "Ally") == "Bob"
    assert job["state"] == "applied"
    assert job["before_text"] == "Alice owns Atlas."
    assert job["after_text"] == "Bob owns Atlas."
    assert meeting.status == db.MINUTES_COMPILED
    assert meeting.lightrag_doc_id == "doc-old"
    assert _sha256(transcript_path) == transcript_before


def test_merge_atomically_rewrites_minutes_and_finishes_the_meeting(
    manifest, monkeypatch, tmp_path
):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Alice owns Atlas.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-26")
    db.advance(
        manifest,
        "m1",
        db.INDEXED,
        minutes_path=str(minutes_path),
        lightrag_doc_id="doc-old",
    )
    db.add_person(manifest, "Alice")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Alice", "confirmed")
    _use_manifest(manifest, monkeypatch)
    approved = people_merge.preview(["Alice"], "Bob")

    result = people_merge.merge(["Alice"], "Bob", expected_digest=approved.digest)

    job = manifest.execute("SELECT state FROM minute_rewrite_jobs").fetchone()
    meeting = db.get_meeting(manifest, "m1")
    assert result.minutes_rewritten == 1
    assert result.pending_rewrites == 0
    assert minutes_path.read_text(encoding="utf-8") == "Bob owns Atlas."
    assert job["state"] == "applied"
    assert meeting.status == db.MINUTES_COMPILED
    assert meeting.lightrag_doc_id == "doc-old"


def test_merge_marks_an_already_correct_minutes_file_unchanged(
    manifest, monkeypatch, tmp_path
):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Atlas is approved.", encoding="utf-8")
    before = _sha256(minutes_path)
    make_meeting(manifest, "m1", "2026-08-26")
    db.advance(manifest, "m1", db.INDEXED, minutes_path=str(minutes_path))
    db.add_person(manifest, "Alice")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Alice", "confirmed")
    _use_manifest(manifest, monkeypatch)
    approved = people_merge.preview(["Alice"], "Bob")

    result = people_merge.merge(["Alice"], "Bob", expected_digest=approved.digest)

    assert result.minutes_unchanged == 1
    assert _sha256(minutes_path) == before
    assert manifest.execute(
        "SELECT state FROM minute_rewrite_jobs"
    ).fetchone()["state"] == "unchanged"
    assert db.get_meeting(manifest, "m1").status == db.MINUTES_COMPILED


def test_merge_reports_missing_and_null_minutes_without_claiming_completion(
    manifest, monkeypatch, tmp_path
):
    for meeting_id, path in (("missing", tmp_path / "gone.md"), ("null", None)):
        make_meeting(manifest, meeting_id, "2026-08-26")
        db.advance(
            manifest,
            meeting_id,
            db.INDEXED,
            minutes_path=str(path) if path else None,
            lightrag_doc_id=f"doc-{meeting_id}",
        )
        db.set_speaker(manifest, meeting_id, "SPEAKER_00", "Alice", "confirmed")
    db.add_person(manifest, "Alice")
    _use_manifest(manifest, monkeypatch)
    approved = people_merge.preview(["Alice"], "Bob")

    result = people_merge.merge(["Alice"], "Bob", expected_digest=approved.digest)

    assert result.minutes_missing == 2
    assert result.pending_rewrites == 0
    assert {
        row["meeting_id"]: row["state"]
        for row in manifest.execute("SELECT meeting_id, state FROM minute_rewrite_jobs")
    } == {"missing": "missing", "null": "missing"}
    for meeting_id in ("missing", "null"):
        meeting = db.get_meeting(manifest, meeting_id)
        assert meeting.status == db.SPEAKERS_RESOLVED
        assert meeting.lightrag_doc_id == f"doc-{meeting_id}"


def test_resume_never_overwrites_a_minutes_file_with_an_unapproved_hash(
    manifest, monkeypatch, tmp_path
):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Owner edited this after preview.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-26")
    db.advance(manifest, "m1", db.SPEAKERS_RESOLVED, minutes_path=str(minutes_path))
    _seed_rewrite_job(
        manifest,
        job_id="job-1",
        meeting_id="m1",
        minutes_path=minutes_path,
        before="Alice owns Atlas.",
        after="Bob owns Atlas.",
    )
    _use_manifest(manifest, monkeypatch)
    before = _sha256(minutes_path)

    result = people_merge.resume_pending_rewrites()

    assert result.rewrite_conflicts == 1
    assert _sha256(minutes_path) == before
    assert manifest.execute(
        "SELECT state FROM minute_rewrite_jobs WHERE id = 'job-1'"
    ).fetchone()["state"] == "conflict"
    assert db.get_meeting(manifest, "m1").status == db.SPEAKERS_RESOLVED


def test_resume_recognizes_a_replacement_completed_before_the_status_update(
    manifest, monkeypatch, tmp_path
):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Bob owns Atlas.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-26")
    db.advance(
        manifest,
        "m1",
        db.SPEAKERS_RESOLVED,
        minutes_path=str(minutes_path),
        lightrag_doc_id="doc-old",
    )
    _seed_rewrite_job(
        manifest,
        job_id="job-1",
        meeting_id="m1",
        minutes_path=minutes_path,
        before="Alice owns Atlas.",
        after="Bob owns Atlas.",
    )
    _use_manifest(manifest, monkeypatch)
    monkeypatch.setattr(
        people_merge.os,
        "replace",
        lambda *_: pytest.fail("an already-applied replacement must not run twice"),
    )

    first = people_merge.resume_pending_rewrites()
    second = people_merge.resume_pending_rewrites()

    assert first.minutes_rewritten == 1
    assert second.minutes_rewritten == 0
    assert manifest.execute(
        "SELECT state FROM minute_rewrite_jobs WHERE id = 'job-1'"
    ).fetchone()["state"] == "applied"
    meeting = db.get_meeting(manifest, "m1")
    assert meeting.status == db.MINUTES_COMPILED
    assert meeting.lightrag_doc_id == "doc-old"


def test_merge_reports_a_failed_atomic_replace_as_pending(
    manifest, monkeypatch, tmp_path
):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Alice owns Atlas.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-26")
    db.advance(manifest, "m1", db.INDEXED, minutes_path=str(minutes_path))
    db.add_person(manifest, "Alice")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Alice", "confirmed")
    _use_manifest(manifest, monkeypatch)
    approved = people_merge.preview(["Alice"], "Bob")
    monkeypatch.setattr(
        people_merge.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("simulated interruption")),
    )

    result = people_merge.merge(["Alice"], "Bob", expected_digest=approved.digest)

    assert result.pending_rewrites == 1
    assert minutes_path.read_text(encoding="utf-8") == "Alice owns Atlas."
    assert manifest.execute(
        "SELECT state FROM minute_rewrite_jobs"
    ).fetchone()["state"] == "pending"
    assert db.get_meeting(manifest, "m1").status == db.SPEAKERS_RESOLVED


def test_atomic_replace_keeps_its_temporary_inside_the_windows_path_limit(
    tmp_path, monkeypatch
):
    """A long minutes name must not push the temporary past MAX_PATH.

    Real minutes filenames run to roughly 175 characters. Embedding that name
    in the temporary alongside a 64-character job id took the path over the
    Windows limit of 260, and Windows reports that as a bare "no such file",
    so every long-named rewrite stalled as pending with no visible cause.
    """
    # Size the name so the file itself is legal but the old temporary, which
    # appended a 64-character job id to it, would not have been.
    stem_length = max(40, 250 - len(str(tmp_path)) - len(".md") - 1)
    target = tmp_path / f"{'a' * stem_length}.md"
    target.write_text("Alice owns Atlas.", encoding="utf-8")
    job_id = "b" * 64
    assert len(str(target.with_name(f".{target.name}.{job_id}.rewrite.tmp"))) > 260

    seen: list[str] = []
    real_replace = os.replace

    def record(source, destination):
        seen.append(str(source))
        return real_replace(source, destination)

    monkeypatch.setattr(people_merge.os, "replace", record)

    people_merge._atomic_replace(target, job_id, "Bob owns Atlas.")

    assert target.read_text(encoding="utf-8") == "Bob owns Atlas."
    assert len(seen) == 1
    # The temporary is named for the job, not the file, so its length is
    # bounded by the directory regardless of how long the minutes name is.
    assert len(seen[0]) < len(str(tmp_path)) + 100
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".rewrite.tmp")]
