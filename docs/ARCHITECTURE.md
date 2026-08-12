# Architecture & Design Decisions

Post adversarial review. For operational reference (env vars, gotchas, stage
internals) see [AGENTS.md](../AGENTS.md); for goals and scope see [PRD.md](PRD.md).

## The one-paragraph version

Audio becomes a verbatim transcript, which is retained forever but never indexed.
The transcript is *compiled* into structured minutes, and only the minutes are
indexed — into a knowledge graph plus vector store. Because the transcript is
retained, the compile step is repeatable: a better template rebuilds the entire
archive without re-transcribing. Six resumable stages over a SQLite manifest,
processed oldest-meeting-first, driven by a nightly timer.

```
inbox/          audio arrives                            [Tier 1: source]
  │  ingest      sha256 dedup, filename parse, ffprobe
  ▼
audio/          archived original                        never indexed
  │  transcribe  ffmpeg 16k mono → ASR + align + diarize
  ▼
transcripts/    word-level, speaker-labeled              never indexed
  │  speakers    SPEAKER_00 → real names
  │  minutes     subscription LLM + versioned template
  ▼
minutes/        600-1200 structured words                [Tier 2: THE CORPUS]
  │  index       replace-on-reindex
  ▼
LightRAG        graph + vector, on Postgres              [Tier 3: derived]
  │  query
  ▼
answers with citations
```

## The compiler analogy

```
audio/        raw input      immutable
transcripts/  source code    immutable, retained forever
LLM           compiler       extracts structure and rationale
minutes/      object code    the indexed corpus
LightRAG      linked binary  derived, disposable, rebuildable
```

Transcription is the only irreversible cost, and it happens exactly once per
recording. Everything downstream is a recompile.

---

## Decision log

Each entry records what was decided, why, and what was rejected. Several were
revised by adversarial review; those are marked.

### D1 — Index minutes, never transcripts

**Decision.** Only compiled minutes enter the RAG index. Transcripts are retained
but excluded.

**Why.** An hour of speech is ~10,000 words with maybe 500 words of durable signal.
Four failure modes follow from indexing it directly:

1. *No chunk boundaries* — speech has no headings, so chunks land mid-thought.
2. *Referential collapse* — "let's do that instead" is a decision in context and
   noise as a standalone chunk. Chunking destroys the anaphora that resolved it.
3. *Embedding dilution* — a 90%-filler chunk embeds toward the centroid of generic
   meeting talk. Everything resembles everything, and precision **degrades as the
   corpus grows** — the opposite of what a multi-year archive needs.
4. *Graph poisoning* — entity extraction mints a node per casual mention, burying
   real structure.

**Rejected.** Indexing transcripts alongside minutes (dilutes retrieval); deleting
transcripts after compiling (destroys D2).

### D2 — Retain transcripts to make the compile repeatable

**Decision.** Transcripts are permanent. `TEMPLATE_VERSION` in frontmatter marks
which template built each minutes file; bumping it makes `pipeline minutes
--recompile` rebuild from retained transcripts.

**Why.** Minutes are a *lossy* compile. The template will improve, and eventually a
field nobody thought of will turn out to matter. With transcripts retained that is
a re-run; without them the history is gone permanently. On CPU, where ASR is 30-50
minutes per meeting, this is the single most valuable property in the design.

**Rejected.** Trusting the template to be right first time.

### D3 — Graph RAG (LightRAG), not vector RAG

**Decision.** LightRAG, hybrid graph + vector, with `global` mode for aggregative
questions.

**Why.** PM questions are entity-centric and aggregative. "Everything customer Y
told us" has an answer spanning forty documents; top-k retrieval returns five. That
is a structural limitation, not a tuning problem.

**Rejected.** Plain vector RAG (fails aggregative queries); RAGFlow (its strength
is parsing scanned PDFs — wasted on plain-text minutes, and it costs Elasticsearch
+ MySQL + MinIO + Redis for a feature we do not use); Graphiti (correct bi-temporal
model, but a library needing Neo4j — an escalation, not a starting point).

**Hedge that makes this reversible.** `minutes/` is portable markdown. LightRAG is
*replaceable*: if it stalls or gets outgrown, the corpus re-indexes into anything.
The data is never hostage to the index.

