# AGENTS.md — Meeting Minutes Compiler Reference

Complete technical reference. Written so an agent (or a person) can understand,
modify, or rebuild this system without reading every source file.

Companion documents: [docs/PRD.md](docs/PRD.md) for goals and scope,
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design decisions and what was
rejected, [docs/REVIEW.md](docs/REVIEW.md) for the adversarial review and the residual
risk that code cannot remove.

## The compiler analogy

```
audio/        = raw input      (recordings, immutable)
transcripts/  = source code    (verbatim, immutable, retained forever)
LLM           = compiler       (extracts structure and rationale)
minutes/      = object code    (the corpus that gets indexed)
LightRAG      = linked binary  (knowledge graph + vector index)
queries       = runtime
```

The critical property: **the compile step is repeatable.** Source is retained, so
a better compiler (an improved `templates/minutes.md`) can rebuild every artifact
without re-acquiring the source. Transcription is the only irreversible cost, and
it happens exactly once per recording.

## Architecture

```
Google Drive     durable private source (Easy Voice Recorder Pro / backfill)
  │  capture     read-only API → verified local handoff in inbox/drive/
  ▼
inbox/          audio arrives (Drive handoff, cloud-synced folder, rclone mount)
  │  ingest      sha256 → dedup → parse filename → ffprobe → copy to audio/
  ▼
audio/          archived source                         [Tier 1, never indexed]
  │  transcribe  ffmpeg 16k mono → ASR → align → diarize
  ▼
transcripts/    <id>.json (word-level + speaker), <id>.md (readable turns)
  │  speakers    SPEAKER_00 → real names
  │  minutes     provider chain + templates/minutes.md + prior context
  │              (also emits entities + relations)
  ▼
minutes/        YYYY-MM-DD-title-<id8>.md               [Tier 2, THE CORPUS]
  │  graph-sync  publish subscription-authored records
  ▼
LightRAG        Postgres-backed graph store             [Tier 3, derived]
  │  deterministic traversal
  ▼
answers with citations
```

### Why three tiers

Raw transcripts must not reach the index. A one-hour meeting is ~10,000 spoken
words with maybe 500 words of durable signal — roughly a 5% ratio. Four failure
modes follow from indexing that directly:

1. **No chunk boundaries.** Speech has no headings; chunks land mid-thought.
2. **Referential collapse.** "Let's do that instead" is a decision in context and
   noise as a standalone chunk. Chunking destroys the anaphora that resolved it.
3. **Embedding dilution.** A 90%-filler chunk embeds toward the centroid of
   generic meeting talk. Everything resembles everything; precision degrades as
   the corpus grows — the opposite of what a multi-year archive needs.
4. **Graph poisoning.** Entity extraction over transcript mints a node per casual
   mention and an edge per incidental co-occurrence, burying real structure.

Minutes fix all four: structured, dense, entity-preserving, ~600–1200 words.

### Why LightRAG rather than vector RAG

Product-management questions are entity-centric and **aggregative**: "why did we
deprioritize X", "everything customer Y has said", "history of this request". The
answers span dozens of meetings. Top-k vector retrieval structurally cannot answer
a question whose answer lives in 40 documents — it returns 5. LightRAG's graph
layer plus `global` query mode addresses exactly that shape.

## Stage machine

State lives in `db/manifest.db`. Each stage claims rows at one status and
advances them.

```
discovered ──transcribe──▶ transcribed ──speakers──▶ speakers_resolved
    ▲                                                        │
    │                                                     minutes
  retry                                                      ▼
    │                                                 minutes_compiled
  failed ◀── any stage on unrecoverable error              index
                                                             ▼
                                                          indexed
```

**Ordering is load-bearing.** `db.pending()` sorts by `meeting_date, meeting_time`,
never by discovery time. The minutes compiler reads earlier minutes to populate
*Changed From Previous Position*; compiling out of order would compare a meeting
against its own future. This also makes backfill build the graph in the order
events actually happened.

`recent_indexed_before()` compares `(date, time)` as a **pair**. A date-only
comparison made every meeting blind to the other four from the same day, so at five
meetings a day a decision reversed after lunch was never flagged.

