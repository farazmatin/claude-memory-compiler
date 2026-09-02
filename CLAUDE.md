# Meeting Minutes Compiler

This repository owns private meeting capture, compiled meeting memory, and a
bounded historical-context service. Read [AGENTS.md](AGENTS.md), then
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), before changing code or operations.

## Canonical commands

    uv run pipeline status
    uv run pipeline capture --dry-run
    uv run pipeline run --owner "Faraz"
    uv run pipeline graph-sync
    uv run pipeline chunk-index
    uv run pipeline dense-index
    uv run pipeline doctor

Start the authenticated loopback dashboard with open-dashboard.ps1 -Port 8765.
Its context routes are GET /api/context/health and POST /api/context/search.

## Rules

- Processing is started on demand; do not add automatic batch scheduling. The
  operator starts it from the dashboard's Sync & Process Recordings button, or
  with `pipeline run`. Only one run at a time: the dashboard's guard is
  in-process, so it cannot see a run started from a shell or by the watcher.
- The continuous Drive watcher (scripts\install-drive-watcher.ps1, log
  logs\drive-watcher.log) is the only sanctioned always-on path and is not
  installed by default. It reacts to a newly staged recording; it never sweeps
  on a timer.
- Replicate is the only transcription backend.
- Codex, Claude, and Antigravity subscription CLIs author synthesis.
- LightRAG/Postgres provide deterministic graph storage and traversal only.
- Product Manager receives only bounded, provenance-bearing background context
  over loopback HTTP. Its QA-gated catalogue remains authoritative.
- Treat data/transcripts/ as source material and regenerate other pipeline
  outputs instead of editing them manually.
- Speaker/person merge application is preview- and digest-bound.
- Never print tokens, private recordings, raw transcripts, or unrestricted
  meeting history.
