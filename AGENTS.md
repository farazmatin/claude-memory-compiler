# Agent guide — Meeting Minutes Compiler

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before work that touches the
pipeline, models, graph context, dashboard, or Product Manager integration. It
is the current architecture source of truth. This file is the execution guide.

## Repository responsibility

Meeting Memory owns private meeting capture, raw and derived meeting records,
speaker/person resolution, subscription-authored minutes/entities/relations,
graph publication, and the loopback context provider on port 8765.

Product Manager owns QA-gated product statements, decisions, requirements,
actions, artifacts, agents, and the PM cockpit. It may consume this
repository's bounded `background` context; it does not read this repository's
files or database directly.

## Work safely

- Check `git status --short` before editing. Preserve unrelated changes.
- `data/transcripts/` is raw source input. All other pipeline data is generated
  output; regenerate it instead of hand-editing it.
- Do not expose tokens, private audio, raw transcripts, or database contents.
- Do not convert a background result into a decision, owner, speaker, date,
  deadline, quote, commitment, or QA-approved Product Manager fact.
- Do not edit the isolated `codex/merge-tightening` worktree for unrelated
  capture, context, or documentation work.

## Normal pipeline and model policy

The normal nightly path is:

```text
Drive audio -> capture -> ingest -> transcribe -> speakers -> voices
-> minutes -> graph-sync -> bounded context provider
```

Use `uv run pipeline run --owner "Faraz"` for the full path and
`uv run pipeline graph-sync` to republish derived graph context.

- Codex, Claude, and Antigravity subscription CLIs author minutes, speaker
  resolution, entities, relations, and answer synthesis.
- No local model of any kind belongs in the normal nightly path: no local LLM,
  text embedding, speaker embedding, diarization, or ASR.
- LightRAG/Postgres provide deterministic graph storage and traversal only.
  Keep model-backed LightRAG document ingestion and query endpoints disabled.
- Replicate ASR is the default. WhisperX and local pyannote/torch voice
  enrollment are prohibited from the nightly job; never silently fall back to
  either.

## Context contract

The dashboard exposes only loopback, authenticated context routes:

```text
GET  /api/context/health
POST /api/context/search
```

Return bounded, provenance-bearing items classified as `background`; never raw
transcripts or unbounded meeting history. Preserve freshness and partial-state
signals. The consumer may use context to orient retrieval, but its own QA gate
remains authoritative.

## Operations and verification

- Check capture without mutation: `uv run pipeline capture --dry-run`.
- If Drive reports unauthorized or an expired token, the operator must complete
  `uv run pipeline auth-drive` in a browser. Never inspect or print token data.
- Start local UI with `open-dashboard.ps1 -Port 8765`; verify the authenticated
  health/context route after restart.
- Run the narrowest relevant test first. For a full Python check, use
  `uv run pytest`; run `uv run ruff check .` for lint.
- Do not claim a scheduled capture succeeded solely because a task exists;
  inspect its last result and pipeline status.
- Do not represent a scheduled run as policy-compliant while it can invoke the
  legacy local voice-enrollment path. The approved replacement workflow must be
  selected and implemented first.

## Approval boundaries

Merge repair is preview-first and digest-bound. A preview creates no live
mutation. Apply only the exact approved digest, and preserve the recovery
record/minute rewrite jobs created by the pipeline.
