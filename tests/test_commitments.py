"""Commitment register and decision/open-question store.

Parser tests use real text copied verbatim from minutes/*.md rather than
hand-written fixtures, on the theory that a model's actual loose formatting is
the thing worth testing against - a tidy hand-written example would pass even
if the regex could not survive contact with the corpus it exists to read.
"""

from __future__ import annotations

from pipeline import commitments, db, people_merge

from .conftest import make_meeting

# From minutes/2026-08-17-iss-2026-adoption-metric-reporting-and-automation-
# planning-bff222d4.md, copied verbatim (including the loose "decided by X,
# based on Y" shape that never says "Rationale:").
ISS_DOCUMENT = """---
date: 2026-08-17
title: ISS2026 Adoption Metric Reporting
---

# ISS2026 Adoption Metric Reporting

## Decisions
- **Use ISS2026 as the externally understood name for the adoption program** — decided by Unknown speaker (SPEAKER_01), based on SPS feedback that "ISS 2.0" causes confusion. Internal dot-release versioning will continue for incremental changes because the product is evergreen. [0:04:50]

- **Apply ISS2026 adoption to net-new and renewal suppliers from 10 August 2026** — decided by Casey following Mario's feedback, as reported by Unknown speaker (SPEAKER_01). Rationale: limiting the requirement to new MSAs would exclude renewals that should follow the updated supplier-adoption expectation. [0:04:50]

## Open Questions
- What ContractIQ metadata is currently available to calculate real-time ISS2026 adoption measures, and what data must be added in future sprints? Faraz and Annalise need to resolve this. [0:12:59]
- Which enterprise and NFI groups in Ruth's reporting slide require correction or more specific status detail? Unknown speaker (SPEAKER_01) needs to review and update them. [0:14:07]

## Action Items
- [ ] **Faraz** — Schedule a follow-up meeting with Annalise and, if needed, ContractIQ engineering counterparts to assess feasible ContractIQ data support. Due: unspecified. [0:12:59]
- [ ] **Neil's team** — Run a spike to confirm the strongest automation candidates among approximately eight controls. Due: unspecified. [0:15:54]
"""

# From minutes/2026-08-14-usc-team-standup-... and
# minutes/2026-08-17-isas-adoption-reporting-discovery-with-contractiq-
# 487f3d2c.md - real due-date and multi-citation shapes.
DUE_DATE_DOCUMENT = """---
date: 2026-08-14
title: USC Standup
---

# USC Standup

## Action Items
- [ ] **Neil** — Create change request for prod Managed Vault secret fix using auto-change-request, with Paul, Faraz, and Tarun in a live session. Due: 2026-08-14 (40 minutes after standup). [?:??]
- [ ] **Unknown speaker (SPEAKER_03)** — assess what ContractIQ can currently report from Ariba for ISAS adoption, identify the quickest 1.0 option, and report the current state and next step. Due: 2026-08-17. [0:15:05] [0:16:57]
- [x] **Tarun** — Close out both ServiceNow change tasks. Due: 2026-08-14. [0:17:12]
"""

# From minutes/2026-08-17-usc-authorization-model-and-permissions-matrix-
# planning-df810b70.md - the exact real-world case the team lead flagged:
# "Faraz Matin" as a compound decided_by, folding to "Faraz".
AUTH_MODEL_DOCUMENT = """---
date: 2026-08-17
title: USC Authorization Model
---

# USC Authorization Model

## Decisions
- **Start with basic AD-group-level permissions, layer granularity later** — decided jointly by Faraz Matin and Yuliya. Rationale: building fine-grained domain-level permissions now would consume too much effort. [0:20:56]
- **CRUD functionality to be visually removed from production release for now** — stated by Yuliya. The edit/add functionality exists in the UI with fake data but will be hidden from the production release. [0:01:20]
"""

