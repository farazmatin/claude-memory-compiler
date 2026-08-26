"""Interface tests for the deep people-merge module."""

from __future__ import annotations

import hashlib
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


def test_preview_digest_changes_when_a_source_alias_changes(manifest, monkeypatch):
    db.add_person(manifest, "Mike")
    _use_manifest(manifest, monkeypatch)
    before = people_merge.preview(["Mike"], "Michael")

    db.add_person(manifest, "Mike", aliases=["Mikey"])
    manifest.commit()
    after = people_merge.preview(["Mike"], "Michael")

    assert after.digest != before.digest


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
