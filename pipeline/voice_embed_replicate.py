"""Replicate backend for the voice-embedding stage.

Shares the ASR client's transport - upload, auth headers, polling - because the
retry budget in `_upload_file` exists for a property of this network path, not
of transcription. It deliberately does NOT reuse `_create_prediction` (its
payload is ASR-shaped) or `_resolve_version` (its lookup failure falls back to
the pinned *whisperx* version, which for a voice model would submit audio to
entirely the wrong cog and return something that looks like a vector).

## The cog contract

`MMC_REMOTE_VOICE_MODEL` must be pinned as `owner/name:version`. A bare model
name is refused: the namespace every stored vector is keyed by has to identify
the weights exactly, and "latest" silently changes what a voiceprint means.

Input:

    {"audio": <url>, "regions": [{"label": str, "start": float, "end": float}, ...]}

Output:

    {"embeddings": {label: [float, ...]}, "dim": int, "encoder": str}

No model is configured by default and none is assumed here. Choosing one is a
paid decision that should start with a comparability probe against the existing
`pyannote/wespeaker-voxceleb-resnet34-LM` vectors: if the new encoder is
comparable the corpus keeps its enrolled people, and if it is not, enrollment
restarts from whatever gets re-embedded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from pipeline.config import REMOTE_VOICE_MODEL, REPLICATE_TIMEOUT_SEC
from pipeline.replicate_asr import REPLICATE_API_BASE, ReplicateBackend, ReplicateError
from pipeline.voice_embed import EmbedResponse, LabelRegion


class ReplicateVoiceBackend:
    """Embeds every region of one meeting in a single prediction."""

    def __init__(self, model: str | None = None) -> None:
        self.model = (model or REMOTE_VOICE_MODEL).strip()
        if not self.model:
            raise ReplicateError("no embedding model configured; set MMC_REMOTE_VOICE_MODEL")
        if ":" not in self.model:
            raise ReplicateError(
                f"MMC_REMOTE_VOICE_MODEL must be pinned as owner/name:version, got {self.model!r}. "
                "An unpinned model changes what every stored voiceprint means."
            )
        self.version = self.model.split(":", 1)[1]
        # Composition, not inheritance: only the transport is shared, and the
        # ASR backend's transcribe() has no business being reachable from here.
        self._transport = ReplicateBackend(model_name=self.model)

    def embed(
        self, audio_path: Path, regions: list[LabelRegion], *, encoder: str
    ) -> EmbedResponse:
        if not regions:
            raise ReplicateError("no regions to embed")

        payload = {
            "version": self.version,
            "input": {
                "audio": None,  # filled in below, after the upload
                "regions": [
                    {"label": r.label, "start": round(r.start, 3), "end": round(r.end, 3)}
                    for r in regions
                ],
            },
        }

        with httpx.Client(timeout=REPLICATE_TIMEOUT_SEC) as client:
            payload["input"]["audio"] = self._transport._upload_file(client, audio_path)
            resp = client.post(
                f"{REPLICATE_API_BASE}/predictions",
                headers=self._transport._headers(),
                json=payload,
                timeout=60.0,
            )
            if resp.status_code not in (200, 201):
                raise ReplicateError(
                    f"could not start embedding prediction ({resp.status_code}): {resp.text[:300]}"
                )
            finished = self._transport._poll_prediction(client, resp.json()["id"])

        return parse_output(finished.get("output"), fallback_encoder=encoder)


def parse_output(output: Any, *, fallback_encoder: str) -> EmbedResponse:
    """Turn the cog's output into an EmbedResponse, or say precisely what is wrong.

    A vector of the wrong shape is worse than no vector: it lands in the corpus
    and quietly poisons every later comparison, and nothing downstream can tell
    it apart from a good one. So every field is checked here, once, at the only
    boundary where the data is still traceable to a specific prediction.
    """
    if not isinstance(output, dict):
        raise ReplicateError(f"embedding output was {type(output).__name__}, expected an object")

    embeddings = output.get("embeddings")
    if not isinstance(embeddings, dict) or not embeddings:
        raise ReplicateError("embedding output carried no 'embeddings' mapping")

    cleaned: dict[str, list[float]] = {}
    for label, vector in embeddings.items():
        if not isinstance(vector, list) or not vector:
            raise ReplicateError(f"embedding for {label!r} was not a non-empty list")
        try:
            cleaned[str(label)] = [float(x) for x in vector]
        except (TypeError, ValueError) as exc:
            raise ReplicateError(f"embedding for {label!r} held a non-number: {exc}") from exc

    dims = {len(v) for v in cleaned.values()}
    if len(dims) != 1:
        raise ReplicateError(f"embeddings had mixed dimensions: {sorted(dims)}")
    only_dim = dims.pop()
    declared = output.get("dim")
    if declared is not None and int(declared) != only_dim:
        raise ReplicateError(f"cog declared dim {declared} but returned vectors of {only_dim}")

    encoder = str(output.get("encoder") or "").strip() or fallback_encoder
    return EmbedResponse(embeddings=cleaned, dim=only_dim, encoder=encoder)