# From minutes/2026-08-12-kafka-acl-production-change-order-creation-
# 52494306.md - a decision with no named decider at all ("decided jointly").
JOINT_DECISION_DOCUMENT = """---
date: 2026-08-12
title: Kafka ACL Change Order
---

# Kafka ACL Change Order

## Decisions
- **Use the prior USC change order as the model** — decided jointly. Rationale: it was the most recent similar change the team had completed, making it the best template for field values and descriptions. [?:??]
"""


# ── Action items -> commitments ────────────────────────────────────────

def test_parses_action_items_owner_task_due_and_citation():
    items = commitments.parse_action_items(ISS_DOCUMENT)
    by_owner = {i["owner"]: i for i in items}

    faraz = by_owner["Faraz"]
    assert faraz["text"].startswith("Schedule a follow-up meeting")
    assert faraz["due_date"] == "unspecified"
    assert faraz["due_date_iso"] is None, '"unspecified" must not crash date parsing'
    assert faraz["timestamp_cite"] == "[0:12:59]"
    assert faraz["state"] == "open"

    assert "Neil's team" in by_owner, "a team, not a person, is still a valid owner"


def test_checkbox_mark_determines_state():
    items = commitments.parse_action_items(DUE_DATE_DOCUMENT)
    states = {i["owner"]: i["state"] for i in items}
    assert states["Neil"] == "open"
    assert states["Tarun"] == "done", "- [x] must mark the commitment done"


def test_due_date_iso_extracted_from_loose_prose():
    items = commitments.parse_action_items(DUE_DATE_DOCUMENT)
    by_owner = {i["owner"]: i for i in items}

    neil = by_owner["Neil"]
    assert neil["due_date"] == "2026-08-14 (40 minutes after standup)"
    assert neil["due_date_iso"] == "2026-08-14", "a real date buried in prose is still extractable"

    speaker = by_owner["Unknown speaker (SPEAKER_03)"]
    assert speaker["due_date_iso"] == "2026-08-17"


def test_multiple_trailing_citations_are_captured_together():
    items = commitments.parse_action_items(DUE_DATE_DOCUMENT)
    speaker = next(i for i in items if i["owner"] == "Unknown speaker (SPEAKER_03)")
    assert speaker["timestamp_cite"] == "[0:15:05] [0:16:57]"


def test_unspecified_and_missing_due_dates_do_not_crash():
    doc = "## Action Items\n- [ ] **Owner** — do the thing. Due: unspecified. [0:00:01]\n"
    items = commitments.parse_action_items(doc)
    assert items[0]["due_date"] == "unspecified"
    assert items[0]["due_date_iso"] is None


def test_missing_action_items_section_yields_nothing():
    assert commitments.parse_action_items("---\ndate: x\n---\n# Title") == []


# ── Decisions ────────────────────────────────────────────────────────────

def test_parses_decision_with_explicit_rationale_label():
    decisions = commitments.parse_decisions(ISS_DOCUMENT)
    apply_decision = next(d for d in decisions if d["text"].startswith("Apply ISS2026"))
    assert apply_decision["decided_by"] == "Casey following Mario's feedback"
    assert apply_decision["rationale"].startswith("limiting the requirement")
    assert apply_decision["timestamp_cite"] == "[0:04:50]"


def test_parses_decision_without_rationale_label():
    """Not every decision line says the word "Rationale:" - the surrounding
    prose still counts as attribution even without it."""
    decisions = commitments.parse_decisions(ISS_DOCUMENT)
    naming_decision = next(d for d in decisions if "externally understood name" in d["text"])
    assert naming_decision["decided_by"] == "Unknown speaker (SPEAKER_01)"
    assert naming_decision["rationale"] is None


def test_decided_by_none_when_no_one_is_named():
    decisions = commitments.parse_decisions(JOINT_DECISION_DOCUMENT)
    assert decisions[0]["decided_by"] is None, '"decided jointly" names no one'
    assert decisions[0]["rationale"].startswith("it was the most recent")


