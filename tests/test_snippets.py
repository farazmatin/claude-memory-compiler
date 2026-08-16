"""Snippet selection: the clips the owner actually hears.

These rules decide whether a labelling card is answerable. The owner cannot
label from transcript text - only by ear - so a card offering six seconds of
crosstalk or silence is not a degraded experience, it is an unanswerable
question that trains them to guess.
"""

from __future__ import annotations

from itertools import pairwise

from pipeline import voices


def dense_words(start: float, end: float, step: float = 0.4):
    """Word spans covering a range solidly, i.e. continuous speech."""
    words, cursor = [], start
    while cursor < end:
        words.append((cursor, min(cursor + step * 0.9, end)))
        cursor += step
    return words


def test_clips_skip_the_opening_window():
    """Meeting openings are join noise and overlapping greetings."""
    regions = [(0.0, 300.0)]
    chosen, _ = voices.choose_snippets(regions, skip_opening_sec=90.0)
    assert chosen
    assert all(start >= 90.0 for start, _ in chosen)


def test_clips_never_span_a_speaker_change():
    """Two voices in one clip makes the card unanswerable."""
    regions = [(100.0, 108.0), (200.0, 208.0), (300.0, 308.0)]
    chosen, _ = voices.choose_snippets(regions, clip_sec=6.0)
    for start, end in chosen:
        assert any(rs <= start and end <= re for rs, re in regions)


def test_clips_are_spread_apart():
    """Three slices of one sentence is one piece of evidence, not three."""
    regions = [(100.0, 400.0)]
    chosen, _ = voices.choose_snippets(regions, count=3, min_separation_sec=60.0)
    starts = sorted(start for start, _ in chosen)
    assert len(chosen) == 3
    assert all(b - a >= 60.0 for a, b in pairwise(starts))


def test_a_region_shorter_than_one_clip_yields_nothing_from_it():
    chosen, quality = voices.choose_snippets([(100.0, 103.0)], clip_sec=6.0)
    assert chosen == []
    assert quality == voices.QUALITY_LOW


def test_mostly_silent_candidates_are_rejected():
    """A clip of someone breathing is not evidence of who they are."""
    regions = [(100.0, 200.0), (300.0, 400.0)]
    sparse = [(100.5, 100.7), (301.0, 301.2)]
    _, quality = voices.choose_snippets(regions, words=sparse)
    assert quality == voices.QUALITY_LOW


def test_dense_speech_is_accepted_at_full_quality():
    regions = [(100.0, 400.0)]
    chosen, quality = voices.choose_snippets(regions, words=dense_words(100.0, 400.0))
    assert len(chosen) == 3
    assert quality == voices.QUALITY_OK


def test_word_dense_candidates_are_preferred_over_silent_ones():
    """Given a choice, hand the owner the clip with speech in it."""
    regions = [(100.0, 130.0), (300.0, 330.0)]
    words = dense_words(300.0, 330.0)
    chosen, _ = voices.choose_snippets(regions, words=words, count=1)
    assert chosen
    assert chosen[0][0] >= 300.0


def test_a_short_meeting_still_produces_a_clip_marked_low():
    """Better a poor clip than none: an unlabellable meeting is worse than a hard one."""
    regions = [(10.0, 40.0)]
    chosen, quality = voices.choose_snippets(regions, skip_opening_sec=90.0)
    assert chosen
    assert quality == voices.QUALITY_LOW


def test_fewer_clips_than_requested_is_flagged_low():
    regions = [(100.0, 107.0)]
    chosen, quality = voices.choose_snippets(regions, count=3)
    assert len(chosen) < 3
    assert quality == voices.QUALITY_LOW


def test_no_regions_at_all_is_handled():
    chosen, quality = voices.choose_snippets([])
    assert chosen == []
    assert quality == voices.QUALITY_LOW
