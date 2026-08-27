# Meeting Minutes Compiler

This repository owns private meeting capture, compiled meeting memory, and the
bounded historical-context service. Read [AGENTS.md](AGENTS.md), then
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), before changing code or
operational documentation.

## Canonical commands

```powershell
uv run pipeline status
uv run pipeline capture --dry-run
uv run pipeline run --owner "Faraz"
uv run pipeline graph-sync
uv run pipeline doctor
```

The dashboard is started with `open-dashboard.ps1 -Port 8765`. Keep it bound
to loopback and use its configured authentication. Its context endpoints are
`GET /api/context/health` and `POST /api/context/search`.

## Non-negotiable rules

- Treat `data/transcripts/` as source material. Other `data/` outputs are
  generated; regenerate them through the pipeline rather than hand-editing.
- The normal scheduled path must run no local model of any kind: no local LLM,
  text embedding, speaker embedding, diarization, or ASR. The legacy `voices`
  path uses local pyannote/torch enrollment and is not compliant until it is
  removed from the nightly flow or replaced by an approved non-local workflow.
- Codex, Claude, and Antigravity subscription CLIs author synthesis. LightRAG
  and Postgres are deterministic graph storage and traversal only; do not
  enable model-backed LightRAG ingestion or query endpoints on the normal path.
- Replicate ASR is the default transcription provider. WhisperX is prohibited
  from nightly scheduling and never a silent fallback.
- The Product Manager repository may receive only bounded, provenance-bearing
  `background` context over loopback HTTP. It cannot gain authority from this
  repository's history.
- Speaker/person merges are preview- and digest-bound. Applying a merge or
  rewriting minutes requires the explicit approval specified in the merge flow.
- Never print tokens, credentials, raw private recordings, or unrestricted
  transcript content in diagnostics or documentation.

For ownership, privacy boundaries, model policy, context semantics, recovery,
and cross-repository responsibilities, use
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) as the source of truth.