### D4 — Resumable stage machine over a SQLite manifest

**Decision.** Each meeting is a row advancing through
`discovered → transcribed → speakers_resolved → minutes_compiled → indexed`. Stages
claim rows at one status and advance them.

**Why.** ASR is the expensive step and must never repeat. A crash during minutes
compilation costs minutes, not the hours behind it. It also makes D2 mechanical:
re-run stages 4-5 over years of history, touching nothing upstream.

**Failure policy differs by stage, deliberately.** Transcription failures mark the
row `failed` — something is wrong with the file or environment and a human should
look. Minutes failures leave the row at `speakers_resolved` — the transcript is
intact and a model call is retryable, so the next batch picks it up automatically.

### D5 — Process oldest-meeting-first, never discovery order

**Decision.** `db.pending()` orders by `(meeting_date, meeting_time)`.

**Why.** The minutes compiler reads earlier minutes to detect reversed decisions.
Out-of-order compilation would compare a meeting against its own future. It also
makes backfill build the graph in the order events actually happened.

**Revised by review.** The comparison originally used date only, which made every
meeting blind to the other four from the same day — at five meetings a day, a
decision reversed after lunch was never flagged.

### D6 — `large-v3-turbo`, not `large-v3`

**Decision.** Turbo is the default ASR model.

**Why.** Arithmetic, not preference. Per one-hour meeting on CPU:

| Step | large-v3 | large-v3-turbo |
|---|---|---|
| ASR | ~60-120 min | ~8-15 min |
| Diarization | ~15-30 min | ~15-30 min |
| Alignment | ~5 min | ~5 min |
| **× 5/day** | ~9 h — not viable | **~3 h — fits overnight** |

Diarization is the second-largest cost and does **not** shrink with a smaller ASR
model. It stays anyway: action-item ownership depends on it.

**Escape hatch.** ASR sits behind a `Backend` protocol. A paid API or GPU model
replaces one class and nothing downstream notices.

### D7 — Copy from the inbox, never move

**Decision.** Ingest copies files out and leaves the inbox untouched.

**Why.** The inbox is expected to be a cloud-synced folder. Deleting from it
propagates upstream and destroys the original recording.

**Consequence, and its fix.** The inbox is never emptied, so a naive scan re-hashes
everything nightly — ~165 GB by year five. A `seen_files` table keyed on
path+size+mtime skips known files without reading them.

### D8 — Never guess a speaker's name

**Decision.** An unresolved label stays `SPEAKER_01`. The resolver returns `null`
rather than a guess, and the prompt says so explicitly.

**Why.** An action item with a *wrong* owner is worse than one with no owner: it
silently assigns work to the wrong person and nobody notices. A visible gap is
fixable.

**Revised by review.** An earlier version assumed the dominant speaker was the
recorder. That is wrong for a large share of a PM's calendar — in stakeholder
interviews, user research and demos the other person talks more — and produced
confidently reversed names, contradicting this very decision. The filename now
supplies *candidate names* only; the LLM decides the mapping from who introduces
themselves and who is addressed by name.

### D9 — Minutes are 600-1200 structured words, not a summary

**Decision.** The template targets substance, and explicitly forbids executive
summarisation.

**Why.** Summaries drop *rationale*, and rationale is what answers "why did we
deprioritise X" — the most common question asked of a PM archive months later.
"Chose Postgres" is nearly worthless; "chose Postgres over DynamoDB because the
team already runs it and the access pattern is relational" is the whole value.

Entities must be preserved **verbatim**: paraphrasing "Project Atlas" into "the
platform project" creates a second disconnected graph node for one thing.

### D10 — Ordered provider chain, not a single LLM

**Decision.** Gemini Flash → Codex → Claude, falling through on failure. Prompts go
in on **stdin**, never argv.

**Why.** All three are subscription-backed. A quota limit on the preferred provider
must not stall a batch that has already paid 40 CPU-minutes for transcription.
Stdin because a full transcript is tens of thousands of tokens and would risk
`ARG_MAX` as a command-line argument.

**Unverified.** The exact invocations (`gemini -p -`, `codex exec -`) could not be
tested — neither CLI was available. Both binary and arguments are env-overridable
so a mismatch is a config change, not a code change.

### D11 — Postgres storage from day one

