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


# ── never downgrade a resolved label ────────────────────────────────────
#
# db.set_speaker is an unconditional upsert, and resolve() used to call it for
# every label on every pass - including labels the LLM failed to name this
# time. Re-running the resolver over the whole corpus therefore reset already
# -confirmed names back to unknown. These pin the fix at both levels: the pure
# merge decision, and the full resolve() cascade that must apply it.

def test_merge_keeps_a_confirmed_name_when_the_new_pass_finds_nothing():
    kept = spk._merge_with_existing(("Faraz", spk.CONFIDENCE_CONFIRMED), None, spk.CONFIDENCE_UNKNOWN)
    assert kept == ("Faraz", spk.CONFIDENCE_CONFIRMED)


def test_merge_keeps_a_confirmed_name_against_a_weaker_inferred_guess():
    """A human's confirmation must not be second-guessed by a later LLM pass."""
    kept = spk._merge_with_existing(
        ("Faraz", spk.CONFIDENCE_CONFIRMED), "SomeoneElse", spk.CONFIDENCE_INFERRED
    )
    assert kept == ("Faraz", spk.CONFIDENCE_CONFIRMED)


def test_merge_lets_a_new_confirmed_value_replace_an_old_one():
    """A human editing speaker-overrides.yaml a second time must still win."""
    replaced = spk._merge_with_existing(
        ("Faraz", spk.CONFIDENCE_CONFIRMED), "Ali", spk.CONFIDENCE_CONFIRMED
    )
    assert replaced == ("Ali", spk.CONFIDENCE_CONFIRMED)


def test_merge_keeps_an_inferred_name_when_the_new_pass_finds_nothing():
    """A visible gap turning into an invisible loss is the exact bug this
    guards against - silence on a later pass is not evidence of anything."""
    kept = spk._merge_with_existing(("Ali", spk.CONFIDENCE_INFERRED), None, spk.CONFIDENCE_UNKNOWN)
    assert kept == ("Ali", spk.CONFIDENCE_INFERRED)


def test_merge_lets_a_fresh_inferred_guess_update_an_older_one():
    """Only nulling-out and downgrading a confirmed row are blocked - a new,
    equally-weak guess replacing an old one is normal cascade behaviour."""
    updated = spk._merge_with_existing(("Ali", spk.CONFIDENCE_INFERRED), "Alistair", spk.CONFIDENCE_INFERRED)
    assert updated == ("Alistair", spk.CONFIDENCE_INFERRED)


def test_merge_resolves_a_fresh_label_normally():
    fresh = spk._merge_with_existing(None, "Faraz", spk.CONFIDENCE_INFERRED)
    assert fresh == ("Faraz", spk.CONFIDENCE_INFERRED)


def test_resolve_does_not_erase_a_confirmed_name_on_a_later_pass(manifest, monkeypatch, tmp_path):
    """End-to-end regression: a second `resolve()` call where the LLM comes
    back empty must not wipe out a name a human already confirmed."""
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text("default:\n  SPEAKER_00: Faraz\n", encoding="utf-8")
    monkeypatch.setattr(spk, "SPEAKER_OVERRIDES_FILE", overrides)
    monkeypatch.setattr(spk, "complete", lambda prompt, order=None: "{}")

    meeting = make_meeting(manifest, "m1", "2026-08-10")
    spk.resolve(manifest, meeting, two_speaker_transcript())

    # The override file is gone, as if the human never wrote it and this pass
    # is relying purely on the (now silent) LLM - the confirmed row must survive.
    monkeypatch.setattr(spk, "SPEAKER_OVERRIDES_FILE", tmp_path / "gone.yaml")
    resolved = spk.resolve(manifest, meeting, two_speaker_transcript())

    assert resolved.get("SPEAKER_00") == "Faraz"
    row = manifest.execute(
        "SELECT name, confidence FROM speakers WHERE meeting_id = 'm1' AND label = 'SPEAKER_00'"
    ).fetchone()
    assert row["name"] == "Faraz"
    assert row["confidence"] == spk.CONFIDENCE_CONFIRMED


