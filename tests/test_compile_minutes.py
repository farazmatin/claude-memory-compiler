"""Minutes compilation: prompt assembly, prompt-size guard, map-reduce."""

from __future__ import annotations

import pytest

from pipeline import compile_minutes as cm
from pipeline import db
from pipeline.asr import Segment, Transcript

from .conftest import make_meeting


def build_transcript(turns: int = 3, words_per_turn: int = 5) -> Transcript:
    segments = [
        Segment(
            start=float(i * 10),
            end=float(i * 10 + 9),
            text=" ".join(["word"] * words_per_turn),
            speaker=f"SPEAKER_0{i % 2}",
        )
        for i in range(turns)
    ]
    return Transcript(
        meeting_id="abc123def456",
        model="test",
        language="en",
        duration_sec=float(turns * 10),
        segments=segments,
    )


# ── helpers ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Roadmap Review: Q4 / 2026!", "roadmap-review-q4-2026"),
        ("  spaces  everywhere  ", "spaces-everywhere"),
        ("---", ""),
    ],
)
def test_slugify(raw, expected):
    assert cm.slugify(raw) == expected


def test_extract_title_prefers_frontmatter():
    doc = '---\ndate: 2026-08-10\ntitle: Roadmap Review\n---\n# Different Heading'
    assert cm.extract_title(doc) == "Roadmap Review"


def test_extract_title_falls_back_to_heading():
    assert cm.extract_title("# Only A Heading\n\nbody") == "Only A Heading"


def test_extract_title_handles_neither():
    assert cm.extract_title("just prose") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("```markdown\n---\ndate: x\n---\nbody\n```", "---\ndate: x\n---\nbody"),
        ("---\ndate: x\n---", "---\ndate: x\n---"),
        ("```\n---\na\n---\n```", "---\na\n---"),
    ],
)
def test_strip_wrapping_fence(raw, expected):
    """Models fence markdown even when told not to, and a stray fence ahead of the
    frontmatter breaks every YAML parser downstream."""
    assert cm.strip_wrapping_fence(raw) == expected


def test_render_dialogue_merges_consecutive_turns():
    transcript = Transcript(
        meeting_id="m", model="t", language="en", duration_sec=30.0,
        segments=[
            Segment(start=0.0, end=5.0, text="first part", speaker="SPEAKER_00"),
            Segment(start=5.0, end=10.0, text="same speaker", speaker="SPEAKER_00"),
            Segment(start=10.0, end=15.0, text="reply", speaker="SPEAKER_01"),
        ],
    )
    rendered = cm.render_dialogue(transcript, {"SPEAKER_00": "Faraz", "SPEAKER_01": "Ali"})
    assert rendered.count("Faraz:") == 1, "consecutive turns must merge into one"
    assert "first part same speaker" in rendered
    assert "Ali: reply" in rendered


# ── prompt-size guard ─────────────────────────────────────────────────

def test_estimate_tokens_scales_with_length():
    assert cm.estimate_tokens("") == 0
    assert cm.estimate_tokens("a" * 400) == 100


def test_split_dialogue_breaks_on_turn_boundaries():
    """Splitting mid-turn would cut a decision away from its rationale."""
    lines = [f"[0:0{i}] Speaker: {'word ' * 50}" for i in range(10)]
    windows = cm.split_dialogue("\n".join(lines), window_tokens=40)

    assert len(windows) > 1, "should split a long dialogue"
    for window in windows:
        for line in window.split("\n"):
            assert line.startswith("[") or not line, "no line may be cut in half"
    # Nothing may be lost in the split.
    assert sum(len(w.split("\n")) for w in windows) == len(lines)


def test_split_dialogue_keeps_short_input_whole():
    assert cm.split_dialogue("[0:00] A: hi", window_tokens=1000) == ["[0:00] A: hi"]


