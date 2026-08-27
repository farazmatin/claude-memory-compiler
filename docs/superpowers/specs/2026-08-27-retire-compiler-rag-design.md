# Retiring the compiler's own RAG

**Status:** design approved, implementation not started
**Supersedes:** the local-Ollama arrangement described in `AGENTS.md` and
`pipeline/graph_sync.py`

## Why

The compiler runs a knowledge stack that has never worked on this machine, and
a second one already works next door.

Measured on 2026-08-27, against the live `mmc-lightrag`:

- 129 of 129 documents sit in `status=failed`, the oldest from 2026-08-15.
- One extraction call to `qwen3:4b` returned HTTP 500 after **exactly 30m0s**,
  the configured `LLM_TIMEOUT`. Ollama logs one such call per document.
- Direct measurement of the model: **1.54 tok/s generation, 2.53 tok/s prompt
  eval**, with `offloaded 0/37 layers to GPU` and `100% CPU`. A LightRAG
  extraction prompt of ~3,000 tokens needs roughly twenty minutes before it
  emits a single token.
- The host GPU is an MX350 with 2 GB VRAM. `qwen3:4b` is 2.5 GB. Nothing will
  offload, so no configuration change rescues this.

The pipeline already routes around the failure. `graph_sync.py` authors the
graph from the manifest instead of asking the local model to rediscover it, and
`answer.py` retrieves without an LLM. `graph_sync.py:229` states the intent
plainly: *"No local model sits anywhere in the answer path."* What remains is
the infrastructure that intent was built to avoid.

Meanwhile the Product Manager repo indexed **120 of 126 documents in 27
minutes** on 2026-08-20 through the same LightRAG software, because its provider
router uses subscription CLIs (`antigravity` → `claude` → `codex` → `gemini`)
rather than a local model. PM has since made that index legacy and serves the
**statement catalogue** with A13 gating as primary.

So the compiler is not gaining a RAG by this change. It is deleting a broken one
and delivering its output to the working one.

## Decisions taken

### The compiler stops answering questions

The compiler becomes capture → transcribe → speakers → voices → minutes. No
retrieval, no synthesis, no chat. Q&A belongs to Product Manager, which has the
evidence gating, the statement catalogue, and the provider router to do it
properly. Two knowledge stores over one corpus is one store too many.

### Removing LightRAG empties the Docker stack

`postgres` is `pgvector` with `POSTGRES_USER=lightrag` and `POSTGRES_DB=rag`; it
exists only as LightRAG's storage. `ollama` exists only to serve LightRAG. With
LightRAG gone, all three containers go and the compiler no longer requires
Docker to run.

### Minutes enrichment survives the deletion

`compile_minutes.py:263` calls `index.query_context()` to pull topically related
prior meetings into the minutes prompt. That is LightRAG feeding the compiler's
core job, not its retrieval feature, and deleting it silently would degrade
minutes quality.

`answer.py:381` already contains `fallback_local_context()`, documented as
*"keyword scan of the minutes files directly on disk - no LLM, no LightRAG."*
That function moves to `pipeline/prior_context.py` and `compile_minutes` calls it
instead. The feature keeps working; the infrastructure does not come with it.

### The handoff is a durable outbox, not a fire-and-forget call

The compiler owns the minutes. PM ingestion is downstream of them. A push that
fails because PM is closed, mid-update, or erroring must never fail the run that
produced the minutes, and must never be silently dropped.

This mirrors `minute_rewrite_jobs`, which is already proven in this codebase and
whose failure mode is understood.

### Any write to a minutes file enqueues, not only creation

The merge repair applied on 2026-08-26 rewrote 86 minutes files in place. Under a
create-only trigger PM would serve pre-repair spellings indefinitely. Creation,
recompile, and merge rewrite all enqueue.

### Local ASR stops being reachable by accident

`MMC_ASR_BACKEND` defaults to `auto`, which selects Replicate when a token is
present and **local CPU WhisperX when it is not**. On this hardware that is a
silent trap: an expired token turns a two-minute transcription into an
unbounded CPU job. The default becomes `replicate`, and a missing or rejected
token fails loudly.

