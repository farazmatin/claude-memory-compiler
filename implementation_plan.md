# Implementation Plan — Replicate Serverless GPU ASR Integration

Enable sub-3-minute meeting minutes generation by integrating **Replicate serverless GPU ASR** (`victor-upmeet/whisperx`) into the Meeting Minutes Compiler (`claude-memory-compiler`). This replaces the slow multi-hour local CPU transcription with an on-demand cloud GPU pipeline while preserving the exact same WhisperX + Pyannote diarization stack and schema.

## User Review Required

> [!IMPORTANT]
> **API Token Required:** To run against live Replicate servers, you will need a Replicate API token from [replicate.com/account/api-tokens](https://replicate.com/account/api-tokens) and add `REPLICATE_API_TOKEN=r8_...` to your `.env` file.
> 
> The pipeline will automatically detect `REPLICATE_API_TOKEN` and use Replicate as the primary ASR backend, while gracefully falling back to local CPU if the token is unset.

---

## Proposed Changes

### Configuration & Environment Layer

#### [MODIFY] [config.py](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/config.py)
- Add `REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN", "")`
- Add `ASR_BACKEND = os.environ.get("MMC_ASR_BACKEND", "auto").lower()` (options: `auto`, `replicate`, `whisperx`)
- Add `REPLICATE_MODEL = os.environ.get("MMC_REPLICATE_MODEL", "victor-upmeet/whisperx")`
- Add `REPLICATE_POLL_INTERVAL_SEC = float(os.environ.get("MMC_REPLICATE_POLL_INTERVAL", "2.0"))`
- Add `REPLICATE_TIMEOUT_SEC = float(os.environ.get("MMC_REPLICATE_TIMEOUT", "600"))`

#### [MODIFY] [.env.example](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/.env.example)
- Add documentation and configuration keys for `REPLICATE_API_TOKEN`, `MMC_ASR_BACKEND`, and `MMC_REPLICATE_MODEL`.

---

### ASR Engine Layer

#### [NEW] [replicate_asr.py](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/replicate_asr.py)
- Create `ReplicateBackend` implementing the `Backend` protocol.
- Normalizes input audio using `normalize_audio()` (to optimize bandwidth and upload speed).
- Uploads audio to Replicate's files API (`POST https://api.replicate.com/v1/files`).
- Creates a prediction for `victor-upmeet/whisperx` with:
  - `audio_file`: uploaded file URL
  - `diarization`: `True`
  - `align_output`: `True`
  - `hf_token`: `HF_TOKEN` (for Pyannote gated access if needed)
  - `initial_prompt`: vocabulary bias from `glossary.md`
  - `batch_size`: `16`
  - `min_speakers` / `max_speakers`: passed from config if configured
- Polls prediction status with exponential backoff / interval polling until `succeeded` or `failed`.
- Maps the result segments and word timestamps into `Transcript` dataclass via `_segments_from_whisperx()`.

#### [MODIFY] [asr.py](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/asr.py)
- Update `default_backend()` to check `ASR_BACKEND` and `REPLICATE_API_TOKEN`.
- Auto-select `ReplicateBackend()` when configured/available, otherwise use `WhisperXBackend()`.

---

### Preflight Diagnostics & Verification Layer

#### [MODIFY] [doctor.py](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/doctor.py)
- Enhance `check_asr()` to detect whether Replicate is active.
- If `REPLICATE_API_TOKEN` is present, verify account authentication / model accessibility via API health probe.
- Clearly report in doctor output: `[ok] asr backend: Replicate GPU (victor-upmeet/whisperx)`.

---

### Testing & Quality Control Layer

#### [NEW] [test_replicate_asr.py](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/tests/test_replicate_asr.py)
- Unit tests for `ReplicateBackend`:
  - Mock file upload and URL response.
  - Mock prediction creation and polling loop (`starting` -> `processing` -> `succeeded`).
  - Verify error handling on prediction failure (`status: failed`).
  - Verify parsing of segments, word timestamps, and speaker labels into `Transcript`.
  - Verify `default_backend()` routing logic (auto-detect vs explicit override vs fallback).

---

## Verification Plan

### Automated Tests
- Run complete test suite:
  ```powershell
  uv run pytest tests/test_replicate_asr.py tests/test_db.py tests/test_compile_minutes.py
  ```
- Run code formatting & linter:
  ```powershell
  uv run ruff check .
  ```

### Manual Verification
- Run `uv run pipeline doctor` to verify environment checks and backend detection.
- Validate with a test audio recording using `uv run pipeline transcribe --limit 1` once token is configured.
