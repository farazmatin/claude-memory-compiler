# Meeting Minutes Compiler

**Meeting audio in. A searchable knowledge base of decisions out.**

Records of your meetings compile themselves into structured minutes, then into a
knowledge graph you can actually ask questions of — "why did we deprioritize
Atlas?", "what has this customer told us?", "when did we reverse on that?"

Built for volume: roughly five meetings a day, indefinitely.

## The idea

```
audio → transcript → minutes → knowledge graph
        (retained)   (indexed)
```

Three tiers, and the split between them is the whole design:

| Tier | What | Indexed? |
|---|---|---|
| 1 | Audio + full diarized transcript | **No** |
| 2 | Structured minutes, one per meeting | **Yes** — this is the corpus |
| 3 | Knowledge graph + vector index (LightRAG) | derived |

**Why transcripts are not indexed.** A one-hour meeting is ~10,000 spoken words
of which maybe 500 are durable signal. Speech has no headings to chunk on, and
it's dense with pronouns — "yeah, let's just do that instead" is a decision when
you hear it and meaningless as a retrieved chunk. Index that and every chunk
embeds toward the centroid of "generic meeting talk," so retrieval precision
collapses as the corpus grows. Worse, entity extraction mints a graph node for
every casual mention and buries the real structure.

**Why transcripts are still kept.** Two reasons, and the second one shapes the
whole architecture:

- *Provenance.* Every claim in the minutes cites an audio timestamp, so "did we
  actually agree to that?" is answerable.
- *Recompilation.* Minutes are a **lossy compile**. Your template will improve;
  eventually you'll want a field you didn't think of. Because transcripts are
  retained, you bump `TEMPLATE_VERSION` and rebuild years of history — with no
  transcription cost. Throw transcripts away and that history is gone forever.

## Quick start

```bash
uv sync                                  # core deps
uv sync --extra asr                      # + whisperx (heavy: torch, CUDA libs)
docker compose up -d                     # LightRAG + Ollama
docker compose exec ollama ollama pull qwen3:4b
docker compose exec ollama ollama pull mxbai-embed-large

uv run pipeline init
cp ~/recordings/*.m4a inbox/
uv run pipeline run --owner "Your Name"
uv run pipeline query "what did we decide about pricing?"
```

