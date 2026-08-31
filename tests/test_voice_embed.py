"""The remote voice producer: embeddings in, auto-applied names out.

The weight here is on what happens when something is already true - a label
already embedded, a name already confirmed, a provider already failed - because
those are the paths that cost money or lose evidence when they go wrong. A
re-run that re-embeds is a bill; a bootstrap that enrolls twice poisons a
voiceprint with duplicated evidence; a provider outage that advances a meeting
loses the labelling for good.
"""

from __future__ import annotations

import argparse
import json

import pytest

from pipeline import asr, cli, db, voice_embed, voices
from pipeline.asr import Segment, Transcript

from .conftest import make_meeting

NAMESPACE = "wespeaker-resnet34-lm@abc123def456"


# ── Fixtures and fakes ────────────────────────────────────────────────

class FakeBackend:
    """A scripted embedding provider, over the real seam.

    Returns whatever vectors the test names, records every call so a test can
    assert the stage did not pay twice, and can be told to fail.
    """

    name = "fake:embed"

    def __init__(self, vectors: dict[str, list[float]], *, namespace: str = NAMESPACE, error=None):
        self.vectors = vectors
        self._namespace = namespace
        self.error = error
        self.calls: list[list[voice_embed.Region]] = []

    def namespace(self) -> str:
        return self._namespace

    def embed(self, audio_path, regions):
        self.calls.append(list(regions))
        if self.error:
            raise self.error
        labels = {region.label for region in regions}
        vectors = {label: self.vectors[label] for label in labels if label in self.vectors}
        return voice_embed.EmbeddingBatch(
            embeddings=vectors,
            dim=len(next(iter(vectors.values()))) if vectors else 0,
            encoder="wespeaker-resnet34-lm",
            speech_sec={label: 60.0 for label in vectors},
        )

    @property
    def embedded_labels(self) -> list[str]:
        return [region.label for call in self.calls for region in call]


def transcript_for(meeting_id: str, labels: dict[str, list[tuple[float, float]]]) -> Transcript:
    """A transcript whose segments give each label the spans the test wants."""
    segments = [
        Segment(
            start=start, end=end,
            text="a real sentence of roughly this length",
            speaker=label,
            words=[],
        )
        for label, spans in labels.items()
        for start, end in spans
    ]
    segments.sort(key=lambda seg: seg.start)
    return Transcript(
        meeting_id=meeting_id,
        model="fake:test-asr",
        language="en",
        duration_sec=max(seg.end for seg in segments) if segments else 0.0,
        segments=segments,
    )


