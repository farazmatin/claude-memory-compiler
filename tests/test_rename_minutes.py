"""Deterministic planning for names rewritten in compiled minutes."""

from pipeline.rename_minutes import discover_spellings, plan_text


def test_rewrites_a_bare_name():
    result = plan_text("Faraz opened the review.", {"Faraz": "Faraz Mateen"})

    assert result.after == "Faraz Mateen opened the review."
    assert result.matched_spellings == ("Faraz",)
    assert result.match_count == 1


def test_leaves_a_longer_word_alone():
    result = plan_text("Ruth and Ru spoke.", {"Ru": "Ru Farrell"})

    assert result.after == "Ruth and Ru Farrell spoke."
    assert result.match_count == 1


def test_rewrites_overlapping_sources_in_one_order_independent_pass():
    text = "Ru F and Ru joined Ru Farrell."
    expected = "Ru Farrell and Ru Farrell joined Ru Farrell."

    for mappings in (
        {"Ru": "Ru Farrell", "Ru F": "Ru Farrell"},
        {"Ru F": "Ru Farrell", "Ru": "Ru Farrell"},
    ):
        result = plan_text(text, mappings)

        assert result.after == expected
        assert result.matched_spellings == ("Ru F", "Ru")
        assert result.match_count == 2


def test_discovers_exact_historical_casing_without_matching_longer_words():
    spellings = discover_spellings("FARAZ, Faraz, and farazian.", "faraz")

    assert spellings == ("FARAZ", "Faraz")


def test_no_mappings_is_an_explicit_no_op():
    result = plan_text("Nothing changes.", {})

    assert result.before == "Nothing changes."
    assert result.after == "Nothing changes."
    assert result.matched_spellings == ()
    assert result.match_count == 0


def test_rewrites_possessive_and_punctuation_ending_names():
    result = plan_text(
        "J.R.'s call followed Faraz's.",
        {"J.R.": "Jordan Reed", "Faraz": "Faraz Mateen"},
    )

    assert result.after == "Jordan Reed's call followed Faraz Mateen's."
    assert result.matched_spellings == ("J.R.", "Faraz")
    assert result.match_count == 2


def test_protects_an_already_correct_target_and_inserts_backslashes_literally():
    result = plan_text(
        r"Faraz Mateen asked Bob to own it.",
        {"Faraz": "Faraz Mateen", "Bob": r"R\D"},
    )

    assert result.after == r"Faraz Mateen asked R\D to own it."
    assert result.matched_spellings == ("Bob",)
    assert result.match_count == 1


def test_common_word_names_are_counted_exactly_for_preview():
    result = plan_text("May approved the May 5 date.", {"May": "May Chen"})

    assert result.after == "May Chen approved the May Chen 5 date."
    assert result.matched_spellings == ("May",)
    assert result.match_count == 2


def test_replanning_rewritten_text_is_idempotent():
    mappings = {"Faraz": "Faraz Mateen"}
    first = plan_text("Faraz decided.", mappings)
    second = plan_text(first.after, mappings)

    assert second.before == "Faraz Mateen decided."
    assert second.after == second.before
    assert second.matched_spellings == ()
    assert second.match_count == 0
