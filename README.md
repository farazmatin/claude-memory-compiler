# Meeting Minutes Compiler

Private meeting audio becomes reviewable minutes, a derived entity/relation graph,
and bounded historical context for Product Manager.

## Architecture

    Drive audio -> capture -> ingest -> Replicate transcription -> speakers
    -> voice embedding -> subscription-authored minutes/entities/relations
    -> graph-sync -> bounded ContextProvider

Meeting Memory owns recordings, transcripts, speaker/person resolution, minutes,
entities, relations, graph publication, and the authenticated loopback dashboard.
Product Manager owns QA-gated product statements, decisions, requirements,
actions, artifacts, agents, and the PM cockpit.

The repositories use only the loopback context API:

    GET  /api/context/health
    POST /api/context/search

Responses are character-bounded, provenance-bearing background. They orient
retrieval but cannot establish or override Product Manager facts. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full contract.

## Providers

- Replicate is the required transcription provider, and the optional
  speaker-embedding provider. No model weights run on this machine.
- Codex, Claude, then Antigravity subscription CLIs author minutes, speaker
  resolution, entities, relations, and answer synthesis.
- LightRAG/Postgres store and traverse the derived graph.

## Quick start

    git clone https://github.com/farazmatin/claude-memory-compiler
    cd claude-memory-compiler
    uv sync --extra dev
    docker compose up -d
    uv run pipeline init
    uv run pipeline doctor

Configure REPLICATE_API_TOKEN in .env before transcription. Keep tokens outside
commits and do not print them.

Process recordings on demand:

    uv run pipeline capture --dry-run
    uv run pipeline watch --owner "Faraz"
    uv run pipeline status

Use capture --dry-run to check Drive authorization without modifying local files.
If it reports authorization failure, run:

    uv run pipeline auth-drive

## Commands

    pipeline init
    pipeline status
    pipeline auth-drive
    pipeline capture --dry-run
    pipeline capture
    pipeline ingest
    pipeline transcribe
    pipeline speakers --owner "Name"
    pipeline voice
    pipeline minutes
    pipeline graph-sync
    pipeline watch --owner "Name"
    pipeline doctor
    pipeline dashboard --open
    pipeline query "question"
    pipeline people
    pipeline entities
    pipeline backup --to /path/to/backup

## Dashboard

Start the local authenticated dashboard:

    .\scripts\open-dashboard.ps1 -Port 8765

It is loopback-bound and presents meeting records, compiled minutes, original
Drive links, speaker review, and bounded context search. Use its review actions
instead of editing generated records directly.

## Data safety

- data/transcripts/ is source material; do not hand-edit it.
- Other generated artifacts are rebuilt through the pipeline.
- Preserve Drive originals and provenance.
- Merge repair is preview-first and digest-bound.
- Keep recording, transcript, token, and database details out of logs and docs.

For phone capture, see [USER_GUIDE.md](USER_GUIDE.md). For speaker correction,
see [SPEAKER_GUIDE.md](SPEAKER_GUIDE.md).
