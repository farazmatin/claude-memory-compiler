# Session Technical Changelog & Architectural Reference (2026-08-17)

> **Audience**: AI Coding Agents & System Engineers working on `claude-memory` / `claude-memory-compiler`.
> **Context**: Complete record of all pipeline upgrades, model configurations, UI features, and execution protocols established during this session.

---

## 1. Executive Summary of Changes

| Domain | Previous State | Current Upgraded State | Key Files |
| :--- | :--- | :--- | :--- |
| **LLM Model** | `gemini-2.5-flash` / default LLM | Enforced **`gemini-3.7-flash`** exclusively for minutes compilation, speaker resolution, and RAG Q&A synthesis. | [`pipeline/llm.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/llm.py), [`pipeline/config.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/config.py) |
| **ASR Backend** | Slow local CPU transcription (hours/meeting) | **Replicate Serverless GPU (`victor-upmeet/whisperx`)** (~1–2m/meeting) with Pyannote diarization and zero local CPU contention. | [`pipeline/replicate_asr.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/replicate_asr.py), [`pipeline/asr.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/asr.py) |
| **Speaker Resolution** | 2-speaker limit, 240s window, strict JSON | Multi-turn full meeting sampling (intro/mid/outro), multi-candidate name extraction from title hints/Drive filenames/team roster, robust markdown-fenced JSON parsing, and `--all` batch CLI flag. | [`pipeline/speakers.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/speakers.py), [`pipeline/cli.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/cli.py) |
| **Voice Snippets** | None / 10s audio player | **20-Second High-Fidelity Audio Snippet streaming** (`/api/audio/snippet?duration=20`) via FFmpeg extraction with 1-click modal speaker assignment. | [`pipeline/dashboard.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/dashboard.py), [`pipeline/static/app.js`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/static/app.js) |
| **Q&A / AI Search** | LightRAG graph only | **Hybrid Search with Instant Local Fallback**: 10s timeout on LightRAG graph, auto-fallback to local `minutes/` context with `gemini-3.7-flash` synthesis. | [`pipeline/answer.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/answer.py) |
| **Meeting Categorization** | Flat list / no categories | **`Personal` vs. `Professional`** classification with sub-types (Standups, 1:1s, Architecture, Household/Rental), archive dropdown filter, and live reader override selector. | [`pipeline/dashboard.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/dashboard.py), [`pipeline/static/app.js`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/static/app.js) |
| **Prominent Date & Time** | Technical discovery timestamp | Real meeting occurrence date & time prominently formatted (`📅 Monday, Aug 17 · 🕒 1:51 PM`) at top of cards and reader. | [`pipeline/dashboard.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/dashboard.py), [`pipeline/static/app.js`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/static/app.js) |
| **Storage & Deletion** | No deletion controls | **Granular Deletion Controls**: `🗑️ Delete Local Audio` (frees disk space while retaining minutes & AI graph) + `🗑️ Delete Meeting & Brief` (complete cascade across DB, disk, and LightRAG). | [`pipeline/db.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/db.py), [`pipeline/dashboard.py`](file:///c:/Users/faraz/Documents/Script/Git/claude-memory/claude-memory-compiler/pipeline/dashboard.py) |

---

## 2. Key Architecture & Coding Patterns for Future Agents

### A. LLM Invocation Pattern (`pipeline/llm.py`)
- Model configured as `GEMINI_MODEL = "gemini-3.7-flash"`.
- Primary generator attempts direct Gemini API call, cascading to Google Codex / CLI with strict timeout handling.
- Never downgrade the model to `2.5-flash` or smaller variants unless explicitly asked by the user.

### B. Headless Browser / Playwright Rule
- **ADE Constraint**: When running browser validation or Playwright automation in this environment, **always use headless mode** (`launch(headless=True)`).
- Never launch GUI browser windows as it can cause terminal/ADE lockup.

### C. Database Cascade & Deletion Architecture (`pipeline/db.py`)
- `delete_meeting(conn, meeting_id)`:
  - Deletes from `speakers`, `stage_runs`, `drive_sources`, and `meetings`.
  - Corresponding files on disk (`audio/*.m4a`, `transcripts/*.json`, `minutes/*.md`) and LightRAG document IDs (`/documents/delete_document`) must be unlinked.
- `clear_audio_path(conn, meeting_id)`:
  - Sets `audio_path = NULL` in `meetings` table when raw audio is purged to free disk space. The meeting remains `indexed` and fully searchable.

### D. Audio Snippet Extraction (`pipeline/dashboard.py`)
- Uses FFmpeg to extract exact timestamps corresponding to a speaker's longest speech segment in a transcript.
- Standard snippet length is **20 seconds** (`duration_sec = 20.0`).
- Streamed as `audio/mpeg` with `X-Snippet-Text` header containing the spoken sentence for context.

---

## 3. Verified Endpoints Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/overview` | Returns system health, meeting counts, velocity, and ASR status. |
| `GET` | `/api/meetings` | Returns summary list of all meetings with categories and dates. |
| `GET` | `/api/meetings/{id}` | Returns meeting detail, markdown minutes, speaker chips, and entities. |
| `GET` | `/api/audio/snippet` | Streams a 20-second MP3 clip for a specific `meeting_id` and speaker `label`. |
| `POST` | `/api/meetings/{id}/speakers` | Assigns a contact name to a speaker tag (`SPEAKER_00`). |
| `POST` | `/api/meetings/{id}/category` | Updates the meeting domain (`Professional` vs `Personal`) in frontmatter. |
| `POST` | `/api/meetings/{id}/delete-audio`| Deletes the local raw audio file to save disk space. |
| `DELETE`| `/api/meetings/{id}` | Permanently deletes the meeting record across disk, SQLite, and LightRAG. |
| `POST` | `/api/ask` | Queries the AI assistant with local context fallback. |
| `GET` | `/api/timeline?topic={t}` | Generates chronological decision evolution milestones. |
