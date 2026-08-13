"""People registry.

Exists for determinism. Asking a model to spell a name the same way it did four
months ago is not a reliable strategy, and every variant it invents becomes a
separate node in the knowledge graph.
"""

from __future__ import annotations

from pipeline import db

from .conftest import make_meeting


def test_aliases_map_to_canonical_case_insensitively(manifest):
    db.add_person(manifest, "Michael", aliases=["Mike", "Mikey"])

    for variant in ("Mike", "mike", "MIKEY", "  Michael  "):
        assert db.canonical_name(manifest, variant) == "Michael"


def test_canonical_is_its_own_alias(manifest):
    db.add_person(manifest, "Faraz")
    assert db.canonical_name(manifest, "faraz") == "Faraz"


def test_unknown_names_pass_through_trimmed(manifest):
    """A new person is normal; silently dropping them would be worse than an
    unnormalized spelling."""
    assert db.canonical_name(manifest, "  Stranger ") == "Stranger"


def test_none_and_empty_are_preserved(manifest):
    assert db.canonical_name(manifest, None) is None
    assert db.canonical_name(manifest, "") == ""


def test_adding_twice_is_idempotent(manifest):
    db.add_person(manifest, "Faraz", aliases=["F"])
    db.add_person(manifest, "Faraz", aliases=["F", "Fa"])
    assert db.canonical_name(manifest, "Fa") == "Faraz"
    assert len(db.list_people(manifest)) == 1


def test_role_is_preserved_when_re_added_without_one(manifest):
    db.add_person(manifest, "Faraz", role="PM")
    db.add_person(manifest, "Faraz")
    assert db.list_people(manifest)[0]["role"] == "PM"


def test_blank_names_are_ignored(manifest):
    db.add_person(manifest, "   ")
    assert db.list_people(manifest) == []


def test_list_people_counts_meetings(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    make_meeting(manifest, "m2", "2026-08-11")
    db.add_person(manifest, "Faraz")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Faraz", "confirmed")
    db.set_speaker(manifest, "m2", "SPEAKER_00", "Faraz", "confirmed")

    assert db.list_people(manifest)[0]["meetings"] == 2


def test_merge_rewrites_history_everywhere(manifest):
    """Merging must move entities and relations too, or the graph keeps both nodes."""
    make_meeting(manifest, "m1", "2026-08-10")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Mike", "inferred")
    db.replace_entities(
        manifest, "m1",
        [{"name": "Mike", "kind": "person", "description": ""}],
        [{"subject": "Mike", "predicate": "owns", "object": "Atlas"}],
    )

    rewritten = db.merge_person(manifest, "Mike", "Michael")

    assert rewritten == 1
    assert db.get_speakers(manifest, "m1") == {"SPEAKER_00": "Michael"}
    assert [e["name"] for e in db.get_entities(manifest, "m1")] == ["Michael"]
    assert db.get_relations(manifest, "m1")[0]["subject"] == "Michael"
    # The old name must now resolve to the new one.
    assert db.canonical_name(manifest, "Mike") == "Michael"


def test_merge_moves_relation_objects_too(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_entities(
        manifest, "m1", [],
        [{"subject": "Atlas", "predicate": "owned by", "object": "Mike"}],
    )
    db.merge_person(manifest, "Mike", "Michael")
    assert db.get_relations(manifest, "m1")[0]["object"] == "Michael"


def test_merge_into_self_is_a_noop(manifest):
    db.add_person(manifest, "Faraz")
    assert db.merge_person(manifest, "Faraz", "Faraz") == 0


def test_merge_carries_existing_aliases_forward(manifest):
    db.add_person(manifest, "Mike", aliases=["mikey"])
    db.merge_person(manifest, "Mike", "Michael")
    assert db.canonical_name(manifest, "mikey") == "Michael"


def test_resolver_normalizes_through_the_registry(manifest, monkeypatch, tmp_path):
    """The point of the registry: the resolver's output is normalized before it is
    persisted, so a model variant cannot fragment the graph."""
    from pipeline import speakers as spk
    from pipeline.asr import Segment, Transcript

    db.add_person(manifest, "Michael", aliases=["Mike"])
    monkeypatch.setattr(spk, "SPEAKER_OVERRIDES_FILE", tmp_path / "none.yaml")
    monkeypatch.setattr(spk, "complete", lambda p, order=None: '{"SPEAKER_00": "Mike"}')

    transcript = Transcript(
        meeting_id="m1", model="t", language="en", duration_sec=10.0,
        segments=[Segment(start=0.0, end=10.0, text="hi", speaker="SPEAKER_00")],
    )
    meeting = make_meeting(manifest, "m1", "2026-08-10")
    resolved = spk.resolve(manifest, meeting, transcript)

    assert resolved["SPEAKER_00"] == "Michael", "model variant must be normalized"
    assert db.get_speakers(manifest, "m1") == {"SPEAKER_00": "Michael"}


def test_new_speakers_are_registered_for_next_time(manifest, monkeypatch, tmp_path):
    from pipeline import speakers as spk
    from pipeline.asr import Segment, Transcript

    monkeypatch.setattr(spk, "SPEAKER_OVERRIDES_FILE", tmp_path / "none.yaml")
    monkeypatch.setattr(spk, "complete", lambda p, order=None: '{"SPEAKER_00": "Newperson"}')

    transcript = Transcript(
        meeting_id="m1", model="t", language="en", duration_sec=10.0,
        segments=[Segment(start=0.0, end=10.0, text="hi", speaker="SPEAKER_00")],
    )
    spk.resolve(manifest, make_meeting(manifest, "m1", "2026-08-10"), transcript)

    assert "Newperson" in [p["canonical"] for p in db.list_people(manifest)]