def test_short_meeting_compiles_in_one_pass(manifest, monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_complete(prompt, order=None):
        calls.append(prompt)
        return "---\ndate: 2026-08-10\ntitle: Short\n---\n# Short\n\nbody"

    monkeypatch.setattr(cm, "complete", fake_complete)
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)

    meeting = make_meeting(manifest, "m1", "2026-08-10")
    path, doc = cm.compile_meeting(manifest, meeting, build_transcript(), {})

    assert len(calls) == 1, "a short meeting must not trigger map/reduce"
    assert "## Transcript" in calls[0]
    assert path.exists()
    assert doc.startswith("---")


def test_long_meeting_triggers_map_reduce(manifest, monkeypatch, tmp_path):
    """Over budget, the compiler condenses rather than overflowing the context
    window or burning a disproportionate slice of quota in one call."""
    prompts: list[str] = []

    def fake_complete(prompt, order=None):
        prompts.append(prompt)
        if "extracting notes from part" in prompt:
            return "- Decided X because Y [0:01]"
        return "---\ndate: 2026-08-10\ntitle: Long\n---\n# Long\n\nbody"

    monkeypatch.setattr(cm, "complete", fake_complete)
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)
    monkeypatch.setattr(cm, "MINUTES_PROMPT_TOKEN_BUDGET", 50)
    monkeypatch.setattr(cm, "MINUTES_MAP_WINDOW_TOKENS", 25)

    meeting = make_meeting(manifest, "m1", "2026-08-10")
    cm.compile_meeting(manifest, meeting, build_transcript(turns=40, words_per_turn=20), {})

    map_calls = [p for p in prompts if "extracting notes from part" in p]
    assert len(map_calls) > 1, "should map over several windows"
    assert "## Extracted notes" in prompts[-1], "reduce step must flag condensed input"


def test_map_failure_is_recorded_not_silent(monkeypatch):
    """A failed window must leave a visible gap, so the reduce step does not
    invent continuity across it."""
    from pipeline.llm import LLMError

    def flaky(prompt, order=None):
        if "part 1 of" in prompt:
            raise LLMError("quota")
        return "- something durable"

    monkeypatch.setattr(cm, "complete", flaky)
    monkeypatch.setattr(cm, "MINUTES_MAP_WINDOW_TOKENS", 25)

    lines = "\n".join(f"[0:0{i}] A: {'word ' * 40}" for i in range(6))
    result = cm.map_reduce_dialogue(lines)
    assert "(extraction failed)" in result
    assert "something durable" in result, "other windows must still contribute"


def test_map_reduce_raises_when_nothing_usable(monkeypatch):
    monkeypatch.setattr(cm, "complete", lambda prompt, order=None: "NOTHING")
    monkeypatch.setattr(cm, "MINUTES_MAP_WINDOW_TOKENS", 25)
    from pipeline.llm import LLMError

    with pytest.raises(LLMError):
        cm.map_reduce_dialogue("\n".join(f"[0:0{i}] A: {'w ' * 40}" for i in range(4)))


def test_missing_frontmatter_is_rejected(manifest, monkeypatch, tmp_path):
    """Frontmatter is required: downstream YAML parsing depends on it."""
    from pipeline.llm import LLMError

    monkeypatch.setattr(cm, "complete", lambda prompt, order=None: "no frontmatter here")
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)
    meeting = make_meeting(manifest, "m1", "2026-08-10")

    with pytest.raises(LLMError, match="frontmatter"):
        cm.compile_meeting(manifest, meeting, build_transcript(), {})


# ── prior context ─────────────────────────────────────────────────────

def test_prior_context_reports_absence_honestly(manifest):
    meeting = make_meeting(manifest, "m1", "2026-08-10")
    assert "no earlier minutes" in cm.load_prior_context(manifest, meeting)


def test_prior_context_includes_earlier_minutes(manifest, tmp_path):
    earlier = tmp_path / "earlier.md"
    earlier.write_text("---\ntitle: Earlier\n---\nWe decided to ship in Q3.", encoding="utf-8")

    make_meeting(manifest, "old", "2026-08-09", "09:00")
    db.advance(manifest, "old", db.INDEXED, minutes_path=str(earlier))

    meeting = make_meeting(manifest, "new", "2026-08-10", "09:00")
    context = cm.load_prior_context(manifest, meeting)
    assert "ship in Q3" in context


