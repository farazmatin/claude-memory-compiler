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

## Continuous Drive flow

    Google Drive audio
      -> capture -> ingest -> Replicate transcription -> speaker resolution
      -> subscription-authored minutes, entities, and relations
      -> graph-sync -> bounded ContextProvider -> Product Manager background

The watcher polls the approved private Drive folder every 60 seconds and starts
the flow only after it captures a newly-arrived recording. It is a single-flight
continuous process, not a nightly batch. Replicate is always the transcription
provider. Existing pending recordings are processed only with an explicit
catch-up request, so starting the watcher never silently starts old paid work.

Start it at sign-in:

    uv run pipeline watch --owner "Faraz"

For a one-time operational catch-up (including the current queue):

    uv run pipeline watch --owner "Faraz" --catch-up

## Provider policy

Replicate is the sole transcription provider. Codex, Claude, and Antigravity
subscription CLIs author minutes, speaker resolution, entities, relations, and
answer synthesis. LightRAG backed by Postgres stores and traverses the
subscription-authored graph; it does not author facts.

## Voice namespace

Voice vectors are namespaced by the encoder that produced them, because vectors
from different encoders are not comparable. Every voice read and write takes that
namespace as a required parameter, resolved once by `voices.active_namespace()`
from the manifest setting `voice.active_namespace`, falling back to
`config.VOICE_VECTOR_NAMESPACE`.

Nothing may default it per call site. A namespace that no stored row uses does
not quarantine the corpus, it silently empties it: every namespaced read returns
nothing, `cluster_pending` rebuilds the review queue as empty, and no error is
raised anywhere. `pipeline doctor` fails when the active namespace is not one the
stored vectors actually use.

## Voice vectors

`pipeline/voice_embed.py` is the only module that talks to an embedding
provider. It plans every label first, makes ONE provider call per meeting, then
persists - so a dry run is pure and a provider failure leaves a meeting either
fully embedded or untouched. `voices.py` consumes the vectors and never produces
them.

Audio is fetched rather than required. Transcription deletes the local copy of a
Drive-backed recording, but Drive holds the original and `capture.rehydrate_audio`
restores it when the checksum still matches, so the stage is not limited to
meetings that happen to have a local file.

The stage no-ops when `MMC_REMOTE_VOICE_MODEL` is unset, and `pipeline doctor`
warns when it is - without a producer, new meetings never reach voice review at
all while the queue still looks healthy.

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
