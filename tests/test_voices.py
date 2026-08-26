"""Voice identity: vectors, voiceprints, banding and resolution.

The banding tests carry the most weight. Every guard in `voices.band` exists to
stop a specific wrong outcome - a confident wrong name applied without asking -
and a regression there is silent by nature: nothing errors, a real person is
simply credited with someone else's commitments.
"""

from __future__ import annotations

import numpy as np
import pytest

from pipeline import db, people_merge, voices
from tests.conftest import make_meeting

MODEL = "test-embed-v1"


def vec(*values: float) -> np.ndarray:
    return np.asarray(values, dtype="float64")


def enroll(conn, canonical: str, vector, *, meetings: int = 2, speech_sec: float = 60.0):
    """Register a person with samples from `meetings` distinct meetings.

    Two by default, because one meeting means one seat and the auto band refuses
    to fire below MIN_ENROLL_MEETINGS.
    """
    db.add_person(conn, canonical)
    blob, dim = voices.pack(vector)
    for index in range(meetings):
        meeting_id = f"m-{canonical}-{index}"
        make_meeting(conn, meeting_id, f"2026-08-0{index + 1}")
        db.add_voice_sample(
            conn,
            canonical=canonical,
            meeting_id=meeting_id,
            label="SPEAKER_00",
            embedding=blob,
            dim=dim,
            model=MODEL,
            speech_sec=speech_sec,
        )


def add_pending(conn, meeting_id: str, label: str, vector, *, speech_sec: float = 60.0, **fields):
    if not db.get_meeting(conn, meeting_id):
        make_meeting(conn, meeting_id, "2026-08-15")
    blob, dim = voices.pack(vector)
    db.upsert_speaker_match(
        conn, meeting_id, label,
        embedding=blob, dim=dim, model=MODEL, speech_sec=speech_sec, **fields,
    )


# ── Vector storage ────────────────────────────────────────────────────

def test_pack_unpack_round_trip_preserves_values_and_dim():
    original = vec(0.1, -0.25, 0.75, 0.5)
    blob, dim = voices.pack(original)
    assert dim == 4
    assert np.allclose(voices.unpack(blob, dim), original, atol=1e-6)


def test_unpack_rejects_a_dim_mismatch():
    blob, _ = voices.pack(vec(1.0, 0.0, 0.0))
    with pytest.raises(ValueError):
        voices.unpack(blob, 4)


def test_cosine_identities():
    assert voices.cosine(vec(1.0, 0.0), vec(1.0, 0.0)) == pytest.approx(1.0)
    assert voices.cosine(vec(1.0, 0.0), vec(0.0, 1.0)) == pytest.approx(0.0)
    assert voices.cosine(vec(1.0, 0.0), vec(-1.0, 0.0)) == pytest.approx(-1.0)


def test_normalize_leaves_a_zero_vector_alone():
    """A NaN here would propagate into every score and look like a model bug."""
    assert not np.isnan(voices.normalize(vec(0.0, 0.0))).any()


# ── Voiceprints ───────────────────────────────────────────────────────

def test_voiceprint_is_duration_weighted(manifest):
    """A long sample should pull the voiceprint further than a short one."""
    db.add_person(manifest, "Ali")
    make_meeting(manifest, "m1", "2026-08-01")
    make_meeting(manifest, "m2", "2026-08-02")

    long_blob, dim = voices.pack(vec(1.0, 0.0))
    short_blob, _ = voices.pack(vec(0.0, 1.0))
    db.add_voice_sample(
        manifest, canonical="Ali", meeting_id="m1", label="SPEAKER_00",
        embedding=long_blob, dim=dim, model=MODEL, speech_sec=300.0,
    )
    db.add_voice_sample(
        manifest, canonical="Ali", meeting_id="m2", label="SPEAKER_00",
        embedding=short_blob, dim=dim, model=MODEL, speech_sec=10.0,
    )

    print_ = voices.voiceprint(manifest, "Ali", model=MODEL)
    assert print_[0] > print_[1]


