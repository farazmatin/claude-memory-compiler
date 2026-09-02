"""The voice-vector producer.

Every test here runs against a scripted fake. The provider call is the only
paid operation in the pipeline's voice path, so a test suite that could reach it
by accident is a test suite that can spend money on a typo.
"""

from __future__ import annotations

import json

import pytest

from pipeline import db, voice_embed, voices
from pipeline.asr import Segment, Transcript, Word

from .conftest import make_meeting

NAMESPACE = "test-encoder@v1"
OTHER = "another-encoder@v1"


class FakeBackend:
    """Returns a deterministic vector per label and records how it was called."""

    def __init__(self, dim: int = 4, fail: bool = False):
        self.dim = dim
        self.fail = fail
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def embed(self, audio_path, regions, *, encoder):
        labels = tuple(dict.fromkeys(r.label for r in regions))
        self.calls.append((encoder, labels))
        if self.fail:
            raise RuntimeError("provider exploded")
        return voice_embed.EmbedResponse(
            embeddings={
                label: [float(index + 1)] + [0.0] * (self.dim - 1)
                for index, label in enumerate(labels)
            },
            dim=self.dim,
            encoder=encoder,
        )


def transcript_with(labels: dict[str, float], meeting_id: str = "m1") -> Transcript:
    """One segment per label, `seconds` long, with word timings."""
    segments, clock = [], 0.0
    for label, seconds in labels.items():
        segments.append(
            Segment(
                start=clock, end=clock + seconds, text="hello there friends",
                speaker=label,
                words=[Word(start=clock, end=clock + seconds, text="hello")],
            )
        )
        clock += seconds
    return Transcript(
        meeting_id=meeting_id, model="fake", language="en",
        duration_sec=clock, segments=segments,
    )


@pytest.fixture()
def meeting(manifest):
    return make_meeting(manifest, "m1", "2026-08-10")


# ── Planning ──────────────────────────────────────────────────────────

def test_an_unresolved_label_is_planned_for_embedding(manifest, meeting):
    t = transcript_with({"SPEAKER_00": 60.0})
    plan = voice_embed.plan_meeting(manifest, meeting, t, namespace=NAMESPACE)[0]
    assert plan.action == voice_embed.ACTION_EMBED
    assert plan.needs_provider


def test_a_label_already_embedded_here_is_skipped(manifest, meeting):
    blob, dim = voices.pack([1.0, 0.0, 0.0, 0.0])
    db.upsert_speaker_match(manifest, "m1", "SPEAKER_00", embedding=blob, dim=dim, model=NAMESPACE)
    t = transcript_with({"SPEAKER_00": 60.0})
    plan = voice_embed.plan_meeting(manifest, meeting, t, namespace=NAMESPACE)[0]
    assert plan.action == voice_embed.ACTION_SKIP


def test_force_re_embeds_a_label_that_already_has_a_vector(manifest, meeting):
    blob, dim = voices.pack([1.0, 0.0, 0.0, 0.0])
    db.upsert_speaker_match(manifest, "m1", "SPEAKER_00", embedding=blob, dim=dim, model=NAMESPACE)
    t = transcript_with({"SPEAKER_00": 60.0})
    plan = voice_embed.plan_meeting(manifest, meeting, t, namespace=NAMESPACE, force=True)[0]
    assert plan.action == voice_embed.ACTION_EMBED


def test_a_vector_from_another_namespace_does_not_count_as_embedded(manifest, meeting):
    """Vectors from two encoders are not comparable, so one cannot stand in
    for the other."""
    blob, dim = voices.pack([1.0, 0.0, 0.0, 0.0])
    db.upsert_speaker_match(manifest, "m1", "SPEAKER_00", embedding=blob, dim=dim, model=OTHER)
    t = transcript_with({"SPEAKER_00": 60.0})
    plan = voice_embed.plan_meeting(manifest, meeting, t, namespace=NAMESPACE)[0]
    assert plan.action == voice_embed.ACTION_EMBED


def test_a_confirmed_label_bootstraps_an_enrollment(manifest, meeting):
    db.add_person(manifest, "Ali")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Ali", "confirmed")
    t = transcript_with({"SPEAKER_00": 60.0})
    plan = voice_embed.plan_meeting(manifest, meeting, t, namespace=NAMESPACE)[0]
    assert plan.action == voice_embed.ACTION_BOOTSTRAP
    assert plan.canonical == "Ali"


