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
git clone https://github.com/farazmatin/claude-memory-compiler
cd claude-memory-compiler
./setup.sh
```

`setup.sh` checks prerequisites, installs dependencies, generates secrets, starts
the services, pulls the local models, and runs preflight checks. It's safe to
re-run and never overwrites an existing `.env`.

**One thing it can't do for you.** Speaker detection needs a HuggingFace token
*and* two accepted licences:

1. Put a **read** token in `.env` as `HF_TOKEN` — https://huggingface.co/settings/tokens
2. Accept both — the token alone is not enough:
   - https://hf.co/pyannote/speaker-diarization-3.1
   - https://hf.co/pyannote/segmentation-3.0

Skip this and you get transcripts with no speaker names, which means action items
with nobody assigned. `./setup.sh` reminds you at the end; `pipeline doctor`
confirms when it's right.

Then your first meeting:

```bash
cp ~/some-recording.m4a inbox/
uv run pipeline run

less minutes/*.md                              # read what it wrote
uv run pipeline query "what did we decide?"
uv run pipeline dashboard --open               # or browse it all in a UI
```

Expect ~30–50 minutes for a one-hour recording on CPU.

**New here?** [docs/USER_GUIDE.md](docs/USER_GUIDE.md) walks through setup, daily
use, and how to judge whether the output is any good.

<details>
<summary>Manual setup, if you'd rather not run a script</summary>

```bash
cp .env.example .env      # then fill in MMC_LIGHTRAG_API_KEY, POSTGRES_PASSWORD, HF_TOKEN
uv sync --extra asr
docker compose up -d
docker compose exec ollama ollama pull qwen3:4b
docker compose exec ollama ollama pull mxbai-embed-large
uv run pipeline init
uv run pipeline doctor
```

</details>

## Which model does what

Three LLM jobs, and they cannot all use the same provider:

| Job | Provider | Why |
|---|---|---|
| Minutes compilation | Gemini Flash → Codex → Claude | Subscription-backed, tried in order, falls through on failure |
| Speaker resolution | same chain | ditto |
| Graph & entity extraction | **local Ollama** | LightRAG needs an HTTP endpoint; no CLI subscription can serve one |

Set the order with `MMC_LLM_PROVIDERS=gemini,codex,claude`. A quota limit or CLI
hiccup on the preferred provider falls through to the next rather than stalling a
batch that already paid for transcription.

**The constraint worth understanding, and how it's handled.** Your subscriptions
can't reach LightRAG's extraction step — it needs an HTTP endpoint, and no
subscription offers embeddings at all. So two jobs were moved to where the
subscription *does* reach:

- **The compiler emits the graph.** Minutes include explicit `Entities` and
  `Relations` sections, written by the frontier model, stored in the manifest, and
  handed to the index pre-stated. A small model reads an explicit list reliably and
  discovers the same facts from prose unreliably.
- **Synthesis is split from retrieval.** LightRAG retrieves; the subscription chain
  writes the answer. `--local` keeps LightRAG's own generation for comparison, and
  it's the automatic fallback when no provider is reachable.

What remains local is graph traversal. That's narrowed, not eliminated.

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
pipeline doctor                   # preflight the environment (run this first)
pipeline ingest                   # discover + dedup new audio
pipeline transcribe               # ASR + alignment + diarization  (the slow one)
pipeline speakers --owner "Name"  # resolve SPEAKER_00 → real names
pipeline minutes                  # compile structured minutes
pipeline index                    # push minutes into LightRAG
pipeline run                      # every pending stage, in order
pipeline status                   # where everything is, plus real stage timings
pipeline dashboard --open         # local, read-only meeting library and RAG search
pipeline query "question"         # ask the knowledge base
pipeline query "..." --mode global   # for answers spanning many meetings
pipeline query "..." --timing     # retrieval vs synthesis time
pipeline people                   # the people registry
pipeline people --merge Mike Michael   # fold a duplicate, rewriting history
pipeline entities                 # most-mentioned entities (graph health check)
pipeline minutes --recompile      # rebuild after a template change, no ASR cost
pipeline backup --to /mnt/backup  # snapshot everything irreplaceable
pipeline retry                    # requeue whatever failed
pipeline capture --complete-backfill  # permanently disable the one-time backfill folder
```

`pipeline run` exits **non-zero if any stage failed**, so a nightly cron reports a
broken batch instead of silently succeeding.

## Meeting Memory dashboard

After the local index is running, open the operator view with:

```powershell
uv run pipeline dashboard --open
```

It listens only on `127.0.0.1:8765` by default and never changes Drive, the
manifest, transcripts, minutes, or speaker records. It provides the meeting
library, compiled minutes, a link back to the original private Drive audio,
speaker-review signals, and the same evidence-backed RAG search as `pipeline
query`. Press `Ctrl+C` in its terminal to stop it. Use `--port 8766` if the
default port is occupied.

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

## Preflight

```bash
pipeline doctor
```

18 checks: ffmpeg, whisperx, the ASR model against your device, **HF token plus
actual gated-model reachability**, every provider in the chain, LightRAG health and
whether its storage is file-based, Ollama models, directories, disk headroom,
manifest state, glossary depth. Each failure prints its fix.

The gated-model check earns its place: a HuggingFace token proves nothing about
licence acceptance, which is the part people miss, and missing diarization costs
every action item its owner while printing only a warning mid-batch.

**What it does not tell you:** whether the output is good. That needs one real
meeting, read against the audio.

## Prerequisites

- **ffmpeg** on `PATH` (audio normalization and duration probing)
- **`HF_TOKEN`** — a HuggingFace read token. pyannote's diarization models are
  gated: you must also manually accept the terms for
  `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0`. Without
  this, diarization is skipped with a warning and you get transcripts with no
  speaker attribution — which means action items with no owners.
- **`MMC_LIGHTRAG_API_KEY`** — see below.
- **Docker** for LightRAG + Ollama.
- At least one of **`gemini`**, **`codex`**, or the **Claude Agent SDK** reachable.

## Security

Both services bind to **loopback only**. This index holds a searchable record of
every decision, customer conversation, and internal disagreement in the corpus;
publishing it on `0.0.0.0` would put that on the network unauthenticated. To reach
the WebUI from another machine, tunnel:

```bash
ssh -L 9621:127.0.0.1:9621 user@server
```

`MMC_LIGHTRAG_API_KEY` is required and enforced — `docker compose` fails fast if
it's unset rather than starting something open.

## Backup

The corpus is not in git, and it is not reconstructible from anything else.

```bash
pipeline backup --to /mnt/backup            # everything
pipeline backup --to /mnt/backup --no-audio # skip the bulkiest tier
```

Uses SQLite's online backup API rather than a file copy, because copying a live
database can capture a torn page and produce a snapshot that restores as corrupt.
The snapshot is integrity-checked, and a `BACKUP_INFO.txt` with restore steps is
written alongside it. The LightRAG index is deliberately **not** backed up — it is
derived from `minutes/` and rebuilt with `pipeline index`.

Add it to the nightly timer after `run`.

## When the batch fails

`pipeline run` exits non-zero, but on a headless server nothing reads cron's mail.
Set `MMC_ALERT_COMMAND` and a failure pushes a summary somewhere you'll see it:

```bash
MMC_ALERT_COMMAND=curl -s -d @- https://ntfy.sh/my-topic
MMC_ALERT_COMMAND=mail -s "{subject}" me@example.com
```

The summary arrives on stdin with `{subject}` substituted, names the failed stages,
and points at `status` / `doctor` / `retry`. A command rather than built-in email
support because whatever your server already has beats a second notification stack.

## Tests

```bash
uv sync --extra dev
sudo apt-get install ffmpeg   # functional tests generate real audio

uv run pytest                 # everything
uv run pytest -m "not e2e"    # unit only, <1s
uv run pytest -m e2e          # functional only
```

**187 tests in two layers**, both gating CI:

- **165 unit tests** — functions in isolation, no network, under a second.
- **22 functional tests** — drive the real CLI end to end over a throwaway tree
  with real audio, real ffmpeg, a real SQLite manifest and real HTTP to a
  LightRAG-shaped server. Only the ASR model, the LLM and LightRAG's internals are
  substituted, and the LLM fake is an actual executable so the subprocess/stdin
  provider path is genuinely exercised.

The functional layer earns its keep: it caught two bugs the unit suite could not
see, including one where the nightly batch reported success after a total
transcription failure. Details in
[docs/TESTING.md](docs/TESTING.md).

**Still not covered:** real Whisper, real pyannote, real LightRAG, real Postgres,
and the actual `gemini`/`codex` binaries. The functional tests prove the app's own
wiring; they cannot prove the transcript is accurate.

## Documentation

| Document | For |
|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | **Start here** — setup, daily use, troubleshooting |
| [docs/PRD.md](docs/PRD.md) | Problem, goals, non-goals, constraints, success criteria |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design decisions with rationale and what was rejected |
| [docs/REVIEW.md](docs/REVIEW.md) | Adversarial review: 20 findings, all addressed |
| [docs/TESTING.md](docs/TESTING.md) | Test strategy and what each layer covers |
| [AGENTS.md](AGENTS.md) | Operational reference — env vars, stage internals, gotchas |

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

**Names are normalized, not trusted.** A people registry maps aliases to one
canonical spelling, because asking a model to spell a name the same way it did four
months ago isn't a strategy — and each variant becomes a separate graph node. Curate
it with `pipeline people`.

**ASR sits behind a `Backend` protocol.** Swapping in a paid API or a GPU model
touches one class and nothing downstream — the designed escape hatch from the CPU
constraint.

**Re-indexing replaces, never appends.** Each meeting's LightRAG document id is
recorded in the manifest and the old version is deleted before a recompiled one is
inserted. If the delete fails, the insert is abandoned rather than leaving two
contradictory copies of the same meeting in the graph.

**The corpus outlives its index.** `minutes/` is portable markdown. If LightRAG
stalls, slows down, or gets outgrown, the corpus re-indexes into anything else —
your data is not hostage to it.

## Not included

Getting audio off your phone. The pipeline starts from "audio file in a
directory," so that decision stays independent. Point `inbox/` at a synced folder,
an rclone mount, or anything else that produces files.

**Known open issues** are tracked honestly in
[docs/REVIEW.md](docs/REVIEW.md#open) — including the one that matters most: no
subscription can serve LightRAG's extraction step, so graph quality is capped by a
small local model regardless of what you pay for.
