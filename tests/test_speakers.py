"""Speaker resolution.

The governing rule for this module: an unresolved label must stay unresolved. A
visible `SPEAKER_01` is fixable; a confidently wrong name silently assigns work to
the wrong person and nobody notices.
"""

from __future__ import annotations

from pipeline import db
from pipeline import speakers as spk
from pipeline.asr import Segment, Transcript

from .conftest import make_meeting


def two_speaker_transcript(dominant_seconds: float = 100.0) -> Transcript:
    """Two speakers, the first talking much more than the second."""
    return Transcript(
        meeting_id="abc123", model="test", language="en", duration_sec=600.0,
        segments=[
            Segment(start=0.0, end=dominant_seconds, text="long stretch",
                    speaker="SPEAKER_00"),
            Segment(start=dominant_seconds, end=dominant_seconds + 30.0,
                    text="short reply", speaker="SPEAKER_01"),
        ],
    )


def n_speaker_transcript(count: int) -> Transcript:
    return Transcript(
        meeting_id="d", model="t", language="en", duration_sec=float(count),
        segments=[
            Segment(start=float(i), end=float(i + 1), text="x", speaker=f"SPEAKER_0{i}")
            for i in range(count)
        ],
    )


# ── candidates, not mappings ──────────────────────────────────────────

def test_candidates_returns_names_without_assigning_labels():
    """Regression: the filename says who was present, not which label is which.

    An earlier version assumed the dominant speaker was the recorder. That is wrong
    whenever the other person talks more - stakeholder interviews, user research,
    demos - and produced confidently reversed names.
    """
    candidates = spk.candidates_from_filename(two_speaker_transcript(), "Ali", "Faraz")
    assert candidates == ["Faraz", "Ali"]
    assert isinstance(candidates, list), "must not be a label->name mapping"


def test_candidates_are_independent_of_who_talks_more():
    """The same candidates regardless of speaking time - no dominance inference."""
    quiet_recorder = spk.candidates_from_filename(
        two_speaker_transcript(dominant_seconds=10.0), "Ali", "Faraz"
    )
    loud_recorder = spk.candidates_from_filename(
        two_speaker_transcript(dominant_seconds=500.0), "Ali", "Faraz"
    )
    assert quiet_recorder == loud_recorder


def test_candidates_skips_subject_like_hints():
    assert spk.candidates_from_filename(
        two_speaker_transcript(), "quarterly roadmap review", "Faraz"
    ) == ["Faraz"]


def test_candidates_empty_for_group_meetings():
    assert spk.candidates_from_filename(n_speaker_transcript(4), "Ali", "Faraz") == []


def test_candidates_without_owner_still_offers_the_hint():
    assert spk.candidates_from_filename(two_speaker_transcript(), "Ali", None) == ["Ali"]


# ── prompt construction ───────────────────────────────────────────────

def test_prompt_tells_model_candidates_are_unordered():
    prompt = spk.build_resolution_prompt(
        two_speaker_transcript(), ["Faraz"], "Ali", ["Faraz", "Ali"]
    )
    assert "which label is which is NOT known" in prompt
    assert "null" in prompt, "must offer an explicit unknown option"


def test_prompt_includes_known_names_for_spelling_consistency():
    """Inconsistent spellings fragment graph entities."""
    prompt = spk.build_resolution_prompt(two_speaker_transcript(), ["Michael"], None)
    assert "Michael" in prompt
    assert "fragment" in prompt


# ── LLM parsing ───────────────────────────────────────────────────────

def test_llm_nulls_are_dropped_not_stringified(monkeypatch):
    monkeypatch.setattr(
        spk, "complete",
        lambda prompt, order=None: '{"SPEAKER_00": "Faraz", "SPEAKER_01": null}',
    )
    resolved = spk.resolve_with_llm(two_speaker_transcript(), [], "Ali")
    assert resolved == {"SPEAKER_00": "Faraz"}, "null must mean unresolved, not 'None'"


def test_llm_output_in_fences_is_parsed(monkeypatch):
    monkeypatch.setattr(
        spk, "complete",
        lambda prompt, order=None: '```json\n{"SPEAKER_00": "Faraz"}\n```',
    )
    assert spk.resolve_with_llm(two_speaker_transcript(), [], None) == {"SPEAKER_00": "Faraz"}


def test_llm_hallucinated_labels_are_discarded(monkeypatch):
    monkeypatch.setattr(
        spk, "complete",
        lambda prompt, order=None: '{"SPEAKER_00": "Faraz", "SPEAKER_99": "Ghost"}',
    )
    assert spk.resolve_with_llm(two_speaker_transcript(), [], None) == {"SPEAKER_00": "Faraz"}