def test_bootstrap_reuses_a_vector_only_inside_its_own_namespace(manifest, meeting):
    """Copying a blob across namespaces would mix two vector spaces and defeat
    the guarantee that a new-model vector never matches an old voiceprint."""
    db.add_person(manifest, "Ali")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Ali", "confirmed")
    blob, dim = voices.pack([1.0, 0.0, 0.0, 0.0])
    db.upsert_speaker_match(manifest, "m1", "SPEAKER_00", embedding=blob, dim=dim, model=OTHER)

    plan = voice_embed.plan_meeting(
        manifest, meeting, transcript_with({"SPEAKER_00": 60.0}), namespace=NAMESPACE
    )[0]
    assert plan.reuse_blob is None, "a foreign-namespace vector must not be reused"
    assert plan.needs_provider

    db.upsert_speaker_match(manifest, "m1", "SPEAKER_00", model=NAMESPACE)
    plan = voice_embed.plan_meeting(
        manifest, meeting, transcript_with({"SPEAKER_00": 60.0}), namespace=NAMESPACE
    )[0]
    assert plan.reuse_blob == blob
    assert not plan.needs_provider, "a reusable vector must cost no provider call"


def test_an_already_enrolled_confirmation_is_not_enrolled_twice(manifest, meeting):
    db.add_person(manifest, "Ali")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Ali", "confirmed")
    blob, dim = voices.pack([1.0, 0.0, 0.0, 0.0])
    db.add_voice_sample(
        manifest, canonical="Ali", meeting_id="m1", label="SPEAKER_00",
        embedding=blob, dim=dim, model=NAMESPACE, speech_sec=60.0,
    )
    plan = voice_embed.plan_meeting(
        manifest, meeting, transcript_with({"SPEAKER_00": 60.0}), namespace=NAMESPACE
    )[0]
    assert plan.action == voice_embed.ACTION_SKIP


def test_regions_are_capped_so_one_talker_cannot_dominate_the_bill():
    regions = [(0.0, 500.0)]
    capped = voice_embed.capped_regions(regions, max_sec=90)
    assert sum(e - s for s, e in capped) == pytest.approx(90.0)


# ── Execution ─────────────────────────────────────────────────────────

def test_every_label_is_embedded_in_one_provider_call(manifest, meeting, monkeypatch, tmp_path):
    """Per-label calls would be four times the cost and four times the
    cold-start exposure at this corpus's average label count."""
    audio = tmp_path / "a.opus"
    audio.write_bytes(b"x")
    monkeypatch.setattr(voice_embed, "audio_for", lambda m: audio)
    monkeypatch.setattr(voice_embed, "write_snippets", lambda *a, **k: [])

    backend = FakeBackend()
    t = transcript_with({"SPEAKER_00": 60.0, "SPEAKER_01": 45.0, "SPEAKER_02": 30.0})
    result = voice_embed.embed_meeting(
        manifest, meeting, t, backend=backend, namespace=NAMESPACE
    )

    assert len(backend.calls) == 1
    assert backend.calls[0] == (NAMESPACE, ("SPEAKER_00", "SPEAKER_01", "SPEAKER_02"))
    assert result.embedded == 3


def test_a_provider_failure_leaves_the_manifest_untouched(manifest, meeting, monkeypatch, tmp_path):
    """Nothing is written before the call returns, so a meeting is either fully
    embedded or exactly as it was."""
    audio = tmp_path / "a.opus"
    audio.write_bytes(b"x")
    monkeypatch.setattr(voice_embed, "audio_for", lambda m: audio)

    with pytest.raises(RuntimeError):
        voice_embed.embed_meeting(
            manifest, meeting, transcript_with({"SPEAKER_00": 60.0}),
            backend=FakeBackend(fail=True), namespace=NAMESPACE,
        )
    assert manifest.execute("SELECT COUNT(*) AS n FROM speaker_matches").fetchone()["n"] == 0


def test_an_embedded_label_lands_in_the_review_queue(manifest, meeting, monkeypatch, tmp_path):
    audio = tmp_path / "a.opus"
    audio.write_bytes(b"x")
    monkeypatch.setattr(voice_embed, "audio_for", lambda m: audio)
    monkeypatch.setattr(voice_embed, "write_snippets", lambda *a, **k: ["m1/SPEAKER_00-0.opus"])

    voice_embed.embed_meeting(
        manifest, meeting, transcript_with({"SPEAKER_00": 60.0}),
        backend=FakeBackend(), namespace=NAMESPACE,
    )

    row = db.get_speaker_match(manifest, "m1", "SPEAKER_00")
    assert row["model"] == NAMESPACE
    assert row["embedding"] is not None
    assert json.loads(row["snippet_paths"]) == ["m1/SPEAKER_00-0.opus"]
    assert db.pending_matches(manifest, model=NAMESPACE), "must be visible to matching"


