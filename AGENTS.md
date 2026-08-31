# Agent guide — Meeting Minutes Compiler

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before work that touches the
pipeline, models, graph context, dashboard, or Product Manager integration.

## Responsibility boundary

Meeting Memory owns private meeting capture, source/derived meeting records,
speaker/person resolution, subscription-authored minutes/entities/relations,
graph publication, and the authenticated loopback context provider on port 8765.

Product Manager owns QA-gated product statements, decisions, requirements,
actions, artifacts, agents, and the PM cockpit. It may consume bounded
`background` context only; it does not read Meeting Memory files or databases.

## Processing policy

Run processing on demand with:

```powershell
uv run pipeline run --owner "Faraz"
```

The supported transcription backend is Replicate, which is also the supported
speaker-embedding backend. No model weights are loaded on this machine. Codex,
Claude, and Antigravity subscription CLIs author minutes, speaker resolution,
entities, relations, and answer synthesis. LightRAG/Postgres store and traverse
the derived graph.

The sequence is:

```text
Drive audio -> capture -> ingest -> Replicate transcription -> speakers
-> voice embedding -> subscription-authored minutes/entities/relations
-> graph-sync -> bounded ContextProvider
```

Voice embedding is off unless `MMC_REMOTE_VOICE_MODEL` is set, and it joins
`run`/`watch` only once the manifest setting `voice.stage_in_run` is on. Until
then it runs explicitly:

```powershell
uv run pipeline voice
```

A voice match is applied only when every guard in `voices.band()` passes, and it
is written with confidence `inferred` so the existing review workflow can correct
it. Everything else becomes a review card.

## Context contract

The authenticated loopback context routes are:

```text
GET  /api/context/health
POST /api/context/search
```

Return character-bounded, provenance-bearing `background` items only. Never
return raw transcripts or convert background into a decision, commitment, owner,
speaker, date, deadline, quotation, or QA-approved Product Manager fact.

## Safety and verification

- Preserve unrelated changes and never expose tokens, audio, raw transcripts, or
  database contents.
- Treat `data/transcripts/` as source material; regenerate other pipeline
  output rather than hand-editing it.
- Check capture without mutation: `uv run pipeline capture --dry-run`.
- If Drive authorization expires, run `uv run pipeline auth-drive` and
  complete browser sign-in.
- Restart the loopback dashboard after dashboard/context changes and verify an
  authenticated context route.
- Run narrow tests first; `uv run pytest` and `uv run ruff check .` are the
  broad Python checks.
- Merge repair stays preview-first and digest-bound. Apply only the explicitly
  approved digest.