def test_deleting_a_sample_changes_the_voiceprint(manifest):
    """The correction path: a confirmation the owner regrets is one DELETE."""
    enroll(manifest, "Ali", vec(1.0, 0.0))
    make_meeting(manifest, "m-extra", "2026-08-09")
    blob, dim = voices.pack(vec(0.0, 1.0))
    sample_id = db.add_voice_sample(
        manifest, canonical="Ali", meeting_id="m-extra", label="SPEAKER_01",
        embedding=blob, dim=dim, model=MODEL, speech_sec=600.0,
    )

    polluted = voices.voiceprint(manifest, "Ali", model=MODEL)
    db.delete_voice_sample(manifest, sample_id)
    corrected = voices.voiceprint(manifest, "Ali", model=MODEL)

    assert polluted[1] > corrected[1]
    assert corrected[0] == pytest.approx(1.0, abs=1e-6)


def test_voiceprint_ignores_other_models(manifest):
    """Embeddings from different models are not comparable at all."""
    enroll(manifest, "Ali", vec(1.0, 0.0))
    make_meeting(manifest, "m-other", "2026-08-10")
    blob, dim = voices.pack(vec(0.0, 1.0))
    db.add_voice_sample(
        manifest, canonical="Ali", meeting_id="m-other", label="SPEAKER_00",
        embedding=blob, dim=dim, model="a-different-model", speech_sec=900.0,
    )

    assert voices.voiceprint(manifest, "Ali", model=MODEL)[0] == pytest.approx(1.0, abs=1e-6)
    assert voices.enrolled(manifest, "a-different-model").keys() == {"Ali"}


def test_voiceprint_is_none_for_an_unenrolled_person(manifest):
    db.add_person(manifest, "Nobody")
    assert voices.voiceprint(manifest, "Nobody", model=MODEL) is None


# ── Matching ──────────────────────────────────────────────────────────

def test_match_returns_best_and_runner_up(manifest):
    enroll(manifest, "Ali", vec(1.0, 0.0, 0.0))
    enroll(manifest, "Sara", vec(0.0, 1.0, 0.0))
    prints = voices.enrolled(manifest, MODEL)

    result = voices.match(vec(0.9, 0.4, 0.0), prints)
    assert result.best == "Ali"
    assert result.next == "Sara"
    assert result.best_score > result.next_score


def test_match_with_no_enrolled_people_is_empty():
    result = voices.match(vec(1.0, 0.0), {})
    assert result.best is None and result.best_score == 0.0


# ── Banding ───────────────────────────────────────────────────────────

THRESHOLDS = voices.Thresholds(
    auto=0.62, review=0.38, margin=0.12, min_speech_sec=30, min_enroll_meetings=2
)


def test_a_clear_confident_match_is_auto():
    result = voices.MatchResult(best="Ali", best_score=0.80, next="Sara", next_score=0.40)
    assert voices.band(result, 60.0, thresholds=THRESHOLDS, enroll_meetings=2) == voices.BAND_AUTO


def test_a_thin_margin_is_forced_to_review():
    """Two people at the same table can both score highly; the margin catches it."""
    result = voices.MatchResult(best="Ali", best_score=0.80, next="Sara", next_score=0.75)
    assert voices.band(result, 60.0, thresholds=THRESHOLDS, enroll_meetings=2) == voices.BAND_REVIEW


def test_a_short_label_never_auto_applies():
    result = voices.MatchResult(best="Ali", best_score=0.95, next=None, next_score=0.0)
    assert voices.band(result, 5.0, thresholds=THRESHOLDS, enroll_meetings=2) == voices.BAND_REVIEW


def test_one_meeting_of_enrollment_never_auto_applies():
    """The far-field rule: enrolled from one meeting means enrolled from one seat."""
    result = voices.MatchResult(best="Ali", best_score=0.95, next=None, next_score=0.0)
    assert voices.band(result, 90.0, thresholds=THRESHOLDS, enroll_meetings=1) == voices.BAND_REVIEW