def test_bootstrap_enrolls_a_sample_without_queueing_a_review_card(
    manifest, meeting, monkeypatch, tmp_path
):
    """A name a human already gave needs no second opinion."""
    audio = tmp_path / "a.opus"
    audio.write_bytes(b"x")
    monkeypatch.setattr(voice_embed, "audio_for", lambda m: audio)
    monkeypatch.setattr(voice_embed, "write_snippets", lambda *a, **k: [])
    db.add_person(manifest, "Ali")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Ali", "confirmed")

    result = voice_embed.embed_meeting(
        manifest, meeting, transcript_with({"SPEAKER_00": 60.0}),
        backend=FakeBackend(), namespace=NAMESPACE,
    )

    assert result.bootstrapped == 1
    assert len(db.person_samples(manifest, "Ali", model=NAMESPACE)) == 1
    assert db.get_speaker_match(manifest, "m1", "SPEAKER_00") is None


def test_a_reused_vector_costs_no_provider_call(manifest, meeting, monkeypatch, tmp_path):
    audio = tmp_path / "a.opus"
    audio.write_bytes(b"x")
    monkeypatch.setattr(voice_embed, "audio_for", lambda m: audio)
    db.add_person(manifest, "Ali")
    db.set_speaker(manifest, "m1", "SPEAKER_00", "Ali", "confirmed")
    blob, dim = voices.pack([1.0, 0.0, 0.0, 0.0])
    db.upsert_speaker_match(manifest, "m1", "SPEAKER_00", embedding=blob, dim=dim, model=NAMESPACE)

    backend = FakeBackend()
    result = voice_embed.embed_meeting(
        manifest, meeting, transcript_with({"SPEAKER_00": 60.0}),
        backend=backend, namespace=NAMESPACE,
    )

    assert backend.calls == [], "a reusable vector must not reach the provider"
    assert result.bootstrapped == 1