## A. Module interface

The seam lives in `pipeline/pm_handoff.py`. `compile_minutes`, `people_merge`,
and the CLI are adapters. None of them reproduces the ordering or talks to PM
directly.

```python
@dataclass(frozen=True)
class HandoffJob:
    id: str
    meeting_id: str
    minutes_path: str
    reason: str            # "created" | "recompiled" | "rewritten"
    state: str             # "pending" | "sent" | "failed"
    attempts: int
    error: str | None
    created_at: str
    sent_at: str | None


@dataclass(frozen=True)
class HandoffResult:
    sent: int
    failed: int
    pending: int
    skipped_disabled: int


def enqueue(conn, meeting_id: str, minutes_path: Path, *, reason: str) -> None: ...
def drain(*, limit: int | None = None) -> HandoffResult: ...
def pending_count() -> int: ...
def is_configured() -> bool: ...
```

`enqueue()` takes the caller's open connection and performs no I/O to PM, so it
can sit inside the same transaction that records the minutes. `drain()` owns
every subprocess call.

## B. Outbox schema

```sql
CREATE TABLE pm_ingest_jobs (
    id            TEXT PRIMARY KEY,
    meeting_id    TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    minutes_path  TEXT NOT NULL,
    reason        TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL,
    sent_at       TEXT
);

CREATE INDEX pm_ingest_jobs_pending ON pm_ingest_jobs(state, created_at);
```

`id` is a digest of `(meeting_id, minutes_path, content_sha256)`. Re-enqueueing
identical content is therefore a no-op, while a rewrite of the same path
produces a new job. A meeting may have many jobs over its life; only the newest
`sent` one describes what PM currently holds.

## C. Delivery

`drain()` invokes PM's current ingestion, not the frozen LightRAG snapshot:

```
<MMC_PM_PYTHON> -m pm_agent_core.cli process <minutes_path> \
    --title <title_hint> --attendees <comma-separated>
```

Run with `cwd=MMC_PM_REPO`. `pm_agent_core.cli process` accepts a single
markdown meeting file and feeds the statement catalogue.

Bulk recovery uses PM's own idempotent path, which reports already-ingested
files as skipped rather than duplicating them:

```
<MMC_PM_PYTHON> -m pm_agent_core.cli ingest <MINUTES_DIR> --pattern '*.md'
```

Delivery rules:

- A non-zero exit leaves the job `pending` with `attempts` incremented and
  `error` set to the last 500 characters of stderr.
- A job that has failed `MMC_PM_MAX_ATTEMPTS` times (default 5) becomes
  `failed` and is reported, never retried silently forever.
- `drain()` never raises into the minutes stage. It returns counts.
- The subprocess inherits a scrubbed environment: no `*_API_KEY`, matching PM's
  own guarantee that it cannot fall through to metered billing.

## D. Configuration

| variable | default | meaning |
|---|---|---|
| `MMC_PM_REPO` | unset | absolute path to the Product Manager repository |
| `MMC_PM_PYTHON` | `<MMC_PM_REPO>/.venv/Scripts/python.exe` | interpreter used for ingestion |
| `MMC_PM_ENABLED` | `1` | set `0` to queue without delivering |
| `MMC_PM_MAX_ATTEMPTS` | `5` | attempts before a job is marked `failed` |

When `MMC_PM_REPO` is unset the outbox still fills. `pipeline status` and
`pipeline doctor` report the backlog. An unconfigured handoff is a visible
backlog, never a silent no-op.

## E. What is removed

