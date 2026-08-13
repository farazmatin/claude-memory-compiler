# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A meeting-minutes compiler. Audio goes in; structured minutes and a queryable knowledge graph come out. Three tiers, and the split between them is the whole design:

| Tier | What | Indexed? |
|---|---|---|
| 1 | Audio + full diarized transcript | **No** — retained for provenance and recompilation |
| 2 | Structured minutes, one per meeting | **Yes** — this is the corpus |
| 3 | Knowledge graph + vector index (LightRAG) | derived |

Transcripts are retained but never indexed: an hour of speech is ~10,000 words containing maybe 500 of durable signal, and indexing the rest collapses retrieval precision. They are kept because minutes are a *lossy compile* — bumping `TEMPLATE_VERSION` rebuilds years of history with no re-transcription cost.

See `docs/PRD.md` and `docs/ARCHITECTURE.md` for the reasoning behind each decision.

## Commands

```bash
uv sync                            # core deps
uv sync --extra asr                # + whisperx (heavy: torch, CUDA libs)
uv sync --extra web                # + fastapi/uvicorn for the browser UI
uv sync --extra dev                # + pytest

uv run pipeline doctor             # preflight the environment (18 checks)
uv run pipeline init               # create directories and the manifest
uv run pipeline run                # every pending stage, in order
uv run pipeline status             # where everything is, plus measured stage timings
uv run pipeline query "question"   # ask the knowledge base
uv run pipeline serve              # the same questions in a browser (loopback)

uv run pytest                      # the suite; no network, no models, ~3s
uv run ruff check pipeline web tests
```

Ruff uses a 100-char line length and an explicit rule set (see `pyproject.toml` — the selection is pinned deliberately so a ruff release cannot break CI).

## Architecture

### Data flow

```
inbox/ (audio)
  → ingest      discover + dedup by content hash        → discovered
  → transcribe  whisperx ASR + align + diarize          → transcribed
  → speakers    SPEAKER_00 → real names                 → speakers_resolved
  → minutes     compile structured minutes              → minutes_compiled
  → index       push minutes into LightRAG              → indexed
```

Each stage claims meetings at one status and advances them to the next, tracked in `db/manifest.db`. Stages are independent and resumable — this is the central property, because ASR costs 30–50 CPU-minutes per meeting and must never be repeated.

Meetings always process **oldest first**: the minutes compiler reads earlier minutes to flag decisions that reverse previous positions, so out-of-order compilation would compare a meeting against its own future.

### Layers

- **`pipeline/`** — the stages plus the CLI. `db.py` is the state machine; `cli.py` orchestrates.
- **`web/`** — a read-only HTTP surface over the same modules, called in process. No second implementation of query semantics.
- **`minutes/`, `transcripts/`, `audio/`, `db/`** — runtime data, all gitignored.

### Key patterns

**The subscription ceiling.** Subscription LLMs (Gemini Flash → Codex → Claude, tried in order) cannot serve LightRAG, which needs an HTTP endpoint, and none offers embeddings. So two jobs moved to where the subscription *does* reach: the compiler emits entities and relations explicitly rather than letting a 4B local model discover them from prose, and synthesis is split from retrieval (`answer.py`). What remains local is graph traversal.

**Citations are grounded in retrieval.** `answer.Answer.sources` carries the `file_source` values LightRAG actually returned, parsed off the retrieved context — not off the answer prose, which a model can populate with meetings it never read. `db.meetings_by_minutes_names` resolves those filenames back to manifest rows.

**Recompilation.** `TEMPLATE_VERSION` in `pipeline/config.py` is stamped into every minutes file's frontmatter. Bumping it marks existing minutes stale so `pipeline minutes --recompile` rebuilds them from retained transcripts. `index.replace_minutes` deletes the previous LightRAG document before inserting — skipping that leaves both copies in the graph and retrieval starts returning contradictory duplicates.

**Failure is loud.** `pipeline run` exits non-zero if any stage failed, and `alert.py` pushes the failure somewhere visible: a headless server's cron mails the local user, and nobody reads local mail.

## Configuration

Everything configurable lives in `pipeline/config.py` or the environment (env wins). `.env.example` documents the required values — `MMC_LIGHTRAG_API_KEY` and `POSTGRES_PASSWORD` are needed before `docker compose up`, and `HF_TOKEN` is needed for diarization (without it, action items have no owners and you get only a warning).

- **Timezone:** `MMC_TIMEZONE`, default `America/Toronto`. A wrong value mislabels every document in the corpus.
- **ASR model:** `MMC_ASR_MODEL`, default `large-v3-turbo` — `large-v3` does not fit an overnight CPU batch.
- **Web UI:** `MMC_WEB_HOST` / `MMC_WEB_PORT`, default `127.0.0.1:8080`. Loopback because the UI has no auth model.

## Testing

Tests run against a throwaway working tree; `tests/conftest.py` sets the `MMC_*` variables *before* `pipeline.config` is imported, since that module reads them at import time. Nothing in the suite reaches the network or spawns a model — LightRAG and the LLM chain are stubbed at their boundaries.