`export HF_TOKEN=hf_...` before the first run, or diarization is skipped — see
[Prerequisites](#prerequisites).

For automatic phone capture, follow [USER_GUIDE.md](USER_GUIDE.md). It covers
Easy Voice Recorder Pro, private Google Drive setup, the June 9, 2026 backfill
cutoff, and a real end-to-end acceptance test.

For the speaker-label workflow—including diarization, identity resolution,
recurring attendees, new guests, and manual overrides—see
[SPEAKER_GUIDE.md](SPEAKER_GUIDE.md).

## Commands

```bash
pipeline init                     # create directories and the manifest
pipeline auth-drive               # one-time private Drive authorization
pipeline capture --dry-run        # preview approved Drive recordings
pipeline capture                  # download approved Drive recordings
pipeline ingest                   # discover + dedup new audio
pipeline transcribe               # ASR + alignment + diarization  (the slow one)
pipeline speakers --owner "Name"  # resolve SPEAKER_00 → real names
pipeline minutes                  # compile structured minutes
pipeline index                    # push minutes into LightRAG
pipeline run                      # every pending stage, in order
pipeline status                   # where everything is, plus real stage timings
pipeline query "question"         # ask the knowledge base
pipeline query "..." --mode global   # for answers spanning many meetings
pipeline minutes --recompile      # rebuild after a template change, no ASR cost
pipeline retry                    # requeue whatever failed
pipeline capture --complete-backfill  # permanently disable the one-time backfill folder
```

Each stage claims meetings at one status and advances them to the next, tracked in
`db/manifest.db`. Stages are independent and resumable — a crash during minutes
compilation costs minutes, not the hours of transcription behind it.

Meetings are always processed **oldest first**, because the minutes compiler reads
earlier minutes to detect decisions that reverse previous positions. Out-of-order
compilation would compare a meeting against its own future.

## This runs on CPU, which dictates the ASR model

Per one-hour meeting, on a modern multicore CPU:

| Step | `large-v3` | `large-v3-turbo` |
|---|---|---|
| ASR | ~60–120 min | ~8–15 min |
| Diarization | ~15–30 min | ~15–30 min |
| Alignment | ~5 min | ~5 min |
| **Per meeting** | **~1.5–2.5 hrs** | **~30–50 min** |
| **× 5/day** | ~9 hrs — not viable | **~3 hrs — fits overnight** |

So the default is `large-v3-turbo`, not `large-v3`. Add ~1 hr/day for graph
indexing and the nightly batch is about **4 hours** — run it from a timer, not a
filesystem watcher, or runs will overlap:

```cron
0 1 * * *  cd /path/to/repo && uv run pipeline run --owner "Your Name" >> pipeline.log 2>&1
```

**Two things worth knowing:**

- **Measure before optimizing the model.** Transcribe one real meeting and count
  errors *only* on names, product terms, numbers, and dates — filler-word errors
  don't matter. Microphone placement swings word error rate by 10–20 points; model
  choice swings it by less than one. A cheap USB-C boundary mic will improve your
  minutes more than any model upgrade.
- **Don't backfill everything first.** A year and a half of history at ~40 min per
  meeting is roughly a week of continuous CPU. Go forward from today, then backfill
  oldest-first in small batches.

`pipeline status` prints measured per-stage timings so you can check the table
above against your actual hardware.

## Prerequisites

- **ffmpeg** on `PATH` (audio normalization and duration probing)
- **`HF_TOKEN`** — a HuggingFace read token. pyannote's diarization models are
  gated: you must also manually accept the terms for
  `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0`. Without
  this, diarization is skipped with a warning and you get transcripts with no
  speaker attribution — which means action items with no owners.
- **Docker** for LightRAG + Ollama.

Two upstream bugs are already worked around in `docker-compose.yml`: Ollama is
pinned to `0.12.11` because `0.13.x` breaks LightRAG's embeddings
([#2495](https://github.com/HKUDS/LightRAG/issues/2495)), and a dummy
`OPENAI_API_KEY` is set because the server won't boot without one even on Ollama
bindings ([#2023](https://github.com/HKUDS/LightRAG/issues/2023)).

## Tuning

`glossary.md` is the highest-leverage file in the repo. Terms in it bias the ASR
decoder, and **order is priority order** — Whisper's prompt is capped at ~224
tokens, so the tail gets dropped. Put people and product names at the top. A
mangled product name fragments the graph: "Project Atlas" transcribed three ways
becomes three disconnected nodes, and no query finds all of it.

`templates/minutes.md` is the compiler specification. It deliberately targets
600–1200 words rather than a tidy summary, because summaries drop *rationale* and
rationale is what answers "why". Change it semantically → bump `TEMPLATE_VERSION`
in `pipeline/config.py` → `pipeline minutes --recompile`.

`speaker-overrides.yaml` (optional) is ground truth for speaker names and beats
every inference:

```yaml
default:
  SPEAKER_00: Faraz
a1b2c3d4e5f6:      # meeting id prefix
  SPEAKER_01: Ali
```

Everything else is environment variables — see `pipeline/config.py`.

## Design notes

**Dedup is content-hash based.** Recorder backups produce byte-identical
duplicates under different names. Without dedup each one costs a 40-minute
transcription and injects a second copy of the same minutes, doubling graph
entities.

**Inbox files are copied, never moved.** The inbox is expected to be a
cloud-synced folder; deleting from it would propagate upstream and destroy the
original recording.

**Unresolved speakers stay unresolved.** The resolver won't guess a name from a
filename alone. A visible `SPEAKER_01` is fixable; a confidently wrong name
silently assigns work to the wrong person.

**ASR sits behind a `Backend` protocol.** Swapping in a paid API or a GPU model
touches one class and nothing downstream — the designed escape hatch from the CPU
constraint.

## Not included

Getting audio off your phone. The pipeline starts from "audio file in a
directory," so that decision stays independent. Point `inbox/` at a synced folder,
an rclone mount, or anything else that produces files.

See **[AGENTS.md](AGENTS.md)** for the full technical reference.
