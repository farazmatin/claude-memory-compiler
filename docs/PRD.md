# Product requirements

## Product

Meeting Minutes Compiler is a private, single-user system that turns approved
meeting recordings into attributable minutes and bounded historical context.

## Goals

| Goal | Measure |
| --- | --- |
| Structured meeting memory | Approved recordings produce reviewable minutes, entities, and relations. |
| Source-aware background | Every context item carries generated-source provenance. |
| Private operation | Source records and graph storage remain loopback-bound. |
| Product-boundary clarity | Product Manager receives background only and retains QA authority. |
| Subscription-authored meaning | Codex, Claude, then Antigravity produce authored synthesis. |

## Required flow

    capture -> ingest -> Replicate transcription -> speaker resolution
    -> minutes/entities/relations -> graph-sync -> ContextProvider

Processing is started on demand. Replicate is the required transcription
provider. LightRAG/Postgres persist and traverse the derived graph.

## Non-functional requirements

- Source recordings and raw transcripts remain private.
- No context endpoint returns unbounded history or raw transcripts.
- Every generated artifact can be regenerated from retained source material.
- A failed stage reports non-zero to the initiating operator.
- Speaker attribution remains reviewable and uncertain identity is preserved.

## Authority

Meeting Memory does not establish product facts. Product Manager's QA-gated
catalogue remains authoritative for decisions, commitments, owners, speakers,
dates, deadlines, quotations, and other product statements.

See [ARCHITECTURE.md](ARCHITECTURE.md) for implementation responsibilities.
