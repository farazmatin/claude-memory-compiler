# Meeting Minutes Compiler

This repository owns private meeting capture, compiled meeting memory, and a
bounded historical-context service. Read [AGENTS.md](AGENTS.md), then
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), before changing code or operations.

## Canonical commands

    uv run pipeline status
    uv run pipeline capture --dry-run
    uv run pipeline run --owner "Faraz"
    uv run pipeline graph-sync
    uv run pipeline doctor

Start the authenticated loopback dashboard with open-dashboard.ps1 -Port 8765.
Its context routes are GET /api/context/health and POST /api/context/search.

## Rules

- Processing is started on demand; do not add automatic batch scheduling.
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
