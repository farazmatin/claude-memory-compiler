"""Speaker embeddings via Replicate, the second and last remote provider.

Transcription proved the shape: send the audio, let the GPU hold the weights,
keep this machine to ffmpeg and SQLite. Embedding follows it exactly, and for the
same reason - the repository's contract is that no ASR, alignment, diarization or
speaker-embedding weights are ever loaded locally.

The interface is per meeting, not per label. The model is given the diarization
labels' own speech regions and embeds them server-side, returning one
duration-weighted centroid per label:

    input:  audio_file  a Replicate files API URI
            regions     [{label, start, end}] taken from the retained transcript
            encoder     which encoder to serve
    output: {embeddings: {label: [floats]}, dim, encoder, speech_sec}

That removes the label-mapping problem rather than solving it: there is no
alignment step here that could pair the wrong vector with the wrong speaker, and
one paid call covers a meeting however many people spoke in it.

Three encoders ship behind the one interface, chosen per request. Which one wins
is a measured question - a benchmark over real meetings scored against the
corpus's human-confirmed labels - not a reputational one, so the encoder and its
version are configuration, and the vectors they produce are namespaced by both.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from pipeline.config import (
    REMOTE_VOICE_ENCODER,
    REMOTE_VOICE_MODEL,
    REMOTE_VOICE_POLL_INTERVAL_SEC,
    REMOTE_VOICE_TIMEOUT_SEC,
    REMOTE_VOICE_VERSION,
    REPLICATE_API_TOKEN,
)
from pipeline.replicate_asr import (
    REPLICATE_API_BASE,
    ReplicateError,
    api_headers,
    normalize_audio_for_upload,
    poll_prediction,
    prediction_output,
    submit_prediction,
    upload_file,
)
from pipeline.voice_embed import EmbeddingBatch, Region, VoiceEmbeddingError

# How much of the version hash names the namespace. Long enough that two
# versions cannot collide, short enough that `encoder@a1b2c3d4e5f6` is something
# an operator can read in a status line.
VERSION_PREFIX_LEN = 12


class ReplicateVoiceBackend:
    """Remote speaker embedding on Replicate."""

    def __init__(
        self,
        api_token: str = REPLICATE_API_TOKEN,
        model_name: str = REMOTE_VOICE_MODEL,
        version: str = REMOTE_VOICE_VERSION,
        encoder: str = REMOTE_VOICE_ENCODER,
        timeout_sec: float = REMOTE_VOICE_TIMEOUT_SEC,
        poll_interval_sec: float = REMOTE_VOICE_POLL_INTERVAL_SEC,
    ) -> None:
        self.api_token = (api_token or "").strip()
        self.model_name = (model_name or "").strip()
        self.version = (version or "").strip()
        self.encoder = (encoder or "").strip()
        self.timeout_sec = timeout_sec
        self.poll_interval_sec = poll_interval_sec
        self.name = f"replicate:{self.model_name}"
        self._resolved_version: str | None = None

    # ── Version and namespace ─────────────────────────────────────────

    def resolve_version(self, client: httpx.Client | None = None) -> str:
        """The version hash this backend will actually run.

        Resolved rather than assumed, and never defaulted: the namespace is
        derived from it, and a namespace that misreports which weights produced a
        vector is worse than no namespace at all - it makes two incomparable
        vector spaces look like one.
        """
        if self._resolved_version:
            return self._resolved_version
        if not self.model_name:
            raise VoiceEmbeddingError(
                "MMC_REMOTE_VOICE_MODEL is unset; the voice stage has no provider"
            )
        if ":" in self.model_name:
            self._resolved_version = self.model_name.split(":", 1)[1]
            return self._resolved_version
        if self.version:
            self._resolved_version = self.version
            return self._resolved_version

        owned = client is None
        client = client or httpx.Client()
        try:
            resp = client.get(
                f"{REPLICATE_API_BASE}/models/{self.model_name}",
                headers=self._headers(),
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise VoiceEmbeddingError(
                f"could not reach Replicate to resolve {self.model_name}: {exc}",
                transient=True,
            ) from exc
        finally:
            if owned:
                client.close()

        if resp.status_code != 200:
            raise VoiceEmbeddingError(
                f"could not resolve {self.model_name} on Replicate "
                f"({resp.status_code}): {resp.text[:120]}"
            )
        version_id = (resp.json().get("latest_version") or {}).get("id")
        if not version_id:
            raise VoiceEmbeddingError(
                f"{self.model_name} has no published version on Replicate"
            )
        self._resolved_version = str(version_id)
        return self._resolved_version

    def namespace(self) -> str:
        """`encoder@version`: the vector space these embeddings belong to."""
        if not self.encoder:
            raise VoiceEmbeddingError("MMC_REMOTE_VOICE_ENCODER is unset")
        return f"{self.encoder}@{self.resolve_version()[:VERSION_PREFIX_LEN]}"

    def _headers(self) -> dict[str, str]:
        try:
            return api_headers(self.api_token)
        except ReplicateError as exc:
            raise VoiceEmbeddingError(str(exc)) from exc

    # ── Embedding ─────────────────────────────────────────────────────

    def embed(self, audio_path: Path, regions: list[Region]) -> EmbeddingBatch:
        """One call: audio plus the labels' regions in, one vector per label out."""
        if not regions:
            return EmbeddingBatch(embeddings={}, dim=0, encoder=self.encoder)

        import tempfile

        headers = self._headers()
        with tempfile.TemporaryDirectory() as tmpdir:
            # The same small 16 kHz mono body transcription sends. Payload size
            # drives the upload failure rate, and the embedding model resamples
            # to 16 kHz internally exactly as the ASR model does.
            upload_path = normalize_audio_for_upload(audio_path, Path(tmpdir) / "upload")
            size_mb = upload_path.stat().st_size / (1024 * 1024)

            try:
                with httpx.Client() as client:
                    version = self.resolve_version(client)
                    print(
                        f"    embedding {len({r.label for r in regions})} voice(s) "
                        f"on Replicate ({size_mb:.1f} MB)..."
                    )
                    audio_url = upload_file(client, headers, upload_path)
                    prediction = submit_prediction(
                        client,
                        headers,
                        {
                            "version": version,
                            "input": {
                                "audio_file": audio_url,
                                "encoder": self.encoder,
                                "regions": [
                                    {
                                        "label": region.label,
                                        "start": round(region.start, 3),
                                        "end": round(region.end, 3),
                                    }
                                    for region in regions
                                ],
                            },
                        },
                    )
                    completed = poll_prediction(
                        client,
                        headers,
                        prediction["id"],
                        timeout_sec=self.timeout_sec,
                        poll_interval_sec=self.poll_interval_sec,
                    )
                    output = prediction_output(client, completed)
            except ReplicateError as exc:
                raise VoiceEmbeddingError(str(exc), transient=exc.transient) from exc
            except httpx.HTTPError as exc:
                raise VoiceEmbeddingError(str(exc), transient=True) from exc

        return _batch_from_output(output, fallback_encoder=self.encoder)