**Decision.** LightRAG uses `PGKVStorage` / `PGVectorStorage` /
`PGDocStatusStorage` / `PGTableGraphStorage`, not the JSON + NanoVectorDB defaults.

**Why.** Cost of delay. The file-based defaults do not hold ~9k documents and
50-150k graph entities, and migrating a *populated* graph is painful. With zero data
today the switch is free; in six months it is a project.

**Why `PGTableGraphStorage` specifically.** It runs the graph on plain PostgreSQL
14+ tables with no Apache AGE and no extensions, so the standard `pgvector` image
works instead of an AGE-bundled one. This removed both a Neo4j service and an
unusual base image from the design.

### D12 — Replace on re-index, never append

**Decision.** The manifest records each meeting's LightRAG document id. Re-indexing
deletes the previous version first, and **abandons the insert if the delete fails**.

**Why.** Found in review: without this, `--recompile` inserted a second copy and
left the old version's entities in the graph, so retrieval returned contradictory
duplicates. That silently invalidated D2 — the whole reason transcripts are
retained. Document ids are derived locally the same way LightRAG derives them
(`doc-{md5(content.strip())}`), so the id is knowable before insert and stable
across restarts.

The bail-out ordering matters and was itself wrong in the first draft of the fix:
inserting before checking the delete creates the duplicate the function exists to
prevent, and the caller cannot undo it.

### D13 — Fail loudly

**Decision.** `pipeline run` aggregates stage results and returns non-zero if any
stage failed.

**Why.** Found in review: the original discarded every return code and returned 0.
A nightly cron would report success after total failure — a break in month 4 would
surface in month 9 as an empty query result. For an unattended multi-year system,
silent failure is the top operational risk.

### D14 — Map-reduce over-budget meetings

**Decision.** Under the token budget, compile in one pass. Over it, extract
per-window notes then compile from the extracts.

**Why.** A three-hour meeting is unbounded input; it would overflow the context
window or burn a disproportionate slice of subscription quota in one call.
Windows break only on speaker-turn boundaries — splitting mid-turn would cut a
decision away from its rationale. A failed window leaves a visible
`(extraction failed)` marker so the reduce step does not invent continuity.

### D15 — Topical prior context, not just recent

**Decision.** Prior-decision context combines the last few meetings
(chronological) with a LightRAG retrieval on this meeting's actual subjects
(topical).

**Why.** Found in review: recency alone systematically misses long-horizon
reversals, which are exactly the valuable case — a decision being reversed is
usually months old, not yesterday.

**A clean guarantee falls out of stage ordering.** Minutes are compiled in stage 4
and indexed in stage 5, so the current meeting is *not yet in the index*. Every hit
is therefore necessarily from an earlier meeting, and no date filtering is needed —
fortunate, because LightRAG offers none. Degrades to chronological-only when the
index is unreachable.

### D16 — Back up with SQLite's online API, not `cp`

**Decision.** `pipeline backup` uses `Connection.backup()` and integrity-checks the
result.

**Why.** A filesystem copy of a live SQLite database can capture a torn page or
miss the WAL, producing a backup that restores as corrupt. A backup you cannot
restore is worse than none: it removes the pressure to have a real one.

**What is deliberately not backed up.** `rag_storage/` and the Postgres volume. The
index is derived data — it re-indexes from `minutes/`, so backing it up spends
space on something reconstructible.

---

## The subscription ceiling

The central open tension, and worth stating plainly.

Three LLM jobs exist. Two can use subscriptions; one cannot:

| Job | Calls/meeting | Provider | Quality-sensitive? |
|---|---|---|---|
| Minutes compilation | 1 | Subscription chain | Very |
| Speaker resolution | 1 | Subscription chain | Moderately |
| Graph & entity extraction | Many | **Local Ollama ~4B** | **Very** |

LightRAG needs an OpenAI-compatible HTTP endpoint. Claude Code, Codex and Gemini
CLI are interactive tools, not servers, and no subscription offers an embedding
model at all. So the most quality-sensitive step in *retrieval* is permanently
capped by a small model on CPU, no matter what is paid for elsewhere.

Both mitigations are now built — see D17 and D18. The ceiling is narrowed rather
than removed: entity *identification* moved to a frontier model, and answer
*writing* moved to one, but graph traversal still runs locally.