def test_a_run_with_nothing_to_do_never_touches_the_provider(manifest, meeting, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("audio must not be fetched when nothing needs embedding")

    monkeypatch.setattr(voice_embed, "audio_for", explode)
    blob, dim = voices.pack([1.0, 0.0, 0.0, 0.0])
    db.upsert_speaker_match(manifest, "m1", "SPEAKER_00", embedding=blob, dim=dim, model=NAMESPACE)

    result = voice_embed.embed_meeting(
        manifest, meeting, transcript_with({"SPEAKER_00": 60.0}),
        backend=FakeBackend(), namespace=NAMESPACE,
    )
    assert result.skipped == 1


def test_the_stage_no_ops_without_a_configured_provider():
    """The provider call is the only paid operation in the voice path."""
    assert voice_embed.configured() is False


def test_the_producer_loads_no_local_model_weights():
    """The whole point of the remote producer: no torch, no pyannote, no nemo.

    Parsed, not grepped - the module docstring names the retired local stack, and
    a substring search would fail on its own explanation of why it is gone.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(voice_embed.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & {"torch", "pyannote", "whisperx", "nemo", "speechbrain"}


# ── Provider contract ─────────────────────────────────────────────────

def test_an_unpinned_model_is_refused():
    """Unpinned weights silently redefine every stored voiceprint."""
    from pipeline.replicate_asr import ReplicateError
    from pipeline.voice_embed_replicate import ReplicateVoiceBackend

    with pytest.raises(ReplicateError, match="pinned version"):
        ReplicateVoiceBackend("owner/name", version="")


def test_the_namespace_names_the_encoder_and_the_build(monkeypatch):
    """A vector is comparable only to one from the same weights, so the
    namespace has to say which model AND which build of it."""
    from pipeline import voice_embed

    monkeypatch.setattr(voice_embed, "REMOTE_VOICE_MODEL", "farazmatin/speaker-embed")
    monkeypatch.setattr(voice_embed, "REMOTE_VOICE_VERSION", "abcdef1234567890")
    monkeypatch.setattr(voice_embed, "REMOTE_VOICE_ENCODER", "wespeaker-resnet34-lm")
    assert voice_embed.configured()
    assert voice_embed.provider_namespace() == "wespeaker-resnet34-lm@abcdef123456"


def test_a_model_without_a_version_is_not_configured(monkeypatch):
    from pipeline import voice_embed

    monkeypatch.setattr(voice_embed, "REMOTE_VOICE_MODEL", "farazmatin/speaker-embed")
    monkeypatch.setattr(voice_embed, "REMOTE_VOICE_VERSION", "")
    assert voice_embed.configured() is False


def test_the_encoder_is_sent_to_the_cog(monkeypatch):
    """The cog hosts several encoders behind one interface; which one answered
    is half the namespace, so it cannot be left to the cog's default."""
    from pipeline.voice_embed_replicate import ReplicateVoiceBackend

    backend = ReplicateVoiceBackend(
        "farazmatin/speaker-embed", version="abc123", encoder="wespeaker-resnet34-lm"
    )
    assert backend.encoder == "wespeaker-resnet34-lm"
    assert backend.version == "abc123"
    assert backend.model == "farazmatin/speaker-embed"


@pytest.mark.parametrize(
    "output,why",
    [
        ({"embeddings": {}}, "no embeddings"),
        ({"embeddings": {"a": [1.0], "b": [1.0, 2.0]}}, "mixed dimensions"),
        ({"embeddings": {"a": [1.0]}, "dim": 9}, "declared dim disagrees"),
        ({"embeddings": {"a": "not-a-vector"}}, "not a list"),
        ({"embeddings": {"a": ["x"]}}, "not a number"),
        ("a string", "not an object"),
    ],
)
def test_a_malformed_vector_is_refused_at_the_boundary(output, why):
    """A wrong-shaped vector lands in the corpus and poisons every later
    comparison, and nothing downstream can tell it from a good one."""
    from pipeline.replicate_asr import ReplicateError
    from pipeline.voice_embed_replicate import parse_output

    with pytest.raises(ReplicateError):
        parse_output(output, fallback_encoder="fb")


def test_a_valid_response_parses():
    from pipeline.voice_embed_replicate import parse_output

    parsed = parse_output(
        {"embeddings": {"SPEAKER_00": [1.0, 2.0]}, "dim": 2, "encoder": "enc@v1"},
        fallback_encoder="fallback",
    )
    assert parsed.dim == 2
    assert parsed.encoder == "enc@v1"
    assert parsed.embeddings["SPEAKER_00"] == [1.0, 2.0]


def test_the_client_payload_is_what_the_cog_parses(monkeypatch, tmp_path):
    """The two sides are in different repos, so nothing else checks they agree.

    Reproduces the cog's own region parsing against the exact payload the client
    builds - a mismatch here is a failure that only shows up as a paid, failed
    prediction.
    """
    import json

    from pipeline.voice_embed_replicate import ReplicateVoiceBackend

    sent: dict = {}

    class _Resp:
        status_code = 201

        @staticmethod
        def json():
            return {"id": "pred-1"}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, headers=None, json=None, timeout=None):
            sent.update(json or {})
            return _Resp()

    backend = ReplicateVoiceBackend("owner/cog", version="v1", encoder="wespeaker-resnet34-lm")
    monkeypatch.setattr(backend._transport, "_upload_file", lambda *a, **k: "https://x/audio")
    monkeypatch.setattr(backend._transport, "_headers", dict)
    monkeypatch.setattr(
        backend._transport, "_poll_prediction",
        lambda *a, **k: {"output": {"embeddings": {"SPEAKER_00": [1.0, 2.0]}, "dim": 2}},
    )
    monkeypatch.setattr("pipeline.voice_embed_replicate.httpx.Client", lambda **k: _Client())

    backend.embed(
        tmp_path / "a.opus",
        [voice_embed.LabelRegion("SPEAKER_00", 0.0, 5.0),
         voice_embed.LabelRegion("SPEAKER_00", 9.0, 12.0)],
        encoder="ignored-fallback",
    )

    assert sent["version"] == "v1"
    payload = sent["input"]
    assert payload["encoder"] == "wespeaker-resnet34-lm"
    assert payload["audio"] == "https://x/audio"

    # The cog's parsing, reproduced.
    parsed = json.loads(payload["regions"])
    assert isinstance(parsed, list) and parsed
    by_label: dict[str, list[tuple[float, float]]] = {}
    for item in parsed:
        label = str(item["label"])
        start, end = float(item["start"]), float(item["end"])
        if end > start:
            by_label.setdefault(label, []).append((start, end))
    assert by_label == {"SPEAKER_00": [(0.0, 5.0), (9.0, 12.0)]}
