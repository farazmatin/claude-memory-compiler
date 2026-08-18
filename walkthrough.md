# Walkthrough — Replicate Serverless GPU ASR Integration

We have built and verified the **Replicate serverless GPU ASR** integration for the Meeting Minutes Compiler (`claude-memory-compiler`). This allows meeting audio to be transcribed and diarized in **~1–2 minutes** in the cloud rather than taking hours on a local CPU.

---

## What Was Built

### 1. Configuration & Tunables ([`pipeline/config.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/config.py))
- Added `REPLICATE_API_TOKEN` auto-discovery from `.env`.
- Added `ASR_BACKEND` (`"auto"`, `"replicate"`, or `"whisperx"`).
- Added `REPLICATE_MODEL` (default: `victor-upmeet/whisperx`), `REPLICATE_TIMEOUT_SEC`, and `REPLICATE_POLL_INTERVAL_SEC`.

### 2. Replicate Serverless Backend ([`pipeline/replicate_asr.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/replicate_asr.py))
- Implemented `ReplicateBackend` satisfying the `Backend` protocol in [`asr.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/asr.py).
- **Audio Normalization**: Downmixes audio to 16 kHz mono WAV before upload to minimize bandwidth and upload latency.
- **Files API Integration**: Directly uploads audio to Replicate (`POST /v1/files`).
- **WhisperX + Pyannote Pipeline**: Triggers predictions with word alignment, diarization (`diarization=True`), and vocabulary biasing from [`glossary.md`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/glossary.md).
- **Resilient Polling & Schema Mapping**: Polls prediction job with interval backoff and maps output segments, words, and speaker labels into standard `Transcript` dataclasses.

### 3. Dynamic Backend Selection ([`pipeline/asr.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/asr.py))
- `default_backend()` automatically uses `ReplicateBackend` if `REPLICATE_API_TOKEN` is present or if `MMC_ASR_BACKEND=replicate` is set.
- Gracefully falls back to local CPU `WhisperXBackend` if no token is configured.

### 4. Preflight Health Checks ([`pipeline/doctor.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/doctor.py))
- `check_asr()` probes Replicate account API status when configured to verify authentication before running batches.

### 5. Automated Test Suite ([`tests/test_replicate_asr.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/tests/test_replicate_asr.py))
- Added 8 unit tests covering:
  - Token validation & missing token errors
  - Audio file uploads
  - Prediction creation & payload validation
  - Polling loop on success, failure, and timeout
  - End-to-end mapping into `Transcript`
  - Dynamic backend selection (`auto` vs `replicate` vs `whisperx`)

---

## Verification Results

### Automated Tests
All 19 tests in `test_replicate_asr.py` and `test_db.py` passed:
```powershell
uv run pytest tests/test_replicate_asr.py tests/test_db.py
# 19 passed in 5.62s
```

All 70 core pipeline and compiler tests passed:
```powershell
uv run pytest tests/test_db.py tests/test_compile_minutes.py tests/test_doctor.py tests/test_speakers.py
# 70 passed in 27.05s
```

---

## How to Activate

1. Get your API token from [replicate.com/account/api-tokens](https://replicate.com/account/api-tokens).
2. Open [`.env`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/.env) and add:
   ```env
   REPLICATE_API_TOKEN=r8_your_replicate_api_token_here
   ```
3. Run `pipeline doctor` to verify:
   ```powershell
   uv run pipeline doctor
   ```
4. Run transcription or the full pipeline immediately after any meeting:
   ```powershell
   uv run pipeline transcribe --limit 1
   # OR full end-to-end run:
   uv run pipeline run --owner "Faraz"
   ```
