"""Digest-bound repair of merge damage created before hidden tombstones."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pipeline import db, people_merge

from .conftest import make_meeting


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_legacy_preview_is_exact_and_non_mutating(manifest, tmp_path):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Mike and Michael agreed.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-26")
    db.advance(
        manifest,
        "m1",
        db.SPEAKERS_RESOLVED,
        minutes_path=str(minutes_path),
    )
    db.add_person(manifest, "Michael", aliases=["Mike"])
    db.upsert_speaker_match(
        manifest,
        "m1",
        "SPEAKER_00",
        best_canonical="Mike",
        next_canonical="Unknown Ghost",
    )
    manifest.commit()
    manifest_path = Path(
        manifest.execute("PRAGMA database_list").fetchone()["file"]
    )
    manifest_before = _sha256(manifest_path)
    minutes_before = _sha256(minutes_path)

    result = people_merge.preview_legacy_repair()

    assert result.mappings == (("mike", "Michael"),)
    assert len(result.suggestion_updates) == 1
    assert len(result.proposed_clears) == 1
    assert result.files_changed == 1
    assert result.literal_matches == 1
    assert len(result.digest) == 64
    assert _sha256(manifest_path) == manifest_before
    assert _sha256(minutes_path) == minutes_before


def test_legacy_preview_writes_only_the_requested_private_artifact(
    manifest, tmp_path
):
    db.add_person(manifest, "Michael", aliases=["Mike"])
    manifest.commit()
    preview = people_merge.preview_legacy_repair()
    output = tmp_path / "private" / "repair.json"

    written = people_merge.write_legacy_repair_preview(preview, output)

    artifact = json.loads(written.read_text(encoding="utf-8"))
    assert written == output.resolve()
    assert artifact["digest"] == preview.digest
    assert artifact["proposal"]["database_path"] == preview.database_path
    assert [path for path in tmp_path.rglob("*.json")] == [output]


def test_legacy_apply_uses_only_the_approved_plan_and_is_idempotent(
    manifest, tmp_path
):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Mike owns Atlas.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-26")
    db.advance(
        manifest,
        "m1",
        db.SPEAKERS_RESOLVED,
        minutes_path=str(minutes_path),
        lightrag_doc_id="doc-old",
    )
    db.add_person(manifest, "Michael", aliases=["Mike", "Mikey"])
    db.upsert_speaker_match(
        manifest,
        "m1",
        "SPEAKER_00",
        best_canonical="Mike",
        best_score=0.9,
        next_canonical="Michael",
        next_score=0.8,
    )
    manifest.commit()
    preview = people_merge.preview_legacy_repair(excluded_aliases=["mikey"])
    artifact = people_merge.write_legacy_repair_preview(
        preview, tmp_path / "repair.json"
    )

    first = people_merge.apply_legacy_repair(
        artifact, expected_digest=preview.digest
    )
    second = people_merge.apply_legacy_repair(
        artifact, expected_digest=preview.digest
    )

    match = db.get_speaker_match(manifest, "m1", "SPEAKER_00")
    meeting = db.get_meeting(manifest, "m1")
    aliases = {
        row["alias"]
        for row in manifest.execute(
            "SELECT alias FROM person_aliases WHERE canonical = 'Michael'"
        )
    }
    assert first.aliases_deleted == 1
    assert first.suggestions_rewritten == 1
    assert first.minutes_rewritten == 1
    assert second.already_applied is True
    assert aliases == {"michael", "mikey"}
    assert db.resolve_merged_name(manifest, "Mike") == "Michael"
    assert (match["best_canonical"], match["next_canonical"]) == ("Michael", None)
    assert minutes_path.read_text(encoding="utf-8") == "Michael owns Atlas."
    assert meeting.status == db.MINUTES_COMPILED
    assert meeting.lightrag_doc_id == "doc-old"


def test_legacy_apply_rejects_file_drift_before_mutating_the_manifest(
    manifest, tmp_path
):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Mike owns Atlas.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-26")
    db.advance(
        manifest,
        "m1",
        db.SPEAKERS_RESOLVED,
        minutes_path=str(minutes_path),
    )
    db.add_person(manifest, "Michael", aliases=["Mike"])
    manifest.commit()
    preview = people_merge.preview_legacy_repair()
    artifact = people_merge.write_legacy_repair_preview(
        preview, tmp_path / "repair.json"
    )
    minutes_path.write_text("Owner changed this after preview.", encoding="utf-8")
    manifest_path = Path(
        manifest.execute("PRAGMA database_list").fetchone()["file"]
    )
    manifest_before = _sha256(manifest_path)

    with pytest.raises(people_merge.PreviewDriftError, match="preview changed"):
        people_merge.apply_legacy_repair(
            artifact, expected_digest=preview.digest
        )

    assert _sha256(manifest_path) == manifest_before
    assert db.canonical_name(manifest, "Mike") == "Michael"
    assert db.resolve_merged_name(manifest, "Mike") is None


def test_legacy_apply_promotes_a_valid_runner_up_when_best_is_cleared(
    manifest, tmp_path
):
    make_meeting(manifest, "m1", "2026-08-26")
    db.advance(manifest, "m1", db.SPEAKERS_RESOLVED)
    db.add_person(manifest, "Michael", aliases=["Mike"])
    db.upsert_speaker_match(
        manifest,
        "m1",
        "SPEAKER_00",
        best_canonical="Unknown Ghost",
        best_score=0.9,
        next_canonical="Michael",
        next_score=0.8,
    )
    manifest.commit()
    preview = people_merge.preview_legacy_repair()
    artifact = people_merge.write_legacy_repair_preview(
        preview, tmp_path / "repair.json"
    )

    result = people_merge.apply_legacy_repair(
        artifact, expected_digest=preview.digest
    )

    match = db.get_speaker_match(manifest, "m1", "SPEAKER_00")
    assert result.suggestions_cleared == 1
    assert (match["best_canonical"], match["best_score"]) == ("Michael", 0.8)
    assert (match["next_canonical"], match["next_score"]) == (None, None)


def test_legacy_preview_excludes_only_requested_alias_keys(manifest, tmp_path):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("MIKE and MAY attended.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-26")
    db.advance(
        manifest,
        "m1",
        db.SPEAKERS_RESOLVED,
        minutes_path=str(minutes_path),
    )
    db.add_person(manifest, "Michael", aliases=["Mike"])
    db.add_person(manifest, "May Chen", aliases=["May"])
    manifest.commit()

    result = people_merge.preview_legacy_repair(excluded_aliases=["MIKE"])

    assert result.mappings == (("may", "May Chen"),)
    assert result.file_rewrites[0]["mappings"] == (("MAY", "May Chen"),)
    assert result.conflicts == ("Common-word alias 'may' matched 1 literals",)


def test_legacy_apply_counts_missing_and_null_minutes(manifest, tmp_path):
    for meeting_id, path in (("missing", tmp_path / "gone.md"), ("null", None)):
        make_meeting(manifest, meeting_id, "2026-08-26")
        db.advance(
            manifest,
            meeting_id,
            db.SPEAKERS_RESOLVED,
            minutes_path=str(path) if path else None,
        )
    db.add_person(manifest, "Michael", aliases=["Mike"])
    manifest.commit()
    preview = people_merge.preview_legacy_repair()
    artifact = people_merge.write_legacy_repair_preview(
        preview, tmp_path / "repair.json"
    )

    result = people_merge.apply_legacy_repair(
        artifact, expected_digest=preview.digest
    )

    assert preview.missing_files == ("missing", "null")
    assert result.minutes_missing == 2
    assert {
        row["meeting_id"]: row["state"]
        for row in manifest.execute("SELECT meeting_id, state FROM minute_rewrite_jobs")
    } == {"missing": "missing", "null": "missing"}
