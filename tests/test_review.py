"""The review write paths.

These are the first operations in the system that modify a compiled record, so
the tests concentrate on the two ways that can go wrong quietly: a correction
that patches prose instead of rebuilding it, and a re-index that leaves the
previous copy alive in the graph.
"""

from __future__ import annotations

import pytest

from pipeline import db, index, review

from .conftest import make_meeting

MINUTES = """---
date: 2026-08-10
title: Kafka ACL production change order
---

# Kafka ACL production change order

## Decisions
- **Ship the ACL** — decided by Faraz. [00:14:22]

## Entities
- Kafka (project): the broker cluster

## Relations
- Faraz -> owns -> Kafka
"""


@pytest.fixture()
def meeting(manifest, tmp_path):
    path = tmp_path / "2026-08-10-kafka-acl.md"
    path.write_text(MINUTES, encoding="utf-8")
    made = make_meeting(
        manifest, "m1", "2026-08-10", status=db.INDEXED,
        minutes_path=str(path), lightrag_doc_id="doc-old",
    )
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Faraz", "inferred")
    db.set_speaker(manifest, "m1", "SPEAKER_01", None, "unknown")
    return made


# ── queue ────────────────────────────────────────────────────────────

def test_queue_flags_unresolved_speakers(manifest, meeting):
    (item,) = review.queue(manifest)

    assert item.unresolved_labels == ["SPEAKER_01"]
    assert item.needs_attention is True
    assert item.title == "Kafka ACL production change order"


def test_queue_flags_speaker_label_left_in_minutes(manifest, tmp_path):
    path = tmp_path / "leaky.md"
    path.write_text("---\ntitle: Leaky\n---\nSPEAKER_02 said the thing.", encoding="utf-8")
    make_meeting(manifest, "m2", "2026-08-09", status=db.MINUTES_COMPILED, minutes_path=str(path))

    (item,) = review.queue(manifest)

    assert item.unresolved_in_minutes is True
    assert item.needs_attention is True


def test_queue_sorts_meetings_needing_attention_first(manifest, tmp_path):
    clean = tmp_path / "clean.md"
    clean.write_text("---\ntitle: Clean\n---\nAll named.", encoding="utf-8")
    make_meeting(manifest, "clean", "2026-08-12", status=db.INDEXED, minutes_path=str(clean))
    db.set_speaker(manifest, "clean", "SPEAKER_00", "Faraz", "confirmed")

    messy = tmp_path / "messy.md"
    messy.write_text("---\ntitle: Messy\n---\nSomeone spoke.", encoding="utf-8")
    make_meeting(manifest, "messy", "2026-08-01", status=db.INDEXED, minutes_path=str(messy))
    db.set_speaker(manifest, "messy", "SPEAKER_00", None, "unknown")

    # "messy" is the older meeting, so date ordering alone would put it last.
    assert [i.meeting.id for i in review.queue(manifest)] == ["messy", "clean"]


def test_queue_hides_reviewed_meetings_by_default(manifest, meeting):
    db.mark_reviewed(manifest, "m1", "2026-08-13T10:00:00")

    assert review.queue(manifest) == []
    assert len(review.queue(manifest, include_reviewed=True)) == 1


def test_queue_ignores_meetings_without_minutes(manifest):
    make_meeting(manifest, "raw", "2026-08-10")
    assert review.queue(manifest) == []


# ── speakers ─────────────────────────────────────────────────────────

def test_correcting_a_name_rewinds_for_recompilation(manifest, meeting):
    """A patched string would leave the compiler's reasoning attributed wrongly."""
    recompiling = review.save_speakers(manifest, meeting, {"SPEAKER_01": "Priya"})

    assert recompiling is True
    assert db.get_meeting(manifest, "m1").status == db.SPEAKERS_RESOLVED
    assert db.get_speakers(manifest, "m1")["SPEAKER_01"] == "Priya"


def test_confirming_unchanged_names_does_not_recompile(manifest, meeting):
    recompiling = review.save_speakers(manifest, meeting, {"SPEAKER_00": "Faraz"})

    assert recompiling is False
    assert db.get_meeting(manifest, "m1").status == db.INDEXED


def test_saved_names_are_canonicalized_and_registered(manifest, meeting):
    """Variants must collapse, or one person becomes several graph nodes."""
    db.add_person(manifest, "Michael", aliases=["Mike"])

    review.save_speakers(manifest, meeting, {"SPEAKER_01": "Mike"})

    assert db.get_speakers(manifest, "m1")["SPEAKER_01"] == "Michael"


