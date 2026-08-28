"""Stage 2 ASR Backend: Replicate serverless GPU.

Runs transcription, alignment, and diarization remotely. This repository never
loads ASR, alignment, diarization, or speaker-embedding weights locally.

Uploads normalized audio to Replicate's files API, submits a prediction
job to victor-upmeet/whisperx, polls to completion, and maps the output JSON
into the standard pipeline.asr.Transcript dataclass.
"""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from pipeline.asr import (
    Transcript,
    _segments_from_provider_output,
)
from pipeline.config import (
    ASR_BATCH_SIZE,
    ASR_LANGUAGE,
    ENABLE_DIARIZATION,
    MAX_SPEAKERS,
    MIN_SPEAKERS,
    REMOTE_ASR_HF_TOKEN,
    REPLICATE_API_TOKEN,
    REPLICATE_MODEL,
    REPLICATE_POLL_INTERVAL_SEC,
    REPLICATE_TIMEOUT_SEC,
    REPLICATE_UPLOAD_ATTEMPTS,
    REPLICATE_UPLOAD_BACKOFF_MAX_SEC,
    REPLICATE_UPLOAD_BACKOFF_SEC,
    TARGET_SAMPLE_RATE,
    UPLOAD_AUDIO_BITRATE,
    UPLOAD_AUDIO_CODEC,
)

REPLICATE_API_BASE = "https://api.replicate.com/v1"
DEFAULT_VERSION = "655845d6190ef70573c669245f245892cd039df4b880a1e3a65852c09252f5cc"


class ReplicateError(RuntimeError):
    """Raised when Replicate API requests or predictions fail.

    ``transient`` distinguishes a network fault, which is worth requeueing, from
    a rejected token or malformed input, which repeats identically forever.
    """

    def __init__(self, *args: object, transient: bool = False) -> None:
        super().__init__(*args)
        self.transient = transient


# ffmpeg encoder -> (container suffix, HTTP content type). The upload body must
# be labelled for what it actually is; an Ogg body sent as audio/mpeg is a silent
# decode failure on the far side rather than an error anyone sees.
_UPLOAD_FORMATS: dict[str, tuple[str, str]] = {
    "libopus": (".ogg", "audio/ogg"),
    "libmp3lame": (".mp3", "audio/mpeg"),
    "aac": (".m4a", "audio/mp4"),
}
_FALLBACK_CODEC = "libmp3lame"


def upload_content_type(path: Path) -> str:
    """HTTP content type for an encoded upload body, by container suffix."""
    for suffix, content_type in _UPLOAD_FORMATS.values():
        if path.suffix.lower() == suffix:
            return content_type
    return "application/octet-stream"


def normalize_audio_for_upload(src: Path, dest: Path) -> Path:
    """Transcode to a small 16 kHz mono body for fast, reliable upload.

    Payload size drives the upload failure rate on a flaky path, so this sends as
    few bytes as the ASR and diarizer can work with: ~2-4 MB/hr of Opus in place
    of ~115 MB/hr of raw WAV. See ``UPLOAD_AUDIO_CODEC`` for the accuracy
    measurements behind the default.

    Falls back to MP3 when the configured encoder is missing rather than failing
    the meeting: an ffmpeg build without libopus is a packaging problem, not a
    reason to lose a recording.
    """
    codec = UPLOAD_AUDIO_CODEC if UPLOAD_AUDIO_CODEC in _UPLOAD_FORMATS else _FALLBACK_CODEC
    for candidate in dict.fromkeys([codec, _FALLBACK_CODEC]):
        suffix, _ = _UPLOAD_FORMATS[candidate]
        out = dest.with_suffix(suffix)
        result = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-i", str(src),
                "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE),
                "-c:a", candidate, "-b:a", UPLOAD_AUDIO_BITRATE, str(out),
            ],
            capture_output=True,
        )
        if result.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return out
        if candidate != _FALLBACK_CODEC:
            print(f"    {candidate} unavailable, falling back to {_FALLBACK_CODEC}")

    raise ReplicateError(
        f"ffmpeg could not encode {src.name} for upload with "
        f"{codec} or {_FALLBACK_CODEC}"
    )