def test_llm_disagreement_is_forced_to_review():
    """Two independent signals disagreeing is information, not a tie to break."""
    result = voices.MatchResult(best="Ali", best_score=0.90, next=None, next_score=0.0)
    decided = voices.band(
        result, 90.0, thresholds=THRESHOLDS, enroll_meetings=2, llm_name="Sara"
    )
    assert decided == voices.BAND_REVIEW


def test_llm_agreement_stays_auto():
    result = voices.MatchResult(best="Ali", best_score=0.90, next=None, next_score=0.0)
    decided = voices.band(
        result, 90.0, thresholds=THRESHOLDS, enroll_meetings=2, llm_name="Ali"
    )
    assert decided == voices.BAND_AUTO


def test_over_segmented_meetings_never_auto_apply():
    """One person split across four labels would enroll four bad voiceprints."""
    result = voices.MatchResult(best="Ali", best_score=0.95, next=None, next_score=0.0)
    decided = voices.band(
        result, 90.0, thresholds=THRESHOLDS, enroll_meetings=2, over_segmented=True
    )
    assert decided == voices.BAND_REVIEW


def test_a_weak_score_is_a_new_voice():
    result = voices.MatchResult(best="Ali", best_score=0.20, next=None, next_score=0.0)
    assert voices.band(result, 90.0, thresholds=THRESHOLDS, enroll_meetings=2) == voices.BAND_NEW


def test_sensitivity_dial_shifts_the_thresholds(manifest):
    baseline = voices.Thresholds.load(manifest)
    db.set_setting(manifest, "voice.sensitivity", "cautious")
    cautious = voices.Thresholds.load(manifest)
    db.set_setting(manifest, "voice.sensitivity", "confident")
    confident = voices.Thresholds.load(manifest)

    assert cautious.auto > baseline.auto > confident.auto
    assert cautious.margin > baseline.margin > confident.margin


# ── Clustering ────────────────────────────────────────────────────────

def test_the_same_voice_across_meetings_becomes_one_cluster(manifest):
    """A colleague in three meetings is one question, not three."""
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    add_pending(manifest, "meet-b", "SPEAKER_01", vec(0.98, 0.05, 0.0))
    add_pending(manifest, "meet-c", "SPEAKER_00", vec(0.97, 0.10, 0.0))

    assert voices.cluster_pending(manifest, MODEL) == 1
    clusters = db.pending_clusters(manifest)
    assert clusters[0]["size"] == 3


def test_distinct_voices_stay_separate(manifest):
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    add_pending(manifest, "meet-a", "SPEAKER_01", vec(0.0, 1.0, 0.0))
    assert voices.cluster_pending(manifest, MODEL) == 2


def test_clusters_are_ordered_by_total_speaking_time(manifest):
    """The earliest answers must resolve the most history."""
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0), speech_sec=30.0)
    add_pending(manifest, "meet-b", "SPEAKER_00", vec(0.0, 1.0, 0.0), speech_sec=900.0)
    voices.cluster_pending(manifest, MODEL)

    clusters = db.pending_clusters(manifest)
    assert clusters[0]["total_speech"] > clusters[1]["total_speech"]


def test_clustering_is_idempotent(manifest):
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    add_pending(manifest, "meet-b", "SPEAKER_01", vec(0.99, 0.02, 0.0))
    first = voices.cluster_pending(manifest, MODEL)
    second = voices.cluster_pending(manifest, MODEL)
    assert first == second == 1


def test_splitting_a_cluster_returns_the_constituents(manifest):
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    add_pending(manifest, "meet-b", "SPEAKER_01", vec(0.99, 0.02, 0.0))
    voices.cluster_pending(manifest, MODEL)
    cluster_id = db.pending_clusters(manifest)[0]["id"]

    voices.split_cluster(manifest, cluster_id)
    clusters = db.pending_clusters(manifest)
    assert len(clusters) == 2
    assert all(c["size"] == 1 for c in clusters)


# ── Resolution ────────────────────────────────────────────────────────

