"""Producer tests: audio + transcript -> speaker_matches / voice_samples rows.

`pipeline/enroll.py` is the only writer of the three voices.py tables, so these
tests carry weight similar to `test_voices.py`'s banding tests - a regression
here means the tables stay empty exactly as they were before this module
existed, just quietly instead of obviously.

Real pyannote inference and real ffmpeg are never exercised here (see
`test_replicate_asr.py`'s "avoid needing ffmpeg in unit test" precedent):
`embed_label` and `write_snippets` are the seams every test monkeypatches.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from pipeline import db, enroll, voices
from tests.conftest import make_meeting

MODEL = "test-embed-v1"


def vec(*values: float) -> np.ndarray:
    return np.asarray(values, dtype="float64")


def make_transcribed_meeting(
    conn, meeting_id: str, *, labels: dict[str, tuple[float, float]], date="2026-08-01"
):
    """A meeting with real audio_path/transcript_path files on disk, and a
    retained transcript whose segments carry the given label -> (start, end)
    speech spans - the shape `eligible_meetings` and `enroll_meeting` require.
    """
    from pipeline import asr
    from pipeline.config import AUDIO_DIR

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = AUDIO_DIR / f"{meeting_id}.wav"
    audio_path.write_bytes(b"RIFF" + b"\x00" * 40)  # never decoded in these tests

    segments = [
        asr.Segment(start=start, end=end, text="hello there", speaker=label)
        for label, (start, end) in labels.items()
    ]
    transcript = asr.Transcript(
        meeting_id=meeting_id, model="test", language="en", duration_sec=600.0, segments=segments
    )
    transcript_path = asr.save_transcript(transcript)

    make_meeting(
        conn, meeting_id, date,
        audio_path=str(audio_path), transcript_path=str(transcript_path), status=db.TRANSCRIBED,
    )
    return db.get_meeting(conn, meeting_id)


def stub_audio_decode(monkeypatch):
    """No real ffmpeg: `_load_waveform` is the only thing in this module that
    touches it, and every path that embeds calls it to build embed_label's
    argument BEFORE embed_label runs - so mocking embed_label alone still
    hits real ffmpeg on the fake WAV bytes these tests write to disk.
    """
    monkeypatch.setattr(enroll, "_load_waveform", lambda _path: np.zeros(1))


def stub_embed(monkeypatch, vector=None):
    """Replace both the audio decode and the pyannote call.

    Still honours the real function's contract that no regions means no
    embedding - several tests rely on that to represent a vanished label.
    """
    stub_audio_decode(monkeypatch)
    result = vector if vector is not None else vec(1.0, 0.0)

    def fake(_waveform, regions, *, model_name=MODEL):
        return result if regions else None

    monkeypatch.setattr(enroll, "embed_label", fake)


def ensure_meetings(conn, samples) -> None:
    """voice_samples.meeting_id is a real foreign key (see test_voices.py's
    own `add_pending` helper) - the accuracy tests use synthetic meeting ids
    that need a minimal row to satisfy it, exactly like production ones would
    already have from `eligible_meetings()`."""
    for _name, meeting_id, *_rest in samples:
        if not db.get_meeting(conn, meeting_id):
            make_meeting(conn, meeting_id, "2026-08-15")


def stub_write_snippets(monkeypatch):
    """No real ffmpeg: record what would have been cut, return fake paths."""
    calls = []

    def fake(audio_path, meeting_id, label, spans):
        calls.append((meeting_id, label, spans))
        return [f"{meeting_id}/{label}-{i}.opus" for i in range(len(spans))]

    monkeypatch.setattr(enroll, "write_snippets", fake)
    return calls


# ── Eligibility ───────────────────────────────────────────────────────

def test_eligible_meetings_requires_audio_and_transcript_on_disk(manifest):
    with_both = make_transcribed_meeting(
        manifest, "m-both", labels={"SPEAKER_00": (0.0, 60.0)}
    )
    make_meeting(manifest, "m-neither", "2026-08-02")  # no audio_path at all

    result = enroll.eligible_meetings(manifest)
    assert [m.id for m in result] == [with_both.id]


def test_eligible_meetings_excludes_a_missing_audio_file(manifest, tmp_path):
    make_meeting(
        manifest, "m-deleted", "2026-08-03",
        audio_path=str(tmp_path / "gone.wav"), transcript_path=str(tmp_path / "gone.json"),
    )
    assert enroll.eligible_meetings(manifest) == []


# ── Region / word extraction ─────────────────────────────────────────

def test_label_regions_use_segment_speaker_not_word_speaker():
    from pipeline import asr

    segments = [
        asr.Segment(start=0.0, end=5.0, text="hi", speaker="SPEAKER_00"),
        asr.Segment(start=5.0, end=9.0, text="bye", speaker="SPEAKER_01"),
    ]
    transcript = asr.Transcript(
        meeting_id="m1", model="t", language="en", duration_sec=9.0, segments=segments
    )
    assert enroll._label_regions(transcript, "SPEAKER_00") == [(0.0, 5.0)]
    assert enroll._label_regions(transcript, "SPEAKER_01") == [(5.0, 9.0)]


def test_select_embed_regions_caps_total_duration():
    regions = [(0.0, 40.0), (40.0, 80.0), (80.0, 120.0)]
    chosen = enroll._select_embed_regions(regions, max_sec=50.0)
    assert sum(e - s for s, e in chosen) == pytest.approx(50.0)
    assert chosen[0] == (0.0, 40.0)


def test_embed_label_returns_none_with_no_regions():
    assert enroll.embed_label(np.zeros(16_000), []) is None


def test_embed_label_concatenates_regions_before_the_model_sees_them(monkeypatch):
    """Two disjoint regions must reach the model as one contiguous clip, not
    two calls averaged after the fact - see the module docstring on why."""
    seen = {}

    class FakeInference:
        def __call__(self, payload):
            seen["length"] = payload["waveform"].shape[-1]
            seen["sample_rate"] = payload["sample_rate"]
            return np.array([1.0, 0.0, 0.0])

    monkeypatch.setattr(enroll, "_embedder", lambda model_name=MODEL: FakeInference())
    sr = 16_000
    waveform = np.ones(sr * 10, dtype="float32")
    vector = enroll.embed_label(waveform, [(0.0, 2.0), (5.0, 6.0)], model_name=MODEL)

    assert seen["length"] == 3 * sr  # 2s + 1s of speech, silence excluded
    assert vector.tolist() == [1.0, 0.0, 0.0]


# ── Per-meeting enrollment: bootstrap path ───────────────────────────

def test_a_confirmed_label_is_bootstrapped_not_queued_for_review(manifest, monkeypatch):
    meeting = make_transcribed_meeting(
        manifest, "m-confirmed", labels={"SPEAKER_00": (0.0, 60.0)}
    )
    db.set_speaker(manifest, meeting.id, "SPEAKER_00", "Ali", "confirmed")
    stub_embed(monkeypatch, vec(1.0, 0.0))
    stub_write_snippets(monkeypatch)

    result = enroll.enroll_meeting(manifest, meeting, model=MODEL)

    assert result.bootstrapped == ["Ali"]
    samples = db.person_samples(manifest, "Ali", model=MODEL)
    assert len(samples) == 1
    assert samples[0]["source"] == "bootstrap"

    row = db.get_speaker_match(manifest, meeting.id, "SPEAKER_00")
    assert row["state"] == voices.STATE_RESOLVED
    assert row["resolved_as"] == "Ali"
    # A resolved label must not show up waiting for review.
    assert db.pending_matches(manifest, model=MODEL) == []


def test_bootstrapping_twice_does_not_duplicate_the_sample(manifest, monkeypatch):
    meeting = make_transcribed_meeting(
        manifest, "m-confirmed", labels={"SPEAKER_00": (0.0, 60.0)}
    )
    db.set_speaker(manifest, meeting.id, "SPEAKER_00", "Ali", "confirmed")
    stub_embed(monkeypatch, vec(1.0, 0.0))
    stub_write_snippets(monkeypatch)

    enroll.enroll_meeting(manifest, meeting, model=MODEL)
    enroll.enroll_meeting(manifest, meeting, model=MODEL)

    assert len(db.person_samples(manifest, "Ali", model=MODEL)) == 1


def test_bootstrapping_reuses_a_previously_embedded_vector(manifest, monkeypatch):
    """A name confirmed AFTER the first enroll pass must not re-run pyannote -
    it reuses the embedding already sitting in speaker_matches."""
    meeting = make_transcribed_meeting(
        manifest, "m-late-confirm", labels={"SPEAKER_00": (0.0, 60.0)}
    )
    calls = []

    def counting_embed(_waveform, regions, *, model_name=MODEL):
        calls.append(regions)
        return vec(1.0, 0.0)

    stub_audio_decode(monkeypatch)
    monkeypatch.setattr(enroll, "embed_label", counting_embed)
    stub_write_snippets(monkeypatch)

    # First pass: unresolved, gets embedded once and left pending.
    enroll.enroll_meeting(manifest, meeting, model=MODEL)
    assert len(calls) == 1
    assert db.get_speaker_match(manifest, meeting.id, "SPEAKER_00")["state"] == "pending"

    # The owner confirms the name through the dashboard, independent of voice review.
    db.set_speaker(manifest, meeting.id, "SPEAKER_00", "Ali", "confirmed")
    enroll.enroll_meeting(manifest, meeting, model=MODEL)

    assert len(calls) == 1  # no second pyannote call
    assert db.person_samples(manifest, "Ali", model=MODEL)[0]["source"] == "bootstrap"


# ── Per-meeting enrollment: pending path ─────────────────────────────

def test_an_unconfirmed_label_is_written_pending_with_llm_name(manifest, monkeypatch):
    meeting = make_transcribed_meeting(
        manifest, "m-inferred", labels={"SPEAKER_00": (0.0, 60.0)}
    )
    db.set_speaker(manifest, meeting.id, "SPEAKER_00", "Sara", "inferred")
    stub_embed(monkeypatch, vec(0.5, 0.5))
    snippet_calls = stub_write_snippets(monkeypatch)

    result = enroll.enroll_meeting(manifest, meeting, model=MODEL)

    assert result.embedded == 1
    assert result.bootstrapped == []
    row = db.get_speaker_match(manifest, meeting.id, "SPEAKER_00")
    assert row["state"] == "pending"
    assert row["llm_name"] == "Sara"
    assert row["embedding"] is not None
    assert json.loads(row["snippet_paths"]) == [f"{meeting.id}/SPEAKER_00-0.opus"]
    assert snippet_calls  # write_snippets was actually invoked


def test_an_unknown_label_is_written_pending_with_no_llm_name(manifest, monkeypatch):
    meeting = make_transcribed_meeting(
        manifest, "m-unknown", labels={"SPEAKER_00": (0.0, 60.0)}
    )
    stub_embed(monkeypatch, vec(0.1, 0.9))
    stub_write_snippets(monkeypatch)

    enroll.enroll_meeting(manifest, meeting, model=MODEL)

    row = db.get_speaker_match(manifest, meeting.id, "SPEAKER_00")
    assert row["llm_name"] is None
    assert row["state"] == "pending"


def test_rerunning_a_pending_label_skips_recompute_by_default(manifest, monkeypatch):
    meeting = make_transcribed_meeting(
        manifest, "m-idempotent", labels={"SPEAKER_00": (0.0, 60.0)}
    )
    calls = []
    stub_audio_decode(monkeypatch)
    monkeypatch.setattr(
        enroll, "embed_label",
        lambda _w, regions, **kw: (calls.append(1), vec(1.0, 0.0))[1] if regions else None,
    )
    stub_write_snippets(monkeypatch)

    enroll.enroll_meeting(manifest, meeting, model=MODEL)
    result = enroll.enroll_meeting(manifest, meeting, model=MODEL)

    assert len(calls) == 1
    assert result.skipped == 1


def test_force_recomputes_an_already_embedded_label(manifest, monkeypatch):
    meeting = make_transcribed_meeting(
        manifest, "m-forced", labels={"SPEAKER_00": (0.0, 60.0)}
    )
    calls = []
    stub_audio_decode(monkeypatch)
    monkeypatch.setattr(
        enroll, "embed_label",
        lambda _w, regions, **kw: (calls.append(1), vec(1.0, 0.0))[1] if regions else None,
    )
    stub_write_snippets(monkeypatch)

    enroll.enroll_meeting(manifest, meeting, model=MODEL)
    enroll.enroll_meeting(manifest, meeting, model=MODEL, force=True)

    assert len(calls) == 2


def test_llm_name_is_refreshed_even_when_the_embedding_is_skipped(manifest, monkeypatch):
    """speakers.py can rerun and change its guess between enroll passes; that
    is cheap to keep in sync even without redoing the expensive part."""
    meeting = make_transcribed_meeting(
        manifest, "m-refresh", labels={"SPEAKER_00": (0.0, 60.0)}
    )
    stub_embed(monkeypatch, vec(1.0, 0.0))
    stub_write_snippets(monkeypatch)

    enroll.enroll_meeting(manifest, meeting, model=MODEL)
    db.set_speaker(manifest, meeting.id, "SPEAKER_00", "Sara", "inferred")
    enroll.enroll_meeting(manifest, meeting, model=MODEL)

    assert db.get_speaker_match(manifest, meeting.id, "SPEAKER_00")["llm_name"] == "Sara"


def test_one_bad_label_does_not_lose_the_others_in_the_same_meeting(manifest, monkeypatch):
    meeting = make_transcribed_meeting(
        manifest, "m-partial-fail",
        labels={"SPEAKER_00": (0.0, 30.0), "SPEAKER_01": (30.0, 90.0)},
    )

    def flaky_embed(_waveform, regions, *, model_name=MODEL):
        if regions and regions[0][0] == 0.0:
            raise RuntimeError("boom")
        return vec(1.0, 0.0)

    stub_audio_decode(monkeypatch)
    monkeypatch.setattr(enroll, "embed_label", flaky_embed)
    stub_write_snippets(monkeypatch)

    result = enroll.enroll_meeting(manifest, meeting, model=MODEL)

    outcomes = {r.label: r.outcome for r in result.labels}
    assert outcomes["SPEAKER_00"] == "error"
    assert outcomes["SPEAKER_01"] == "embedded"


# ── Finalize: wiring rematch_pending / cluster_pending ───────────────

def test_finalize_promotes_a_pending_label_once_its_person_is_bootstrapped(manifest, monkeypatch):
    """The compounding behaviour the plan promises: a name confirmed via the
    transcript pass, in two OTHER meetings, resolves an unrelated pending
    label in a third meeting without anyone being asked."""
    stub_write_snippets(monkeypatch)
    stub_embed(monkeypatch, vec(1.0, 0.0))

    m1 = make_transcribed_meeting(
        manifest, "m1", labels={"SPEAKER_00": (0.0, 60.0)}, date="2026-08-01"
    )
    db.set_speaker(manifest, m1.id, "SPEAKER_00", "Ali", "confirmed")
    m2 = make_transcribed_meeting(
        manifest, "m2", labels={"SPEAKER_00": (0.0, 60.0)}, date="2026-08-02"
    )
    db.set_speaker(manifest, m2.id, "SPEAKER_00", "Ali", "confirmed")
    m3 = make_transcribed_meeting(
        manifest, "m3", labels={"SPEAKER_00": (0.0, 90.0)}, date="2026-08-03"
    )

    for meeting in (m1, m2, m3):
        enroll.enroll_meeting(manifest, meeting, model=MODEL)

    promoted, clusters = enroll.finalize(manifest, model=MODEL)

    assert promoted == 1
    assert clusters == 0
    row = db.get_speaker_match(manifest, m3.id, "SPEAKER_00")
    assert row["band"] == voices.BAND_AUTO
    assert row["best_canonical"] == "Ali"


# ── Accuracy evaluation ───────────────────────────────────────────────

def test_leave_one_out_accuracy_on_perfectly_separated_voices(manifest):
    samples = [
        ("Ali", "m1", "SPEAKER_00", vec(1.0, 0.0), 60.0),
        ("Ali", "m2", "SPEAKER_00", vec(0.95, 0.05), 60.0),
        ("Sara", "m1", "SPEAKER_01", vec(0.0, 1.0), 60.0),
        ("Sara", "m3", "SPEAKER_00", vec(0.05, 0.95), 60.0),
    ]
    ensure_meetings(manifest, samples)
    result = enroll.leave_one_out_accuracy(manifest, samples, model=MODEL)

    assert result.total == 4  # every sample here belongs to a two-meeting person
    assert result.correct == 4
    assert result.top1 == pytest.approx(1.0)


def test_leave_one_out_accuracy_ignores_single_meeting_people(manifest):
    samples = [
        ("Ali", "m1", "SPEAKER_00", vec(1.0, 0.0), 60.0),
        ("Ali", "m2", "SPEAKER_00", vec(0.95, 0.05), 60.0),
        ("OneOff", "m1", "SPEAKER_01", vec(0.0, 1.0), 60.0),
    ]
    ensure_meetings(manifest, samples)
    result = enroll.leave_one_out_accuracy(manifest, samples, model=MODEL)

    assert result.total == 2  # only Ali's two appearances are testable
    assert "OneOff" not in {name for name, _m, _p in result.misses}


def test_leave_one_out_accuracy_holds_out_the_whole_meeting_not_just_one_label(manifest):
    """Ali split across two labels in the SAME meeting must not let the
    voiceprint recognise its own recording session."""
    samples = [
        ("Ali", "m1", "SPEAKER_00", vec(1.0, 0.0), 30.0),
        ("Ali", "m1", "SPEAKER_02", vec(0.99, 0.01), 30.0),  # over-segmented fragment, same meeting
        ("Ali", "m2", "SPEAKER_00", vec(0.9, 0.1), 60.0),
        ("Sara", "m3", "SPEAKER_00", vec(0.0, 1.0), 60.0),
        ("Sara", "m4", "SPEAKER_00", vec(0.02, 0.98), 60.0),
    ]
    ensure_meetings(manifest, samples)
    enroll.leave_one_out_accuracy(manifest, samples, model=MODEL)

    # After the function returns, every sample must be restored - a
    # leave-one-out measurement must not itself change the enrollment state.
    assert len(db.person_samples(manifest, "Ali", model=MODEL)) == 3
    assert len(db.person_samples(manifest, "Sara", model=MODEL)) == 2


def test_leave_one_out_accuracy_reports_a_real_miss(manifest):
    """Two people close enough in vector space that top-1 gets it wrong -
    the whole point of measuring rather than assuming."""
    samples = [
        ("Ali", "m1", "SPEAKER_00", vec(1.0, 0.0, 0.0), 60.0),
        ("Ali", "m2", "SPEAKER_00", vec(0.5, 0.5, 0.0), 60.0),  # far from its own voiceprint
        ("Sara", "m3", "SPEAKER_00", vec(0.4, 0.6, 0.0), 60.0),
        ("Sara", "m4", "SPEAKER_00", vec(0.45, 0.55, 0.0), 60.0),
    ]
    ensure_meetings(manifest, samples)
    result = enroll.leave_one_out_accuracy(manifest, samples, model=MODEL)

    assert result.total == 4
    assert result.correct < result.total
    assert any(name == "Ali" for name, _m, _pred in result.misses)