class ReplicateBackend:
    """Serverless GPU transcription via Replicate."""

    def __init__(
        self,
        api_token: str = REPLICATE_API_TOKEN,
        model_name: str = REPLICATE_MODEL,
        language: str = ASR_LANGUAGE,
        diarize: bool = ENABLE_DIARIZATION,
        min_speakers: int | None = MIN_SPEAKERS,
        max_speakers: int | None = MAX_SPEAKERS,
        batch_size: int = ASR_BATCH_SIZE,
        timeout_sec: float = REPLICATE_TIMEOUT_SEC,
        poll_interval_sec: float = REPLICATE_POLL_INTERVAL_SEC,
    ) -> None:
        self.api_token = (api_token or "").strip()
        self.model_name = model_name.strip()
        self.language = language
        self.diarize = diarize
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers
        self.batch_size = batch_size
        self.timeout_sec = timeout_sec
        self.poll_interval_sec = poll_interval_sec
        self.name = f"replicate:{self.model_name}"
        self._cached_version: str | None = None

    def _headers(self) -> dict[str, str]:
        if not self.api_token:
            raise ReplicateError(
                "REPLICATE_API_TOKEN is unset. Add your Replicate token to .env: "
                "REPLICATE_API_TOKEN=r8_..."
            )
        return {
            "Authorization": f"Bearer {self.api_token}",
            "User-Agent": "meeting-minutes-compiler/1.0",
        }

    def _resolve_version(self, client: httpx.Client) -> str:
        """Resolve model version hash from model name or cache."""
        if ":" in self.model_name:
            return self.model_name.split(":", 1)[1]
        if self._cached_version:
            return self._cached_version

        # Default fallback for victor-upmeet/whisperx to avoid extra network roundtrip
        if self.model_name in ("victor-upmeet/whisperx", "whisperx"):
            self._cached_version = DEFAULT_VERSION
            return DEFAULT_VERSION

        url = f"{REPLICATE_API_BASE}/models/{self.model_name}"
        resp = client.get(url, headers=self._headers(), timeout=15.0)
        if resp.status_code == 200:
            version_id = resp.json().get("latest_version", {}).get("id")
            if version_id:
                self._cached_version = version_id
                return str(version_id)

        # Fallback to default version if lookup fails
        return DEFAULT_VERSION

    def _upload_file(self, client: httpx.Client, audio_path: Path) -> str:
        """Upload audio to Replicate's temporary files store with retry on network error.

        Retry hard rather than briefly. Large bodies are reset mid-transfer at a
        rate that climbs with payload size (see the measurements on
        ``REPLICATE_UPLOAD_ATTEMPTS``), so a long recording can lose several
        attempts in a row on an otherwise healthy connection. Giving up early
        parks the meeting at ``failed`` and no minutes are ever compiled for it.
        """
        upload_url = f"{REPLICATE_API_BASE}/files"
        last_exc: Exception | None = None
        attempts = max(1, REPLICATE_UPLOAD_ATTEMPTS)

        for attempt in range(1, attempts + 1):
            try:
                with open(audio_path, "rb") as f:
                    files = {
                        "content": (
                            audio_path.name, f, upload_content_type(audio_path),
                        )
                    }
                    resp = client.post(
                        upload_url,
                        headers=self._headers(),
                        files=files,
                        timeout=300.0,
                    )

                if resp.status_code in (200, 201):
                    data = resp.json()
                    file_url = data.get("urls", {}).get("get") or data.get("url")
                    if file_url:
                        return str(file_url)

                if resp.status_code == 429:
                    retry_after = int(resp.json().get("retry_after", 8))
                    print(f"    rate limited on upload, waiting {retry_after}s...")
                    time.sleep(retry_after + 1)
                    continue

                raise ReplicateError(
                    f"Failed to upload audio to Replicate ({resp.status_code}): {resp.text}"
                )
            except (httpx.RemoteProtocolError, httpx.NetworkError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < attempts:
                    delay = min(
                        REPLICATE_UPLOAD_BACKOFF_SEC * (2 ** (attempt - 1)),
                        REPLICATE_UPLOAD_BACKOFF_MAX_SEC,
                    )
                    print(
                        f"    upload network retry ({attempt}/{attempts}), "
                        f"waiting {delay:.0f}s..."
                    )
                    time.sleep(delay)

        raise ReplicateError(
            f"Audio upload failed after {attempts} attempts: {last_exc}",
            transient=True,
        )

    def _create_prediction(
        self, client: httpx.Client, audio_url: str, initial_prompt: str
    ) -> dict[str, Any]:
        """Submit an ASR prediction job to Replicate with rate-limit handling."""
        version_id = self._resolve_version(client)
        create_url = f"{REPLICATE_API_BASE}/predictions"

        input_payload: dict[str, Any] = {
            "audio_file": audio_url,
            "diarization": self.diarize,
            "align_output": True,
            "batch_size": self.batch_size,
        }

        if self.language and self.language != "auto":
            input_payload["language"] = self.language
        if initial_prompt:
            input_payload["initial_prompt"] = initial_prompt
        if self.min_speakers is not None:
            input_payload["min_speakers"] = self.min_speakers
        if self.max_speakers is not None:
            input_payload["max_speakers"] = self.max_speakers
        if REMOTE_ASR_HF_TOKEN:
            input_payload["huggingface_access_token"] = REMOTE_ASR_HF_TOKEN

        payload = {"version": version_id, "input": input_payload}
        headers = {**self._headers(), "Content-Type": "application/json"}

        for _attempt in range(1, 4):
            resp = client.post(create_url, headers=headers, json=payload, timeout=30.0)
            if resp.status_code in (200, 201):
                return resp.json()

            if resp.status_code == 429:
                retry_after = int(resp.json().get("retry_after", 8))
                print(f"    rate limited by Replicate, waiting {retry_after}s...")
                time.sleep(retry_after + 1)
                continue

            raise ReplicateError(
                f"Failed to create Replicate prediction ({resp.status_code}): {resp.text}"
            )

        raise ReplicateError("Replicate prediction creation rate limited after retries")

    def _poll_prediction(self, client: httpx.Client, prediction_id: str) -> dict[str, Any]:
        """Poll prediction until completion or timeout."""
        poll_url = f"{REPLICATE_API_BASE}/predictions/{prediction_id}"
        headers = self._headers()
        start_time = time.time()
        interval = self.poll_interval_sec

        while True:
            elapsed = time.time() - start_time
            if elapsed > self.timeout_sec:
                with contextlib.suppress(Exception):
                    client.post(f"{poll_url}/cancel", headers=headers, timeout=10.0)
                raise TimeoutError(
                    f"Replicate prediction {prediction_id} timed out after {self.timeout_sec:.0f}s"
                )

            resp = client.get(poll_url, headers=headers, timeout=30.0)
            if resp.status_code != 200:
                raise ReplicateError(
                    f"Failed to poll prediction {prediction_id} ({resp.status_code}): {resp.text}"
                )

            data = resp.json()
            status = data.get("status")

            if status == "succeeded":
                return data
            if status in ("failed", "canceled"):
                error_msg = data.get("error") or f"Prediction ended with status: {status}"
                raise ReplicateError(f"Replicate prediction failed: {error_msg}")

            time.sleep(interval)

    def transcribe(self, audio_path: Path, meeting_id: str, initial_prompt: str) -> Transcript:
        """Transcribe meeting audio using Replicate GPU worker."""
        if not self.api_token:
            raise ReplicateError(
                "REPLICATE_API_TOKEN is unset. Add your Replicate token to .env: "
                "REPLICATE_API_TOKEN=r8_..."
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Compress to a small 16 kHz mono body for fast, reliable upload
            upload_path = normalize_audio_for_upload(audio_path, Path(tmpdir) / "upload")
            upload_size_mb = upload_path.stat().st_size / (1024 * 1024)

            with httpx.Client() as client:
                print(f"    uploading to Replicate ({upload_size_mb:.1f} MB)...")
                audio_url = self._upload_file(client, upload_path)

                print(f"    transcribing on Replicate GPU ({self.model_name})...")
                result_data: dict[str, Any] | None = None
                last_pred_error: Exception | None = None

                for pred_attempt in range(1, 3):
                    try:
                        if pred_attempt > 1:
                            print(f"    retrying GPU prediction (attempt {pred_attempt}/2)...")
                            time.sleep(3.0)
                        pred = self._create_prediction(client, audio_url, initial_prompt)
                        prediction_id = pred["id"]
                        result_data = self._poll_prediction(client, prediction_id)
                        break
                    except ReplicateError as err:
                        last_pred_error = err
                        if "Worker crashed" in str(err) or "canceled" in str(err):
                            continue
                        raise
                else:
                    if last_pred_error:
                        raise last_pred_error

                raw_output = (result_data or {}).get("output")
                if isinstance(raw_output, str) and raw_output.startswith("http"):
                    out_resp = client.get(raw_output, timeout=60.0)
                    out_resp.raise_for_status()
                    raw_output = out_resp.json()

                if not isinstance(raw_output, dict):
                    raw_output = {"segments": raw_output if isinstance(raw_output, list) else []}

                language = (
                    raw_output.get("language")
                    or raw_output.get("detected_language")
                    or self.language
                )

                segments = _segments_from_provider_output(raw_output)

        # Estimate duration from segments
        duration_sec = 0.0
        if segments:
            duration_sec = max(seg.end for seg in segments)

        return Transcript(
            meeting_id=meeting_id,
            model=self.name,
            language=language,
            duration_sec=duration_sec,
            segments=segments,
        )
