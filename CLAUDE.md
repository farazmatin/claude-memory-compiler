# CLAUDE.md

Guidance for Claude Code working in this repository.

> This file previously described a different project entirely — an Obsidian-wiki
> compiler with `hooks/`, `scripts/compile.py` and `knowledge/`, none of which
> exist here. It was left over from an earlier project in the same directory tree
> and every command in it was wrong. Kept short on purpose: it loads into every
> session, so depth belongs in AGENTS.md.

## What this is

The **Meeting Minutes Compiler**. Meeting audio in, a queryable knowledge base out:

```
audio/        raw recordings (immutable)
transcripts/  verbatim, retained forever  -> source code
LLM           extracts structure + rationale -> compiler
minutes/      the indexed corpus          -> object code
LightRAG      knowledge graph + vectors   -> linked binary
```

The load-bearing property: **the compile step is repeatable.** Transcription is the
only irreversible cost and happens once per recording. A better
`templates/minutes.md` can rebuild every minutes file from retained transcripts.

**Read [AGENTS.md](AGENTS.md) for the full technical reference** — it is thorough and
current. Also `docs/PRD.md` (scope), `docs/ARCHITECTURE.md` (what was rejected and
why), `docs/REVIEW.md`, `docs/VOICE_LABELLING_PLAN.md`.

## Commands

```bash
uv run pipeline init          # dirs + manifest
uv run pipeline doctor        # preflight; run this first when something is wrong
uv run pipeline capture       # pull new audio from the private Drive folder
uv run pipeline ingest        # discover + content-hash dedup
uv run pipeline transcribe    # ASR + align + diarize (the expensive stage)
uv run pipeline speakers      # SPEAKER_xx -> names
uv run pipeline minutes       # compile structured minutes
uv run pipeline graph-sync    # publish subscription-authored graph records
uv run pipeline graph-sync    # author the graph from the manifest's entities
uv run pipeline run           # every pending stage, in order
uv run pipeline dashboard     # local web UI on 127.0.0.1:8765
uv run pipeline query "..."   # ask the knowledge base
uv run pipeline status        # where everything is, plus stage timings
uv run pipeline retry         # requeue failed meetings
uv run pipeline people        # registry: --add / --merge
uv run pipeline entities      # extracted entities and graph health
uv run pipeline backup        # snapshot the manifest and artifacts
uv run pipeline auth-drive    # authorize the private Drive collector

uv run pytest -q              # full suite
uvx ruff check pipeline/ tests/   # ruff is NOT a declared dependency; use uvx
```

Stages are resumable and each claims meetings at one status. A crash costs minutes,
not the hours of transcription behind it.

## Non-obvious things that will bite you

- **The manifest is `db/manifest.db`**, set by `config.DB_PATH`. Anything that opens
  a bare `./manifest.db` silently reads an empty database and every count comes back
  zero. This has produced false conclusions more than once.
- **ASR runs on Replicate** by default (`replicate_asr.py`), because local CPU
  transcription costs 30–50 minutes per meeting. Do not spend Replicate credits
  casually; the owner has asked that they be conserved.
- **The local stack IS installed**, contrary to what this file said earlier:
  `whisperx` 3.8.6, `pyannote.audio` 4.0.7 and `torch` 2.8.0+cpu all import, and the
  `pyannote/wespeaker-voxceleb-resnet34-LM` weights are already cached. Verify by
  running it, not by trusting a doc.
- **`torchcodec` cannot load its bundled FFmpeg DLLs here**, so pyannote's default
  file-path decoding fails. pyannote's own warning names the fix and
  `pipeline/enroll.py` uses it: decode with `whisperx.load_audio` (which shells out
  to the real ffmpeg on PATH) and hand pyannote
  `{"waveform": tensor, "sample_rate": sr}` in memory.
- **Audio is deleted right after transcription.** `cmd_transcribe` calls
  `capture.cleanup_transcribed_audio()` in the same loop iteration, which removes
  local audio for any meeting with a `drive_sources` row — i.e. every Drive-captured
  meeting. Anything that needs the waveform (voice embeddings, snippets) must run
  BEFORE that call or the audio is already gone. Only 7 of 40 meetings still have
  audio on disk, and they do only because they lack a `drive_sources` row.
- **LightRAG is storage and graph traversal only.** The subscription provider
  chain authors minutes, entities, and relations; `pipeline graph-sync` publishes
  those exact graph records. Model-backed document ingestion and `/query` are not
  part of the active build. `doctor` checks that the graph is non-empty; a
  reachable service is not the same as working retrieval.
- **Retrieval uses direct graph traversal.** `graph_sync.retrieve_context()` does
  deterministic label matching plus `GET /graphs`, and synthesis runs through the
  subscription provider chain. No local LLM or text-embedding model is used.
- **The LLM chain is subscription-backed CLIs**, tried in the enforced order
  Codex, Claude, then Antigravity (`config.LLM_PROVIDER_ORDER`). The standalone
  `gemini` CLI has no credentials here. Note
  the two have *different model namespaces* — `gemini-3.7-flash-*` exists in
  Antigravity and is unknown to `gemini`.
- **Antigravity must be driven via `--input-format stream-json`.** `agy --print`
  takes the prompt as an argument and rejects stdin, and a transcript is far past
  the ~32 KB Windows argv limit. Every provider is fed on stdin for this reason.
- **Filenames arrive with underscores.** Google Drive substitutes `_` for every
  space, and `_` is a word character so `\b` never fires beside it. `ingest.py` uses
  `[\s_]` plus explicit alphanumeric lookarounds; a naive `\b` fix does not work.
  Also `at 11-31 a.m.` means 11:31 — `:` is illegal in filenames.
- **`templates/minutes.md` is the compiler specification.** Editing it changes every
  future compile. A semantic change should bump `TEMPLATE_VERSION` in `config.py`,
  which marks existing minutes stale — but recompiling is ~7.8 min per meeting, so
  confirm with the owner before bumping.
- **The dashboard has no authentication** and exposes `DELETE /api/meetings/{id}`.
  It is safe only because it binds to `127.0.0.1`. Do not expose it.
- **`pipeline doctor` verifies the environment, not output quality.** It cannot tell
  you the minutes are any good.

## Conventions

- Comments explain **why**, not what. Match the surrounding density — this codebase
  documents the reasoning behind non-obvious choices, and that is deliberate.
- Tests: `tests/test_*.py` are unit tests; `tests/test_e2e.py` drives the real CLI
  over a throwaway tree with real ffmpeg and is marked `e2e`. `tests/e2e_harness.py`
  fakes only what needs a GPU, a subscription, or a docker stack.
- `SESSION_STATE.md` carries hand-off notes from recent work, including corrections
  to earlier wrong conclusions. Read it before re-deriving anything.