def test_resolve_does_not_erase_an_inferred_name_on_a_later_pass(manifest, monkeypatch, tmp_path):
    monkeypatch.setattr(spk, "SPEAKER_OVERRIDES_FILE", tmp_path / "none.yaml")
    monkeypatch.setattr(spk, "complete", lambda prompt, order=None: '{"SPEAKER_00": "Faraz"}')

    meeting = make_meeting(manifest, "m1", "2026-08-10")
    spk.resolve(manifest, meeting, two_speaker_transcript())

    # A later pass that returns nothing for this label must not blank it out.
    monkeypatch.setattr(spk, "complete", lambda prompt, order=None: "{}")
    resolved = spk.resolve(manifest, meeting, two_speaker_transcript())

    assert resolved.get("SPEAKER_00") == "Faraz"
    assert db.get_speakers(manifest, "m1") == {"SPEAKER_00": "Faraz"}


# ── glossary as a candidate source ───────────────────────────────────────

def test_glossary_people_reads_only_the_people_section(tmp_path, monkeypatch):
    glossary = tmp_path / "glossary.md"
    glossary.write_text(
        "# Glossary\n\n"
        "## People\n\n"
        "- Faraz\n"
        "- Ali - the co-founder\n\n"
        "## Acronyms and jargon\n\n"
        "- PRD\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(spk, "GLOSSARY_FILE", glossary)
    assert spk.glossary_people() == ["Faraz", "Ali"]


def test_glossary_people_missing_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(spk, "GLOSSARY_FILE", tmp_path / "nope.md")
    assert spk.glossary_people() == []


def test_glossary_people_feed_the_resolution_candidates(manifest, monkeypatch, tmp_path):
    glossary = tmp_path / "glossary.md"
    glossary.write_text("## People\n\n- Christine\n", encoding="utf-8")
    monkeypatch.setattr(spk, "GLOSSARY_FILE", glossary)
    monkeypatch.setattr(spk, "SPEAKER_OVERRIDES_FILE", tmp_path / "none.yaml")

    seen_prompts: list[str] = []

    def fake_complete(prompt, order=None):
        seen_prompts.append(prompt)
        return "{}"

    monkeypatch.setattr(spk, "complete", fake_complete)
    meeting = make_meeting(manifest, "m1", "2026-08-10")
    spk.resolve(manifest, meeting, two_speaker_transcript())

    assert "Christine" in seen_prompts[0], "a glossary name is a candidate even with no filename hint"


# ── direct address as evidence ───────────────────────────────────────────

def test_direct_address_catches_a_thanks_greeting():
    transcript = Transcript(
        meeting_id="m", model="t", language="en", duration_sec=10.0,
        segments=[Segment(start=0.0, end=2.0, text="Thanks, Ruth, that's helpful.", speaker="SPEAKER_00")],
    )
    assert "Ruth" in spk.direct_address_names(transcript)


def test_direct_address_catches_a_vocative_at_the_end_of_a_turn():
    transcript = Transcript(
        meeting_id="m", model="t", language="en", duration_sec=10.0,
        segments=[Segment(start=0.0, end=2.0, text="What do you think, Paul", speaker="SPEAKER_00")],
    )
    assert "Paul" in spk.direct_address_names(transcript)


def test_direct_address_ignores_filler_words():
    transcript = Transcript(
        meeting_id="m", model="t", language="en", duration_sec=10.0,
        segments=[Segment(start=0.0, end=2.0, text="Thanks, right, sounds good.", speaker="SPEAKER_00")],
    )
    assert spk.direct_address_names(transcript) == []


def test_direct_address_names_are_deduplicated():
    transcript = Transcript(
        meeting_id="m", model="t", language="en", duration_sec=10.0,
        segments=[
            Segment(start=0.0, end=2.0, text="Thanks, Ruth.", speaker="SPEAKER_00"),
            Segment(start=2.0, end=4.0, text="No problem, Ruth", speaker="SPEAKER_01"),
        ],
    )
    assert spk.direct_address_names(transcript) == ["Ruth"]


# ── time-bounded introduction window ────────────────────────────────────

def test_dialogue_excerpt_covers_the_full_intro_window_even_when_choppy():
    """A fast back-and-forth must not burn through the intro window before the
    self-introductions happen - see INTRO_WINDOW_SEC."""
    # 80 one-second segments: introductions land at second 60, well inside
    # INTRO_WINDOW_SEC (240s), but past the old fixed 50-segment cutoff.
    segments = [
        Segment(start=float(i), end=float(i + 1), text=f"filler {i}", speaker="SPEAKER_00")
        for i in range(80)
    ]
    segments[60] = Segment(start=60.0, end=61.0, text="Hi, I'm Ruth", speaker="SPEAKER_01")
    transcript = Transcript(
        meeting_id="m", model="t", language="en", duration_sec=80.0, segments=segments
    )
    excerpt = spk.dialogue_excerpt(transcript, max_lines=70)
    assert "Hi, I'm Ruth" in excerpt


# ── folding near-duplicate identities ────────────────────────────────────

def _confirm(conn, meeting_id: str, name: str) -> None:
    """Register one confirmed speaker row so `name` gains meeting history.

    Mirrors what resolve() itself does: a speaker row alone does not put a
    name in the people registry, `add_person` does - so both calls are needed
    to reproduce real meeting history for fold_into_existing_person to see.
    """
    make_meeting(conn, meeting_id, "2026-08-10")
    db.set_speaker(conn, meeting_id, "SPEAKER_00", name, spk.CONFIDENCE_CONFIRMED)
    db.add_person(conn, name)


def test_fold_prefers_the_name_with_more_meeting_history(manifest):
    for i in range(3):
        _confirm(manifest, f"faraz{i}", "Faraz")
    _confirm(manifest, "farazmatin0", "Faraz Matin")

    assert spk.fold_into_existing_person(manifest, "Faraz Matin") == "Faraz"
    assert spk.fold_into_existing_person(manifest, "Faraz") == "Faraz"


def test_fold_declines_when_a_bare_name_is_ambiguous(manifest):
    """Three different Pauls already in the registry - a bare "Paul" cannot
    be assumed to be any particular one of them."""
    _confirm(manifest, "graham0", "Paul Graham")
    _confirm(manifest, "mclean0", "Paul McLean")
    _confirm(manifest, "wood0", "Paul Wood")

    assert spk.fold_into_existing_person(manifest, "Paul") == "Paul"


def test_fold_returns_the_name_unchanged_when_nothing_matches(manifest):
    _confirm(manifest, "m1", "Faraz")
    assert spk.fold_into_existing_person(manifest, "Someone New") == "Someone New"


def test_fold_does_not_conflate_unrelated_single_token_names(manifest):
    """Spelling-similarity folding is deliberately not implemented - see the
    docstring on fold_into_existing_person for why."""
    _confirm(manifest, "m1", "Tarun")
    assert spk.fold_into_existing_person(manifest, "Varun") == "Varun"


def test_resolve_folds_a_fuller_name_and_registers_the_alias(manifest, monkeypatch, tmp_path):
    _confirm(manifest, "history0", "Faraz")
    _confirm(manifest, "history1", "Faraz")

    overrides = tmp_path / "overrides.yaml"
    overrides.write_text("default:\n  SPEAKER_00: Faraz Matin\n", encoding="utf-8")
    monkeypatch.setattr(spk, "SPEAKER_OVERRIDES_FILE", overrides)
    monkeypatch.setattr(spk, "complete", lambda prompt, order=None: "{}")

    meeting = make_meeting(manifest, "new-meeting", "2026-08-11")
    resolved = spk.resolve(manifest, meeting, two_speaker_transcript())

    assert resolved["SPEAKER_00"] == "Faraz"
    alias = manifest.execute(
        "SELECT canonical FROM person_aliases WHERE alias = 'faraz matin'"
    ).fetchone()
    assert alias["canonical"] == "Faraz"


def test_merge_label_never_downgrades_a_confirmed_row(manifest):
    """The single-label wrapper carries the same guard as the whole-meeting pass.

    `db.set_speaker` is an unconditional upsert, so a machine pass reaching for
    it directly would quietly replace a name a human confirmed by ear.
    """
    make_meeting(manifest, "m1", "2026-08-10")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Ali", spk.CONFIDENCE_CONFIRMED)

    stored = spk.merge_label(manifest, "m1", "SPEAKER_00", "Alison", spk.CONFIDENCE_INFERRED)

    assert stored == ("Ali", spk.CONFIDENCE_CONFIRMED)
    row = manifest.execute(
        "SELECT name, confidence FROM speakers WHERE meeting_id = 'm1'"
    ).fetchone()
    assert (row["name"], row["confidence"]) == ("Ali", spk.CONFIDENCE_CONFIRMED)


def test_merge_label_fills_an_empty_label(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    assert spk.merge_label(manifest, "m1", "SPEAKER_00", "Ali", spk.CONFIDENCE_INFERRED) == (
        "Ali", spk.CONFIDENCE_INFERRED
    )


def test_resolve_records_its_conclusion_as_the_voice_matchers_cross_check(
    manifest, monkeypatch, tmp_path
):
    """`band()` reads a missing llm_name as "no disagreement", so the LLM veto
    on auto-applying a name is absent wherever this is unset - which was every
    row, because only the retired local enrollment stage ever wrote it.
    """
    monkeypatch.setattr(spk, "load_overrides", lambda: {})
    monkeypatch.setattr(spk, "glossary_people", list)
    monkeypatch.setattr(
        spk, "resolve_with_llm", lambda *a, **k: {"SPEAKER_00": "Ali", "SPEAKER_01": "Ruth"}
    )
    meeting = make_meeting(manifest, "m1", "2026-08-10")
    db.upsert_speaker_match(manifest, "m1", "SPEAKER_00", embedding=b"x" * 4, dim=1)

    spk.resolve(manifest, meeting, two_speaker_transcript())

    assert db.get_speaker_match(manifest, "m1", "SPEAKER_00")["llm_name"] == "Ali"


def test_resolve_does_not_manufacture_match_rows(manifest, monkeypatch, tmp_path):
    """A match row with no embedding is invisible to matching, clustering and
    the review queue alike, so creating one would be pure table pollution."""
    monkeypatch.setattr(spk, "load_overrides", lambda: {})
    monkeypatch.setattr(spk, "glossary_people", list)
    monkeypatch.setattr(spk, "resolve_with_llm", lambda *a, **k: {"SPEAKER_00": "Ali"})
    meeting = make_meeting(manifest, "m1", "2026-08-10")

    spk.resolve(manifest, meeting, two_speaker_transcript())

    assert manifest.execute("SELECT COUNT(*) AS n FROM speaker_matches").fetchone()["n"] == 0


def _synthetic(labels: int, segments: int, words: int = 5) -> Transcript:
    return Transcript(
        meeting_id="x", model="m", language="en", duration_sec=segments * 3.0,
        segments=[
            Segment(start=i * 3.0, end=i * 3.0 + 2.0, text=" ".join(["word"] * words),
                    speaker=f"SPEAKER_{i % labels:02d}")
            for i in range(segments)
        ],
    )


def test_a_normal_meeting_is_shown_in_full():
    """Measured against 37 confirmed labels: sampling cost the resolver 18% vs
    73% correct, because most misses were names spoken outside the window."""
    assert len(spk.dialogue_excerpt(_synthetic(5, 600)).splitlines()) == 600


def test_a_crowded_meeting_is_still_sampled():
    """Past eight labels the full transcript bought each extra correct answer
    with an extra wrong one; at nine or more it was net negative."""
    assert len(spk.dialogue_excerpt(_synthetic(12, 600)).splitlines()) < 600


def test_the_gate_sits_below_the_measured_crossover():
    """Six labels is the last count measured clean; eight is where wrong answers
    started matching correct ones, so the gate must not sit at eight."""
    assert len(spk.dialogue_excerpt(_synthetic(6, 600)).splitlines()) == 600
    assert len(spk.dialogue_excerpt(_synthetic(7, 600)).splitlines()) < 600
    assert len(spk.dialogue_excerpt(_synthetic(8, 600)).splitlines()) < 600


def test_an_over_budget_transcript_falls_back_to_the_sample():
    """The sample, not a truncation - cutting the tail drops the end of the
    meeting silently, and that is where a late joiner introduces themselves."""
    lines = len(spk.dialogue_excerpt(_synthetic(5, 600, words=400)).splitlines())
    assert lines < 600


def test_a_short_meeting_is_shown_in_full_however_crowded():
    assert len(spk.dialogue_excerpt(_synthetic(12, 40)).splitlines()) == 40
