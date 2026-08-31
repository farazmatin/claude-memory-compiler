"""The remote embedding provider: version pinning and response handling.

Two things here can go wrong quietly. A namespace that misreports which weights
produced a vector makes two incomparable vector spaces look like one, and every
score computed across them is meaningless but plausible. A response whose shape
has drifted, absorbed into an empty batch, looks exactly like "this meeting had
nothing to embed" - and keeps costing money for nothing on every later run.
"""

from __future__ import annotations

import pytest

from pipeline import replicate_voice, voice_embed
from pipeline.replicate_voice import ReplicateVoiceBackend, _batch_from_output


def backend(**overrides) -> ReplicateVoiceBackend:
    defaults = dict(
        api_token="r8_test",
        model_name="farazmatin/speaker-embed",
        version="abc123def456789",
        encoder="wespeaker-resnet34-lm",
    )
    defaults.update(overrides)
    return ReplicateVoiceBackend(**defaults)


# ── Namespace ─────────────────────────────────────────────────────────

def test_the_namespace_names_both_the_encoder_and_the_weights():
    assert backend().namespace() == "wespeaker-resnet34-lm@abc123def456"


def test_a_pinned_version_needs_no_network_call():
    """The pin is the point: a run must not depend on what `latest` means today."""
    assert backend().resolve_version(client=None) == "abc123def456789"


def test_a_version_in_the_model_id_wins():
    resolved = backend(model_name="owner/model:deadbeefcafe", version="").resolve_version()
    assert resolved == "deadbeefcafe"


def test_an_unconfigured_model_refuses_rather_than_guessing():
    with pytest.raises(voice_embed.VoiceEmbeddingError, match="MMC_REMOTE_VOICE_MODEL"):
        backend(model_name="", version="").resolve_version()


def test_a_missing_encoder_refuses_rather_than_producing_a_bare_namespace():
    with pytest.raises(voice_embed.VoiceEmbeddingError, match="ENCODER"):
        backend(encoder="").namespace()


def test_an_unpinned_version_is_resolved_from_the_model(monkeypatch):
    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"latest_version": {"id": "0123456789abcdef"}}

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

    subject = backend(version="")
    assert subject.resolve_version(FakeClient()) == "0123456789abcdef"
    assert subject.namespace() == "wespeaker-resnet34-lm@0123456789ab"


def test_a_model_that_does_not_resolve_is_an_error_not_a_default(monkeypatch):
    """`replicate_asr` falls back to a known-good whisperx hash when a lookup
    fails. That is wrong here: the version names the namespace, so guessing it
    would silently mislabel the vector space."""
    class FakeResponse:
        status_code = 404
        text = "not found"

    class FakeClient:
        def get(self, *args, **kwargs):
            return FakeResponse()

    with pytest.raises(voice_embed.VoiceEmbeddingError, match="could not resolve"):
        backend(version="").resolve_version(FakeClient())


def test_a_missing_token_is_reported_in_this_module_s_own_error():
    with pytest.raises(voice_embed.VoiceEmbeddingError, match="REPLICATE_API_TOKEN"):
        backend(api_token="").embed(__file__, [voice_embed.Region("SPEAKER_00", 0.0, 10.0)])


# ── Response handling ─────────────────────────────────────────────────

def test_a_well_formed_response_maps_onto_the_batch():
    batch = _batch_from_output(
        {
            "embeddings": {"SPEAKER_00": [0.1, 0.2, 0.3], "SPEAKER_01": [0.4, 0.5, 0.6]},
            "dim": 3,
            "encoder": "titanet-large",
            "speech_sec": {"SPEAKER_00": 120.5},
        },
        fallback_encoder="wespeaker-resnet34-lm",
    )

    assert batch.embeddings["SPEAKER_00"] == [0.1, 0.2, 0.3]
    assert batch.dim == 3
    assert batch.encoder == "titanet-large"
    assert batch.speech_sec == {"SPEAKER_00": 120.5}


def test_a_drifted_response_fails_loudly():
    for output in ({"vectors": {}}, [1, 2, 3], "ok", None):
        with pytest.raises(voice_embed.VoiceEmbeddingError):
            _batch_from_output(output, fallback_encoder="wespeaker-resnet34-lm")


def test_an_empty_vector_is_dropped_rather_than_stored():
    """A zero-length vector unpacks to nothing and would score as NaN-adjacent
    nonsense against every voiceprint."""
    batch = _batch_from_output(
        {"embeddings": {"SPEAKER_00": [], "SPEAKER_01": [1.0, 0.0]}},
        fallback_encoder="wespeaker-resnet34-lm",
    )
    assert list(batch.embeddings) == ["SPEAKER_01"]
    assert batch.dim == 2


def test_the_dimension_comes_from_the_vectors_not_the_field():
    """The blob's length is what `voices.unpack` checks against, so a `dim` that
    disagrees with the payload must not be the one recorded."""
    batch = _batch_from_output(
        {"embeddings": {"SPEAKER_00": [1.0, 0.0]}, "dim": 512},
        fallback_encoder="wespeaker-resnet34-lm",
    )
    assert batch.dim == 2


def test_no_regions_means_no_call():
    """A meeting with nothing to embed must not upload audio or pay for a run."""
    batch = backend().embed(__file__, [])
    assert batch.embeddings == {}


def test_the_provider_reuses_the_transport_the_asr_path_hardened():
    """The upload retry policy was measured, not guessed. A second copy of it
    would drift from the first."""
    from pipeline import replicate_asr

    assert replicate_voice.upload_file is replicate_asr.upload_file
    assert replicate_voice.poll_prediction is replicate_asr.poll_prediction
    assert replicate_voice.normalize_audio_for_upload is replicate_asr.normalize_audio_for_upload


def test_vectors_of_different_widths_in_one_response_are_refused():
    """They cannot be scored against each other, and the mismatch would surface
    far away - as a numpy shape error inside matching, not a provider problem."""
    with pytest.raises(voice_embed.VoiceEmbeddingError, match="values for"):
        _batch_from_output(
            {"embeddings": {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [1.0, 0.0, 0.0]}},
            fallback_encoder="wespeaker-resnet34-lm",
        )