Prior context combines **chronological** (the last few meetings) with **topical** (a
LightRAG retrieval on this meeting's actual subjects). Recency alone systematically
missed long-horizon reversals, which are the valuable case — a decision being
reversed is usually months old.

The topical lookup is safe by construction: minutes compile in stage 4 and index in
stage 5, so this meeting is not yet in the index and every hit is necessarily from an
earlier one. No date filter is needed, which is fortunate because LightRAG offers
none.

**Failure policy differs by stage on purpose.** Transcription failures mark the row
`failed` (something is wrong with the file or environment; a human should look).
Minutes failures leave the row at `speakers_resolved` — the transcript is intact
and a model call is retryable, so the next batch picks it up automatically.

### Schema

```sql
meetings(   id PK,              -- full sha256 of audio bytes; also the dedup key
            source_path, source_name, audio_path,
            meeting_date, meeting_time, title_hint, duration_sec,
            status, asr_model, template_version,
            transcript_path, minutes_path,
            lightrag_doc_id,    -- so a recompile can DELETE before re-inserting
            error, created_at, updated_at)

speakers(   meeting_id, label, name, confidence,  PK(meeting_id, label))

people(         canonical PK, role, notes, created_at)
person_aliases( alias PK, canonical FK)          -- lowercased for matching

entities(   meeting_id, name, kind, description,  PK(meeting_id, name))
relations(  meeting_id, subject, predicate, object,
            PK(meeting_id, subject, predicate, object))

seen_files( path PK, size, mtime, meeting_id, seen_at)  -- skip re-hashing

stage_runs( meeting_id, stage, started_at, finished_at, ok, detail)
```

New columns are applied to existing manifests via `MIGRATIONS` in `db.py`:
`CREATE TABLE IF NOT EXISTS` silently skips an existing table, so an upgraded install
would otherwise keep the old shape and fail at the first write.

`stage_runs` exists so the CPU budget is validated against measurements rather
than estimates — `pipeline status` aggregates it.

`id` is the **full** sha256, not a truncated prefix. It is the primary key and the
dedup key; the hashing cost is trivial next to the transcription it protects.

## Stages in detail

### 1. ingest (`pipeline/ingest.py`)

Walks `inbox/` recursively for known audio extensions, hashes each file, skips
anything already in `meetings`.

**Files are copied, never moved or deleted.** The inbox is expected to be a
cloud-synced folder — deleting propagates upstream and destroys the original.

Consequence: the inbox never empties, so a naive scan re-hashes everything nightly —
~165 GB read by year five. The `seen_files` table keys on path+size+mtime and skips
known files **without reading them**. Identity rather than content is deliberate:
the point is to avoid the read, and a file edited in place with identical size and
mtime does not happen to finished recordings.

**Filename parsing** handles two conventions:

- Pixel Recorder: `Ali Aug 10 at 11-12 a.m..m4a` → date `2026-08-10`, time
  `11:00`, hint `Ali`. Month-name dates carry no year, so the file's mtime year
  fills in.
- ISO: `2026-08-10T1100_roadmap-review.m4a` → hint `roadmap review`.

The date span is **masked out before searching for a time**. In
`2026-08-10T1100` the `T` separator is legitimately preceded by a digit, which the
compact-time lookbehind would otherwise reject; masking also prevents a date from
overlapping a time pattern (`2026_0810`). Masking preserves string length so match
offsets stay valid. Unparseable names fall back to mtime, so nothing is ever
dropped for having a bad name.

### 0. capture (`pipeline/capture.py`)

The optional Drive capture stage reads only the configured private `future` and
one-time `backfill` folders. It stages a download through a `.part` file, checks
the Drive byte count and MD5 when available, then atomically hands it to
`inbox/drive/`. It never writes, moves, or deletes Drive files.

`drive_sources` records the Drive file ID and version, metadata, parsed recording
date, state, local handoff path, and linked meeting. The collector is safe to
rerun. The backfill accepts only an explicit filename date on or after
`2026-06-09`; ambiguous names are held for review. Once it is ingested,
`pipeline capture --complete-backfill` disables that source.

After a Drive-backed audio file is transcribed, its local archive is deleted and
the transcript remains. Re-transcription re-downloads only if Drive still serves
the same file version, never silently substituting modified audio.

### 2. transcribe (`pipeline/asr.py`)

Three distinct models doing three orthogonal jobs:

| Job | Input → Output | Model |
|---|---|---|
| ASR | audio → text | faster-whisper (`large-v3-turbo`) |
| Alignment | audio + text → per-word times | wav2vec2 |
| Diarization | audio → who-spoke-when | pyannote |

Diarization produces **no words**; ASR has **no idea who is speaking**. Merging
them via `assign_word_speakers` is what makes action-item ownership possible.

Audio is normalized to **16 kHz mono** first. Every ASR model resamples to this
internally, so it costs no accuracy and turns ~18 MB/hr into ~2–4 MB/hr.

`glossary.md` becomes Whisper's `initial_prompt`, capped at ~224 tokens
(estimated at 4 chars/token, erring small — overrunning silently drops the tail).
File order is priority order.

Degradation is graceful and loud: a missing alignment model logs and continues
with segment-level timestamps; a missing `HF_TOKEN` or failed diarization logs and
continues without speaker labels. A transcript is always worth keeping.

`WhisperXBackend` implements the `Backend` protocol
(`transcribe(path, meeting_id, initial_prompt) -> Transcript`). Swapping in a paid
API or a GPU model touches this one class — the designed escape hatch from CPU.

Version tolerance: `DiarizationPipeline` is constructed with `token=`, falling back
to `use_auth_token=` on `TypeError`; `assign_word_speakers` is looked up on both
`whisperx` and `whisperx.diarize`. Both moved between releases.

### 3. speakers (`pipeline/speakers.py`)

Cascade, later sources winning:

1. **Filename hint** — applied *only* when the meeting has exactly two speakers,
   the hint is one or two words, and `--owner` is known. The dominant speaker is
   assumed to be the recorder. All three conditions must hold; otherwise which
   label to assign is a coin flip.
2. **LLM pass** over the first four minutes, looking for self-introductions. Given
   names from previous meetings so recurring attendees get consistent spelling —
   "Mike" and "Michael" as separate graph nodes is a real failure.
3. **`speaker-overrides.yaml`** — ground truth, beats everything.

**Unresolved labels stay unresolved.** The prompt explicitly instructs `null` over
a guess. A visible `SPEAKER_01` is fixable; a confidently wrong name silently
assigns work to the wrong person and nobody notices.

The JSON transcript keeps raw labels as ground truth; the Markdown is rewritten
with resolved names.

### 4. minutes (`pipeline/compile_minutes.py`)

Runs through the provider chain in `pipeline/llm.py` — Codex, then Claude, then
Antigravity — falling through on failure. All three are subscription-backed. The
minutes response includes the entities and relations that are later published
directly to LightRAG; LightRAG never performs model-backed extraction.

Prompts reach the CLI providers on **stdin**, never argv: a full transcript is tens
of thousands of tokens and would risk `ARG_MAX`.

Prompt assembles: `templates/minutes.md` (the spec) + meeting metadata + resolved
attendees + explicit unresolved-label instructions + excerpts from up to 3 earlier
minutes + turn-merged dialogue.

Dialogue is **turn-merged**, not segment-per-line: speech chopped every few
seconds reads as noise and loses track of who is arguing what.

Output is validated to start with `---`. A fence wrapping the whole document is
stripped first — models fence markdown even when told not to, and a stray ``` ahead
of the frontmatter breaks every YAML parser downstream.

`template_version` is stamped into frontmatter. `db.stale_template()` finds
documents built by an older version; `pipeline minutes --recompile` rebuilds them
from retained transcripts. **This is the payoff for the three-tier design.**

### 4b. entities (`pipeline/entities.py`)

The minutes compiler emits explicit `Entities` and `Relations` sections. These are
parsed, person names canonicalized through the people registry, stored in the
`entities` / `relations` tables, and appended to the **indexed text** (never the
file on disk) as a normalized `## Knowledge Graph` block.

The subscription model already running once per meeting states graph facts
explicitly. `pipeline graph-sync` publishes those exact records, so graph quality
does not depend on a second extraction pass or a text-embedding model.

The parser is tolerant by design — `-` bullets, `[]` brackets, `->`/`→`/`|` arrows,
missing descriptions — because models produce all of those for one instruction, and
recovering most of a messy block beats discarding all of a slightly-malformed one.

Storing them in the manifest also means the corpus no longer depends on LightRAG's
extraction quality: the entities survive independently of the index.

### 5. graph publication (`pipeline/graph_sync.py`)

`pipeline graph-sync` reads the manifest's subscription-authored entities and
relations and publishes them through LightRAG's graph storage endpoints. Retrieval
uses deterministic label matching plus graph traversal. The old document-ingestion
client in `pipeline/index.py` is retained only for the approval-gated legacy repair
preview; it is not called by `pipeline run` or the dashboard.

### 6. query

Answering is split in two (`pipeline/answer.py`): deterministic graph traversal plus
a bounded minutes/register scan retrieves context, and the subscription chain writes
the answer. LightRAG's model-backed `/query` route is never called. If no subscription
provider is reachable, the caller receives the cited retrieved context directly.

`--timing` reports retrieval and synthesis separately, so when queries get slow the
number says which phase is responsible.

For rare verbatim lookups, grep `transcripts/` directly — cheap, precise, no
embedding cost. That's why they're retained as readable Markdown, not only JSON.

### People registry (`db.people` / `db.person_aliases`)

Every resolved speaker name and every `person` entity normalizes through
`db.canonical_name()`. Asking a model to spell a name the same way it did four months
ago is not a strategy, and each variant becomes a separate graph node.

`pipeline.people_merge` is the only merge seam. CLI and dashboard code may request
`preview()` and `merge()`, but must not reproduce database/voice/file ordering.
Preview hashes the exact database and minutes impact; apply requires that digest.

```powershell
pipeline people --merge Mike Michael
pipeline people --merge Mike Michael --apply --expected-digest <sha256>
pipeline people --resume-merge-rewrites
```

A merge rewrites speakers, suggestions, voice samples, structured registers, and
both ends of every relation. It then drains durable, hash-checked minutes rewrite
jobs. A crash may leave a job pending, but never an untracked file change. Merged
spellings become tombstoned **hidden redirects**, not visible aliases, so stale
speaker-review cards cannot recreate the absorbed person.

Historical aliases created before tombstones require a separate preview/apply:

```powershell
pipeline people --repair-merges --preview-to db/merge-control/repair.json
pipeline people --repair-merges --apply db/merge-control/repair.json --expected-digest <sha256>
```

Repair artifacts contain private name/file evidence and belong under
`config.DB_DIR / "merge-control"`. Generating or verifying one is read-only and
does **not** authorize applying it to the live manifest; application needs explicit
approval of the exact digest.

Unknown names pass through unchanged: a new person appearing is normal, and dropping
them would be worse than an unnormalized spelling.

## Configuration

Everything in `pipeline/config.py`, all overridable by environment variable.

| Variable | Default | Notes |
|---|---|---|
| `MMC_LLM_PROVIDERS` | `codex,claude,antigravity` | Enforced priority order; falls through on failure |
| `MMC_ALERT_COMMAND` | unset | Failure summary on stdin, `{subject}` substituted |
| `MMC_MIN_SPEAKERS` / `MMC_MAX_SPEAKERS` | unset | Bounds passed to pyannote |
| `MMC_IMPLAUSIBLE_SPEAKERS` | `8` | Above this, warn about over-segmentation |
| `MMC_MINUTES_TOKEN_BUDGET` | `60000` | Over this, the compiler map-reduces |
| `MMC_GEMINI_MODEL` | unset | Pin a Flash version; unset lets the CLI choose |
| `MMC_GEMINI_ARGS` / `MMC_CODEX_ARGS` | `-p -` / `exec -` | Override if a CLI changes its invocation |
| `MMC_LLM_TIMEOUT` | `900` | Per-call ceiling; a CLI wanting a TTY would otherwise hang the batch |
| `MMC_LIGHTRAG_API_KEY` | — | **Required**; compose fails fast without it |
| `MMC_OWNER_NAME` | unset | Default for `--owner` |
| `MMC_TIMEZONE` | `America/Toronto` | Meeting dates depend on it; wrong value mislabels the corpus |
| `MMC_ASR_MODEL` | `large-v3-turbo` | `large-v3` only if you have a GPU |
| `MMC_ASR_DEVICE` | `cpu` | |
| `MMC_ASR_COMPUTE_TYPE` | `int8` | `float16` on GPU |
| `MMC_DIARIZATION` | `1` | `0` disables (much faster, no owners) |
| `HF_TOKEN` | — | Required for diarization |
| `MMC_LIGHTRAG_URL` | `http://localhost:9621` | |
| `MMC_INBOX` / `_AUDIO` / `_TRANSCRIPTS` / `_MINUTES` / `_DB_DIR` | repo-relative | Point Tier 1 at bulk storage |

`TEMPLATE_VERSION` is a code constant, not an env var — it must move together with
the template it describes.

## Known upstream issues, already worked around

| Issue | Symptom | Workaround |
|---|---|---|
| pyannote gating | Diarization fails at *runtime*, not install | `HF_TOKEN` + accept model terms |
| LightRAG starts model routes even though this build does not use them | Accidental calls could invoke model-backed ingestion/query | Compose points both bindings at a closed loopback port so calls fail closed |

LightRAG and Postgres bind to **loopback only** and `LIGHTRAG_API_KEY` is enforced
by compose. The graph contains private meeting context; never widen the bind.

## Cost model

- **Transcription** — free, CPU time only. ~30–50 min per meeting.
- **Minutes** — Claude subscription, no metered cost. One call per meeting.
- **Graph publication/retrieval** — deterministic storage operations, no model call.
- **Answer synthesis** — the same subscription provider chain.

Total marginal cost per meeting is electricity. The tradeoff is a ~4 hour nightly
batch, which is why `pipeline run` belongs on a timer rather than a file watcher.

## Backup and restore

`pipeline backup --to PATH`. Priority order reflects what is actually
irreplaceable:

1. `transcripts/` — immutable source; everything downstream rebuilds from these
   without re-running ASR.
2. `audio/` — recreates transcripts, but only at 30-50 CPU-minutes each.
3. `minutes/` — rebuildable from transcripts, at the cost of LLM quota.
4. `db/manifest.db` — rebuildable in principle, painful in practice.

Uses `sqlite3.Connection.backup()` plus an integrity check, **not** a file copy: a
copy of a live database can capture a torn page or miss the WAL, producing a
snapshot that restores as corrupt. Tree sync is incremental on size+mtime, and
nothing is ever deleted from the destination — a file vanishing from the source is
precisely when the copy matters.

`rag_storage/` and the Postgres volume are deliberately excluded. The graph is
derived; `pipeline graph-sync` rebuilds it from manifest entities and relations.

## Extending

**Better ASR** — implement `Backend` in `pipeline/asr.py`. Candidates as of
mid-2026: Granite Speech 4.1 2B and Cohere Transcribe 2B lead the open leaderboard;
Parakeet TDT leads on speed. Note leaderboard WER is measured on clean read
speech — fractional-percent gaps are noise next to what far-field meeting audio
does to accuracy. Measure on your own recordings first.

**Better diarization** — Sortformer v2 and DiariZen benchmark ahead of pyannote
specifically on meeting audio, though both are GPU-oriented. pyannote 4.0
community-1 supersedes the 3.1 that whisperx bundles.

**True temporal reasoning** — *Changed From Previous Position* is a pragmatic
approximation. If "which decision is current?" becomes the dominant question,
Graphiti offers real bi-temporal modeling: facts carry validity windows and are
invalidated rather than deleted when superseded. It's a library, not a service, and
needs Neo4j or FalkorDB — a deliberate escalation, not a starting point.

**Claude Code integration** — a `SessionStart` hook could inject the LightRAG index
into coding sessions. The previous system in this repo did something similar but
inlined the *entire* knowledge base into every prompt, so cost and context grew
linearly with corpus size. Query the index; never dump it.