def test_confirming_resolves_every_label_in_the_cluster(manifest):
    """One answer covers every appearance - the point of clustering."""
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    add_pending(manifest, "meet-b", "SPEAKER_01", vec(0.99, 0.02, 0.0))
    voices.cluster_pending(manifest, MODEL)
    cluster_id = db.pending_clusters(manifest)[0]["id"]

    assert voices.confirm(manifest, cluster_id, "Ali", model=MODEL) == 2
    assert db.get_speakers(manifest, "meet-a") == {"SPEAKER_00": "Ali"}
    assert db.get_speakers(manifest, "meet-b") == {"SPEAKER_01": "Ali"}
    assert len(db.person_samples(manifest, "Ali")) == 2


def test_a_confirmed_cluster_does_not_come_back(manifest):
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    voices.cluster_pending(manifest, MODEL)
    voices.confirm(manifest, db.pending_clusters(manifest)[0]["id"], "Ali", model=MODEL)

    voices.cluster_pending(manifest, MODEL)
    assert db.pending_clusters(manifest) == []


def test_confirming_marks_the_speaker_confirmed_not_inferred(manifest):
    """The owner listened and named it. That outranks any inference."""
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    voices.cluster_pending(manifest, MODEL)
    voices.confirm(manifest, db.pending_clusters(manifest)[0]["id"], "Ali", model=MODEL)

    row = manifest.execute(
        "SELECT confidence FROM speakers WHERE meeting_id = 'meet-a'"
    ).fetchone()
    assert row["confidence"] == "confirmed"


def test_confirming_voice_queues_existing_minutes_for_refresh(manifest):
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    db.advance(manifest, "meet-a", db.INDEXED, lightrag_doc_id="doc-old")
    voices.cluster_pending(manifest, MODEL)

    voices.confirm(manifest, db.pending_clusters(manifest)[0]["id"], "Ali", model=MODEL)

    meeting = db.get_meeting(manifest, "meet-a")
    assert meeting.status == db.SPEAKERS_RESOLVED
    assert meeting.lightrag_doc_id == "doc-old"


def test_confirming_normalizes_through_the_people_registry(manifest):
    """Otherwise 'Mike' and 'Michael' become two voiceprints of one person."""
    db.add_person(manifest, "Michael", aliases=["Mike"])
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    voices.cluster_pending(manifest, MODEL)
    voices.confirm(manifest, db.pending_clusters(manifest)[0]["id"], "Mike", model=MODEL)

    assert len(db.person_samples(manifest, "Michael")) == 1
    assert db.person_samples(manifest, "Mike") == []


def test_stale_confirmation_cannot_resurrect_a_merged_name(manifest):
    db.add_person(manifest, "Michael")
    db.flatten_and_record_merge(manifest, "Mike", "Michael")
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    voices.cluster_pending(manifest, MODEL)

    voices.confirm(manifest, db.pending_clusters(manifest)[0]["id"], "Mike", model=MODEL)

    assert db.get_speakers(manifest, "meet-a") == {"SPEAKER_00": "Michael"}
    assert len(db.person_samples(manifest, "Michael")) == 1
    assert [person["canonical"] for person in db.list_people(manifest)] == ["Michael"]


def test_dismiss_keeps_the_embedding(manifest):
    """A dismissed fragment is sometimes a real person who barely spoke."""
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    voices.cluster_pending(manifest, MODEL)
    voices.dismiss(manifest, db.pending_clusters(manifest)[0]["id"])

    row = db.get_speaker_match(manifest, "meet-a", "SPEAKER_00")
    assert row["state"] == voices.STATE_DISMISSED
    assert row["embedding"] is not None


def test_unsure_leaves_the_label_pending(manifest):
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0))
    voices.cluster_pending(manifest, MODEL)
    voices.unsure(manifest, db.pending_clusters(manifest)[0]["id"])

    assert db.get_speaker_match(manifest, "meet-a", "SPEAKER_00")["state"] == "pending"
    assert voices.cluster_pending(manifest, MODEL) == 1


