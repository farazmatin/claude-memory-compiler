# Current architecture and repository boundary

This document is the current source of truth for Meeting Memory. Historical
plans and reviews may describe superseded experiments; they do not override this
document, runtime configuration, or the executable pipeline.

## Purpose

Meeting Memory turns private meeting recordings into reviewable, attributable
meeting memory. It keeps source records private, makes speaker correction
possible, and offers narrowly bounded historical context to Product Manager.
It does not decide product truth.

## Ownership

| Concern | Meeting Memory (`claude-memory-compiler`) | Product Manager |
| --- | --- | --- |
| Source records | Captures Drive audio; stores transcripts and derived meeting artifacts. | Does not import, mount, or query these files/databases. |
| Meeting interpretation | Maintains speakers/persons; authors minutes, entities, and relations. | May use supplied background only to orient retrieval. |
| Historical graph | Publishes and traverses the derived meeting graph. | Consumes a bounded loopback HTTP contract only. |
| Product truth | Never certifies product statements. | Owns QA-gated statements, decisions, requirements, actions, artifacts, and agents. |
| User interface | Authenticated loopback dashboard and speaker-resolution work. | PM cockpit and its own QA/review controls. |

The repositories are intentionally separate. They communicate through a small,
read-only loopback context API, not shared directories, direct database access,
environment files, or cross-repository writes.

## Normal data flow

```text
Google Drive audio
  -> capture
  -> ingest
  -> transcription
  -> speaker and voice resolution
  -> subscription-authored minutes, entities, and relations
  -> graph-sync
  -> bounded ContextProvider (loopback)
  -> Product Manager background retrieval
```

`pipeline run --owner "Faraz"` executes the normal sequence. `graph-sync`
publishes the derived meeting graph after minutes/entities/relations exist. The
old normal-path `index` stage is retired; do not reintroduce it as a synonym for
graph publication.

## Model and graph policy

### What authors meaning

Codex, Claude, and Antigravity subscription CLIs are the permitted providers
for authored synthesis: minutes, speaker-resolution assistance, entities,
relations, and answer synthesis. Provider order and credentials are controlled
by runtime configuration; documentation must not embed credentials.

Replicate ASR is the default transcription provider. No local ASR provider is
permitted in the scheduled nightly job; WhisperX is not an automatic fallback.

### What does not run nightly

The normal capture and compilation path must not run a local model of any kind:
no LLM, text embedding, speaker embedding, diarization, or ASR. Local
generation/embedding experiments, legacy Ollama references, WhisperX, local
pyannote/torch voice enrollment, and model-backed LightRAG document
ingestion/query endpoints are not part of production operation.

The present legacy `voices` implementation loads a local pyannote/torch speaker
embedding model. It is a compliance blocker for overnight operation, not an
exception to this policy. Before the job can be described as fully compliant,
voice enrollment must be moved to an approved non-local/manual workflow or
retired; the scheduler must not silently invoke it.

### LightRAG and Postgres

LightRAG backed by Postgres is used only for deterministic storage and
traversal of subscription-authored entities and relations. It does not author
facts, retrieve raw source text for a caller, or run its own model-backed
ingestion/query workflow in the normal path.

This makes the responsibility boundary explicit:

```text
Subscription CLI: author and synthesize meaning
LightRAG/Postgres: store and traverse approved derived graph structure
ContextProvider: bound and classify historical context
Product Manager: retrieve, QA, and establish product authority
```

## Context API contract

The Meeting Memory dashboard, normally on loopback port `8765`, exposes:

```text
GET  /api/context/health
POST /api/context/search
```

The endpoint is authenticated and loopback-bound. It returns a limited number
of character-bounded `background` items with source provenance, not raw
transcripts or unrestricted meeting archives. Results include freshness and
availability signals so a consumer can distinguish `available`, `partial`,
`stale`, and `unavailable` context.

Product Manager must treat every response as non-authoritative background.
Meeting context cannot create or override any of the following: a decision,
commitment, owner, speaker, date, deadline, quotation, or QA-approved product
statement. Its QA-gated catalogue remains authoritative, including when the
context API is unavailable.

## Privacy and data integrity

- `data/transcripts/` contains source transcript material. Do not hand-edit it.
- Other pipeline artifacts under `data/` are generated; regenerate them through
  the pipeline rather than editing them manually.
- Keep services loopback-bound. Never place recordings, transcripts, tokens, or
  credentials in logs, docs, commits, or context responses.
- Preserve provenance from a context item to its generated source path.
- Treat speaker/person resolution as a reviewable correction workflow, not an
  excuse to fabricate identity or attribution.

## Operational responsibilities

### Capture

The scheduled job runs the normal pipeline. Its existence is not evidence of a
successful capture: inspect task result and `uv run pipeline status`.

Use the non-mutating check below when diagnosing capture:

```powershell
uv run pipeline capture --dry-run
```

If Drive reports an unauthorized or expired token, a human must complete:

```powershell
uv run pipeline auth-drive
```

This browser OAuth action is intentionally not automated and token contents
must never be printed. Scheduler power/interruption policy is an operational
setting to inspect separately from OAuth health.

### Speaker and person merges

Merge repair is preview-first and digest-bound. A preview changes nothing.
Apply only the exact digest explicitly approved by the user; preserve the
pipeline's recovery record and minute-rewrite jobs. The isolated
`codex/merge-tightening` worktree remains separate until deliberately reviewed
and merged.

### Dashboard and context verification

After changing dashboard or context code, restart the loopback dashboard and
verify the authenticated route. Validate payload classification, bounds,
provenance, and freshness—not merely that an HTTP route exists.

## Commands

```powershell
uv run pipeline status
uv run pipeline capture --dry-run
uv run pipeline run --owner "Faraz"
uv run pipeline graph-sync
uv run pipeline doctor
open-dashboard.ps1 -Port 8765
```

Run the narrowest relevant tests first. `uv run pytest` and
`uv run ruff check .` are the broad Python checks when the changed area warrants
them.

## Change rules

- Keep the normal pipeline free of local model dependencies of every kind.
- Keep all callers behind the bounded ContextProvider seam; do not add a direct
  Product Manager read of Meeting Memory files or Postgres.
- Do not silently degrade to another model, transcript source, or ASR provider.
- Do not hand-edit generated minutes, graph, or merge state.
- Preserve unrelated worktree changes and do not merge isolated work solely to
  make documentation appear current.
