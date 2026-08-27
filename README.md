# Meeting Minutes Compiler

**Meeting audio in. Reviewable meeting memory and bounded historical context out.**

Records of your meetings compile themselves into structured minutes, then into a
meeting memory with speaker, entity, and relation records. It can supply
bounded historical background for questions such as "why did we deprioritize
Atlas?", "what has this customer told us?", or "when did we reverse on that?"

Built for volume: roughly five meetings a day, indefinitely.

## The idea

```
audio → transcript → minutes/entities/relations → graph context
        (retained)                       (derived)
```

Three tiers, and the split between them is the whole design:

| Tier | What | Role |
|---|---|---|
| 1 | Audio + full diarized transcript | Private source material; retained, never returned wholesale as context |
| 2 | Structured minutes plus entity/relation registers | Subscription-authored, reviewable derived meeting memory |
| 3 | LightRAG + Postgres graph | Deterministic storage and traversal of the derived graph |

**Why transcripts are not used as the retrieval corpus.** A one-hour meeting is ~10,000 spoken words
of which maybe 500 are durable signal. Speech has no headings to chunk on, and
it's dense with pronouns — "yeah, let's just do that instead" is a decision when
you hear it and meaningless as isolated context. Publishing raw transcript text
would blur the durable record with conversational filler and overwhelm the
derived entity/relation structure.

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
the graph-storage services, and runs preflight checks. It's safe to
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
uv run pipeline dashboard --open               # foreground dashboard server
# Windows: .\scripts\open-dashboard.ps1        # background server + browser
```

Expect ~30–50 minutes for a one-hour recording on CPU.

**New here?** [USER_GUIDE.md](USER_GUIDE.md) walks through setup, daily use, and
how to judge whether the output is any good. For the current model policy and
the boundary with Product Manager, read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

<details>
<summary>Manual setup, if you'd rather not run a script</summary>

```bash
cp .env.example .env      # then fill in MMC_LIGHTRAG_API_KEY, POSTGRES_PASSWORD, HF_TOKEN
uv sync --extra asr
docker compose up -d
uv run pipeline init
uv run pipeline doctor
```

</details>

## Which model does what

Three LLM jobs, and they cannot all use the same provider:

| Job | Provider | Why |
|---|---|---|
| Minutes compilation | Codex → Claude → Antigravity | Subscription-backed, tried in order, falls through on failure |
| Speaker resolution | same chain | ditto |
| Entities and relations | same chain | Emitted with the minutes, then published directly to the graph |
| Context retrieval | deterministic LightRAG traversal | Storage/traversal only; no model call |

Codex and Claude remain the first two providers even when an older environment
value lists another order. A quota limit or CLI hiccup falls through to the next
provider rather than stalling a batch that already paid for transcription.

**The constraint worth understanding, and how it's handled.** The active build
does not call LightRAG's model-backed document or query routes:

- **The compiler emits the graph.** Minutes include explicit `Entities` and
  `Relations` sections, written by the frontier model, stored in the manifest, and
  published directly to the graph.
- **Synthesis is split from retrieval.** LightRAG stores and traverses the graph;
  the subscription chain writes the answer.

The loopback services store and traverse private data; they run no local model
of any kind. Nightly operation must not load local LLM, text-embedding,
speaker-embedding, diarization, or ASR weights. The legacy local pyannote voice
enrollment path is a migration blocker until it is removed from nightly work or
replaced by an approved non-local/manual workflow.

## Boundary with Product Manager

This repository owns private meeting records and their derived meeting memory:
capture, transcripts, speaker/person resolution, minutes, entities, relations,
and graph context. Product Manager owns QA-gated product statements, decisions,
requirements, actions, artifacts, agents, and the PM cockpit.

The repositories connect only through the authenticated, loopback-bound context
API (`GET /api/context/health`, `POST /api/context/search`). Meeting Memory
returns character-bounded, provenance-bearing `background` items; Product
Manager never reads Meeting Memory files or databases directly. Background can
orient retrieval but cannot establish or override a decision, commitment, owner,
speaker, date, deadline, quotation, or QA-approved Product Manager fact. The
complete contract is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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
pipeline graph-sync               # publish subscription-authored graph records
pipeline run                      # every pending stage, in order
pipeline status                   # where everything is, plus real stage timings
pipeline dashboard --open         # local meeting library, speaker review, and bounded context search
pipeline query "question"         # deterministic graph traversal plus subscription-authored synthesis
pipeline query "..." --timing     # retrieval vs synthesis time
pipeline people                   # the people registry
pipeline people --merge Mike Michael   # preview folding a duplicate
pipeline people --merge Mike Michael --apply --expected-digest <sha256>
pipeline people --resume-merge-rewrites  # finish an interrupted hash-checked rewrite
pipeline entities                 # most-mentioned entities (graph health check)
pipeline minutes --recompile      # rebuild after a template change, no ASR cost
pipeline backup --to /mnt/backup  # snapshot everything irreplaceable
pipeline retry                    # requeue whatever failed
pipeline capture --complete-backfill  # permanently disable the one-time backfill folder
```