def test_unresolved_speakers_are_flagged_in_prompt(manifest):
    """The prompt must tell the model not to guess names."""
    meeting = make_meeting(manifest, "m1", "2026-08-10")
    prompt = cm.build_prompt(
        meeting, build_transcript(), {"SPEAKER_00": "Faraz"}, "none",
        source_audio="audio/x.m4a",
    )
    assert "SPEAKER_01" in prompt
    assert "do not" in prompt.lower()


# ── topical prior context ─────────────────────────────────────────────

def test_salient_terms_favours_proper_nouns():
    dialogue = (
        "[0:00] Faraz: we should ship Atlas before the Q4 release\n"
        "[0:10] Ali: the thing is the thing about the thing is really just okay\n"
        "[0:20] Faraz: Atlas needs the rate-limiter first\n"
    )
    terms = salient = cm.salient_terms(dialogue)
    assert "Atlas" in terms, "repeated proper noun must rank"
    assert "rate-limiter" in terms, "hyphenated term must rank"
    assert not {"the", "just", "okay", "really"} & set(t.lower() for t in salient), \
        "stopwords and filler must be excluded"


def test_topic_query_includes_title_and_terms(manifest):
    meeting = make_meeting(manifest, "m1", "2026-08-10", title_hint="Roadmap")
    query = cm.build_topic_query(meeting, "[0:00] A: Atlas Atlas pricing pricing")
    assert "Roadmap" in query
    assert "Atlas" in query
    assert "decided" in query.lower(), "query should ask for decisions and reasoning"


def test_prior_context_merges_topical_retrieval(manifest, monkeypatch):
    """Recency alone misses long-horizon reversals, which are the valuable case."""
    from pipeline import index

    monkeypatch.setattr(
        index, "query_context",
        lambda *a, **k: "Six months ago we decided NOT to build Atlas.",
    )
    meeting = make_meeting(manifest, "m1", "2026-08-10", title_hint="Atlas")
    context = cm.load_prior_context(manifest, meeting, dialogue="[0:00] A: Atlas Atlas")

    assert "Six months ago" in context
    assert "Topically related" in context


def test_prior_context_survives_unreachable_index(manifest, monkeypatch):
    """Prior context is a quality improvement, not a prerequisite for compiling."""
    from pipeline import index

    monkeypatch.setattr(index, "query_context", lambda *a, **k: "")
    meeting = make_meeting(manifest, "m1", "2026-08-10")
    assert "no earlier minutes" in cm.load_prior_context(
        manifest, meeting, dialogue="[0:00] A: hello"
    )


def test_topical_lookup_skipped_without_dialogue(manifest, monkeypatch):
    from pipeline import index

    called = []
    monkeypatch.setattr(index, "query_context", lambda *a, **k: called.append(1) or "x")
    meeting = make_meeting(manifest, "m1", "2026-08-10")
    cm.load_prior_context(manifest, meeting)
    assert called == []


# ── title stability across recompiles ──────────────────────────────────
#
# A recompile re-runs the SAME retained transcript through a newer template -
# the words spoken never change - so a title that drifts on every pass is a
# model quirk, not a reflection of new content. Left unchecked, every drift
# also orphans the previous minutes file: this corpus already has 32 of them.

def _stub_meeting(minutes_path: str | None) -> db.Meeting:
    """A Meeting with just enough fields for the helpers under test."""
    return db.Meeting(
        id="abc123def456", source_path="", source_name="", audio_path=None,
        meeting_date="2026-08-10", meeting_time="09:00", title_hint=None,
        duration_sec=60.0, status=db.MINUTES_COMPILED, asr_model=None,
        template_version=None, transcript_path=None, minutes_path=minutes_path,
        lightrag_doc_id=None, error=None, created_at="", updated_at="",
    )