def test_decided_by_extracts_compound_attribution():
    decisions = commitments.parse_decisions(AUTH_MODEL_DOCUMENT)
    layered = next(d for d in decisions if "layer granularity" in d["text"])
    assert layered["decided_by"] == "Faraz Matin and Yuliya"

    hidden = next(d for d in decisions if "visually removed" in d["text"])
    assert hidden["decided_by"] == "Yuliya"


def test_missing_decisions_section_yields_nothing():
    assert commitments.parse_decisions("---\ndate: x\n---\n# Title") == []


# ── Open questions ────────────────────────────────────────────────────

def test_open_question_owner_extracted_from_template_shape():
    questions = commitments.parse_open_questions(ISS_DOCUMENT)
    contractiq_q = next(q for q in questions if "ContractIQ metadata" in q["text"])
    assert contractiq_q["owner"] == "Faraz and Annalise"
    assert contractiq_q["timestamp_cite"] == "[0:12:59]"


def test_open_question_owner_is_none_without_the_template_shape():
    """Most open questions in this corpus never say "needs to resolve" at all;
    guessing an owner from arbitrary prose would be worse than leaving it
    unattributed."""
    questions = commitments.parse_open_questions(ISS_DOCUMENT)
    ruth_q = next(q for q in questions if "Ruth's reporting slide" in q["text"])
    assert ruth_q["owner"] is None


def test_missing_open_questions_section_yields_nothing():
    assert commitments.parse_open_questions("---\ndate: x\n---\n# Title") == []


# ── extract() combines all three ───────────────────────────────────────

def test_extract_returns_all_three_lists():
    parsed = commitments.extract(ISS_DOCUMENT)
    assert len(parsed["commitments"]) == 2
    assert len(parsed["decisions"]) == 2
    assert len(parsed["open_questions"]) == 2


def test_extract_on_a_document_with_no_sections_yields_empty_lists():
    parsed = commitments.extract("---\ndate: x\n---\n# Title")
    assert parsed == {"commitments": [], "decisions": [], "open_questions": []}


# ── Canonicalization ────────────────────────────────────────────────────

def test_owner_canonicalizes_through_people_registry(manifest):
    db.add_person(manifest, "Faraz", aliases=["Faraz Matin"])
    parsed = commitments.canonicalize(manifest, commitments.extract(ISS_DOCUMENT))
    owners = {i["owner"] for i in parsed["commitments"]}
    assert "Faraz" in owners


def test_compound_decided_by_canonicalizes_each_name(manifest):
    """The exact case the team lead flagged: "Faraz Matin" folds to "Faraz"
    even inside a two-person attribution."""
    db.add_person(manifest, "Faraz", aliases=["Faraz Matin"])
    parsed = commitments.canonicalize(manifest, commitments.extract(AUTH_MODEL_DOCUMENT))
    layered = next(d for d in parsed["decisions"] if "layer granularity" in d["text"])
    assert layered["decided_by"] == "Faraz and Yuliya"


def test_unknown_names_pass_through_unchanged(manifest):
    parsed = commitments.canonicalize(manifest, commitments.extract(ISS_DOCUMENT))
    owners = {i["owner"] for i in parsed["commitments"]}
    assert "Neil's team" in owners, "a non-person owner must not be dropped or mangled"


def test_none_owner_is_left_alone(manifest):
    parsed = commitments.canonicalize(manifest, commitments.extract(ISS_DOCUMENT))
    ruth_q = next(q for q in parsed["open_questions"] if "Ruth's reporting slide" in q["text"])
    assert ruth_q["owner"] is None


# ── Persistence: replace-on-recompile ───────────────────────────────────

