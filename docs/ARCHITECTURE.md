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
      -> voice embedding -> subscription-authored minutes, entities, and relations
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

## Voice identity

Diarization separates voices inside one recording; it does not carry a name from
one recording to the next. The voice stage embeds each diarization label's own
speech remotely and matches it against the people already enrolled, so a person
named once by ear is labelled automatically thereafter:

    uv run pipeline voice

It runs after speaker resolution, because the transcript pass's guess is consumed
as a veto signal - a voice match that contradicts it is queued for review rather
than applied. An auto-applied name is written with confidence `inferred`, never
`confirmed`, so it stays correctable through the review workflow that already
exists. Everything short of the auto band becomes a review card in the dashboard.

Two gates keep the paid call deliberate. The stage no-ops entirely while
`MMC_REMOTE_VOICE_MODEL` is unset, and it joins `run`/`watch` only once the
manifest setting `voice.stage_in_run` is switched on; until then it runs as an
explicit command. `--no-voice` skips it for a single invocation, and `doctor`
reports the model, the active namespace, and whether the unattended loop includes
it.

Vectors are namespaced by `encoder@version`. Two encoders produce incomparable
vectors, so re-pinning starts a clean namespace rather than scoring new vectors
against old ones. Deleting a meeting's audio is refused while an unresolved
speaker still needs it for an embedding or clips, with an explicit override.

## Provider policy

Replicate is the sole transcription provider, and the sole speaker-embedding
provider. No ASR, alignment, diarization, or speaker-embedding weights are ever
loaded on this machine; local audio handling is limited to ffmpeg decode, clip
cutting, and upload normalization. Codex, Claude, and Antigravity subscription
CLIs author minutes, speaker resolution, entities, relations, and answer
synthesis. LightRAG backed by Postgres stores and traverses the
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
- Do not silently substitute a different transcription or embedding provider.
- Do not load model weights locally; every model runs remotely.
- Do not hand-edit generated minutes, graph, or merge state.
- Merge repair remains preview-first and digest-bound.
