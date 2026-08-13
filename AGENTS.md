# AGENTS.md — Meeting Minutes Compiler Reference

Complete technical reference. Written so an agent (or a person) can understand,
modify, or rebuild this system without reading every source file.

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
  │  minutes     Claude Agent SDK + templates/minutes.md + prior context
  ▼
minutes/        YYYY-MM-DD-title-<id8>.md               [Tier 2, THE CORPUS]
  │  index       POST /documents/text
  ▼
LightRAG        knowledge graph + vector index          [Tier 3, derived]
  │  query
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
            transcript_path, minutes_path, error, created_at, updated_at)

speakers(   meeting_id, label, name, confidence,  PK(meeting_id, label))

stage_runs( meeting_id, stage, started_at, finished_at, ok, detail)
```

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
Consequence: every run re-hashes the inbox. That's intentional and cheap (~60s for
a year's worth), and dedup makes rescanning free.

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

Claude Agent SDK, no tools, `max_turns=3`. Subscription-covered, which is why the
highest-value artifact uses it while bulk entity extraction runs on local Ollama.

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

### 5. index (`pipeline/index.py`)

`POST /documents/text` with `file_source` set to the filename so citations trace
back to a meeting. Minutes only. Long timeout (default 600s) because CPU-bound
entity extraction blocks the request.

### 6. query

`POST /query`. Modes: `hybrid` (default, graph + vector), `global` (aggregative,
spans many meetings), `local` (tight entity lookup), `naive` (plain vector).

For rare verbatim lookups, grep `transcripts/` directly — cheap, precise, no
embedding cost. That's why they're retained as readable Markdown, not only JSON.

## Configuration

Everything in `pipeline/config.py`, all overridable by environment variable.

| Variable | Default | Notes |
|---|---|---|
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

| Issue | Symptom | Workaround (in `docker-compose.yml`) |
|---|---|---|
| [LightRAG#2495](https://github.com/HKUDS/LightRAG/issues/2495) | Embeddings fail on Ollama 0.13.x | Pin `ollama/ollama:0.12.11` |
| [LightRAG#2023](https://github.com/HKUDS/LightRAG/issues/2023) | Server won't boot on Ollama bindings | Set a dummy `OPENAI_API_KEY` |
| pyannote gating | Diarization fails at *runtime*, not install | `HF_TOKEN` + accept model terms |
| `EMBEDDING_DIM` mismatch | Opaque dimension error at insert | Must match model (mxbai-embed-large = 1024) |

CPU contention is also configured for: `MAX_ASYNC=2`, `MAX_PARALLEL_INSERT=1`,
`OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KEEP_ALIVE=30m`. Transcription and indexing share
one CPU in the nightly batch; without these they fight for cores and both slow
down. `OLLAMA_KEEP_ALIVE` matters because LightRAG makes many small extraction
calls, and reloading weights between them otherwise dominates runtime.

## Cost model

- **Transcription** — free, CPU time only. ~30–50 min per meeting.
- **Minutes** — Claude subscription, no metered cost. One call per meeting.
- **Indexing** — free, local Ollama. Several extraction calls per document,
  ~5–20 min on CPU.
- **Queries** — free, local Ollama.

Total marginal cost per meeting is electricity. The tradeoff is a ~4 hour nightly
batch, which is why `pipeline run` belongs on a timer rather than a file watcher.

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