def test_replace_commitments_is_not_additive(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_commitments(manifest, "m1", [{"text": "old task", "owner": "A", "state": "open"}])
    db.replace_commitments(manifest, "m1", [{"text": "new task", "owner": "B", "state": "open"}])

    rows = manifest.execute(
        "SELECT text FROM commitments WHERE meeting_id = ?", ("m1",)
    ).fetchall()
    assert [r["text"] for r in rows] == ["new task"]


def test_replace_decisions_is_not_additive(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_decisions(manifest, "m1", [{"text": "old decision"}])
    db.replace_decisions(manifest, "m1", [{"text": "new decision"}])

    assert [d["text"] for d in db.get_decisions(manifest, "m1")] == ["new decision"]


def test_replace_open_questions_is_not_additive(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_open_questions(manifest, "m1", [{"text": "old question"}])
    db.replace_open_questions(manifest, "m1", [{"text": "new question"}])

    assert [q["text"] for q in db.get_open_questions(manifest, "m1")] == ["new question"]


def test_blank_text_rows_are_dropped(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_commitments(manifest, "m1", [{"text": "  ", "owner": "A"}])
    db.replace_decisions(manifest, "m1", [{"text": ""}])
    db.replace_open_questions(manifest, "m1", [{"text": None}])

    assert manifest.execute(
        "SELECT COUNT(*) AS n FROM commitments WHERE meeting_id = ?", ("m1",)
    ).fetchone()["n"] == 0
    assert db.get_decisions(manifest, "m1") == []
    assert db.get_open_questions(manifest, "m1") == []


def test_duplicate_text_within_one_document_does_not_crash(manifest):
    """The UNIQUE(meeting_id, text) constraint must degrade to "keep the
    first" rather than raising IntegrityError over a loosely-formatted model
    output that happens to repeat a line."""
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_commitments(
        manifest, "m1",
        [{"text": "same task", "owner": "A"}, {"text": "same task", "owner": "B"}],
    )
    rows = manifest.execute(
        "SELECT owner FROM commitments WHERE meeting_id = ?", ("m1",)
    ).fetchall()
    assert len(rows) == 1


# ── Persistence: merging a person moves historical attributions ────────

def test_merge_person_rewrites_commitments_decisions_and_questions(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.add_person(manifest, "Mike")
    db.replace_commitments(manifest, "m1", [{"text": "task", "owner": "Mike"}])
    db.replace_decisions(manifest, "m1", [{"text": "decision", "decided_by": "Mike"}])
    db.replace_open_questions(manifest, "m1", [{"text": "question", "owner": "Mike"}])

    manifest.commit()
    approved = people_merge.preview(["Mike"], "Michael")
    people_merge.merge(["Mike"], "Michael", expected_digest=approved.digest)

    assert db.get_decisions(manifest, "m1")[0]["decided_by"] == "Michael"
    assert db.get_open_questions(manifest, "m1")[0]["owner"] == "Michael"
    commitment = manifest.execute(
        "SELECT owner FROM commitments WHERE meeting_id = ?", ("m1",)
    ).fetchone()
    assert commitment["owner"] == "Michael"


# ── Query surfaces used by the API ──────────────────────────────────────

def test_list_commitments_filters_by_owner_case_insensitively(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_commitments(
        manifest, "m1",
        [{"text": "task A", "owner": "Faraz"}, {"text": "task B", "owner": "Yuliya"}],
    )
    rows = db.list_commitments(manifest, owner="faraz")
    assert [r["text"] for r in rows] == ["task A"]


def test_list_commitments_overdue_flag(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_commitments(
        manifest, "m1",
        [
            {"text": "late task", "owner": "A", "due_date_iso": "2000-01-01", "state": "open"},
            {"text": "future task", "owner": "A", "due_date_iso": "2999-01-01", "state": "open"},
            {"text": "no date", "owner": "A", "state": "open"},
            {
                "text": "late but done", "owner": "A",
                "due_date_iso": "2000-01-01", "state": "done",
            },
        ],
    )
    by_text = {r["text"]: r for r in db.list_commitments(manifest)}
    assert by_text["late task"]["overdue"] is True
    assert by_text["future task"]["overdue"] is False
    assert by_text["no date"]["overdue"] is False
    assert by_text["late but done"]["overdue"] is False, "a done item is never overdue"

    overdue_only = db.list_commitments(manifest, overdue=True)
    assert [r["text"] for r in overdue_only] == ["late task"]


def test_list_decisions_filters_by_topic(manifest):
    make_meeting(manifest, "m1", "2026-08-10", title_hint="Kafka Review")
    db.replace_decisions(
        manifest, "m1",
        [{"text": "Use Kafka ACLs", "rationale": "throughput"}, {"text": "Ship the UI", "rationale": None}],
    )
    rows = db.list_decisions(manifest, topic="kafka")
    assert [r["text"] for r in rows] == ["Use Kafka ACLs"]
    assert rows[0]["title_hint"] == "Kafka Review", "joined meeting context for display"


def test_list_decisions_without_topic_returns_everything(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_decisions(manifest, "m1", [{"text": "A"}, {"text": "B"}])
    assert len(db.list_decisions(manifest)) == 2


# ── Backfill from disk ──────────────────────────────────────────────────

def test_backfill_from_disk_parses_existing_minutes_files(manifest, tmp_path):
    minutes_path = tmp_path / "meeting.md"
    minutes_path.write_text(ISS_DOCUMENT, encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-17", minutes_path=str(minutes_path))

    counts = commitments.backfill_from_disk(manifest)

    assert counts == {"meetings": 1, "commitments": 2, "decisions": 2, "open_questions": 2}
    assert len(db.list_commitments(manifest)) == 2
    assert len(db.get_decisions(manifest, "m1")) == 2


def test_backfill_from_disk_skips_meetings_without_a_minutes_file(manifest):
    make_meeting(manifest, "m1", "2026-08-17")  # minutes_path is NULL
    counts = commitments.backfill_from_disk(manifest)
    assert counts["meetings"] == 0


def test_backfill_from_disk_skips_a_missing_path_without_crashing(manifest, tmp_path):
    missing = tmp_path / "does-not-exist.md"
    make_meeting(manifest, "m1", "2026-08-17", minutes_path=str(missing))
    counts = commitments.backfill_from_disk(manifest)
    assert counts["meetings"] == 0


def test_backfill_from_disk_is_replace_not_additive(manifest, tmp_path):
    """Running the backfill twice (e.g. after fixing a bug in the parser)
    must not double every row."""
    minutes_path = tmp_path / "meeting.md"
    minutes_path.write_text(ISS_DOCUMENT, encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-17", minutes_path=str(minutes_path))

    commitments.backfill_from_disk(manifest)
    commitments.backfill_from_disk(manifest)

    assert len(db.list_commitments(manifest)) == 2


def test_unresolved_speaker_owners_collapse_to_one_spelling(manifest):
    """The same anonymous person arrived under two owner strings.

    The template asks for `Unknown speaker (SPEAKER_xx)`; the model also writes a
    bare `SPEAKER_xx`. On the real corpus SPEAKER_00 split 11/5 across the two
    forms and grouped as two different owners.
    """
    parsed = {
        "commitments": [
            {"owner": "Unknown speaker (SPEAKER_00)", "text": "a"},
            {"owner": "SPEAKER_00", "text": "b"},
            {"owner": "speaker_03", "text": "c"},
        ],
        "decisions": [{"decided_by": "Unknown speaker (SPEAKER_01)", "text": "d"}],
        "open_questions": [{"owner": "Faraz", "text": "e"}],
    }
    out = commitments.canonicalize(manifest, parsed)
    assert [c["owner"] for c in out["commitments"]] == [
        "SPEAKER_00",
        "SPEAKER_00",
        "SPEAKER_03",
    ]
    assert out["decisions"][0]["decided_by"] == "SPEAKER_01"
    # A real name and a non-person fragment must pass through untouched.
    assert out["open_questions"][0]["owner"] == "Faraz"


def test_non_person_attributions_are_left_alone():
    for text in ("Neil's team", "the meeting group", "Engineering"):
        assert commitments._normalize_unresolved(text) == text