`pipeline run` exits **non-zero if any stage failed**, so a nightly cron reports a
broken batch instead of silently succeeding. Do not schedule a full run as
policy-compliant while it can invoke legacy local voice enrollment.

## Meeting Memory dashboard

Open the operator view with:

```powershell
.\scripts\open-dashboard.ps1
```

The launcher starts the local server in the background when it is not already
running, then opens the browser. To start it automatically after every Windows
sign-in, install the one-time user-level sign-in setup:

```powershell
.\scripts\install-dashboard-task.ps1
```

It uses a Scheduled Task when Windows permits it and otherwise installs a Startup
shortcut for the current user. Neither option requires administrator access.

It listens only on `127.0.0.1:8765` by default and uses its configured
authentication. It never uploads or deletes Drive recordings. The meeting
library provides compiled minutes, a link back to the original private Drive
audio, speaker-review actions, and bounded context search. Review actions are
explicit and pipeline-backed; they are not permission to hand-edit generated
records. Use another unused loopback port, such as `-Port 8767`, if needed.

`pipeline dashboard --open` remains available for a temporary foreground server;
press `Ctrl+C` in that terminal to stop it.

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
publication and the nightly batch is about **4 hours** — run it from a timer, not a
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

The checks cover ffmpeg, whisperx, the ASR backend, **HF token plus actual
gated-model reachability**, every subscription provider in the chain, LightRAG
graph/storage health, directories, disk headroom, manifest state, and glossary
depth. Each failure prints its fix.

The gated-model check earns its place: a HuggingFace token proves nothing about
licence acceptance, which is the part people miss, and missing diarization costs
every action item its owner while printing only a warning mid-batch.

**What it does not tell you:** whether the output is good. That needs one real
meeting, read against the audio.

## Prerequisites

- **ffmpeg** on `PATH` (audio normalization and duration probing)
- **`HF_TOKEN`** — required only by an explicitly approved manual speaker/voice
  workflow while the legacy pyannote assets remain available. It must not enable
  local pyannote/torch work from the scheduled nightly job.
- **`MMC_LIGHTRAG_API_KEY`** — see below.
- **Docker** for loopback-only LightRAG graph storage and Postgres.
- At least one of **Codex**, **Claude**, or **Antigravity** reachable.

## Security

Both services bind to **loopback only**. The derived graph holds private meeting
context: decisions, customer conversations, and internal disagreements.
Publishing it on `0.0.0.0` would put that on the network. To reach the WebUI from
another machine, tunnel:

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
written alongside it. The LightRAG graph is deliberately **not** backed up — it is
derived from manifest entities and relations and rebuilt with `pipeline graph-sync`.

`pipeline run` already includes graph publication; do not add a second legacy
indexing step to the scheduler.

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

`docker-compose.yml` deliberately points LightRAG's LLM and embedding bindings at
a closed loopback port. The active build uses only graph storage/traversal, so an
accidental model-backed ingestion or query call fails closed.

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
it with `pipeline people`. Merges are preview-bound: first run `--merge FROM INTO`,
inspect its digest and impact, then repeat with `--apply --expected-digest SHA256`.
Folded spellings become hidden redirects rather than visible aliases, and compiled
minutes are corrected deterministically without another model call.

For the one-time repair of aliases created before hidden redirects existed, write a
private preview under the database directory and apply only after approving its
exact digest:

```powershell
pipeline people --repair-merges --preview-to db/merge-control/repair.json
pipeline people --repair-merges --apply db/merge-control/repair.json --expected-digest <sha256>
```

The preview can contain private names and file paths. Creating it is read-only and
does not authorize modifying the live manifest.

**ASR sits behind a `Backend` protocol.** The scheduled job must select the
approved remote provider. A local CPU/GPU backend may not be introduced as a
nightly fallback.

**Re-publishing graph records replaces, never appends.** Each meeting's derived
graph record is tracked in the manifest and the old version is removed before a
recompiled version is published. If removal fails, publication is abandoned
rather than leaving contradictory copies in the graph.

**The corpus outlives its graph store.** `minutes/` and the entity/relation
registers are portable derived artifacts. If LightRAG stalls, slows down, or gets
outgrown, graph records can be republished elsewhere — your data is not hostage
to it.

## Not included

Getting audio off your phone. The pipeline starts from "audio file in a
directory," so that decision stays independent. Point `inbox/` at a synced folder,
an rclone mount, or anything else that produces files.

**Known open issues** are tracked honestly in
[docs/REVIEW.md](docs/REVIEW.md#open). Graph completeness now depends on the
subscription-authored entity/relation output and deterministic publication, not a
second local extraction pass.