def test_merging_people_moves_their_samples(manifest):
    enroll(manifest, "Mike", vec(1.0, 0.0))
    enroll(manifest, "Michael", vec(0.99, 0.02))

    manifest.commit()
    approved = people_merge.preview(["Mike"], "Michael")
    result = people_merge.merge(
        ["Mike"], "Michael", expected_digest=approved.digest
    )

    assert result.target == "Michael"
    assert db.person_samples(manifest, "Mike") == []
    assert len(db.person_samples(manifest, "Michael")) == 4
    assert db.canonical_name(manifest, "Mike") == "Michael"


# ── Re-matching ───────────────────────────────────────────────────────

def test_rematch_promotes_a_label_once_its_person_is_enrolled(manifest):
    """The compounding job: yesterday's unknown resolves without being asked."""
    add_pending(manifest, "meet-new", "SPEAKER_00", vec(1.0, 0.0, 0.0), speech_sec=120.0)
    assert voices.rematch_pending(manifest, MODEL) == 0

    enroll(manifest, "Ali", vec(1.0, 0.0, 0.0))
    assert voices.rematch_pending(manifest, MODEL) == 1

    row = db.get_speaker_match(manifest, "meet-new", "SPEAKER_00")
    assert row["band"] == voices.BAND_AUTO
    assert row["best_canonical"] == "Ali"


def test_rematch_ignores_resolved_labels(manifest):
    enroll(manifest, "Ali", vec(1.0, 0.0, 0.0))
    add_pending(manifest, "meet-a", "SPEAKER_00", vec(1.0, 0.0, 0.0), state="resolved")
    assert voices.rematch_pending(manifest, MODEL) == 0


# ── Person deletion ───────────────────────────────────────────────────

def test_forget_removes_every_sample(manifest):
    """Enroll-everyone is the owner's policy, so this is the remedy it rests on."""
    enroll(manifest, "Ali", vec(1.0, 0.0))
    deleted, _ = voices.forget(manifest, "Ali")

    assert deleted == 2
    assert db.person_samples(manifest, "Ali") == []
    assert voices.voiceprint(manifest, "Ali", model=MODEL) is None


def test_forget_deletes_snippet_files(manifest, tmp_path, monkeypatch):
    from pipeline import config

    snippets = tmp_path / "snippets"
    (snippets / "meet-a").mkdir(parents=True)
    clip = snippets / "meet-a" / "SPEAKER_00-0.opus"
    clip.write_bytes(b"audio")
    monkeypatch.setattr(config, "SNIPPETS_DIR", snippets)

    make_meeting(manifest, "meet-a", "2026-08-15")
    db.add_person(manifest, "Ali")
    blob, dim = voices.pack(vec(1.0, 0.0))
    db.upsert_speaker_match(
        manifest, "meet-a", "SPEAKER_00",
        embedding=blob, dim=dim, model=MODEL, speech_sec=60.0,
        snippet_paths='["meet-a/SPEAKER_00-0.opus"]',
    )
    db.add_voice_sample(
        manifest, canonical="Ali", meeting_id="meet-a", label="SPEAKER_00",
        embedding=blob, dim=dim, model=MODEL, speech_sec=60.0,
    )

    _, snippets_removed = voices.forget(manifest, "Ali")
    assert snippets_removed == 1
    assert not clip.exists()


# ── Sample survival ───────────────────────────────────────────────────

def test_deleting_a_meeting_keeps_the_voice_sample(manifest):
    """ON DELETE SET NULL, not CASCADE.

    Cascading would mean deleting one old meeting silently degrades the
    voiceprint of everyone who spoke in it - a failure nobody notices until
    names start going wrong.
    """
    enroll(manifest, "Ali", vec(1.0, 0.0), meetings=1)
    manifest.execute("PRAGMA foreign_keys = ON")
    manifest.execute("DELETE FROM meetings WHERE id = 'm-Ali-0'")

    samples = db.person_samples(manifest, "Ali")
    assert len(samples) == 1
    assert samples[0]["meeting_id"] is None
    assert voices.voiceprint(manifest, "Ali", model=MODEL) is not None
