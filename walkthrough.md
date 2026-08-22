# Master Walkthrough & System Guide — Meeting Minutes Compiler

This document provides a full review of all components, pipeline stages, model configurations, and UI features operating in `claude-memory-compiler`.

---

## 1. Cloud-Powered Pipeline Architecture

1. **ASR Transcription Engine ([`pipeline/replicate_asr.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/replicate_asr.py))**:
   - Uses **Replicate Serverless GPU (`victor-upmeet/whisperx`)** with Pyannote diarization.
   - Pre-normalizes audio to 16 kHz mono WAV, uploads directly via Replicate Files API, and completes full meeting transcription in **~1–2 minutes**.
2. **LLM Synthesis & Models ([`pipeline/llm.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/llm.py), [`pipeline/config.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/config.py))**:
   - Enforced model: **`gemini-3.7-flash`** across all prompts (structured minutes generation, attendee candidate resolution, and Q&A).
3. **AI Search & Local Fallback Engine ([`pipeline/answer.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/answer.py))**:
   - Features dual-mode search: LightRAG knowledge graph lookup with a 10s fast timeout and automatic fallback to local `minutes/` context.
4. **Speaker Diarization & Resolution Engine ([`pipeline/speakers.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/speakers.py), [`pipeline/cli.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/cli.py))**:
   - Resolves speaker labels (`SPEAKER_00`, `SPEAKER_01`) by extracting candidate names from Drive filenames, title hints, known roster names, and sampling full-meeting dialogue turns.
   - Run batch resolution on all meetings: `uv run pipeline speakers --all --owner "Faraz"`.

---

## 2. Dashboard UI & Interactive Capabilities ([`http://localhost:8765`](http://localhost:8765))

1. **Prominent Meeting Dates & Times**:
   - Real occurrence timestamp (e.g. `📅 Monday, Aug 17 · 🕒 1:51 PM`) displayed prominently at the top of cards and reader headers.
2. **Categorization (`Personal` vs. `Professional`)**:
   - Automated classification into `💼 Professional` (Standups, 1:1s, Architecture, Reviews) and `🏠 Personal & Household` (Rental properties, household notes, family, health).
   - Category filter dropdown in archive toolbar + 1-click live category switcher in the reader header.
3. **20-Second High-Fidelity Audio Snippets**:
   - Streams a 20-second audio clip of any speaker directly in the modal player (`/api/audio/snippet?duration=20`) to verify voice and confirm name.
4. **Granular Storage & Deletion Controls**:
   - **`🗑️ Delete Local Audio (Free Space)`**: Deletes raw audio files (`audio/*.m4a`) to reclaim disk storage while keeping text briefs, transcripts, and AI search indexing 100% intact.
   - **`🗑️ Delete Meeting & Brief`**: Complete cascade deletion across SQLite database, disk files, and LightRAG graph with confirmation modal.

---

## 3. Reference Commands

```powershell
# Run preflight system checks
uv run pipeline doctor

# Ingest and process all pending recordings end-to-end
uv run pipeline run --owner "Faraz"

# Run automated speaker name resolution across all meetings
uv run pipeline speakers --all --owner "Faraz"

# Start the local interactive dashboard
uv run pipeline dashboard
```
