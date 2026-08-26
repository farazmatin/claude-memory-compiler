"""Durable merge tombstones and minutes rewrite job schema."""

from __future__ import annotations

import sqlite3

import pytest

from pipeline import db

from .conftest import make_meeting


def test_tombstone_target_must_be_a_living_person(manifest):
    with pytest.raises(ValueError, match="living person"):
        db.flatten_and_record_merge(manifest, "Faraz", "Missing")


def test_living_tombstone_target_cannot_be_deleted(manifest):
    db.add_person(manifest, "Michael")
    db.flatten_and_record_merge(manifest, "Mike", "Michael")

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        manifest.execute("DELETE FROM people WHERE canonical = 'Michael'")


def test_tombstone_lookup_is_case_insensitive_and_retains_display_spelling(manifest):
    db.add_person(manifest, "Michael")

    db.flatten_and_record_merge(manifest, "  Mike McKay  ", "Michael")

    assert db.resolve_merged_name(manifest, "mike mckay") == "Michael"
    assert db.resolve_merged_name(manifest, "MIKE MCKAY") == "Michael"
    row = manifest.execute("SELECT old_key, old_spelling FROM merged_names").fetchone()
    assert dict(row) == {"old_key": "mike mckay", "old_spelling": "Mike McKay"}


def test_chained_merge_is_flattened_before_intermediate_person_is_deleted(manifest):
    for name in ("A", "B", "C"):
        db.add_person(manifest, name)

    db.flatten_and_record_merge(manifest, "A", "B")
    manifest.execute("DELETE FROM people WHERE canonical = 'A'")
    db.flatten_and_record_merge(manifest, "B", "C")
    manifest.execute("DELETE FROM people WHERE canonical = 'B'")

    assert db.resolve_merged_name(manifest, "A") == "C"
    assert db.resolve_merged_name(manifest, "B") == "C"
    assert {row["canonical"] for row in manifest.execute("SELECT canonical FROM merged_names")} == {
        "C"
    }


def test_normal_merge_writes_cannot_create_a_cycle(manifest):
    db.add_person(manifest, "A")
    db.add_person(manifest, "B")
    db.flatten_and_record_merge(manifest, "A", "B")

    with pytest.raises(ValueError, match="tombstoned"):
        db.flatten_and_record_merge(manifest, "B", "A")


def test_resolver_follows_a_manually_created_chain_defensively(manifest):
    for name in ("A", "B", "C"):
        db.add_person(manifest, name)
    manifest.executemany(
        "INSERT INTO merged_names VALUES (?, ?, ?, ?)",
        [
            ("a", "A", "B", "2026-08-26T12:00:00Z"),
            ("b", "B", "C", "2026-08-26T12:01:00Z"),
        ],
    )

    assert db.resolve_merged_name(manifest, "A") == "C"


def test_resolver_stops_at_a_manually_created_cycle(manifest):
    for name in ("A", "B"):
        db.add_person(manifest, name)
    manifest.executemany(
        "INSERT INTO merged_names VALUES (?, ?, ?, ?)",
        [
            ("a", "A", "B", "2026-08-26T12:00:00Z"),
            ("b", "B", "A", "2026-08-26T12:01:00Z"),
        ],
    )

    assert db.resolve_merged_name(manifest, "A") is None


def test_tombstoned_spelling_cannot_be_registered_as_a_living_person(manifest):
    db.add_person(manifest, "Michael")
    db.flatten_and_record_merge(manifest, "Mike", "Michael")

    with pytest.raises(ValueError, match="tombstoned"):
        db.add_person(manifest, "Mike")


def test_affected_meetings_cover_every_person_bearing_table(manifest):
    expected = {
        "speaker",
        "entity",
        "relation-subject",
        "relation-object",
        "commitment",
        "decision",
        "open-question",
    }
    for meeting_id in {*expected, "unrelated"}:
        make_meeting(manifest, meeting_id, "2026-08-26")

    db.set_speaker(manifest, "speaker", "SPEAKER_00", "Mike", "confirmed")
    db.replace_entities(
        manifest,
        "entity",
        [{"name": "Mike", "kind": "person"}],
        [],
    )
    db.replace_entities(
        manifest,
        "relation-subject",
        [],
        [{"subject": "Mike", "predicate": "owns", "object": "Atlas"}],
    )
    db.replace_entities(
        manifest,
        "relation-object",
        [],
        [{"subject": "Atlas", "predicate": "owned by", "object": "Mike"}],
    )
    db.replace_commitments(manifest, "commitment", [{"text": "Ship it", "owner": "Mike"}])
    db.replace_decisions(manifest, "decision", [{"text": "Ship it", "decided_by": "Mike"}])
    db.replace_open_questions(
        manifest, "open-question", [{"text": "Who ships it?", "owner": "Mike"}]
    )
    db.set_speaker(manifest, "unrelated", "SPEAKER_00", "Michael", "confirmed")

    assert db.affected_meeting_ids(manifest, ["Mike"]) == expected


def test_rewrite_job_retains_before_and_after_text_and_hashes(manifest):
    make_meeting(manifest, "m1", "2026-08-26")
    manifest.execute(
        """
        INSERT INTO minute_rewrite_jobs (
            id, operation_id, meeting_id, minutes_path, mappings_json,
            before_sha256, after_sha256, before_text, after_text,
            state, created_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "job-1",
            "op-1",
            "m1",
            "/minutes/m1.md",
            '{"Mike":"Michael"}',
            "before-hash",
            "after-hash",
            "# Mike spoke",
            "# Michael spoke",
            "applied",
            "2026-08-26T12:00:00Z",
            "2026-08-26T12:01:00Z",
        ),
    )

    row = manifest.execute("SELECT * FROM minute_rewrite_jobs WHERE id = 'job-1'").fetchone()
    assert row["before_sha256"] == "before-hash"
    assert row["after_sha256"] == "after-hash"
    assert row["before_text"] == "# Mike spoke"
    assert row["after_text"] == "# Michael spoke"


def test_missing_minutes_path_is_representable_as_a_missing_job(manifest):
    make_meeting(manifest, "m1", "2026-08-26")
    manifest.execute(
        """
        INSERT INTO minute_rewrite_jobs (
            id, operation_id, meeting_id, minutes_path, mappings_json,
            state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("job-missing", "op-1", "m1", None, "{}", "missing", "2026-08-26T12:00:00Z"),
    )

    row = manifest.execute(
        "SELECT minutes_path, state FROM minute_rewrite_jobs WHERE id = 'job-missing'"
    ).fetchone()
    assert dict(row) == {"minutes_path": None, "state": "missing"}
