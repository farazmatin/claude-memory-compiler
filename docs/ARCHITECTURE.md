# Current architecture and repository boundary

This is the source of truth for Meeting Memory's active architecture.

## Purpose

Meeting Memory turns private meeting recordings into reviewable, attributable
meeting memory and supplies bounded historical background to Product Manager.
It does not establish product truth.

## Ownership

| Concern | Meeting Memory | Product Manager |
| --- | --- | --- |
| Source records | Captures Drive audio and stores transcripts and derived meeting artifacts. | Does not import, mount, or query these files/databases. |
| Meeting interpretation | Maintains speakers/persons and authors minutes, entities, and relations. | Uses supplied background only to orient retrieval. |
| Historical graph | Publishes and traverses the derived graph. | Consumes a bounded loopback HTTP contract only. |
| Product truth | Never certifies product statements. | Owns QA-gated statements, decisions, requirements, actions, artifacts, and agents. |

The repositories communicate through a small, read-only loopback context API,
not shared directories, direct database access, environment files, or
cross-repository writes.

## On-demand flow

    Google Drive audio
      -> capture -> ingest -> Replicate transcription -> speaker resolution
      -> subscription-authored minutes, entities, and relations
      -> graph-sync -> bounded ContextProvider -> Product Manager background

Run the flow when requested with:

    uv run pipeline run --owner "Faraz"

## Provider policy

Replicate is the sole transcription provider. Codex, Claude, and Antigravity
subscription CLIs author minutes, speaker resolution, entities, relations, and
answer synthesis. LightRAG backed by Postgres stores and traverses the
subscription-authored graph; it does not author facts.

## Context contract

The authenticated loopback dashboard, normally on port 8765, exposes:

    GET  /api/context/health
    POST /api/context/search

It returns a limited number of character-bounded background items with source
provenance, freshness, and availability signals. It never returns raw
transcripts or unrestricted meeting history.

Product Manager treats every response as non-authoritative background. Meeting
context cannot create or override a decision, commitment, owner, speaker, date,
deadline, quotation, or QA-approved product statement. Product Manager's
QA-gated catalogue remains authoritative when context is unavailable.

## Privacy and integrity

- data/transcripts/ contains source material; do not hand-edit it.
- Other data artifacts are generated; regenerate them through the pipeline.
- Keep services loopback-bound and do not place recordings, transcripts, tokens,
  or credentials in logs, docs, commits, or context responses.
- Preserve source provenance for every context item.
- Treat speaker/person resolution as a reviewable correction workflow.

## Operations

Check Drive capture without mutation:

    uv run pipeline capture --dry-run

If authorization has expired, a human completes:

    uv run pipeline auth-drive

Restart the dashboard after dashboard/context changes and verify its authenticated
context route. Run the narrowest relevant tests first; use uv run pytest and
uv run ruff check . for broad Python validation.

## Change rules

- Keep callers behind the bounded ContextProvider seam.
- Do not add direct Product Manager reads of Meeting Memory files or Postgres.
- Do not silently substitute a different transcription provider.
- Do not hand-edit generated minutes, graph, or merge state.
- Merge repair remains preview-first and digest-bound.