def test_new_names_join_the_people_registry(manifest, meeting):
    review.save_speakers(manifest, meeting, {"SPEAKER_01": "Priya"})

    assert db.canonical_name(manifest, "priya") == "Priya"


def test_clearing_a_name_returns_the_label_to_unresolved(manifest, meeting):
    review.save_speakers(manifest, meeting, {"SPEAKER_00": ""})

    assert "SPEAKER_00" not in db.get_speakers(manifest, "m1")


def test_saving_an_unknown_label_is_refused(manifest, meeting):
    with pytest.raises(review.ReviewError, match="SPEAKER_99"):
        review.save_speakers(manifest, meeting, {"SPEAKER_99": "Nobody"})


def test_correction_clears_a_previous_review_stamp(manifest, meeting):
    db.mark_reviewed(manifest, "m1", "2026-08-13T10:00:00")

    review.save_speakers(manifest, meeting, {"SPEAKER_01": "Priya"})

    assert db.get_meeting(manifest, "m1").reviewed_at is None


# ── minutes ──────────────────────────────────────────────────────────

def test_saving_minutes_rewrites_the_file_and_rewinds_status(manifest, meeting, tmp_path):
    review.save_minutes(manifest, meeting, MINUTES.replace("Ship the ACL", "Hold the ACL"))

    assert "Hold the ACL" in (tmp_path / "2026-08-10-kafka-acl.md").read_text()
    assert db.get_meeting(manifest, "m1").status == db.MINUTES_COMPILED


def test_saving_minutes_re_derives_the_graph_block(manifest, meeting):
    """An edit that renames an entity has to reach the graph, not just the prose."""
    edited = MINUTES.replace("Kafka (project)", "Kafka Platform (project)")
    edited = edited.replace("Faraz -> owns -> Kafka", "Faraz -> owns -> Kafka Platform")

    review.save_minutes(manifest, meeting, edited)

    names = {e["name"] for e in db.get_entities(manifest, "m1")}
    assert "Kafka Platform" in names
    assert "Kafka" not in names


def test_saving_empty_minutes_is_refused(manifest, meeting):
    with pytest.raises(review.ReviewError, match="empty"):
        review.save_minutes(manifest, meeting, "   ")


# ── approve ──────────────────────────────────────────────────────────

def test_approve_reindexes_and_stamps_the_review(manifest, meeting, monkeypatch):
    monkeypatch.setattr(index, "replace_minutes", lambda *a, **k: ("doc-new", True))

    doc_id = review.approve(manifest, meeting)

    row = db.get_meeting(manifest, "m1")
    assert doc_id == "doc-new"
    assert row.lightrag_doc_id == "doc-new"
    assert row.status == db.INDEXED
    assert row.reviewed_at is not None


def test_approve_passes_the_previous_doc_id_so_the_stale_copy_is_deleted(
    manifest, meeting, monkeypatch
):
    seen = {}

    def capture(path, previous_doc_id, augment=""):
        seen["previous"] = previous_doc_id
        seen["augment"] = augment
        return "doc-new", True

    monkeypatch.setattr(index, "replace_minutes", capture)
    review.approve(manifest, meeting)

    assert seen["previous"] == "doc-old"


def test_approve_refuses_when_the_stale_copy_survives(manifest, meeting, monkeypatch):
    """Indexing anyway would leave two contradictory copies retrieval could return."""
    monkeypatch.setattr(index, "replace_minutes", lambda *a, **k: ("doc-new", False))

    with pytest.raises(review.ReviewError, match="two contradictory copies"):
        review.approve(manifest, meeting)

    row = db.get_meeting(manifest, "m1")
    assert row.lightrag_doc_id == "doc-old"
    assert row.reviewed_at is None


def test_approve_surfaces_an_unreachable_index_as_a_review_error(manifest, meeting, monkeypatch):
    def explode(*args, **kwargs):
        raise index.IndexError_("LightRAG unreachable")

    monkeypatch.setattr(index, "replace_minutes", explode)

    with pytest.raises(review.ReviewError, match="unreachable"):
        review.approve(manifest, meeting)


def test_approve_without_a_minutes_file_is_refused(manifest):
    bare = make_meeting(manifest, "bare", "2026-08-10", status=db.MINUTES_COMPILED)

    with pytest.raises(review.ReviewError, match="no minutes file"):
        review.approve(manifest, bare)
