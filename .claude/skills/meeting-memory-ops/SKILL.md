---
name: meeting-memory-ops
description: Operations, diagnostics, speaker resolution, and knowledge-graph maintenance for the Meeting Memory Compiler (claude-memory-compiler). Use whenever running meeting pipeline tasks, resolving unassigned speakers, fixing audio errors, managing dashboard lifecycle, or maintaining the LightRAG knowledge base.
---

# Meeting Memory Operations (`meeting-memory-ops`)

> Standard operating procedures for running, maintaining and debugging the
> `claude-memory-compiler` pipeline.

Companion skills: **`safe-db-mutation`** before changing anything in
`db/manifest.db`, **`regression-triage`** when tests fail. See `AGENTS.md` for the
full technical reference and `SESSION_STATE.md` for recent hand-off notes,
including corrections to earlier wrong conclusions.

---

## 1. Command reference

Run everything with `uv run` from inside `claude-memory-compiler/`:

```powershell
uv run pipeline doctor              # preflight. Run this FIRST when anything is wrong.
uv run pipeline run --owner "Faraz" # every pending stage, in order
uv run pipeline dashboard           # local UI on 127.0.0.1:8765
uv run pipeline graph-sync          # author the graph from the manifest's entities
uv run pipeline voices              # embed voiceprints, then rematch and cluster
uv run pipeline status              # queue state, stage timings, recent stage failures
uv run pytest -q                    # full suite (~2 min)
uvx ruff check pipeline/ tests/     # ruff is NOT a declared dependency - use uvx
```

**Commands that cost real money or hours — never run casually:**

| Command | Cost |
|---|---|
| `pipeline transcribe` | Replicate GPU credits. The owner has asked these be conserved. |
| `pipeline minutes --recompile` | ~7.8 min per meeting; ~6 h across the corpus. Explicitly declined. |
| `pipeline speakers --all` | Re-resolves every label in every meeting with any unresolved label. |

---

## 2. Architecture rules

### A. Intelligence runs on subscription CLIs, in priority order

`config.LLM_PROVIDER_ORDER` — default `antigravity,codex,claude,gemini`:

- **Antigravity (`agy`)** — the provider that actually holds a live Google session
  on this machine. Default model `gemini-3.7-flash-medium`.
- **Codex**, **Claude Agent SDK** — fallbacks, both working.
- **`gemini` CLI** — last, and currently unusable: no `~/.gemini/oauth_creds.json`.
  `doctor` reports it as unavailable and the chain skips past it.

**Two different model namespaces, and this has already caused a wrong diagnosis.**
`gemini-3.7-flash-*` exists in Antigravity and is unknown to the standalone `gemini`
CLI, whose registry tops out at `gemini-3.5-flash`. A model id is only meaningful
paired with the CLI that serves it.

**Antigravity must be driven via `--input-format stream-json`.** `agy --print` takes
the prompt as an argument and rejects stdin; a transcript is far past the ~32 KB
Windows argv limit. Every provider here is fed on stdin for that reason.

### B. Where local models legitimately run

The intent is that no local model does intelligence work. Reality is narrower than
the old "strict ban" wording, so be precise:

- **Ollama `qwen3:4b`** was LightRAG's extraction model and **failed all 43
  documents** — measured at 3.6 tok/s against a 240s timeout. It is bypassed, not
  fixed: `pipeline graph-sync` authors the graph from entities the minutes stage
  already extracted with a frontier model. Do not try to make qwen3 work; that path
  is ~18 h of CPU for a worse graph.
- **Ollama `mxbai-embed-large`** still serves embeddings, legitimately — no
  subscription CLI offers an embeddings endpoint. 2.5s per chunk once resident;
  `OLLAMA_MAX_LOADED_MODELS=2` stops qwen3 evicting it (it was 69s then 239s when it
  did).
- **pyannote**, locally, for diarization and voice embeddings. Not an LLM.

### C. Retrieval does not use LightRAG's `/query`

That endpoint runs keyword extraction through its own LLM and returns HTTP 500 after
~242s here. `graph_sync.retrieve_context()` does a label match plus a `GET /graphs`
traversal in ~5s with no LLM, and `answer.py` combines it with a keyword scan of
`minutes/`. A reachable LightRAG is **not** the same as a working one — `doctor`
checks that the graph has labels for exactly this reason.

### D. Tokens

