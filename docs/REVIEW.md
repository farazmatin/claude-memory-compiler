# Adversarial Review — Findings & Status

Review of the initial implementation across architecture, code, design, features,
scalability and deployment. Kept in the repo because a finding with its reasoning
attached is worth more than a fixed line of code with neither, and because the
residual-risk section is the honest state of the system.

**Reviewed:** 2026-08-12 · **Findings:** 20 · **Addressed:** 20 · **Open:** 0

All twenty findings have been worked. Two carry residual caveats that code cannot
remove — see [Residual risk](#residual-risk). Nothing here is a to-do list any
more; it is the record of what was wrong and what was done about it.

---

## Fixed

### Blocking — would have failed silently in production

| # | Finding | Fix | Test |
|---|---|---|---|
| B1 | `_run_all` discarded every stage's return code and returned 0. A nightly cron reported **success after total failure** — a break in month 4 would surface in month 9 as an empty query. | Stage results aggregated; per-stage crashes caught; non-zero on any failure. | — |
| B2 | Nothing recorded LightRAG's document id, so re-indexing a recompiled document **inserted a second copy** and left the old entities in the graph. This silently invalidated the recompilation guarantee that justifies retaining transcripts at all. | `lightrag_doc_id` in the manifest (with migration); id derived locally as LightRAG derives it; delete-before-insert; **abandon the insert if the delete fails**. | `test_index.py` (6) |
| B3 | `stale_template` matched on transcript presence alone, so `--recompile` could take a meeting still at `transcribed` straight to `minutes_compiled` — **skipping speaker resolution** and producing minutes whose action items were owned by `SPEAKER_01`. | Restricted to `speakers_resolved` and later. | `test_db.py::test_stale_template_requires_speakers_resolved` |
| B4 | LightRAG published on `0.0.0.0` with the API key defaulting to empty — an **unauthenticated, network-exposed index** of every decision and customer conversation in the corpus. | All services bound to loopback; compose fails fast without `MMC_LIGHTRAG_API_KEY`; SSH-tunnel instructions documented. | — |

**B2 is worth re-reading.** The first draft of its fix was itself wrong: it called
`insert_text` before checking whether the delete succeeded, creating exactly the
duplicate the function existed to prevent. Caught by the test asserting no insert
occurs on a failed delete.

### Serious

| # | Finding | Fix | Test |
|---|---|---|---|
| C1 | Prior-decision context compared **dates only**, so at five meetings a day none could see the other four — a decision reversed after lunch was never flagged. | `(date, time)` tuple comparison; self excluded. | `test_db.py::test_prior_context_sees_earlier_same_day_meetings` |
| C2 | Prior context selected by **recency, not relevance**, so long-horizon reversals — the valuable case — were systematically missed. | Topical LightRAG retrieval merged with the chronological window. Safe by construction: minutes compile before indexing, so every hit is necessarily earlier. | `test_compile_minutes.py` (5) |
| C3 | **No prompt-size guard.** A three-hour meeting was unbounded input. | Token budget; map-reduce over speaker-turn-aligned windows; failed windows leave a visible marker. | `test_compile_minutes.py` (7) |
| C4 | LightRAG on **file-based storage** cannot hold ~9k documents, and migrating a populated graph is painful. | Postgres from day one via `PGTableGraphStorage` — plain tables, no Apache AGE, so the standard pgvector image suffices. | — |
| C5 | **No backup mechanism** for the only irreplaceable data. | `pipeline backup` using SQLite's online backup API with integrity check; incremental tree sync; documented restore. | `test_backup.py` (8) |
| C6 | `guess_from_filename` assumed the **dominant speaker was the recorder** — wrong for stakeholder interviews, user research and demos, producing confidently reversed names. It contradicted the module's own no-guessing rule. | Replaced with `candidates_from_filename`: supplies candidate names, never a label mapping. The LLM decides from who introduces themselves. | `test_speakers.py` (5) |

### Worth fixing

| # | Finding | Fix |
|---|---|---|
| W1 | Tests lived in a scratchpad, uncommitted. | 165 tests in `tests/`, each regression test naming the failure it prevents. |
| W2 | No CI — tests only ran when someone remembered. | GitHub Actions on push and PR: pytest + ruff. |
| W3 | `whisperx>=3.1.1` unpinned upward, though its diarization API moves between releases. | Capped below 4. |
| W4 | Ruff ruleset unpinned — a release could add rules and break CI on an unrelated push. | Explicit `select` list, with documented ignores. |
| W5 | Inbox re-hashed nightly and grows forever — ~165 GB read per night by year five. | `seen_files` table on path+size+mtime; known files skipped unread. |
| W6 | `ingest --then-run` ingested twice. | Chain skips the ingest it just ran. |

One ruff finding was **not** applied: `SIM118` on `db.py` wanted `key in dict`
instead of `.keys()`, but iterating a `sqlite3.Row` yields values, not keys.
Applying it would have silently broken row conversion. Suppressed with the reason
inline.

---

## Previously open — now addressed

These were deferred in the first pass and have since been built. Each notes what
code cannot fix.

### O1 — The subscription ceiling ✔ mitigated

No subscription can serve LightRAG (it needs an HTTP endpoint) and none offers an
embedding model, so graph extraction runs on a ~4B local model on CPU. That
constraint is permanent. Both designed mitigations are now built:

**O1a — the compiler emits the graph.** `pipeline/entities.py` parses explicit
`Entities` and `Relations` sections from the minutes, canonicalizes person names
through the people registry, stores them in the manifest, and appends a normalized
block to the indexed document. The reasoning: a small model reads
`Atlas (feature): the platform rewrite` reliably and discovers the same fact from
narrative prose unreliably. The frontier model that compiles the minutes now does
that work once, and the local model only has to read it.

The parser is deliberately tolerant of shape (`-` bullets, `[]` brackets, `->`/`→`/`|`
arrows, missing descriptions) because models produce all of those for one
instruction, and recovering most of a messy block beats discarding all of a
slightly-malformed one.

**O1b — retrieval and synthesis are split.** `pipeline/answer.py` retrieves via
LightRAG and hands the context to the subscription chain to write the answer.
`--local` keeps LightRAG's own generation for comparison, and it is the automatic
fallback when no provider is reachable — an answer from the small model beats no
answer. Empty retrieval short-circuits rather than asking a model to answer from
nothing, which is how a knowledge base starts inventing.

*Residual:* the manifest copy of entities means the corpus is no longer dependent on
LightRAG's extraction quality, but graph *traversal* still happens inside LightRAG.

### O2 — Verification ✔ made possible, not completed

`pipeline doctor` runs 18 checks: ffmpeg/ffprobe, whisperx importability, the ASR
model against the device, **HF token plus actual gated-model reachability**, every
provider in the chain, LightRAG health and whether its storage backend is
file-based, Ollama models, directories, disk headroom, manifest state, and glossary
depth. Each failure carries the fix.

The gated-model check matters most: a token proves nothing about licence acceptance,
which is the part people miss, and missing diarization costs every action item its
owner while printing only a warning nobody reads mid-batch.

*Residual, and it cannot be closed by code:* `doctor` verifies the environment is
ready. It does not verify the output is good. Transcription accuracy, diarization
quality and graph usefulness require running one real meeting and reading the
result. The CLI invocations (`gemini -p -`, `codex exec -`) and the LightRAG delete
endpoint shape remain unconfirmed against live services; all are env-overridable or
try multiple shapes.

### O3 — Speaker identity ✔ made deterministic

A `people` table with `person_aliases` now normalizes every resolved name.
`db.canonical_name()` maps any known alias to one canonical spelling; the resolver
normalizes before persisting and registers new people for next time.
`pipeline people --merge Mike Michael` folds a duplicate and **rewrites history** —
speakers, entities, and both ends of every relation — because leaving those behind
keeps two graph nodes for one person.

Unknown names pass through unchanged rather than being rejected: a new person
appearing is normal, and dropping them would be worse than an unnormalized spelling.

*Residual:* still no voiceprints. This makes spelling deterministic, not
identification automatic. pyannote emits speaker embeddings, so voiceprint matching
remains a feasible extension.

### O4 — Query latency ✔ made measurable

`pipeline query --timing` reports retrieval and synthesis separately, so when
queries get slow the number says which phase is responsible. The split itself (O1b)
also decouples answer generation from CPU-bound local inference, which was the
larger part of the projected latency.

*Residual:* actual latency at 9k documents remains unmeasured because the corpus is
empty. The instrumentation is what was missing; the measurement needs data.

### O5 — Diarization on large meetings ✔ bounded

`MMC_MIN_SPEAKERS` / `MMC_MAX_SPEAKERS` are passed to pyannote when set, with a
`TypeError` fallback for versions that do not accept them. `Transcript.diarization_warning`
flags an implausible speaker count (>8 by default) as likely over-segmentation, and
zero speakers as "action items will have no owners". Surfaced rather than corrected:
the fix is a bound or a better microphone, not a guess.

*Residual:* Sortformer v2 and DiariZen benchmark better on meeting audio but are
GPU-oriented, so they stay out of a CPU-only deployment.

### O6 — Failure alerting ✔ built

`MMC_ALERT_COMMAND` is invoked when the nightly batch fails, with the summary on
**stdin** and `{subject}` substituted. Deliberately a command rather than built-in
email or webhook support: whatever the server already has (`mail`, `curl` to ntfy,
`systemd-cat`) beats a second notification stack to configure and maintain.

The summary names the failed stages, includes crash detail, points at
`status`/`doctor`/`retry`, and states that nothing is lost — a 3am alert should not
imply data loss when stages are resumable. Alert delivery never raises: an alerting
failure masking the pipeline failure it reports would be strictly worse than no
alert.

---

## Residual risk

Two things remain true that no amount of code changes:

1. **Nothing has been run against real audio, real LightRAG, or the real CLIs.**
   165 tests cover logic. `pipeline doctor` will tell you the environment is sound.
   Neither tells you the minutes are any good. That requires one real meeting, read
   against the audio, counting errors only on names, product terms, numbers and
   dates.
2. **Graph traversal quality is still bounded by a small local model.** O1a moves
   entity *identification* upstream to a frontier model, which is the larger half of
   the problem, but the traversal that answers a question still runs locally.

---

## Not defects

Reviewed and deliberately kept:

- **Three-tier split, minutes-only indexing** — the reasoning holds.
- **Copy-never-move from the inbox** — correct and non-obvious; moving would
  propagate deletes upstream and destroy originals.
- **`large-v3-turbo` on CPU** — forced by arithmetic, not preference.
- **Content-hash dedup** — validated against real duplicate files in the source
  folder.
- **Stage-machine resumability** — the right shape when ASR costs 40 minutes.
- **`Backend` protocol** — a real, cheap escape hatch from the CPU constraint.
- **Portable markdown corpus** — the strongest property in the design. LightRAG is
  replaceable; the data is never hostage to the index.
