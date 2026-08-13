"""Entity and relation emission.

This is the mitigation for the subscription ceiling: a frontier model states
entities explicitly so a small local extraction model does not have to discover
them from prose. The parser is therefore deliberately tolerant - recovering most of
a messy block beats discarding all of a slightly-malformed one.
"""

from __future__ import annotations

import pytest

from pipeline import db, entities

from .conftest import make_meeting

DOCUMENT = """---
date: 2026-08-10
title: Roadmap Review
---

# Roadmap Review

## Decisions
- Deferred Atlas to Q1 [0:14:02]

## Entities
- Atlas (feature): the platform rewrite
- Faraz (person): product manager
- Northwind (customer): asked for SSO
- 2026.4 (release)

## Relations
- Faraz -> deprioritized -> Atlas
- Northwind -> requested -> SSO
- Atlas -> part of -> 2026.4
"""


def test_parses_entities_with_and_without_descriptions():
    parsed = entities.parse_entities(DOCUMENT)
    by_name = {e["name"]: e for e in parsed}

    assert by_name["Atlas"]["kind"] == "feature"
    assert by_name["Atlas"]["description"] == "the platform rewrite"
    assert by_name["2026.4"]["kind"] == "release"
    assert by_name["2026.4"]["description"] == "", "a bare entity is still valid"


def test_parses_relation_triples():
    parsed = entities.parse_relations(DOCUMENT)
    assert {"subject": "Faraz", "predicate": "deprioritized", "object": "Atlas"} in parsed
    assert len(parsed) == 3


def test_only_reads_its_own_sections():
    """A decision line mentioning Atlas must not become an entity."""
    parsed = entities.parse_entities(DOCUMENT)
    assert not any("Deferred" in e["name"] for e in parsed)


@pytest.mark.parametrize(
    "line,name,kind",
    [
        ("Atlas (feature): x", "Atlas", "feature"),
        ("- Atlas [feature]: x", "Atlas", "feature"),
        ("* Atlas (Feature) - x", "Atlas", "feature"),
        ("Atlas (nonsense): x", "Atlas", "other"),
        ("Atlas (feature)", "Atlas", "feature"),
    ],
)
def test_tolerates_shape_variation(line, name, kind):
    """Models produce all of these for one instruction; a strict parser would
    discard most of the block."""
    parsed = entities.parse_entities(f"## Entities\n{line}\n")
    assert parsed[0]["name"] == name
    assert parsed[0]["kind"] == kind


@pytest.mark.parametrize("arrow", ["->", "→", "|"])
def test_tolerates_arrow_variation(arrow):
    doc = f"## Relations\n- A {arrow} owns {arrow} B\n"
    assert entities.parse_relations(doc) == [
        {"subject": "A", "predicate": "owns", "object": "B"}
    ]


def test_incomplete_lines_are_skipped_not_fatal():
    doc = "## Entities\n- no kind here\n- Atlas (feature): good\n"
    parsed = entities.parse_entities(doc)
    assert [e["name"] for e in parsed] == ["Atlas"]


def test_duplicates_collapse_case_insensitively():
    doc = "## Entities\n- Atlas (feature): a\n- atlas (feature): b\n"
    assert len(entities.parse_entities(doc)) == 1


def test_missing_sections_yield_nothing():
    parsed_entities, parsed_relations = entities.extract("---\ndate: x\n---\n# Title")
    assert parsed_entities == []
    assert parsed_relations == []


# ── canonicalization ──────────────────────────────────────────────────

def test_person_names_canonicalize_both_ends_of_relations(manifest):
    """Mike and Michael must be one graph node, on entities and relations alike."""
    db.add_person(manifest, "Michael", aliases=["Mike", "mikey"])

    ents = [{"name": "Mike", "kind": "person", "description": ""}]
    rels = [{"subject": "Mike", "predicate": "owns", "object": "Atlas"}]
    ents, rels = entities.canonicalize(manifest, ents, rels)

    assert ents[0]["name"] == "Michael"
    assert rels[0]["subject"] == "Michael"
    assert rels[0]["object"] == "Atlas", "non-person ends pass through"


def test_unknown_people_pass_through(manifest):
    """A new person appearing is normal; dropping them would be worse."""
    ents = [{"name": "Brandnew", "kind": "person", "description": ""}]
    ents, _ = entities.canonicalize(manifest, ents, [])
    assert ents[0]["name"] == "Brandnew"


def test_non_person_entities_are_not_canonicalized(manifest):
    db.add_person(manifest, "Atlas Person", aliases=["atlas"])
    ents = [{"name": "Atlas", "kind": "feature", "description": ""}]
    ents, _ = entities.canonicalize(manifest, ents, [])
    assert ents[0]["name"] == "Atlas", "a feature must not be renamed to a person"


# ── rendering for the index ───────────────────────────────────────────

def test_render_states_facts_explicitly():
    block = entities.render_for_index(
        [{"name": "Atlas", "kind": "feature", "description": "the rewrite"}],
        [{"subject": "Faraz", "predicate": "owns", "object": "Atlas"}],
    )
    assert "## Knowledge Graph" in block
    assert "Atlas (feature): the rewrite" in block
    assert "Faraz -> owns -> Atlas" in block


def test_render_empty_when_nothing_to_say():
    assert entities.render_for_index([], []) == ""


# ── persistence ───────────────────────────────────────────────────────

def test_replace_entities_is_not_additive(manifest):
    """A recompile must not leave the previous run's entities behind, for the same
    reason re-indexing deletes before inserting."""
    make_meeting(manifest, "m1", "2026-08-10")

    db.replace_entities(
        manifest, "m1",
        [{"name": "Old", "kind": "feature", "description": ""}],
        [{"subject": "Old", "predicate": "blocks", "object": "X"}],
    )
    db.replace_entities(
        manifest, "m1",
        [{"name": "New", "kind": "feature", "description": ""}],
        [],
    )

    assert [e["name"] for e in db.get_entities(manifest, "m1")] == ["New"]
    assert db.get_relations(manifest, "m1") == []


def test_blank_entities_and_partial_relations_are_dropped(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_entities(
        manifest, "m1",
        [{"name": "  ", "kind": "other", "description": ""}],
        [{"subject": "A", "predicate": "", "object": "B"}],
    )
    assert db.get_entities(manifest, "m1") == []
    assert db.get_relations(manifest, "m1") == []


def test_entity_mentions_ranks_across_meetings(manifest):
    for index_, date in enumerate(["2026-08-10", "2026-08-11", "2026-08-12"]):
        make_meeting(manifest, f"m{index_}", date)
        ents = [{"name": "Atlas", "kind": "feature", "description": ""}]
        if index_ == 0:
            ents.append({"name": "Rare", "kind": "feature", "description": ""})
        db.replace_entities(manifest, f"m{index_}", ents, [])

    ranked = db.entity_mentions(manifest)
    assert ranked[0]["name"] == "Atlas"
    assert ranked[0]["meetings"] == 3
