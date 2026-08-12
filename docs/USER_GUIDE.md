# User Guide

How to run this thing day to day. For *why* it works the way it does see
[ARCHITECTURE.md](ARCHITECTURE.md); for the technical reference see
[AGENTS.md](../AGENTS.md).

---

## Contents

1. [What you get](#1-what-you-get)
2. [First-time setup](#2-first-time-setup)
3. [Your first meeting](#3-your-first-meeting)
4. [Judging the output](#4-judging-the-output-do-not-skip-this)
5. [Running it every night](#5-running-it-every-night)
6. [Asking questions](#6-asking-questions)
7. [Keeping it accurate](#7-keeping-it-accurate)
8. [Backups](#8-backups)
9. [When something breaks](#9-when-something-breaks)
10. [Backfilling old recordings](#10-backfilling-old-recordings)
11. [Improving the minutes later](#11-improving-the-minutes-later)
12. [Command reference](#12-command-reference)

---

## 1. What you get

Drop meeting recordings in a folder. Overnight, each one becomes a structured
minutes document — decisions with rationale, action items with owners, customer
signals, risks — and lands in a knowledge base you can ask questions of months
later.

```
inbox/  →  transcript  →  minutes  →  knowledge graph  →  answers
           (kept)          (indexed)
```

Three things are worth knowing before you start, because they explain most of the
design:

- **Transcripts are kept but never searched.** An hour of speech is ~10,000 words
  containing maybe 500 worth keeping. Searching the raw transcript gets steadily
  worse as your archive grows. The minutes are what you search.
- **Transcripts are kept anyway** so every claim can cite the moment in the audio,
  and so you can rebuild all your minutes later with a better template *without
  re-transcribing anything*. That second point matters more than it sounds — see
  [§11](#11-improving-the-minutes-later).
- **It runs overnight, not live.** About 4 hours for five meetings on CPU. This is
  an archive, not a meeting assistant.

---

## 2. First-time setup

### 2.1 Install

```bash
git clone <your-repo> && cd claude-memory-compiler
uv sync --extra asr          # heavy: torch and friends
sudo apt-get install ffmpeg  # or brew install ffmpeg
```

### 2.2 Configure

```bash
cp .env.example .env
```

Two values are **required**:

| Variable | How to get it |
|---|---|
| `MMC_LIGHTRAG_API_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `HF_TOKEN` | A HuggingFace **read** token from hf.co/settings/tokens |
| `POSTGRES_PASSWORD` | Generate one the same way as the API key |

**The HuggingFace token is not enough on its own.** You must also visit both pages
below and accept the terms, or speaker detection silently does nothing and every
action item comes out with no owner:

- https://hf.co/pyannote/speaker-diarization-3.1
- https://hf.co/pyannote/segmentation-3.0

Worth setting too:

```bash
MMC_OWNER_NAME=Your Name      # helps identify who is who in 1:1s
MMC_TIMEZONE=America/Toronto  # meeting dates depend on this
```

### 2.3 Start the services

```bash
docker compose up -d
docker compose exec ollama ollama pull qwen3:4b
docker compose exec ollama ollama pull mxbai-embed-large
```

Everything binds to **localhost only**. This index will contain every decision and
customer conversation you record, so it is not exposed to your network. To reach
the web UI from a laptop:

```bash
ssh -L 9621:127.0.0.1:9621 you@yourserver
# then open http://localhost:9621
```

### 2.4 Check it

```bash
uv run pipeline init
uv run pipeline doctor
```

`doctor` runs 18 checks and prints a fix next to anything wrong. Get it to zero
failures before continuing. Warnings are usually fine to start with.

---

## 3. Your first meeting

```bash
cp ~/some-recording.m4a inbox/
uv run pipeline run --owner "Your Name"
```

Expect roughly 30–50 minutes for a one-hour recording on CPU. Run the stages
individually the first time so you can look at each artifact:

```bash
uv run pipeline ingest       # finds the file, reads its date and duration
uv run pipeline transcribe   # the slow one
uv run pipeline speakers --owner "Your Name"
uv run pipeline minutes
uv run pipeline index
```

**Filenames matter a little.** Dates and times are read from them where possible:

| Filename | Understood as |
|---|---|
| `Ali Aug 10 at 11-12 a.m..m4a` | 10 Aug, 11:00, about Ali |
| `2026-08-10T1100_roadmap.m4a` | 10 Aug, 11:00, about "roadmap" |
| `voice 0042.m4a` | falls back to the file's timestamp |

Nothing is rejected for having an odd name.

---

## 4. Judging the output (do not skip this)

`doctor` tells you the plumbing works. It cannot tell you the minutes are any good.
Only this step does.

Open the readable transcript and listen to the audio alongside it:

```bash
less transcripts/*.md
```

**Count errors only in things that matter**: people's names, product and feature
names, numbers, and dates. Ignore filler-word mistakes entirely — nobody will ever
search for "um".

Then read the minutes:

```bash
less minutes/*.md
```

Ask three questions:

1. Does every decision include *why*, not just what?
2. Does every action item have the right owner?
3. Do the `[0:14:02]` timestamps point at the right moment in the audio?

### If the transcript is poor

**Try the microphone before the model.** A phone flat on a table with five people
around it is the hard case. Room acoustics and mic placement move accuracy by
10–20 percentage points; changing the model moves it by less than one. A ~$30
USB-C boundary microphone is the single highest-leverage improvement available in
this entire system.

After that, add your vocabulary to `glossary.md` (see [§7](#7-keeping-it-accurate)).

### Check the timings against reality

```bash
uv run pipeline status
```

This prints *measured* per-stage times. Compare them with the estimates in the
README. If transcription is much slower than ~50 min/meeting, five a day will not
fit in a night and you need either a GPU or fewer recordings.

---

## 5. Running it every night

Once one meeting works end to end, automate it:

```cron
0 1 * * *  cd /path/to/repo && uv run pipeline run --owner "Your Name" >> pipeline.log 2>&1
30 5 * * * cd /path/to/repo && uv run pipeline backup --to /mnt/backup >> backup.log 2>&1
```

Use a timer, not a folder watcher — a batch takes hours, and overlapping runs would
collide.

**Set up failure alerts.** `pipeline run` exits non-zero when something breaks, but
on a headless server nothing reads that. Add to `.env`:

```bash
MMC_ALERT_COMMAND=curl -s -d @- https://ntfy.sh/your-private-topic
# or
MMC_ALERT_COMMAND=mail -s "{subject}" you@example.com
```

Without this, a failure in month four surfaces in month nine as an empty search
result.

---

## 6. Asking questions

```bash
uv run pipeline query "why did we deprioritize Atlas?"
uv run pipeline query "what has Northwind asked us for?"
uv run pipeline query "what changed about the Q4 plan?"
```

**Choosing a mode:**

| Mode | Use for |
|---|---|
| *(default)* | Most questions |
| `--mode global` | Answers spanning many meetings: "summarize all budget discussion this year" |
| `--mode local` | One specific thing: "what is the rate-limiter?" |

**Other flags:**

- `--timing` — shows how long retrieval took versus writing the answer. Use this
  when queries feel slow, to see which half is responsible.
- `--local` — lets the small local model write the answer instead of your
  subscription. Faster to start, noticeably worse. Mostly useful for comparison.

**If an answer looks wrong**, check whether the meeting is actually indexed:

```bash
uv run pipeline status
```

The system is told to say "the records don't cover this" rather than guess, so a
confident wrong answer usually means the minutes themselves are wrong — read the
underlying file in `minutes/`.

---

## 7. Keeping it accurate

Three files reward a few minutes of attention. They are the difference between a
knowledge base that works in year two and one that doesn't.

### `glossary.md` — product and people names

Terms here are fed to the transcriber so it spells them correctly.

**Order matters** — the list is cut off at roughly 40 terms, so put the most
important at the top.

Why bother: if "Project Atlas" gets transcribed three different ways, your
knowledge base ends up with three unrelated entries for one project, and no search
finds all of it.

### The people registry

```bash
uv run pipeline people                        # who is known
uv run pipeline people --add "Michael" "Mike" "Mikey"   # aliases
uv run pipeline people --merge "Mike" "Michael"         # fix a duplicate
```

`--merge` rewrites your existing history too, not just future meetings.

Check this every few weeks. Duplicate people are the most common way the knowledge
base quietly degrades.

### `speaker-overrides.yaml` — when names are wrong

The system leaves a speaker as `SPEAKER_01` rather than guessing, because a
confidently wrong name silently assigns work to the wrong person. Correct it here:

```yaml
default:
  SPEAKER_00: Faraz        # you, usually the same voice every time
a1b2c3d4e5f6:              # or per meeting, using the id from `pipeline status`
  SPEAKER_01: Ali
```

### Checking graph health

```bash
uv run pipeline entities
```

The top entries should be your products, people and customers. If they are generic
words, something is wrong with the minutes template or the transcripts.

---

## 8. Backups

Your recordings, transcripts and minutes are **not in git** and cannot be
reconstructed from anything else.

```bash
uv run pipeline backup --to /mnt/backup
uv run pipeline backup --to /mnt/backup --no-audio   # skip the bulky part
```

Runs incrementally, so nightly is cheap. It never deletes from the destination — a
file disappearing from your source is exactly when the backup matters.

The search index is deliberately **not** backed up; it is rebuilt from your minutes
with `pipeline index`.

**Restoring:** copy the folders back, then follow the steps in `BACKUP_INFO.txt`
inside the backup.

---

## 9. When something breaks

**Always start here:**

```bash
uv run pipeline status   # what state is everything in
uv run pipeline doctor   # what is misconfigured
```

| Symptom | Likely cause | Fix |
|---|---|---|
| Speakers all `SPEAKER_00` | `HF_TOKEN` missing, or the two licences not accepted | [§2.2](#22-configure) |
| Transcription very slow | `MMC_ASR_MODEL` set to `large-v3` on CPU | Unset it to use the faster default |
| "LightRAG unreachable" | Services not running | `docker compose up -d` |
| Indexing fails oddly | Ollama models not pulled | `docker compose exec ollama ollama pull qwen3:4b` |
| Meetings stuck at `failed` | Bad file or a transient error | `pipeline retry` then `pipeline run` |
| Wrong names in minutes | Speaker mis-identified | `speaker-overrides.yaml`, then `pipeline minutes --recompile` |
| Too many speakers detected | Noisy audio being over-split | Set `MMC_MAX_SPEAKERS`, or improve the mic |

**Nothing is ever lost when a stage fails.** Your audio and transcripts are kept,
every stage is resumable, and the next run picks up where it stopped. A failure
costs time, never data.

---

## 10. Backfilling old recordings

You probably have a backlog. **Do not start with it.**

At ~40 minutes of CPU per meeting, a year and a half of history is roughly a week
of continuous processing. Start with new meetings, confirm quality, then backfill
gradually:

```bash
cp ~/old-recordings/2025-03*.m4a inbox/
uv run pipeline run --limit 10 --owner "Your Name"
```

Oldest recordings are always processed first, so your history builds in the order
things actually happened — which is what lets the system notice when a decision
was later reversed.

---

## 11. Improving the minutes later

This is the feature most worth understanding, because it changes how you should
think about the template.

Your minutes template will improve. Eventually you will want a field you never
thought of — competitive intel, hiring signals, whatever. **You can add it
retroactively to your entire history**, because every transcript was kept:

1. Edit `templates/minutes.md`
2. Increment `TEMPLATE_VERSION` in `pipeline/config.py`
3. `uv run pipeline minutes --recompile && uv run pipeline index`

Every meeting is rebuilt with the new template. **No re-transcription** — the
expensive part is never repeated, and old index entries are replaced rather than
duplicated.

`pipeline status` tells you how many documents are on an old template version.

---

## 12. Command reference

```
pipeline init                      create folders and the database
pipeline doctor                    check the environment (18 checks)
pipeline status                    state of every meeting + measured timings

pipeline ingest                    find new recordings
pipeline transcribe                speech to text with speakers  (slow)
pipeline speakers --owner "Name"   put real names to voices
pipeline minutes                   write the minutes
pipeline index                     add to the knowledge base
pipeline run                       all of the above, in order

pipeline query "question"          ask the knowledge base
  --mode global                      answers spanning many meetings
  --mode local                       one specific thing
  --timing                           show retrieval vs answer time
  --local                            use the local model to answer

pipeline people                    list known people
  --add NAME [ALIAS...]              register aliases
  --merge FROM INTO                  fold a duplicate, rewriting history
pipeline entities                  most-mentioned things (health check)

pipeline minutes --recompile       rebuild after a template change
pipeline backup --to PATH          back up everything irreplaceable
  --no-audio                         skip the bulky part
pipeline retry                     requeue failed meetings
```

Useful flags on most stage commands: `--limit N` to process only a few,
`--traceback` to see full errors.

---

## Getting audio onto the server

Deliberately not part of this system, so you can change it without touching
anything else. The pipeline starts from "a file in the `inbox/` folder".

If you record on a Pixel: the built-in Recorder app syncs to Google's servers,
which cannot be read programmatically. The recommended alternative is **Fossify
Voice Recorder** (free, open source, lets you set the filename pattern) plus
**FolderSync** to wherever your server can see. Details in
[PRD.md](PRD.md#capture-deferred).
