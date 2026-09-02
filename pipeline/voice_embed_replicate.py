"""Replicate backend for the voice-embedding stage.

Shares the ASR client's transport - upload, auth headers, polling - because the
retry budget in `_upload_file` exists for a property of this network path, not
of transcription. It deliberately does NOT reuse `_create_prediction` (its
payload is ASR-shaped) or `_resolve_version` (its lookup failure falls back to
the pinned *whisperx* version, which for a voice model would submit audio to
entirely the wrong cog and return something that looks like a vector).

## The cog contract

The provider is the self-deployed cog named in the 2026-08-27 design:
`farazmatin/speaker-embed`, hosting several encoders behind one interface and
embedding the whisperx labels' own regions server-side, so no label-mapping
happens on the wire.

    MMC_REMOTE_VOICE_MODEL     the cog, e.g. farazmatin/speaker-embed
    MMC_REMOTE_VOICE_VERSION   pinned version hash (required)
    MMC_REMOTE_VOICE_ENCODER   which encoder to serve

Input:

    {"audio": <url>, "encoder": str,
     "regions": "<JSON array of {label, start, end}>"}

`regions` is a JSON *string*, not an array: cog inputs are scalars, files and
flat lists, so a list of objects has to travel encoded.

Output:

    {"embeddings": {label: [float, ...]}, "dim": int, "encoder": str}

The version is required and separate from the name, because the namespace every
vector is keyed by is `encoder@version`. An unpinned model would let the weights
change under a namespace that claims to identify them, silently redefining every
stored voiceprint.

The default encoder, `wespeaker-resnet34-lm`, is the same representation family
as the 268 vectors already in the corpus. If the cog serves those weights the
existing 23 enrolled people carry over rather than restarting - which is what
scripts/probe_voice_comparability.py exists to confirm before any backfill.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from pipeline.config import (
    REMOTE_VOICE_ENCODER,
    REMOTE_VOICE_MODEL,
    REMOTE_VOICE_VERSION,
    REPLICATE_TIMEOUT_SEC,
)
from pipeline.replicate_asr import REPLICATE_API_BASE, ReplicateBackend, ReplicateError
from pipeline.voice_embed import EmbedResponse, LabelRegion


class ReplicateVoiceBackend:
    """Embeds every region of one meeting in a single prediction."""

    def __init__(
        self,
        model: str | None = None,
        version: str | None = None,
        encoder: str | None = None,
    ) -> None:
        raw = (model or REMOTE_VOICE_MODEL).strip()
        # Accept owner/name:version as a convenience, but keep the two apart
        # internally: the version is half the namespace, not part of the name.
        if ":" in raw:
            raw, embedded_version = raw.split(":", 1)
        else:
            embedded_version = ""
        self.model = raw
        self.version = (version or embedded_version or REMOTE_VOICE_VERSION).strip()
        self.encoder = (encoder or REMOTE_VOICE_ENCODER).strip()

        if not self.model:
            raise ReplicateError("no embedding model configured; set MMC_REMOTE_VOICE_MODEL")
        if not self.version:
            raise ReplicateError(
                f"{self.model} has no pinned version; set MMC_REMOTE_VOICE_VERSION. "
                "Unpinned weights silently redefine every stored voiceprint."
            )
        if not self.encoder:
            raise ReplicateError("no encoder selected; set MMC_REMOTE_VOICE_ENCODER")
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
                "encoder": self.encoder,
                # Encoded, because a cog input cannot be a list of objects.
                "regions": json.dumps(
                    [
                        {"label": r.label, "start": round(r.start, 3), "end": round(r.end, 3)}
                        for r in regions
                    ]
                ),
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