@pytest.fixture()
def staged(manifest, tmp_path, monkeypatch):
    """A transcribed meeting with a retained transcript and audio on disk.

    ffmpeg snippet cutting is replaced: it is exercised for real by the snippet
    tests, and every clip here would be six seconds of a sine wave.
    """
    monkeypatch.setattr(asr, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(
        voice_embed, "write_snippets",
        lambda audio, meeting_id, label, spans: [f"{meeting_id}/{label}-{i}.opus"
                                                 for i, _ in enumerate(spans)],
    )
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    def stage(meeting_id: str, labels: dict[str, list[tuple[float, float]]], date="2026-08-10"):
        audio_path = audio_dir / f"{meeting_id}.m4a"
        audio_path.write_bytes(b"not really audio, but really on disk")
        asr.save_transcript(transcript_for(meeting_id, labels))
        make_meeting(manifest, meeting_id, date, status=db.TRANSCRIBED)
        manifest.execute(
            "UPDATE meetings SET audio_path = ? WHERE id = ?", (str(audio_path), meeting_id)
        )
        manifest.commit()
        return db.get_meeting(manifest, meeting_id)

    return stage


def long_regions(count: int = 4, start: float = 100.0) -> list[tuple[float, float]]:
    """Enough speech to clear VOICE_MIN_SPEECH_SEC comfortably."""
    return [(start + index * 100.0, start + index * 100.0 + 40.0) for index in range(count)]


# ── Namespace ─────────────────────────────────────────────────────────

def test_namespace_is_resolved_once_and_recorded(manifest):
    backend = FakeBackend({})
    assert voice_embed.resolve_namespace(manifest, backend) == NAMESPACE
    assert db.get_setting(manifest, "voice.active_namespace") == NAMESPACE
    assert voices.active_namespace(manifest) == NAMESPACE


def test_the_quarantined_namespace_is_never_written_to(manifest):
    """The retired local enroller's vectors are a different vector space.

    Writing new work into it would make two incomparable spaces look like one,
    and every score computed across them would be meaningless but plausible.
    """
    backend = FakeBackend({}, namespace="historical")
    with pytest.raises(voice_embed.VoiceEmbeddingError, match="historical"):
        voice_embed.resolve_namespace(manifest, backend)


def test_a_new_namespace_never_matches_an_old_voiceprint(manifest, staged):
    """Namespace isolation, end to end: the same vector, two spaces."""
    vector = [1.0, 0.0, 0.0]
    blob, dim = voices.pack(vector)
    db.add_person(manifest, "Ruth")
    for index in range(2):
        make_meeting(manifest, f"old-{index}", f"2026-07-0{index + 1}")
        db.add_voice_sample(
            manifest, canonical="Ruth", meeting_id=f"old-{index}", label="SPEAKER_00",
            embedding=blob, dim=dim, model="historical", speech_sec=120.0,
        )
    meeting = staged("new-meeting", {"SPEAKER_00": long_regions()})
    manifest.commit()

    voice_embed.embed_meeting(
        manifest, meeting, FakeBackend({"SPEAKER_00": vector}), namespace=NAMESPACE
    )
    voices.rematch_pending(manifest, model=NAMESPACE)

    row = db.get_speaker_match(manifest, "new-meeting", "SPEAKER_00")
    assert row["model"] == NAMESPACE
    assert row["best_canonical"] is None, "an identical vector in another space is not a match"
    assert db.get_speakers(manifest, "new-meeting") == {}


# ── Embedding ─────────────────────────────────────────────────────────

def test_labels_are_embedded_and_written_into_the_active_namespace(manifest, staged):
    meeting = staged("m-embed", {"SPEAKER_00": long_regions(), "SPEAKER_01": long_regions(2)})
    backend = FakeBackend({"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]})

    result = voice_embed.embed_meeting(manifest, meeting, backend, namespace=NAMESPACE)

    assert result.embedded == 2
    for label, expected_sec in (("SPEAKER_00", 160.0), ("SPEAKER_01", 80.0)):
        row = db.get_speaker_match(manifest, "m-embed", label)
        assert row["model"] == NAMESPACE
        assert row["embedding"] is not None
        # Full speech, not the capped amount that was sent: this number gates
        # auto-matching and has to mean "how much this person spoke".
        assert row["speech_sec"] == pytest.approx(expected_sec)
        assert row["state"] == voices.STATE_PENDING
        assert json.loads(row["snippet_paths"])


def test_the_request_is_capped_but_the_row_is_not(manifest, staged):
    """Cost is bounded per label; the recorded speech time still tells the truth."""
    meeting = staged("m-cap", {"SPEAKER_00": [(0.0, 600.0)]})
    backend = FakeBackend({"SPEAKER_00": [1.0, 0.0]})

    voice_embed.embed_meeting(manifest, meeting, backend, namespace=NAMESPACE)

    sent = sum(region.end - region.start for region in backend.calls[0])
    assert sent == pytest.approx(voice_embed.VOICE_MAX_EMBED_SEC)
    assert db.get_speaker_match(manifest, "m-cap", "SPEAKER_00")["speech_sec"] == 600.0


def test_one_call_covers_the_whole_meeting(manifest, staged):
    """Per meeting, not per label: the paid call must not scale with attendees."""
    meeting = staged(
        "m-one-call",
        {f"SPEAKER_0{index}": long_regions(2, start=100.0 + index) for index in range(4)},
    )
    backend = FakeBackend({f"SPEAKER_0{index}": [float(index), 1.0] for index in range(4)})

    voice_embed.embed_meeting(manifest, meeting, backend, namespace=NAMESPACE)

    assert len(backend.calls) == 1
    assert {region.label for region in backend.calls[0]} == {f"SPEAKER_0{i}" for i in range(4)}


def test_re_running_never_re_pays_for_an_embedded_label(manifest, staged):
    meeting = staged("m-rerun", {"SPEAKER_00": long_regions()})
    backend = FakeBackend({"SPEAKER_00": [1.0, 0.0]})

    voice_embed.embed_meeting(manifest, meeting, backend, namespace=NAMESPACE)
    second = voice_embed.embed_meeting(manifest, meeting, backend, namespace=NAMESPACE)

    assert len(backend.calls) == 1
    assert second.skipped == 1
    assert second.embedded == 0


def test_force_re_embeds_deliberately(manifest, staged):
    meeting = staged("m-force", {"SPEAKER_00": long_regions()})
    backend = FakeBackend({"SPEAKER_00": [1.0, 0.0]})

    voice_embed.embed_meeting(manifest, meeting, backend, namespace=NAMESPACE)
    result = voice_embed.embed_meeting(
        manifest, meeting, backend, namespace=NAMESPACE, force=True
    )

    assert len(backend.calls) == 2
    assert result.embedded == 1


def test_a_vector_from_another_namespace_is_not_reused(manifest, staged):
    """A row left by a different encoder is not a cheaper copy of this one."""
    meeting = staged("m-repin", {"SPEAKER_00": long_regions()})
    blob, dim = voices.pack([9.0, 9.0])
    db.upsert_speaker_match(
        manifest, "m-repin", "SPEAKER_00",
        embedding=blob, dim=dim, model="historical", speech_sec=160.0,
    )
    backend = FakeBackend({"SPEAKER_00": [1.0, 0.0]})

    voice_embed.embed_meeting(manifest, meeting, backend, namespace=NAMESPACE)

    assert backend.embedded_labels.count("SPEAKER_00") >= 1
    row = db.get_speaker_match(manifest, "m-repin", "SPEAKER_00")
    assert row["model"] == NAMESPACE
    assert voices.unpack(row["embedding"], row["dim"]).tolist() == [1.0, 0.0]


def test_the_transcript_guess_rides_along_as_a_veto_signal(manifest, staged):
    meeting = staged("m-guess", {"SPEAKER_00": long_regions()})
    db.set_speaker(manifest, "m-guess", "SPEAKER_00", "Ruth", "inferred")

    voice_embed.embed_meeting(
        manifest, meeting, FakeBackend({"SPEAKER_00": [1.0, 0.0]}), namespace=NAMESPACE
    )

    assert db.get_speaker_match(manifest, "m-guess", "SPEAKER_00")["llm_name"] == "Ruth"


# ── Bootstrap ─────────────────────────────────────────────────────────

def test_a_confirmed_label_is_enrolled_and_leaves_the_queue(manifest, staged):
    """The owner already named this voice; asking again would be noise."""
    meeting = staged("m-boot", {"SPEAKER_00": long_regions()})
    db.add_person(manifest, "Ruth")
    db.set_speaker(manifest, "m-boot", "SPEAKER_00", "Ruth", "confirmed")

    result = voice_embed.embed_meeting(
        manifest, meeting, FakeBackend({"SPEAKER_00": [1.0, 0.0]}), namespace=NAMESPACE
    )

    assert result.bootstrapped == ["Ruth"]
    samples = db.person_samples(manifest, "Ruth", model=NAMESPACE)
    assert len(samples) == 1
    assert samples[0]["source"] == "bootstrap"
    row = db.get_speaker_match(manifest, "m-boot", "SPEAKER_00")
    assert row["state"] == voices.STATE_RESOLVED
    assert row["resolved_as"] == "Ruth"


def test_bootstrap_enrolls_once_per_person_meeting_and_label(manifest, staged):
    """`add_voice_sample` has no unique constraint; this module owns not
    inserting the same evidence twice, or one person's voiceprint quietly
    doubles the weight of one seat."""
    meeting = staged("m-boot-twice", {"SPEAKER_00": long_regions()})
    db.add_person(manifest, "Ruth")
    db.set_speaker(manifest, "m-boot-twice", "SPEAKER_00", "Ruth", "confirmed")
    backend = FakeBackend({"SPEAKER_00": [1.0, 0.0]})

    voice_embed.embed_meeting(manifest, meeting, backend, namespace=NAMESPACE)
    voice_embed.embed_meeting(manifest, meeting, backend, namespace=NAMESPACE)

    assert len(db.person_samples(manifest, "Ruth", model=NAMESPACE)) == 1
    assert len(backend.calls) == 1


def test_a_confirmed_alias_enrolls_under_the_canonical_spelling(manifest, staged):
    meeting = staged("m-alias", {"SPEAKER_00": long_regions()})
    db.add_person(manifest, "Michael", aliases=["Mike"])
    db.set_speaker(manifest, "m-alias", "SPEAKER_00", "Mike", "confirmed")

    voice_embed.embed_meeting(
        manifest, meeting, FakeBackend({"SPEAKER_00": [1.0, 0.0]}), namespace=NAMESPACE
    )

    assert db.person_samples(manifest, "Michael", model=NAMESPACE)
    assert not db.person_samples(manifest, "Mike", model=NAMESPACE)


# ── Failure semantics ─────────────────────────────────────────────────

def test_a_provider_failure_costs_this_meeting_and_nothing_else(manifest, staged):
    good = staged("m-ok", {"SPEAKER_00": long_regions()})
    bad = staged("m-bad", {"SPEAKER_00": long_regions()}, date="2026-08-11")

    voice_embed.embed_meeting(
        manifest, good, FakeBackend({"SPEAKER_00": [1.0, 0.0]}), namespace=NAMESPACE
    )
    failed = voice_embed.embed_meeting(
        manifest, bad,
        FakeBackend({}, error=voice_embed.VoiceEmbeddingError("upstream is down")),
        namespace=NAMESPACE,
    )

    assert failed.failed and "upstream is down" in failed.failed
    assert db.get_speaker_match(manifest, "m-ok", "SPEAKER_00")["embedding"] is not None
    assert db.get_speaker_match(manifest, "m-bad", "SPEAKER_00") is None
    # The stage sits beside the pipeline, not in it: a provider outage must not
    # park a meeting and stop its minutes from ever compiling.
    assert db.get_meeting(manifest, "m-bad").status == db.TRANSCRIBED


def test_a_failure_leaves_earlier_embeddings_of_the_same_meeting_intact(manifest, staged):
    meeting = staged("m-keep", {"SPEAKER_00": long_regions(), "SPEAKER_01": long_regions(2)})
    voice_embed.embed_meeting(
        manifest, meeting,
        FakeBackend({"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]}),
        namespace=NAMESPACE,
    )

    voice_embed.embed_meeting(
        manifest, meeting,
        FakeBackend({}, error=voice_embed.VoiceEmbeddingError("down")),
        namespace=NAMESPACE, force=True,
    )

    assert db.get_speaker_match(manifest, "m-keep", "SPEAKER_00")["embedding"] is not None
    assert db.get_speaker_match(manifest, "m-keep", "SPEAKER_01")["embedding"] is not None


def test_a_label_the_provider_skipped_is_reported_not_invented(manifest, staged):
    meeting = staged("m-partial", {"SPEAKER_00": long_regions(), "SPEAKER_01": long_regions(2)})

    result = voice_embed.embed_meeting(
        manifest, meeting, FakeBackend({"SPEAKER_00": [1.0, 0.0]}), namespace=NAMESPACE
    )

    assert result.embedded == 1
    assert [r.outcome for r in result.labels if r.label == "SPEAKER_01"] == [
        voice_embed.OUTCOME_EMPTY
    ]
    # No row at all, rather than a row with a null vector: an empty row would
    # sit in the queue forever looking like something the matcher could use.
    assert db.get_speaker_match(manifest, "m-partial", "SPEAKER_01") is None


def test_deleted_audio_is_reported_per_label_not_as_a_meeting_failure(manifest, staged, tmp_path):
    meeting = staged("m-gone", {"SPEAKER_00": long_regions()})
    from pathlib import Path

    Path(meeting.audio_path).unlink()
    backend = FakeBackend({"SPEAKER_00": [1.0, 0.0]})

    result = voice_embed.embed_meeting(manifest, meeting, backend, namespace=NAMESPACE)

    assert result.failed is None
    assert [r.outcome for r in result.labels] == [voice_embed.OUTCOME_NO_AUDIO]
    assert backend.calls == []


# ── Eligibility ───────────────────────────────────────────────────────

def test_only_meetings_with_a_retained_transcript_are_queued(manifest, staged):
    staged("m-has-transcript", {"SPEAKER_00": long_regions()})
    make_meeting(manifest, "m-no-transcript", "2026-08-12", status=db.TRANSCRIBED)
    manifest.commit()

    queued = [m.id for m in voice_embed.eligible_meetings(manifest)]

    assert queued == ["m-has-transcript"]


def test_a_meeting_still_awaiting_transcription_is_not_queued(manifest, staged):
    staged("m-done", {"SPEAKER_00": long_regions()})
    make_meeting(manifest, "m-pending", "2026-08-12", status=db.DISCOVERED)
    manifest.commit()

    assert [m.id for m in voice_embed.eligible_meetings(manifest)] == ["m-done"]


# ── The loop this stage exists to close ───────────────────────────────

def test_a_name_given_once_labels_the_next_meeting_by_itself(manifest, staged, monkeypatch):
    """The whole point, over the real CLI: confirm a voice twice, and the third
    meeting is labelled with nobody being asked anything."""
    vector = [1.0, 0.0, 0.0]
    backend = FakeBackend({"SPEAKER_00": vector, "SPEAKER_01": [0.0, 0.0, 1.0]})
    monkeypatch.setattr(voice_embed, "default_backend", lambda: backend)

    db.add_person(manifest, "Ruth")
    for index in range(2):
        meeting_id = f"known-{index}"
        staged(meeting_id, {"SPEAKER_00": long_regions()}, date=f"2026-08-0{index + 1}")
        db.set_speaker(manifest, meeting_id, "SPEAKER_00", "Ruth", "confirmed")
    staged("unknown", {"SPEAKER_00": long_regions(), "SPEAKER_01": long_regions(2)},
           date="2026-08-05")
    manifest.commit()

    exit_code = cli.cmd_voice(argparse.Namespace(limit=None, force=False, meeting=None))

    assert exit_code == 0
    with db.connect() as conn:
        assert db.get_speakers(conn, "unknown") == {"SPEAKER_00": "Ruth"}
        row = conn.execute(
            "SELECT confidence FROM speakers WHERE meeting_id = ? AND label = ?",
            ("unknown", "SPEAKER_00"),
        ).fetchone()
        # Inferred, never confirmed: an auto-applied name has to stay
        # correctable by the same review flow a human name goes through.
        assert row["confidence"] == "inferred"
        # The voice nobody has ever named is still a question, not a guess.
        match = db.get_speaker_match(conn, "unknown", "SPEAKER_01")
        assert match["state"] == voices.STATE_PENDING


def test_the_command_is_a_no_op_when_no_provider_is_configured(monkeypatch, capsys):
    """The state every install is in until the owner turns this on."""
    monkeypatch.setattr(voice_embed, "REMOTE_VOICE_MODEL", "")

    exit_code = cli.cmd_voice(argparse.Namespace(limit=None, force=False, meeting=None))

    assert exit_code == 0
    assert "not configured" in capsys.readouterr().out


def test_the_stage_stays_out_of_run_until_it_is_switched_on(manifest):
    """Explicit-first: an unattended loop opens exactly one paid gate until the
    owner has watched what this one produces."""
    args = argparse.Namespace(no_voice=False)
    assert cli._voice_stage_enabled(args) is False

    db.set_setting(manifest, cli.VOICE_STAGE_IN_RUN, "1")
    manifest.commit()
    assert cli._voice_stage_enabled(args) is True

    assert cli._voice_stage_enabled(argparse.Namespace(no_voice=True)) is False


def test_an_unreachable_provider_is_reported_not_raised(monkeypatch, capsys):
    """A misconfigured provider is something the operator has to read, and a
    traceback out of a CLI command is not that."""
    class Unreachable:
        name = "fake:broken"

        def namespace(self):
            raise voice_embed.VoiceEmbeddingError("could not resolve owner/model on Replicate")

        def embed(self, audio_path, regions):  # pragma: no cover - never reached
            raise AssertionError("must not be called")

    monkeypatch.setattr(voice_embed, "default_backend", lambda: Unreachable())

    exit_code = cli.cmd_voice(argparse.Namespace(limit=None, force=False, meeting=None))

    assert exit_code == 1
    assert "could not resolve" in capsys.readouterr().err