### D17 — The compiler emits the knowledge graph

**Decision.** The minutes template emits explicit `Entities` and `Relations`
sections. These are parsed, canonicalized through the people registry, stored in the
manifest, and appended to the indexed document as a normalized block.

**Why.** The first half of the subscription-ceiling fix. A ~4B model reads
`Atlas (feature): the platform rewrite` reliably and discovers the same fact from
narrative prose unreliably. The frontier model already running once per meeting can
state it outright, so it does. Storing them in the manifest also means the corpus no
longer depends on LightRAG's extraction quality at all.

**Why a tolerant line parser, not JSON.** Models emit stray prose around JSON often
enough that a strict parse throws away a whole usable block. Recovering most of a
messy block beats discarding all of a slightly-malformed one.

### D18 — Retrieval and synthesis are separate

**Decision.** `pipeline/answer.py` retrieves via LightRAG, then hands the context to
the subscription chain to write the answer. `--local` keeps LightRAG's own
generation, and it is the automatic fallback when no provider is reachable.

**Why.** The second half of the ceiling fix, and the answer to query latency: answer
quality and speed stop depending on CPU-bound local generation. Empty retrieval
short-circuits rather than asking a model to answer from nothing — that is how a
knowledge base starts inventing.

### D19 — A people registry, because models do not spell consistently

**Decision.** `people` + `person_aliases` tables. Every resolved name and every
person entity normalizes through `canonical_name()`. `pipeline people --merge`
rewrites history across speakers, entities, and both ends of every relation.

**Why.** Asking a model to spell a name the same way it did four months ago is not a
strategy. Each variant becomes a separate graph node, so one person recorded three
ways is three disconnected entities no query finds together. Unknown names pass
through unchanged — a new person is normal, and dropping them would be worse than an
unnormalized spelling.

**Rejected for now.** Voiceprints. pyannote emits speaker embeddings so it is
feasible, but this makes spelling deterministic, which was the actual failure.

### D20 — Speaker bounds are supplied, over-segmentation is surfaced

**Decision.** `MMC_MIN_SPEAKERS` / `MMC_MAX_SPEAKERS` are passed to pyannote when
set. An implausible count (>8) or zero speakers produces a warning.

**Why.** pyannote over-segments a noisy 1:1 into three or four speakers and
under-counts large meetings with overlapping speech, and diarization accuracy is the
main lever on whether action items get correct owners. Surfaced rather than
corrected: the fix is a bound or a better microphone, not a guess.

### D21 — Preflight as a command

**Decision.** `pipeline doctor` runs 18 environment checks, each with a fix.

**Why.** Almost everything that goes wrong here fails at *runtime*, and several
degrade quietly. The gated-model check is the important one: a HuggingFace token
proves nothing about licence acceptance, which is the part people miss, and missing
diarization costs every action item its owner while printing only a mid-batch
warning.

**What it deliberately does not claim.** That the output is good. It verifies the
environment; only a real meeting read against its audio verifies the minutes.

### D22 — Alerting is a command, not a feature

**Decision.** `MMC_ALERT_COMMAND` receives the failure summary on stdin, with
`{subject}` substituted.

**Why.** Exiting non-zero (D13) is necessary but not sufficient: cron mails the local
user and nobody reads local mail on a headless server. A command rather than
built-in email/webhook support because whatever the server already has beats a
second notification stack. Delivery never raises — an alerting failure masking the
pipeline failure it reports would be strictly worse than no alert.

## Known limitations

Accepted, with the reasoning:

- **Graph traversal is still local.** D17 moved entity identification to a frontier
  model and D18 moved answer writing, but the traversal between them runs on the
  small local model.
- **No voiceprints.** D19 makes spelling deterministic, not identification
  automatic.
- **Token estimation is `len // 4`.** Deliberately rough and biased conservative:
  over-estimating costs an unnecessary map-reduce pass, under-estimating costs a
  blown context window.
- **`seen_files` keys on path+size+mtime.** A file edited in place with identical
  size and mtime would be missed. This does not happen to finished recordings.
- **Nothing has run against real audio, LightRAG, or the CLI providers.** 165 tests
  cover logic, not integration. `pipeline doctor` verifies the environment; it
  cannot verify output quality.
