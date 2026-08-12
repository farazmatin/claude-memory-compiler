# Adversarial Review — Findings & Status

Review of the initial implementation across architecture, code, design, features,
scalability and deployment. Kept in the repo because the *open* section is the
honest state of the system, and because a finding with its reasoning attached is
worth more than a fixed line of code with neither.

**Reviewed:** 2026-08-12 · **Findings:** 20 · **Fixed:** 14 · **Open:** 6

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
| W1 | Tests lived in a scratchpad, uncommitted. | 102 tests in `tests/`, each regression test naming the failure it prevents. |
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

## Open

Not fixed. Each carries why it was deferred rather than merely being listed.

### O1 — The subscription ceiling (architectural)

No subscription can serve LightRAG, which needs an HTTP endpoint, and none offers
an embedding model. So graph and entity extraction — the most quality-sensitive
step in retrieval — is permanently capped by a ~4B local model on CPU.

Two designed mitigations: have the subscription-backed compiler emit entities *and
relations* for deterministic indexing, and split retrieval from synthesis
(`index.query_context()` is already the hook). Deferred because both are
meaningful work and neither is knowable as necessary until the local model's
extraction quality has been observed on real minutes.

### O2 — Nothing verified against real infrastructure

ASR, diarization, LightRAG, Postgres, and both CLI providers are **unrun**. The 102
tests cover logic; there is no integration coverage. Specifically unverified:
`gemini -p -` and `codex exec -` invocations, the LightRAG delete endpoint shape,
and whether `only_need_context` is exposed by the server.

Mitigated rather than fixed: CLI binaries and arguments are env-overridable, the
delete path tries two endpoint shapes, and `query_context` degrades to `""`.

### O3 — Speaker identity is per-meeting

No voiceprints, no global registry. Cross-year spelling consistency depends on the
resolver re-picking the same name from `known_speaker_names`. Inconsistent
spellings fragment graph entities. pyannote emits speaker embeddings, so a
persistent registry is feasible — deferred as a larger feature than the review's
scope.

### O4 — Query latency at scale is unmeasured

`global`-mode traversal over 50-150k entities with CPU-bound generation could take
minutes per question. A knowledge base you wait three minutes for is one you stop
using. Postgres (C4) addresses storage but not generation speed; the
retrieval/synthesis split (O1) is the real fix. Unmeasurable until the corpus is
large.

### O5 — Diarization degrades on large meetings

No `min_speakers` / `max_speakers` hint is passed, and pyannote degrades on
overlapping speech and high speaker counts. Sortformer v2 and DiariZen benchmark
better on meeting audio but are GPU-oriented. Deferred pending measurement on real
recordings.

### O6 — No failure alerting

`pipeline run` now exits non-zero (B1), but nothing *notifies*. Cron mails output by
default on many systems, which may suffice; if not, this needs a push or email hook.
Deferred because the fix depends on how the server is actually operated.

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
