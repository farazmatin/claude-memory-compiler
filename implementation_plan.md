# Implementation Plan: Cloud ASR, Gemini 3.7 Flash, Categories, 20s Snippets, and Deletion Controls

This design and implementation plan details the complete architectural overhaul of `claude-memory-compiler`.

---

## 1. Cloud-Based Serverless GPU Transcription

### Architecture
- Replace slow CPU transcription with **Replicate Serverless GPU ASR (`victor-upmeet/whisperx`)**.
- Uses Pyannote diarization and word alignment in the cloud, completing 40-minute meetings in ~1–2 minutes.
- Configured with `REPLICATE_API_TOKEN` and dynamic fallback to local CPU if token is unset.

### Core Modules
- [`pipeline/replicate_asr.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/replicate_asr.py): Backend implementation uploading audio to `/v1/files` and polling predictions.
- [`pipeline/asr.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/asr.py): Dynamic backend factory routing.

---

## 2. LLM Engine Enforcement: Gemini 3.7 Flash

### Architecture
- Enforce **`gemini-3.7-flash`** across minutes compilation, speaker label resolution, and RAG Q&A synthesis.
- Replaces generic default models.

### Core Modules
- [`pipeline/llm.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/llm.py): Centralized model prompt execution with fallback cascades and timeout handling.
- [`pipeline/config.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/config.py): Global `GEMINI_MODEL = "gemini-3.7-flash"`.

---

## 3. Full-Meeting Speaker Diarization Resolution & 20s Audio Snippets

### Architecture
- Multi-candidate name extraction from Google Drive filenames, title hints, known roster directory, and compiled minutes attendees.
- Dialogue sampling spanning introduction, middle, and conclusion of meetings.
- 20-second audio snippet streaming (`/api/audio/snippet?duration=20`) via FFmpeg extraction with 1-click modal speaker assignment.
- Added `--all` flag to `pipeline speakers` CLI command.

### Core Modules
- [`pipeline/speakers.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/speakers.py): Name extraction and LLM resolution heuristics.
- [`pipeline/cli.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/cli.py): CLI parser with `--all` support.
- [`pipeline/dashboard.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/dashboard.py): `extract_speaker_snippet()` with 20s duration.

---

## 4. Meeting Categorization & Prominent Date/Time Hierarchy

### Architecture
- Classification into `Personal` (Household, rental properties like 157 Blacklock, family, health) vs `Professional` (Standups, 1:1s, Architecture Discovery, Planning, Reviews).
- Real meeting occurrence date and time (`📅 Monday, Aug 17 · 🕒 1:51 PM`) prominently featured at the top of cards and reader headers.
- Category filter in archive toolbar + 1-click live category selector in the reader header.

---

## 5. Storage Management & Granular Deletion Controls

### Architecture
- **Delete Local Audio (`POST /api/meetings/{id}/delete-audio`)**: Deletes the local raw audio file (`.m4a`/`.mp4`), clears `audio_path` in SQLite, and frees disk space while retaining the text minutes, speaker assignments, and LightRAG search indexing.
- **Delete Meeting & Brief (`DELETE /api/meetings/{id}`)**: Permanently removes the meeting from SQLite, minutes markdown, transcript JSON, local audio, and the LightRAG knowledge graph with safety confirmation.

### Core Modules
- [`pipeline/db.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/db.py): `delete_meeting()`, `clear_audio_path()`.
- [`pipeline/dashboard.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/dashboard.py): `delete_meeting_audio()`, `delete_entire_meeting()`, `do_DELETE`.
- [`pipeline/static/app.js`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/static/app.js), [`pipeline/static/index.html`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/static/index.html): Deletion modal dialog and management bar.