- **`REPLICATE_API_TOKEN`** — serverless GPU ASR (`victor-upmeet/whisperx`),
  ~1–2 min per 40-minute meeting, including diarization.
- **`HF_TOKEN`** — gated pyannote weights: `speaker-diarization-3.1`,
  `segmentation-3.0`, and the `wespeaker-voxceleb-resnet34-LM` voice-embedding
  model. Not an LLM.
- **`MMC_DASHBOARD_TOKEN`** — optional on loopback; **mandatory** to bind anywhere
  else, or the dashboard refuses to start.

### E. Browser testing

Launch headless (`headless=True`). Visible browser windows freeze the terminal and
IDE in this environment.

---

## 3. Speaker resolution

This is the pipeline's main bottleneck: unresolved labels leave commitments and
decisions without owners. Two independent mechanisms:

**Per meeting, from the transcript** (`pipeline speakers`) — candidates come from
the Drive filename, the people registry and its aliases, direct-address cues
("thanks, Ruth"), and `glossary.md`'s People section. Dialogue is sampled across the
first `INTRO_WINDOW_SEC` (240s) rather than a fixed segment count.

**Across meetings, by voice** (`pipeline voices`) — pyannote embeddings per label,
matched against enrolled voiceprints. Auto-applies above `VOICE_AUTO` (0.62) with a
`VOICE_MARGIN` (0.12) gap, and only for people enrolled from at least
`VOICE_MIN_ENROLL_MEETINGS` (2) meetings. Everything else queues for review in the
dashboard.

**The embed step lives inside `cmd_transcribe`, before
`capture.cleanup_transcribed_audio()`.** That cleanup deletes the audio in the same
loop iteration for any Drive-captured meeting. Move the embed after it and the
enrollable set can never grow past whatever backlog happens to still have audio.
Do not reorder those two calls.

**Naming a speaker compounds.** It fixes that meeting's commitments and decisions,
and builds a voiceprint that auto-names future meetings. Confirmed names are
protected: `speakers.resolve` will not let a later automated pass overwrite one with
NULL. That guard is the only thing that makes `speakers --all` safe to re-run.

**Review clips**: prefer `GET /api/voices/snippet?meeting_id=&label=&index=`, which
serves the durable opus clips written at enrollment. The older
`/api/audio/snippet` cuts from live audio and silently plays nothing for any meeting
whose audio has been released — which is most of them.

---

## 4. Troubleshooting

### Corrupted container on interrupted recordings
Google Recorder files missing the MP4 `moov` atom fail normal conversion.

```powershell
ffmpeg -nostdin -y -err_detect ignore_err -ignore_length 1 -i input.m4a -ac 1 -ar 16000 output.wav
```

### "Ask AI returns nothing useful"
Check `doctor`'s **lightrag graph** line first. If it reports zero entities,
retrieval is falling back to a keyword scan of `minutes/`. Fix with
`pipeline graph-sync`, not by restarting LightRAG.

### Stale dashboards
Instances accumulate on :8765 and serve old code, which looks like a fix not
working. Kill them before concluding anything:

```bash
for pid in $(netstat -ano | grep ":8765" | grep LISTENING | awk '{print $NF}' | sort -u); do taskkill //PID $pid //F; done
```

### `torchcodec` warnings on every pyannote load
Its bundled FFmpeg DLLs do not load here. Expected and worked around: decode with
`whisperx.load_audio` and pass pyannote `{"waveform", "sample_rate"}` in memory.
Not an error to chase.

### Every table reads as empty
You opened `./manifest.db` instead of `db/manifest.db`. Use `config.DB_PATH`.

---

## 5. Deletion tiers

| Tier | Endpoint | Behaviour |
|---|---|---|
| **Release audio** | `POST /api/meetings/{id}/delete-audio` | Unlinks `audio/*.m4a`, sets `audio_path = NULL`. Minutes, speakers and graph intact. **Take voice embeddings first** — they need the waveform. |
| **Delete meeting** | `DELETE /api/meetings/{id}` | Unlinks audio, transcript, minutes; deletes the LightRAG document; cascades across `speakers`, `stage_runs`, `drive_sources`, `seen_files`, `entities`, `relations`, `commitments`, `decisions`, `open_questions`, `speaker_matches`, `voice_samples`. |

Archive files you care about **before** calling the second one — it unlinks them.
Verify orphans afterwards per `safe-db-mutation`, and re-run `graph-sync`: graph
entity nodes are keyed by name and nothing removes them implicitly.