| removed | reason |
|---|---|
| `docker-compose.yml` (all three services) | nothing left to run |
| `pipeline/index.py` | LightRAG client |
| `pipeline/graph_sync.py` | authored the graph the compiler no longer keeps |
| `pipeline/answer.py` | Q&A moves to PM |
| `pipeline index`, `graph-sync`, `query` | same |
| dashboard ask box and `dashboard.ask()` (`dashboard.py:949`) with its route | Q&A moves to PM |
| `chat_turns` table writes | no chat to record |
| `check_ollama`, LightRAG and graph checks in `doctor.py` | nothing to check |
| `tests/test_index.py`, `test_graph_sync.py`, `test_answer.py` | test removed modules |
| `LIGHTRAG_*`, `POSTGRES_*`, Ollama settings in `config.py` and `.env.example` | unused |

`ollama_data` and `postgres_data` volumes are dropped. The 824-entity graph is
discarded; it was authored from the manifest, which remains the source of truth,
and PM's store is independent of it.

`chat_turns` rows are retained in the manifest rather than dropped — deleting a
user's history is not required by this change and is not reversible.

## F. What is added

| added | purpose |
|---|---|
| `pipeline/pm_handoff.py` | the seam |
| `pipeline/prior_context.py` | lifted `fallback_local_context` |
| `pipeline pm-sync` | drain pending jobs; `--backfill` uses PM's bulk ingest |
| `pm_ingest_jobs` table | durable outbox |
| doctor check `pm handoff` | reports configuration and backlog |

`pipeline status` gains one line: pending and failed handoff counts.

## G. Order of operations

Each step leaves the tree working and tested.

1. Lift `fallback_local_context` into `prior_context.py`; point
   `compile_minutes` at it. Minutes stop depending on LightRAG.
2. Add the `pm_ingest_jobs` schema and `pm_handoff` with delivery disabled.
3. Enqueue from `compile_minutes` and from the merge rewrite path.
4. Add `pipeline pm-sync` and the doctor check; enable delivery.
5. Backfill the existing corpus once, and confirm PM's catalogue holds it.
6. Only then delete `index.py`, `graph_sync.py`, `answer.py`, the CLI commands,
   the dashboard ask box, and their tests.
7. Delete `docker-compose.yml`, the config entries, and stop the containers.

Deletion is last. Until PM demonstrably holds the corpus, the old path stays
where it is, unused but recoverable.

## H. Testing

- `prior_context` keeps the behaviour tests that covered
  `fallback_local_context`, moved rather than rewritten.
- `pm_handoff` tests inject the subprocess runner. No test invokes PM.
  Covered: enqueue inside the caller's transaction; identical content does not
  re-enqueue; a rewrite of the same path does; non-zero exit leaves the job
  pending with the error recorded; attempts cap moves a job to `failed`;
  `drain()` never raises; a scrubbed environment reaches the child; disabled
  delivery queues without calling out.
- Crash recovery mirrors the rewrite-job tests: killed mid-push, PM absent, PM
  erroring, duplicate push is a no-op.
- ASR: `auto` with a token selects Replicate; without a token it fails with an
  actionable message and never selects WhisperX; explicit `whisperx` still works.
- Removal is verified by absence: no module imports `index`, `graph_sync`, or
  `answer`; `pipeline --help` lists no retrieval command; the repository
  contains no reference to `11434`, `9621`, or `qwen3`.

## Out of scope

- Changing anything inside the Product Manager repository. The compiler adapts
  to PM's existing CLI; PM is not modified by this work.
- PM's own retrieval quality, gating, or catalogue schema.
- Re-ingesting into the frozen Aug-20 LightRAG snapshot. It stays frozen.
- Transcription behaviour beyond the backend-selection default.
- The voice and speaker stages, which use vectors returned by the diarization
  backend and run no local model of their own.

## Completion criteria

1. The compiler runs end to end with no Docker daemon present.
2. `grep -rE '11434|9621|qwen3|mxbai|lightrag'` over `pipeline/` returns nothing.
3. Every minutes write enqueues a handoff job, including merge rewrites.
4. A PM outage leaves minutes intact and the backlog visible in `status` and
   `doctor`.
5. `pipeline pm-sync --backfill` lands the existing corpus in PM's catalogue.
6. A missing Replicate token fails transcription loudly and never starts a local
   model.
7. The full offline suite passes, with pre-existing failures reported separately.