def test_existing_title_reads_prior_frontmatter(tmp_path):
    old = tmp_path / "old.md"
    old.write_text("---\ndate: 2026-08-10\ntitle: Prior Title\n---\nbody", encoding="utf-8")
    assert cm._existing_title(_stub_meeting(str(old))) == "Prior Title"


def test_existing_title_is_none_without_a_prior_file():
    assert cm._existing_title(_stub_meeting(None)) is None


def test_existing_title_is_none_when_file_is_gone(tmp_path):
    assert cm._existing_title(_stub_meeting(str(tmp_path / "missing.md"))) is None


def test_build_prompt_anchors_to_the_existing_title(manifest):
    meeting = make_meeting(manifest, "m1", "2026-08-10")
    prompt = cm.build_prompt(
        meeting, build_transcript(), {}, "none", source_audio="audio/x.m4a",
        existing_title="Prior Title",
    )
    assert "Prior Title" in prompt
    assert "reuse it verbatim" in prompt


def test_build_prompt_has_no_title_anchor_on_a_first_compile(manifest):
    meeting = make_meeting(manifest, "m1", "2026-08-10")
    prompt = cm.build_prompt(
        meeting, build_transcript(), {}, "none", source_audio="audio/x.m4a",
    )
    assert "reuse it verbatim" not in prompt


def test_delete_superseded_minutes_removes_the_old_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)
    old = tmp_path / "old.md"
    old.write_text("stale", encoding="utf-8")
    cm._delete_superseded_minutes(_stub_meeting(str(old)), tmp_path / "new.md")
    assert not old.exists()


def test_delete_superseded_minutes_leaves_file_when_path_is_unchanged(tmp_path, monkeypatch):
    """Rewriting the same path is not a rename - nothing should be unlinked."""
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)
    same = tmp_path / "same.md"
    same.write_text("content", encoding="utf-8")
    cm._delete_superseded_minutes(_stub_meeting(str(same)), same)
    assert same.exists()


def test_delete_superseded_minutes_refuses_to_touch_outside_minutes_dir(tmp_path, monkeypatch):
    """Same guard as voices._delete_snippet_files: never unlink outside the
    directory this stage owns, no matter what the manifest says."""
    minutes_dir = tmp_path / "minutes"
    minutes_dir.mkdir()
    monkeypatch.setattr(cm, "MINUTES_DIR", minutes_dir)
    outside = tmp_path / "outside.md"
    outside.write_text("do not touch", encoding="utf-8")
    cm._delete_superseded_minutes(_stub_meeting(str(outside)), minutes_dir / "new.md")
    assert outside.exists()


def test_delete_superseded_minutes_tolerates_an_already_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)
    cm._delete_superseded_minutes(_stub_meeting(str(tmp_path / "gone.md")), tmp_path / "new.md")


def test_delete_superseded_minutes_noop_without_a_prior_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)
    cm._delete_superseded_minutes(_stub_meeting(None), tmp_path / "new.md")


def test_recompile_with_a_stable_title_rewrites_the_same_file(manifest, monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)
    old_path = tmp_path / "2026-08-10-stable-title-abc123de.md"
    old_path.write_text(
        "---\ndate: 2026-08-10\ntitle: Stable Title\n---\n# Stable Title\n\nold body",
        encoding="utf-8",
    )
    prompts: list[str] = []

    def fake_complete(prompt, order=None):
        prompts.append(prompt)
        return "---\ndate: 2026-08-10\ntitle: Stable Title\n---\n# Stable Title\n\nnew body"

    monkeypatch.setattr(cm, "complete", fake_complete)
    meeting = make_meeting(
        manifest, "abc123def456", "2026-08-10",
        status=db.MINUTES_COMPILED, minutes_path=str(old_path),
    )

    path, doc = cm.compile_meeting(manifest, meeting, build_transcript(), {})

    assert "Stable Title" in prompts[0], "the model must see its own previous title"
    assert "reuse it verbatim" in prompts[0]
    assert path == old_path
    assert path.exists()
    assert "new body" in doc