def test_unparseable_llm_output_leaves_labels_unresolved(monkeypatch):
    monkeypatch.setattr(spk, "complete", lambda prompt, order=None: "I think it's Faraz?")
    assert spk.resolve_with_llm(two_speaker_transcript(), [], None) == {}


def test_llm_failure_leaves_labels_unresolved(monkeypatch):
    from pipeline.llm import LLMError

    def boom(prompt, order=None):
        raise LLMError("all providers failed")

    monkeypatch.setattr(spk, "complete", boom)
    assert spk.resolve_with_llm(two_speaker_transcript(), [], None) == {}


# ── overrides ─────────────────────────────────────────────────────────

def test_overrides_merge_default_and_meeting_specific():
    merged = spk.overrides_for(
        "abc123def",
        {"default": {"SPEAKER_00": "Faraz"}, "abc123": {"SPEAKER_01": "Ali"}},
    )
    assert merged == {"SPEAKER_00": "Faraz", "SPEAKER_01": "Ali"}


def test_meeting_specific_override_beats_default():
    merged = spk.overrides_for(
        "abc123", {"default": {"SPEAKER_00": "Faraz"}, "abc123": {"SPEAKER_00": "Ali"}}
    )
    assert merged == {"SPEAKER_00": "Ali"}


def test_unreadable_override_file_does_not_halt_the_batch(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("{{{ not yaml", encoding="utf-8")
    assert spk.load_overrides(bad) == {}


def test_missing_override_file_is_fine(tmp_path):
    assert spk.load_overrides(tmp_path / "nope.yaml") == {}


# ── full cascade ──────────────────────────────────────────────────────

def test_overrides_win_over_llm(manifest, monkeypatch, tmp_path):
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text("default:\n  SPEAKER_00: RealName\n", encoding="utf-8")
    monkeypatch.setattr(spk, "SPEAKER_OVERRIDES_FILE", overrides)
    monkeypatch.setattr(
        spk, "complete", lambda prompt, order=None: '{"SPEAKER_00": "LLMGuess"}'
    )

    meeting = make_meeting(manifest, "m1", "2026-08-10", title_hint="Ali")
    resolved = spk.resolve(manifest, meeting, two_speaker_transcript(), owner_name="Faraz")

    assert resolved["SPEAKER_00"] == "RealName"
    speakers = manifest.execute(
        "SELECT label, name, confidence FROM speakers WHERE meeting_id = 'm1' ORDER BY label"
    ).fetchall()
    assert speakers[0]["confidence"] == spk.CONFIDENCE_CONFIRMED


def test_unresolved_labels_are_persisted_as_unknown(manifest, monkeypatch, tmp_path):
    """The gap must be recorded, so it is visible and fixable."""
    monkeypatch.setattr(spk, "SPEAKER_OVERRIDES_FILE", tmp_path / "none.yaml")
    monkeypatch.setattr(spk, "complete", lambda prompt, order=None: '{"SPEAKER_00": "Faraz"}')

    meeting = make_meeting(manifest, "m1", "2026-08-10")
    spk.resolve(manifest, meeting, two_speaker_transcript(), owner_name="Faraz")

    rows = {
        r["label"]: (r["name"], r["confidence"])
        for r in manifest.execute(
            "SELECT label, name, confidence FROM speakers WHERE meeting_id = 'm1'"
        )
    }
    assert rows["SPEAKER_00"] == ("Faraz", spk.CONFIDENCE_INFERRED)
    assert rows["SPEAKER_01"] == (None, spk.CONFIDENCE_UNKNOWN)
    assert db.get_speakers(manifest, "m1") == {"SPEAKER_00": "Faraz"}


def test_no_llm_flag_skips_the_model(manifest, monkeypatch, tmp_path):
    monkeypatch.setattr(spk, "SPEAKER_OVERRIDES_FILE", tmp_path / "none.yaml")
    called = []
    monkeypatch.setattr(spk, "complete", lambda p, order=None: called.append(1) or "{}")

    meeting = make_meeting(manifest, "m1", "2026-08-10", title_hint="Ali")
    resolved = spk.resolve(
        manifest, meeting, two_speaker_transcript(), owner_name="Faraz", use_llm=False
    )
    assert called == []
    assert resolved == {}, "without the LLM there is no evidence to map labels"


def test_transcript_without_diarization_resolves_to_nothing(manifest):
    no_speakers = Transcript(
        meeting_id="m", model="t", language="en", duration_sec=10.0,
        segments=[Segment(start=0.0, end=10.0, text="hello", speaker=None)],
    )
    meeting = make_meeting(manifest, "m1", "2026-08-10")
    assert spk.resolve(manifest, meeting, no_speakers) == {}