def _batch_from_output(output: Any, *, fallback_encoder: str) -> EmbeddingBatch:
    """Map the provider's JSON onto the batch, refusing anything unrecognised.

    A response whose shape has drifted must fail loudly. Quietly returning an
    empty batch would look exactly like "this meeting had nothing to embed" and
    would keep costing money for nothing on every later run.
    """
    if not isinstance(output, dict):
        raise VoiceEmbeddingError(
            f"embedding provider returned {type(output).__name__}, expected an object"
        )
    raw = output.get("embeddings")
    if not isinstance(raw, dict):
        raise VoiceEmbeddingError("embedding provider returned no 'embeddings' object")

    embeddings: dict[str, list[float]] = {}
    width: int | None = None
    for label, vector in raw.items():
        if not isinstance(vector, list) or not vector:
            continue
        if width is None:
            width = len(vector)
        elif len(vector) != width:
            # Vectors of different widths in one response cannot be scored
            # against each other, and the mismatch would surface far away, as a
            # numpy shape error inside matching rather than a provider problem.
            raise VoiceEmbeddingError(
                f"embedding provider returned {len(vector)} values for {label} "
                f"and {width} for another label in the same response"
            )
        embeddings[str(label)] = [float(value) for value in vector]

    speech_sec: dict[str, float] = {}
    for label, seconds in (output.get("speech_sec") or {}).items():
        try:
            speech_sec[str(label)] = float(seconds)
        except (TypeError, ValueError):
            continue

    # The dimension is taken from the vectors themselves, not the field: the
    # blob's length is what `voices.unpack` will check it against.
    dim = len(next(iter(embeddings.values()))) if embeddings else 0
    return EmbeddingBatch(
        embeddings=embeddings,
        dim=dim,
        encoder=str(output.get("encoder") or fallback_encoder),
        speech_sec=speech_sec,
    )