def test_recompile_with_a_changed_title_orphans_nothing(manifest, monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)
    old_path = tmp_path / "2026-08-10-old-title-abc123de.md"
    old_path.write_text(
        "---\ndate: 2026-08-10\ntitle: Old Title\n---\n# Old Title\n\nbody", encoding="utf-8"
    )
    monkeypatch.setattr(
        cm, "complete",
        lambda prompt, order=None: "---\ndate: 2026-08-10\ntitle: New Title\n---\n# New Title\n\nbody",
    )
    meeting = make_meeting(
        manifest, "abc123def456", "2026-08-10",
        status=db.MINUTES_COMPILED, minutes_path=str(old_path),
    )

    path, _ = cm.compile_meeting(manifest, meeting, build_transcript(), {})

    assert path != old_path
    assert path.exists()
    assert not old_path.exists(), "a retitled recompile must not leave the old file behind"


# ── Commitments, decisions and open questions ───────────────────────────
# compile_meeting parses these out of the same document it just got back from
# the model, in the same pass as entities - see pipeline/commitments.py.

_NOTES_DOCUMENT = """---
date: 2026-08-10
title: Notes Test
---

# Notes Test

## Decisions
- **Ship the change today** — decided by Faraz. Rationale: the window closes tonight. [0:01:00]

## Open Questions
- Who owns the follow-up? Faraz needs to resolve this. [0:02:00]

## Action Items
- [ ] **Faraz** — file the change request. Due: 2026-08-11. [0:03:00]
"""


def test_compile_meeting_populates_commitments_decisions_and_open_questions(
    manifest, monkeypatch, tmp_path
):
    monkeypatch.setattr(cm, "complete", lambda prompt, order=None: _NOTES_DOCUMENT)
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)

    meeting = make_meeting(manifest, "m1", "2026-08-10")
    cm.compile_meeting(manifest, meeting, build_transcript(), {})

    commitment_rows = db.list_commitments(manifest)
    assert [r["text"] for r in commitment_rows] == ["file the change request."]
    assert commitment_rows[0]["due_date_iso"] == "2026-08-11"

    decisions = db.get_decisions(manifest, "m1")
    assert decisions[0]["decided_by"] == "Faraz"

    questions = db.get_open_questions(manifest, "m1")
    assert questions[0]["owner"] == "Faraz"


def test_recompile_replaces_rather_than_duplicates_notes(manifest, monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)
    meeting = make_meeting(manifest, "m1", "2026-08-10")

    monkeypatch.setattr(cm, "complete", lambda prompt, order=None: _NOTES_DOCUMENT)
    cm.compile_meeting(manifest, meeting, build_transcript(), {})
    meeting = db.get_meeting(manifest, "m1")  # picks up the just-written minutes_path

    second_pass = _NOTES_DOCUMENT.replace("file the change request", "file the change request v2")
    monkeypatch.setattr(cm, "complete", lambda prompt, order=None: second_pass)
    cm.compile_meeting(manifest, meeting, build_transcript(), {})

    commitment_rows = db.list_commitments(manifest)
    assert [r["text"] for r in commitment_rows] == ["file the change request v2."]
    assert len(db.get_decisions(manifest, "m1")) == 1
    assert len(db.get_open_questions(manifest, "m1")) == 1


def test_owner_canonicalizes_during_compile(manifest, monkeypatch, tmp_path):
    db.add_person(manifest, "Faraz", aliases=["Faraz Matin"])
    doc = _NOTES_DOCUMENT.replace("**Faraz**", "**Faraz Matin**").replace(
        "decided by Faraz", "decided by Faraz Matin"
    )
    monkeypatch.setattr(cm, "complete", lambda prompt, order=None: doc)
    monkeypatch.setattr(cm, "MINUTES_DIR", tmp_path)

    meeting = make_meeting(manifest, "m1", "2026-08-10")
    cm.compile_meeting(manifest, meeting, build_transcript(), {})

    assert db.list_commitments(manifest)[0]["owner"] == "Faraz"
    assert db.get_decisions(manifest, "m1")[0]["decided_by"] == "Faraz"
