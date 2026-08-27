# Retiring the Compiler's Own RAG — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the compiler's non-functional LightRAG/Ollama/Postgres stack and deliver every minutes write to Product Manager's working statement catalogue through a durable outbox.

**Architecture:** A new deep module, `pipeline/pm_handoff.py`, owns the outbox: enqueue inside the caller's transaction, deliver by subprocess to PM's CLI. `compile_minutes`, `people_merge`, and the CLI are adapters. Minutes enrichment moves to `pipeline/prior_context.py`, a lifted keyword scan that needs no server. Deletion of the old retrieval stack happens last, only after PM demonstrably holds the corpus.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, `hashlib`, `subprocess`, `os`, `pathlib`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-08-27-retire-compiler-rag-design.md`

## Global Constraints

- Use `config.DB_PATH`; never open a bare `db/manifest.db`.
- Transcripts are immutable. No code in this plan writes to `TRANSCRIPTS_DIR`.
- Run Python tests with `./.venv/Scripts/python.exe -m pytest`.
- Run lint with `uvx ruff check` and report unrelated baseline failures separately.
- No unit test may invoke Product Manager. The subprocess runner is injected.
- No unit test may contact LightRAG, Ollama, or Replicate.
- New tables are added by appending `CREATE TABLE IF NOT EXISTS` to `db.SCHEMA`. `db.MIGRATIONS` is keyed on **`meetings` columns only** — a new table needs no entry there.
- Deletion tasks (9, 10) run only after Task 8 confirms PM holds the corpus. Do not reorder.
- `meetings.lightrag_doc_id` and its `MIGRATIONS` entry stay. Dropping a SQLite column requires a full table rebuild and buys nothing; the column becomes inert.
- `chat_turns` rows are retained. Deleting a user's history is not required by this change and is not reversible.
- Known pre-existing failures, not caused by this work: 4 tests in `tests/test_answer.py` (alert return values), 1 in `tests/test_ingest.py` (filename parse), 1 in `tests/test_llm.py` (provider order). Record actual results at Task 0; do not encode this list as an expected count.

## File Structure

| file | responsibility |
|---|---|
| `pipeline/prior_context.py` | **new.** Keyword scan of `MINUTES_DIR` for topically related prior minutes. No LLM, no server. |
| `pipeline/pm_handoff.py` | **new.** The seam: outbox enqueue, delivery, attempt accounting, configuration reporting. |
| `pipeline/db.py` | **modify.** `pm_ingest_jobs` table plus row-level helpers. Owns no workflow. |
| `pipeline/compile_minutes.py` | **modify.** Calls `prior_context` instead of `index`; enqueues after writing minutes. |
| `pipeline/people_merge.py` | **modify.** Enqueues after a merge rewrite replaces a minutes file. |
| `pipeline/cli.py` | **modify.** Adds `pm-sync`; removes `index`, `graph-sync`, `query`. |
| `pipeline/doctor.py` | **modify.** Adds `pm handoff`; removes Ollama/LightRAG/graph checks. |
| `pipeline/asr.py`, `pipeline/config.py` | **modify.** Backend default becomes `replicate`; missing token fails loudly. |
| `pipeline/dashboard.py` | **modify.** Removes the ask box and its route. |
| `tests/test_prior_context.py`, `tests/test_pm_handoff.py` | **new.** |
| `tests/conftest.py` | **modify.** The autouse LightRAG-blocking fixture must go in the same commit as the module deletion. |

---

### Task 0: Establish a safe baseline

**Files:** none.

- [ ] **Step 1: Record the working tree**

```bash
git status --short
git log --oneline -3
```

- [ ] **Step 2: Run the baseline suite and record exact failures**

```bash
./.venv/Scripts/python.exe -m pytest -q
```

Record failing test *names*, not a count. These are the pre-existing failures every later task compares against.

- [ ] **Step 3: Record the corpus size for the Task 8 backfill check**

```bash
./.venv/Scripts/python.exe -c "
import sqlite3
from pipeline import config
c = sqlite3.connect(f'file:{config.DB_PATH}?mode=ro', uri=True)
print('meetings:', c.execute('SELECT COUNT(*) FROM meetings').fetchone()[0])
print('with minutes:', c.execute('SELECT COUNT(*) FROM meetings WHERE minutes_path IS NOT NULL').fetchone()[0])
"
ls pipeline/../minutes/*.md | wc -l
```

**Checkpoint:** no commit; this task changes nothing.

---

### Task 1: Lift the keyword scan into `prior_context`

**Files:**
- Create: `pipeline/prior_context.py`
- Create: `tests/test_prior_context.py`
- Modify: `pipeline/compile_minutes.py:260-268`

**Interfaces:**
- Consumes: `config.MINUTES_DIR`
- Produces: `prior_context.related_minutes(query: str, top_n: int = 5) -> str`

This task removes the minutes stage's dependency on LightRAG. Nothing is deleted yet.

- [ ] **Step 1: Write the failing tests**

```python
"""Topical prior-minutes lookup, with no server behind it."""

from __future__ import annotations

import pytest

from pipeline import config, prior_context


@pytest.fixture()
def minutes_dir(tmp_path, monkeypatch):
    directory = tmp_path / "minutes"
    directory.mkdir()
    monkeypatch.setattr(config, "MINUTES_DIR", directory)
    monkeypatch.setattr(prior_context, "MINUTES_DIR", directory, raising=False)
    return directory


def test_ranks_by_keyword_frequency(minutes_dir):
    (minutes_dir / "a.md").write_text("mongodb migration mongodb", encoding="utf-8")
    (minutes_dir / "b.md").write_text("mongodb once", encoding="utf-8")

    result = prior_context.related_minutes("What about the mongodb migration?")

    assert result.index("a.md") < result.index("b.md")


def test_ignores_question_words_so_every_file_is_not_a_match(minutes_dir):
    (minutes_dir / "a.md").write_text("unrelated content about nothing", encoding="utf-8")
    (minutes_dir / "b.md").write_text("crowdstrike rollout", encoding="utf-8")

    result = prior_context.related_minutes("What did we discuss about crowdstrike?")

    assert "b.md" in result
    assert "a.md" not in result


def test_falls_back_to_newest_when_nothing_matches(minutes_dir):
    (minutes_dir / "2026-01-01-old.md").write_text("alpha", encoding="utf-8")
    (minutes_dir / "2026-09-09-new.md").write_text("beta", encoding="utf-8")

    result = prior_context.related_minutes("zzzzz nonexistent term")

    assert "2026-09-09-new.md" in result


def test_returns_empty_string_when_there_are_no_minutes(minutes_dir):
    assert prior_context.related_minutes("anything") == ""


def test_unreadable_file_is_skipped_not_fatal(minutes_dir, monkeypatch):
    (minutes_dir / "good.md").write_text("crowdstrike", encoding="utf-8")
    (minutes_dir / "bad.md").write_text("crowdstrike", encoding="utf-8")
    real_read = type(minutes_dir / "good.md").read_text

    def explode(self, *args, **kwargs):
        if self.name == "bad.md":
            raise OSError("permission denied")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(type(minutes_dir / "good.md"), "read_text", explode)

    result = prior_context.related_minutes("crowdstrike")

    assert "good.md" in result
    assert "bad.md" not in result


def test_honours_top_n(minutes_dir):
    for i in range(6):
        (minutes_dir / f"f{i}.md").write_text("crowdstrike", encoding="utf-8")

    result = prior_context.related_minutes("crowdstrike", top_n=2)

    assert result.count("## f") == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_prior_context.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.prior_context'`

- [ ] **Step 3: Write the implementation**

Create `pipeline/prior_context.py`. This is `answer.py:381` moved, with two deliberate changes: it reads `MINUTES_DIR` from `config` at call time so tests can redirect it, and it returns `""` rather than raising when the directory is empty.

```python
"""Find earlier minutes related to a topic, without a server or a model.

The minutes stage used to ask LightRAG for this. LightRAG's extraction never
worked on this machine, and the retrieval it offered here was a keyword match
dressed up as semantic search. A direct scan of the minutes directory returns
the same shape of answer in ~0.03s with nothing to deploy.
"""

from __future__ import annotations

import re

from pipeline.config import MINUTES_DIR

# Question scaffolding that matches every file and therefore ranks nothing.
QUESTION_WORDS = frozenset({
    "what", "when", "where", "which", "about", "did", "were", "the", "and",
    "for", "with", "recent", "meetings", "meeting", "discuss", "discussed",
    "tell", "show", "summary",
})

EXCERPT_CHARS = 3500
FALLBACK_EXCERPT_CHARS = 3000
FALLBACK_FILES = 3


def keywords(query: str) -> list[str]:
    """Words worth scoring: three or more characters, not question scaffolding."""
    return [
        word.lower()
        for word in re.findall(r"\b[a-zA-Z0-9_-]{3,}\b", query)
        if word.lower() not in QUESTION_WORDS
    ]


def related_minutes(query: str, top_n: int = 5) -> str:
    """The `top_n` minutes files most densely matching `query`.

    Returns "" when there are no minutes at all. Falls back to the newest few
    files when there are minutes but none match, because "here is what is
    recent" is more useful to a minutes prompt than silence.
    """
    from pipeline import config as _config

    directory = getattr(_config, "MINUTES_DIR", MINUTES_DIR)
    terms = keywords(query)

    scored: list[tuple[int, str, str]] = []
    for path in sorted(directory.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        score = sum(content.lower().count(term) for term in terms) if terms else 1
        if score > 0:
            scored.append((score, path.name, content))

    if scored:
        scored.sort(key=lambda row: (-row[0], row[1]))
        return "\n\n---\n\n".join(
            f"## {name}\n{text[:EXCERPT_CHARS]}" for _, name, text in scored[:top_n]
        )

    newest = sorted(directory.glob("*.md"), reverse=True)[:FALLBACK_FILES]
    if not newest:
        return ""
    parts = []
    for path in newest:
        try:
            parts.append(
                f"## {path.name}\n{path.read_text(encoding='utf-8')[:FALLBACK_EXCERPT_CHARS]}"
            )
        except OSError:
            continue
    return "\n\n---\n\n".join(parts)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_prior_context.py -q`
Expected: PASS

- [ ] **Step 5: Point `compile_minutes` at it**

In `pipeline/compile_minutes.py`, replace lines 260-268:

```python
    if dialogue:
        from pipeline import index

        retrieved = index.query_context(build_topic_query(meeting, dialogue)).strip()
        if retrieved:
            blocks.append(
                "### Topically related earlier material\n\n"
                f"{retrieved[:TOPICAL_EXCERPT_CHARS]}"
            )
```

with:

```python
    if dialogue:
        from pipeline import prior_context

        retrieved = prior_context.related_minutes(
            build_topic_query(meeting, dialogue)
        ).strip()
        if retrieved:
            blocks.append(
                "### Topically related earlier material\n\n"
                f"{retrieved[:TOPICAL_EXCERPT_CHARS]}"
            )
```

- [ ] **Step 6: Verify the minutes suite still passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_compile_minutes.py tests/test_prior_context.py -q`
Expected: PASS. `tests/test_compile_minutes.py` monkeypatches `index` at lines 251, 266, 276 — those patches now target a module the code no longer calls, so update them to patch `prior_context.related_minutes` instead.

- [ ] **Step 7: Commit**

```bash
git add pipeline/prior_context.py tests/test_prior_context.py pipeline/compile_minutes.py tests/test_compile_minutes.py
git commit -m "Compile minutes without asking LightRAG for prior context"
```

---

### Task 2: Add the outbox table and row helpers

**Files:**
- Modify: `pipeline/db.py` (append to `SCHEMA`; add helpers near `minute_rewrite_jobs` helpers)
- Create: `tests/test_pm_handoff_schema.py`

**Interfaces:**
- Produces:
  - `db.pm_job_id(meeting_id: str, minutes_path: str, content_sha256: str) -> str`
  - `db.record_pm_job(conn, *, job_id, meeting_id, minutes_path, reason, created_at) -> bool`
  - `db.pending_pm_jobs(conn, limit: int | None = None) -> list[sqlite3.Row]`
  - `db.mark_pm_job_sent(conn, job_id: str, sent_at: str) -> None`
  - `db.mark_pm_job_attempt(conn, job_id: str, error: str, max_attempts: int) -> str`

`mark_pm_job_attempt` returns the resulting state (`"pending"` or `"failed"`), so the caller need not re-read the row.

- [ ] **Step 1: Write the failing tests**

```python
"""Outbox schema and row helpers. No delivery, no subprocess."""

from __future__ import annotations

from pipeline import config, db

from .conftest import make_meeting


def test_job_id_is_stable_for_identical_content():
    first = db.pm_job_id("m1", "/x/a.md", "deadbeef")
    second = db.pm_job_id("m1", "/x/a.md", "deadbeef")
    assert first == second


def test_job_id_changes_when_content_changes():
    assert db.pm_job_id("m1", "/x/a.md", "aaaa") != db.pm_job_id("m1", "/x/a.md", "bbbb")


def test_job_id_changes_when_path_changes():
    assert db.pm_job_id("m1", "/x/a.md", "aaaa") != db.pm_job_id("m1", "/x/b.md", "aaaa")


def test_recording_a_job_returns_true_and_stores_it(manifest):
    make_meeting(manifest, "m1", "2026-08-27")
    job_id = db.pm_job_id("m1", "/x/a.md", "aaaa")

    created = db.record_pm_job(
        manifest,
        job_id=job_id,
        meeting_id="m1",
        minutes_path="/x/a.md",
        reason="created",
        created_at=config.now_iso(),
    )

    assert created is True
    row = manifest.execute(
        "SELECT * FROM pm_ingest_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    assert row["reason"] == "created"


def test_recording_identical_content_twice_is_a_no_op(manifest):
    make_meeting(manifest, "m1", "2026-08-27")
    job_id = db.pm_job_id("m1", "/x/a.md", "aaaa")
    args = dict(
        job_id=job_id,
        meeting_id="m1",
        minutes_path="/x/a.md",
        reason="created",
        created_at=config.now_iso(),
    )

    assert db.record_pm_job(manifest, **args) is True
    assert db.record_pm_job(manifest, **args) is False
    assert manifest.execute("SELECT COUNT(*) FROM pm_ingest_jobs").fetchone()[0] == 1


def test_a_rewrite_of_the_same_path_records_a_second_job(manifest):
    make_meeting(manifest, "m1", "2026-08-27")
    for digest, reason in (("aaaa", "created"), ("bbbb", "rewritten")):
        db.record_pm_job(
            manifest,
            job_id=db.pm_job_id("m1", "/x/a.md", digest),
            meeting_id="m1",
            minutes_path="/x/a.md",
            reason=reason,
            created_at=config.now_iso(),
        )

    assert manifest.execute("SELECT COUNT(*) FROM pm_ingest_jobs").fetchone()[0] == 2


def test_pending_jobs_are_oldest_first_and_exclude_sent(manifest):
    make_meeting(manifest, "m1", "2026-08-27")
    for i, stamp in enumerate(("2026-08-27T01:00:00", "2026-08-27T02:00:00")):
        db.record_pm_job(
            manifest,
            job_id=f"job-{i}",
            meeting_id="m1",
            minutes_path=f"/x/{i}.md",
            reason="created",
            created_at=stamp,
        )
    db.mark_pm_job_sent(manifest, "job-0", config.now_iso())

    pending = db.pending_pm_jobs(manifest)

    assert [row["id"] for row in pending] == ["job-1"]


def test_an_attempt_records_the_error_and_stays_pending(manifest):
    make_meeting(manifest, "m1", "2026-08-27")
    db.record_pm_job(
        manifest,
        job_id="job-1",
        meeting_id="m1",
        minutes_path="/x/a.md",
        reason="created",
        created_at=config.now_iso(),
    )

    state = db.mark_pm_job_attempt(manifest, "job-1", "boom", max_attempts=5)

    assert state == "pending"
    row = manifest.execute("SELECT * FROM pm_ingest_jobs WHERE id = 'job-1'").fetchone()
    assert row["attempts"] == 1
    assert row["error"] == "boom"


def test_the_attempt_cap_moves_a_job_to_failed(manifest):
    make_meeting(manifest, "m1", "2026-08-27")
    db.record_pm_job(
        manifest,
        job_id="job-1",
        meeting_id="m1",
        minutes_path="/x/a.md",
        reason="created",
        created_at=config.now_iso(),
    )

    states = [
        db.mark_pm_job_attempt(manifest, "job-1", "boom", max_attempts=3)
        for _ in range(3)
    ]

    assert states == ["pending", "pending", "failed"]
    assert db.pending_pm_jobs(manifest) == []


def test_deleting_a_meeting_removes_its_jobs(manifest):
    make_meeting(manifest, "m1", "2026-08-27")
    db.record_pm_job(
        manifest,
        job_id="job-1",
        meeting_id="m1",
        minutes_path="/x/a.md",
        reason="created",
        created_at=config.now_iso(),
    )

    manifest.execute("PRAGMA foreign_keys = ON")
    manifest.execute("DELETE FROM meetings WHERE id = 'm1'")

    assert manifest.execute("SELECT COUNT(*) FROM pm_ingest_jobs").fetchone()[0] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_pm_handoff_schema.py -q`
Expected: FAIL with `AttributeError: module 'pipeline.db' has no attribute 'pm_job_id'`

- [ ] **Step 3: Append the table to `db.SCHEMA`**

Add immediately after the `minute_rewrite_jobs` block (`pipeline/db.py:75`), inside the same `SCHEMA` string:

```sql
CREATE TABLE IF NOT EXISTS pm_ingest_jobs (
    id            TEXT PRIMARY KEY,
    meeting_id    TEXT NOT NULL,
    minutes_path  TEXT NOT NULL,
    reason        TEXT NOT NULL,  -- created|recompiled|rewritten
    state         TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|failed
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL,
    sent_at       TEXT,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pm_ingest_pending
    ON pm_ingest_jobs(state, created_at);
```

Add no entry to `MIGRATIONS`: that dict is keyed on `meetings` columns, and `CREATE TABLE IF NOT EXISTS` already handles an upgraded install.

- [ ] **Step 4: Add the helpers**

```python
def pm_job_id(meeting_id: str, minutes_path: str, content_sha256: str) -> str:
    """Identity of one delivery: this meeting, this path, this exact content.

    Content is part of the identity so a recompile or a merge rewrite of the
    same path is a NEW job, while re-running a stage over unchanged minutes is
    not. That is what makes enqueueing safe to call unconditionally.
    """
    payload = "\x00".join((meeting_id, str(minutes_path), content_sha256))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_pm_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    meeting_id: str,
    minutes_path: str,
    reason: str,
    created_at: str,
) -> bool:
    """Insert one outbox row. Returns False when this exact job already exists."""
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO pm_ingest_jobs
            (id, meeting_id, minutes_path, reason, state, attempts, created_at)
        VALUES (?, ?, ?, ?, 'pending', 0, ?)
        """,
        (job_id, meeting_id, str(minutes_path), reason, created_at),
    )
    return cursor.rowcount > 0


def pending_pm_jobs(
    conn: sqlite3.Connection, limit: int | None = None
) -> list[sqlite3.Row]:
    """Undelivered jobs, oldest first. `failed` rows are excluded deliberately."""
    sql = (
        "SELECT * FROM pm_ingest_jobs WHERE state = 'pending' "
        "ORDER BY created_at, id"
    )
    if limit is None:
        return list(conn.execute(sql))
    return list(conn.execute(f"{sql} LIMIT ?", (limit,)))


def mark_pm_job_sent(conn: sqlite3.Connection, job_id: str, sent_at: str) -> None:
    conn.execute(
        "UPDATE pm_ingest_jobs SET state = 'sent', sent_at = ?, error = NULL "
        "WHERE id = ?",
        (sent_at, job_id),
    )


def mark_pm_job_attempt(
    conn: sqlite3.Connection, job_id: str, error: str, max_attempts: int
) -> str:
    """Record one failed delivery. Returns the resulting state.

    A job that has exhausted its attempts becomes `failed` rather than retrying
    forever: an unfixable job that stays pending hides every fixable one behind
    it in the queue.
    """
    conn.execute(
        "UPDATE pm_ingest_jobs SET attempts = attempts + 1, error = ? WHERE id = ?",
        (error[:500], job_id),
    )
    attempts = conn.execute(
        "SELECT attempts FROM pm_ingest_jobs WHERE id = ?", (job_id,)
    ).fetchone()["attempts"]
    if attempts >= max_attempts:
        conn.execute(
            "UPDATE pm_ingest_jobs SET state = 'failed' WHERE id = ?", (job_id,)
        )
        return "failed"
    return "pending"
```

Confirm `hashlib` is imported at the top of `db.py`; add it if not.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_pm_handoff_schema.py tests/test_db.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pipeline/db.py tests/test_pm_handoff_schema.py
git commit -m "Add a durable outbox for Product Manager ingestion"
```

---

### Task 3: Configuration and the handoff module's read-only half

**Files:**
- Modify: `pipeline/config.py`
- Create: `pipeline/pm_handoff.py`
- Create: `tests/test_pm_handoff.py`

**Interfaces:**
- Consumes: `db.pm_job_id`, `db.record_pm_job`, `db.pending_pm_jobs`
- Produces:
  - `pm_handoff.is_configured() -> bool`
  - `pm_handoff.pending_count() -> int`
  - `pm_handoff.failed_count() -> int`
  - `pm_handoff.enqueue(conn, meeting_id: str, minutes_path: Path, *, reason: str) -> bool`
  - `pm_handoff.HandoffResult` dataclass with `sent`, `failed`, `pending`, `skipped_disabled` ints

- [ ] **Step 1: Add configuration**

Append to `pipeline/config.py`, after the LLM provider block:

```python
# ── Product Manager handoff ───────────────────────────────────────────
# Minutes are the compiler's product; the knowledge base that answers questions
# about them lives in the Product Manager repository, which routes its models
# through the same CLI subscriptions and therefore actually completes an index.
# This compiler delivers minutes there and keeps no retrieval of its own.
PM_REPO = os.environ.get("MMC_PM_REPO", "").strip()
PM_PYTHON = os.environ.get("MMC_PM_PYTHON", "").strip()
PM_ENABLED = os.environ.get("MMC_PM_ENABLED", "1").strip() not in {"0", "false", "no"}
PM_MAX_ATTEMPTS = int(os.environ.get("MMC_PM_MAX_ATTEMPTS", "5"))
PM_TIMEOUT_SEC = float(os.environ.get("MMC_PM_TIMEOUT", "900"))


def pm_python() -> str:
    """Interpreter used to run Product Manager's CLI.

    Defaults to the venv inside PM_REPO rather than this compiler's own, because
    PM has its own dependency set and running its CLI on our interpreter fails
    in ways that look like a PM bug.
    """
    if PM_PYTHON:
        return PM_PYTHON
    if not PM_REPO:
        return ""
    return str(Path(PM_REPO) / ".venv" / "Scripts" / "python.exe")
```

- [ ] **Step 2: Write the failing tests for the read-only half**

```python
"""The Product Manager handoff seam."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pipeline import config, db, pm_handoff

from .conftest import make_meeting


@pytest.fixture()
def minutes_file(tmp_path):
    path = tmp_path / "2026-08-27-standup-abc123.md"
    path.write_text("# Standup\n\nAlice owns Atlas.\n", encoding="utf-8")
    return path


def test_not_configured_without_a_repo(monkeypatch):
    monkeypatch.setattr(config, "PM_REPO", "")
    assert pm_handoff.is_configured() is False


def test_configured_with_a_repo_and_interpreter(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PM_REPO", str(tmp_path))
    monkeypatch.setattr(config, "PM_PYTHON", str(tmp_path / "python.exe"))
    assert pm_handoff.is_configured() is True


def test_enqueue_records_a_job_for_the_file_content(manifest, minutes_file):
    make_meeting(manifest, "m1", "2026-08-27")

    created = pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")

    assert created is True
    expected = db.pm_job_id(
        "m1",
        str(minutes_file),
        hashlib.sha256(minutes_file.read_bytes()).hexdigest(),
    )
    row = manifest.execute(
        "SELECT * FROM pm_ingest_jobs WHERE id = ?", (expected,)
    ).fetchone()
    assert row["reason"] == "created"


def test_enqueueing_unchanged_content_twice_is_a_no_op(manifest, minutes_file):
    make_meeting(manifest, "m1", "2026-08-27")

    assert pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created") is True
    assert pm_handoff.enqueue(manifest, "m1", minutes_file, reason="recompiled") is False


def test_enqueueing_after_a_rewrite_records_a_new_job(manifest, minutes_file):
    make_meeting(manifest, "m1", "2026-08-27")
    pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")

    minutes_file.write_text("# Standup\n\nAlice Smith owns Atlas.\n", encoding="utf-8")

    assert pm_handoff.enqueue(manifest, "m1", minutes_file, reason="rewritten") is True
    assert manifest.execute("SELECT COUNT(*) FROM pm_ingest_jobs").fetchone()[0] == 2


def test_enqueue_of_a_missing_file_is_refused_not_recorded(manifest, tmp_path):
    make_meeting(manifest, "m1", "2026-08-27")

    created = pm_handoff.enqueue(
        manifest, "m1", tmp_path / "gone.md", reason="created"
    )

    assert created is False
    assert manifest.execute("SELECT COUNT(*) FROM pm_ingest_jobs").fetchone()[0] == 0


def test_counts_report_the_backlog(manifest, minutes_file, monkeypatch):
    make_meeting(manifest, "m1", "2026-08-27")
    pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")
    manifest.commit()
    monkeypatch.setattr(config, "DB_PATH", config.DB_PATH)

    assert pm_handoff.pending_count() == 1
    assert pm_handoff.failed_count() == 0
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_pm_handoff.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.pm_handoff'`

- [ ] **Step 4: Write the read-only half**

```python
"""Deliver minutes to the Product Manager statement catalogue.

The compiler owns the minutes. Product Manager owns the knowledge base that
answers questions about them: it routes models through CLI subscriptions and
completed a real index, where this repository's local-model attempt failed every
document it was ever given.

Delivery is an outbox rather than a call at the end of the minutes stage. PM may
be closed, mid-update, or erroring, and none of that is a reason to fail the run
that produced the minutes or to lose the fact that they need delivering.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pipeline import config, db


@dataclass(frozen=True)
class HandoffResult:
    sent: int = 0
    failed: int = 0
    pending: int = 0
    skipped_disabled: int = 0

    def summary(self) -> str:
        return (
            f"PM handoff: {self.sent} sent, {self.failed} failed, "
            f"{self.pending} pending, {self.skipped_disabled} skipped"
        )


def is_configured() -> bool:
    """True when a repository and an interpreter are both known."""
    return bool(config.PM_REPO) and bool(config.pm_python())


def enqueue(
    conn, meeting_id: str, minutes_path: Path, *, reason: str
) -> bool:
    """Record that this minutes file needs delivering. No I/O to PM.

    Takes the caller's connection so the job lands in the same transaction that
    recorded the minutes: a committed minutes file with no queued delivery is
    the one state this design must never produce.

    Returns False when the file is missing or this exact content is already
    queued.
    """
    path = Path(minutes_path)
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False

    return db.record_pm_job(
        conn,
        job_id=db.pm_job_id(meeting_id, str(path), digest),
        meeting_id=meeting_id,
        minutes_path=str(path),
        reason=reason,
        created_at=config.now_iso(),
    )


def pending_count() -> int:
    with db.connect() as conn:
        return len(db.pending_pm_jobs(conn))


def failed_count() -> int:
    with db.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM pm_ingest_jobs WHERE state = 'failed'"
        ).fetchone()[0]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_pm_handoff.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pipeline/config.py pipeline/pm_handoff.py tests/test_pm_handoff.py
git commit -m "Add the Product Manager handoff seam and its outbox enqueue"
```

---

### Task 4: Delivery

**Files:**
- Modify: `pipeline/pm_handoff.py`
- Modify: `tests/test_pm_handoff.py`

**Interfaces:**
- Produces:
  - `pm_handoff.build_command(job_row, title: str | None, attendees: str) -> list[str]`
  - `pm_handoff.child_env() -> dict[str, str]`
  - `pm_handoff.drain(*, limit: int | None = None, runner=None) -> HandoffResult`

`runner` is a callable `(cmd: list[str], cwd: str, env: dict, timeout: float) -> subprocess.CompletedProcess`. It defaults to a real `subprocess.run`; every test injects a fake.

- [ ] **Step 1: Write the failing delivery tests**

```python
def _fake_runner(returncode=0, stderr=""):
    calls = []

    def run(cmd, cwd, env, timeout):
        calls.append({"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout})
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    run.calls = calls
    return run


@pytest.fixture()
def configured_pm(monkeypatch, tmp_path):
    repo = tmp_path / "pm"
    repo.mkdir()
    monkeypatch.setattr(config, "PM_REPO", str(repo))
    monkeypatch.setattr(config, "PM_PYTHON", str(repo / "python.exe"))
    monkeypatch.setattr(config, "PM_ENABLED", True)
    monkeypatch.setattr(config, "PM_MAX_ATTEMPTS", 3)
    return repo


def test_delivery_calls_pm_process_with_the_minutes_path(
    manifest, minutes_file, configured_pm, monkeypatch
):
    make_meeting(manifest, "m1", "2026-08-27", title_hint="Standup")
    manifest.execute(
        "INSERT INTO speakers (meeting_id, label, name) VALUES ('m1','SPEAKER_00','Alice')"
    )
    pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")
    manifest.commit()
    runner = _fake_runner()

    result = pm_handoff.drain(runner=runner)

    assert result.sent == 1
    cmd = runner.calls[0]["cmd"]
    assert cmd[1:4] == ["-m", "pm_agent_core.cli", "process"]
    assert cmd[4] == str(minutes_file)
    assert "--title" in cmd and "Standup" in cmd
    assert "--attendees" in cmd and "Alice" in cmd
    assert runner.calls[0]["cwd"] == str(configured_pm)


def test_a_sent_job_is_not_delivered_twice(
    manifest, minutes_file, configured_pm
):
    make_meeting(manifest, "m1", "2026-08-27")
    pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")
    manifest.commit()

    assert pm_handoff.drain(runner=_fake_runner()).sent == 1
    second = _fake_runner()
    assert pm_handoff.drain(runner=second).sent == 0
    assert second.calls == []


def test_a_nonzero_exit_leaves_the_job_pending_with_the_error(
    manifest, minutes_file, configured_pm
):
    make_meeting(manifest, "m1", "2026-08-27")
    pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")
    manifest.commit()

    result = pm_handoff.drain(runner=_fake_runner(returncode=1, stderr="PM exploded"))

    assert result.sent == 0
    assert result.pending == 1
    row = manifest.execute("SELECT * FROM pm_ingest_jobs").fetchone()
    assert row["state"] == "pending"
    assert "PM exploded" in row["error"]


def test_the_attempt_cap_marks_the_job_failed(
    manifest, minutes_file, configured_pm
):
    make_meeting(manifest, "m1", "2026-08-27")
    pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")
    manifest.commit()

    for _ in range(3):
        pm_handoff.drain(runner=_fake_runner(returncode=1, stderr="nope"))

    row = manifest.execute("SELECT * FROM pm_ingest_jobs").fetchone()
    assert row["state"] == "failed"
    assert pm_handoff.drain(runner=_fake_runner()).sent == 0


def test_drain_never_raises_when_the_runner_explodes(
    manifest, minutes_file, configured_pm
):
    make_meeting(manifest, "m1", "2026-08-27")
    pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")
    manifest.commit()

    def explode(cmd, cwd, env, timeout):
        raise OSError("interpreter not found")

    result = pm_handoff.drain(runner=explode)

    assert result.pending == 1
    assert manifest.execute(
        "SELECT state FROM pm_ingest_jobs"
    ).fetchone()["state"] == "pending"


def test_a_timeout_leaves_the_job_pending(manifest, minutes_file, configured_pm):
    make_meeting(manifest, "m1", "2026-08-27")
    pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")
    manifest.commit()

    def time_out(cmd, cwd, env, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    result = pm_handoff.drain(runner=time_out)

    assert result.pending == 1


def test_disabled_delivery_queues_without_calling_out(
    manifest, minutes_file, configured_pm, monkeypatch
):
    monkeypatch.setattr(config, "PM_ENABLED", False)
    make_meeting(manifest, "m1", "2026-08-27")
    pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")
    manifest.commit()
    runner = _fake_runner()

    result = pm_handoff.drain(runner=runner)

    assert result.skipped_disabled == 1
    assert runner.calls == []
    assert manifest.execute(
        "SELECT state FROM pm_ingest_jobs"
    ).fetchone()["state"] == "pending"


def test_unconfigured_delivery_queues_without_calling_out(
    manifest, minutes_file, monkeypatch
):
    monkeypatch.setattr(config, "PM_REPO", "")
    make_meeting(manifest, "m1", "2026-08-27")
    pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")
    manifest.commit()
    runner = _fake_runner()

    result = pm_handoff.drain(runner=runner)

    assert result.skipped_disabled == 1
    assert runner.calls == []


def test_api_keys_are_stripped_from_the_child_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = pm_handoff.child_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env
    assert "REPLICATE_API_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"


def test_a_vanished_minutes_file_fails_the_job_without_calling_pm(
    manifest, minutes_file, configured_pm
):
    make_meeting(manifest, "m1", "2026-08-27")
    pm_handoff.enqueue(manifest, "m1", minutes_file, reason="created")
    manifest.commit()
    minutes_file.unlink()
    runner = _fake_runner()

    result = pm_handoff.drain(runner=runner)

    assert runner.calls == []
    assert result.sent == 0
    assert "missing" in (
        manifest.execute("SELECT error FROM pm_ingest_jobs").fetchone()["error"] or ""
    )


def test_limit_bounds_one_drain(manifest, tmp_path, configured_pm):
    make_meeting(manifest, "m1", "2026-08-27")
    for i in range(3):
        path = tmp_path / f"m{i}.md"
        path.write_text(f"content {i}", encoding="utf-8")
        pm_handoff.enqueue(manifest, "m1", path, reason="created")
    manifest.commit()
    runner = _fake_runner()

    result = pm_handoff.drain(limit=2, runner=runner)

    assert result.sent == 2
    assert len(runner.calls) == 2
```

Add `import subprocess` to the test module's imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_pm_handoff.py -q`
Expected: FAIL with `AttributeError: module 'pipeline.pm_handoff' has no attribute 'drain'`

- [ ] **Step 3: Write delivery**

Append to `pipeline/pm_handoff.py`:

```python
# Keys stripped from the child. Product Manager guarantees its provider router
# uses subscriptions only; leaking a key into its environment is exactly how a
# subscription-only run silently becomes a metered one.
SECRET_KEY_MARKERS = ("API_KEY", "API_TOKEN", "SECRET", "PASSWORD")


def child_env() -> dict[str, str]:
    """The parent environment minus anything that could enable metered billing."""
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in SECRET_KEY_MARKERS)
    }


def _attendees(conn, meeting_id: str) -> str:
    rows = conn.execute(
        "SELECT DISTINCT name FROM speakers "
        "WHERE meeting_id = ? AND name IS NOT NULL AND name != '' "
        "ORDER BY name",
        (meeting_id,),
    ).fetchall()
    return ",".join(str(row["name"]) for row in rows)


def build_command(job_row, title: str | None, attendees: str) -> list[str]:
    """PM's single-file ingestion command.

    `process` feeds the current statement catalogue. It is deliberately not
    `ingest`, which is a directory walk, and deliberately not anything touching
    the frozen August LightRAG snapshot.
    """
    cmd = [
        config.pm_python(),
        "-m",
        "pm_agent_core.cli",
        "process",
        str(job_row["minutes_path"]),
    ]
    if title:
        cmd += ["--title", title]
    if attendees:
        cmd += ["--attendees", attendees]
    return cmd


def _default_runner(cmd, cwd, env, timeout):
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


def drain(*, limit: int | None = None, runner=None) -> HandoffResult:
    """Deliver pending jobs. Never raises into a caller's stage.

    Returns honest counts. A caller that reports "minutes compiled" while this
    returns pending work has told the truth about the minutes and can say so
    about the delivery separately.
    """
    run = runner or _default_runner
    sent = failed = pending = skipped = 0

    with db.connect() as conn:
        jobs = db.pending_pm_jobs(conn, limit=limit)

        if not (config.PM_ENABLED and is_configured()):
            return HandoffResult(skipped_disabled=len(jobs))

        for job in jobs:
            path = Path(job["minutes_path"])
            if not path.exists():
                state = db.mark_pm_job_attempt(
                    conn,
                    job["id"],
                    f"minutes file is missing: {path}",
                    config.PM_MAX_ATTEMPTS,
                )
                failed += state == "failed"
                pending += state == "pending"
                continue

            meeting = conn.execute(
                "SELECT title_hint FROM meetings WHERE id = ?", (job["meeting_id"],)
            ).fetchone()
            cmd = build_command(
                job,
                meeting["title_hint"] if meeting else None,
                _attendees(conn, job["meeting_id"]),
            )

            try:
                completed = run(cmd, str(config.PM_REPO), child_env(), config.PM_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                error = f"timed out after {config.PM_TIMEOUT_SEC:.0f}s"
                completed = None
            except OSError as exc:
                error = f"could not start PM: {exc}"
                completed = None
            else:
                error = "" if completed.returncode == 0 else (completed.stderr or "").strip()

            if completed is not None and completed.returncode == 0:
                db.mark_pm_job_sent(conn, job["id"], config.now_iso())
                sent += 1
                continue

            state = db.mark_pm_job_attempt(
                conn, job["id"], error or "unknown failure", config.PM_MAX_ATTEMPTS
            )
            failed += state == "failed"
            pending += state == "pending"

    return HandoffResult(sent=sent, failed=failed, pending=pending, skipped_disabled=skipped)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_pm_handoff.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pipeline/pm_handoff.py tests/test_pm_handoff.py
git commit -m "Deliver queued minutes to Product Manager's catalogue"
```

---

### Task 5: Enqueue from every minutes write

**Files:**
- Modify: `pipeline/compile_minutes.py`
- Modify: `pipeline/people_merge.py:1059` region
- Modify: `tests/test_compile_minutes.py`
- Modify: `tests/test_people_merge.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_compile_minutes.py`:

```python
def test_compiling_minutes_enqueues_a_handoff_job(manifest, tmp_path, monkeypatch):
    """A committed minutes file with no queued delivery is the one state this
    design must never produce."""
    from pipeline import db

    # (Arrange a compiled meeting using this module's existing helper for a
    # successful compile, then assert the outbox row exists.)
    meeting_id = _compile_one_meeting(manifest, tmp_path, monkeypatch)

    rows = list(manifest.execute(
        "SELECT * FROM pm_ingest_jobs WHERE meeting_id = ?", (meeting_id,)
    ))
    assert len(rows) == 1
    assert rows[0]["reason"] in {"created", "recompiled"}
    assert rows[0]["state"] == "pending"
```

In `tests/test_people_merge.py`:

```python
def test_a_merge_rewrite_enqueues_a_handoff_job(manifest, monkeypatch, tmp_path):
    """The merge repair rewrote 86 minutes files in place. Without this, PM
    serves pre-merge spellings forever."""
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Alice owns Atlas.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-27")
    db.advance(manifest, "m1", db.SPEAKERS_RESOLVED, minutes_path=str(minutes_path))
    _seed_rewrite_job(
        manifest,
        job_id="job-1",
        meeting_id="m1",
        minutes_path=minutes_path,
        before="Alice owns Atlas.",
        after="Alice Smith owns Atlas.",
    )
    _use_manifest(manifest, monkeypatch)

    people_merge.resume_pending_rewrites()

    rows = list(manifest.execute(
        "SELECT * FROM pm_ingest_jobs WHERE meeting_id = 'm1'"
    ))
    assert len(rows) == 1
    assert rows[0]["reason"] == "rewritten"


def test_an_unchanged_minutes_file_does_not_enqueue(manifest, monkeypatch, tmp_path):
    minutes_path = tmp_path / "minutes.md"
    minutes_path.write_text("Alice Smith owns Atlas.", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-27")
    db.advance(manifest, "m1", db.SPEAKERS_RESOLVED, minutes_path=str(minutes_path))
    _seed_rewrite_job(
        manifest,
        job_id="job-1",
        meeting_id="m1",
        minutes_path=minutes_path,
        before="Alice Smith owns Atlas.",
        after="Alice Smith owns Atlas.",
    )
    _use_manifest(manifest, monkeypatch)

    people_merge.resume_pending_rewrites()

    assert manifest.execute(
        "SELECT COUNT(*) FROM pm_ingest_jobs"
    ).fetchone()[0] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_people_merge.py -k handoff -q`
Expected: FAIL — no `pm_ingest_jobs` rows are written.

- [ ] **Step 3: Enqueue from `compile_minutes`**

Find the point where the compiled minutes file has been written and the meeting advanced, inside the same `with db.connect()` block. Add:

```python
        from pipeline import pm_handoff

        pm_handoff.enqueue(
            conn,
            meeting.id,
            Path(minutes_path),
            reason="recompiled" if recompiled else "created",
        )
```

Use whichever local flag the function already has to distinguish a first compile from a recompile; if none exists, pass `"created"` and note it.

- [ ] **Step 4: Enqueue from the merge rewrite path**

In `pipeline/people_merge.py`, at the point where `_atomic_replace(path, job["id"], job["after_text"])` has succeeded and the job is marked applied (around line 1059), add inside the same transaction:

```python
                    from pipeline import pm_handoff

                    pm_handoff.enqueue(
                        conn, job["meeting_id"], path, reason="rewritten"
                    )
```

Enqueue only on the applied branch. An `unchanged` job wrote no new bytes, so its content digest is already queued or already sent.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_people_merge.py tests/test_compile_minutes.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pipeline/compile_minutes.py pipeline/people_merge.py tests/test_compile_minutes.py tests/test_people_merge.py
git commit -m "Queue a Product Manager handoff on every minutes write"
```

---

### Task 6: `pm-sync`, the doctor check, and the status line

**Files:**
- Modify: `pipeline/cli.py`
- Modify: `pipeline/doctor.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
def test_pm_sync_reports_what_it_delivered(monkeypatch, capsys):
    from pipeline import cli, pm_handoff

    monkeypatch.setattr(
        pm_handoff, "drain", lambda **kw: pm_handoff.HandoffResult(sent=2, pending=1)
    )

    exit_code = cli.main(["pm-sync"])

    out = capsys.readouterr().out
    assert "2 sent" in out
    assert "1 pending" in out
    assert exit_code == 1  # pending work is not success


def test_pm_sync_exits_zero_when_the_queue_is_empty(monkeypatch, capsys):
    from pipeline import cli, pm_handoff

    monkeypatch.setattr(pm_handoff, "drain", lambda **kw: pm_handoff.HandoffResult())

    assert cli.main(["pm-sync"]) == 0


def test_pm_sync_backfill_uses_pms_bulk_ingest(monkeypatch):
    from pipeline import cli, pm_handoff

    seen = {}
    monkeypatch.setattr(
        pm_handoff, "backfill", lambda **kw: seen.setdefault("called", True) or 0
    )

    cli.main(["pm-sync", "--backfill"])

    assert seen["called"] is True


# tests/test_doctor.py
def test_doctor_warns_when_the_handoff_is_unconfigured(monkeypatch):
    from pipeline import config, doctor

    monkeypatch.setattr(config, "PM_REPO", "")
    checks = doctor.check_pm_handoff()
    assert any(c.status == doctor.WARN and "not configured" in c.detail for c in checks)


def test_doctor_warns_when_a_backlog_is_waiting(monkeypatch, tmp_path):
    from pipeline import config, doctor, pm_handoff

    monkeypatch.setattr(config, "PM_REPO", str(tmp_path))
    monkeypatch.setattr(config, "PM_PYTHON", str(tmp_path / "python.exe"))
    monkeypatch.setattr(pm_handoff, "pending_count", lambda: 7)
    monkeypatch.setattr(pm_handoff, "failed_count", lambda: 0)

    checks = doctor.check_pm_handoff()

    assert any("7 pending" in c.detail for c in checks)


def test_doctor_is_ok_when_the_queue_is_drained(monkeypatch, tmp_path):
    from pipeline import config, doctor, pm_handoff

    monkeypatch.setattr(config, "PM_REPO", str(tmp_path))
    monkeypatch.setattr(config, "PM_PYTHON", str(tmp_path / "python.exe"))
    monkeypatch.setattr(pm_handoff, "pending_count", lambda: 0)
    monkeypatch.setattr(pm_handoff, "failed_count", lambda: 0)

    checks = doctor.check_pm_handoff()

    assert all(c.status == doctor.OK for c in checks)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_cli.py -k pm_sync tests/test_doctor.py -k pm_handoff -q`
Expected: FAIL — unknown command `pm-sync`; `doctor` has no `check_pm_handoff`.

- [ ] **Step 3: Add `backfill` to `pm_handoff`**

```python
def backfill(*, runner=None) -> int:
    """Hand the whole minutes directory to PM's bulk ingest.

    PM reports already-ingested files as skipped rather than duplicating them,
    so this is safe to re-run. Used once when adopting the handoff, and as the
    repair path when the outbox has lost track of history.
    """
    if not (config.PM_ENABLED and is_configured()):
        return 1
    run = runner or _default_runner
    cmd = [
        config.pm_python(),
        "-m",
        "pm_agent_core.cli",
        "ingest",
        str(config.MINUTES_DIR),
        "--pattern",
        "*.md",
    ]
    try:
        completed = run(cmd, str(config.PM_REPO), child_env(), config.PM_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return completed.returncode
```

- [ ] **Step 4: Add the CLI command**

```python
def cmd_pm_sync(args: argparse.Namespace) -> int:
    """Deliver queued minutes to Product Manager."""
    from pipeline import pm_handoff

    db.init_db()

    if args.backfill:
        return pm_handoff.backfill()

    if not pm_handoff.is_configured():
        print(
            "PM handoff is not configured. Set MMC_PM_REPO to the Product "
            "Manager repository. Minutes keep queuing until you do."
        )
        return 1

    result = pm_handoff.drain(limit=args.limit)
    print(result.summary())
    return 0 if (result.pending == 0 and result.failed == 0) else 1
```

Register it beside `doctor`:

```python
    p_pm = subparsers.add_parser(
        "pm-sync", help="deliver queued minutes to Product Manager"
    )
    p_pm.add_argument("--limit", type=int, help="deliver at most N queued minutes")
    p_pm.add_argument(
        "--backfill",
        action="store_true",
        help="hand the whole minutes directory to PM's idempotent bulk ingest",
    )
    p_pm.set_defaults(func=cmd_pm_sync)
```

Add the help line in the module docstring's command list:

```
    pipeline pm-sync               deliver queued minutes to Product Manager
```

- [ ] **Step 5: Add the doctor check**

```python
def check_pm_handoff() -> list[Check]:
    """Minutes are only delivered if this is configured and the queue drains."""
    from pipeline import pm_handoff

    if not pm_handoff.is_configured():
        return [
            Check(
                "pm handoff",
                WARN,
                "not configured - minutes queue but are never delivered",
                "set MMC_PM_REPO to the Product Manager repository",
            )
        ]

    try:
        pending = pm_handoff.pending_count()
        failed = pm_handoff.failed_count()
    except Exception as exc:  # diagnostics must never crash the run
        return [Check("pm handoff", WARN, f"queue unreadable ({exc})"[:120])]

    if failed:
        return [
            Check(
                "pm handoff",
                WARN,
                f"{pending} pending, {failed} failed",
                "pipeline pm-sync",
            )
        ]
    if pending:
        return [Check("pm handoff", WARN, f"{pending} pending", "pipeline pm-sync")]
    return [Check("pm handoff", OK, "queue drained")]
```

Register it in the check list at `doctor.py:729`, replacing `check_ollama`.

- [ ] **Step 6: Add the status line**

In `cmd_status`, after the existing counts, add:

```python
    from pipeline import pm_handoff

    pending = pm_handoff.pending_count()
    failed = pm_handoff.failed_count()
    if pending or failed:
        print(f"PM handoff: {pending} pending, {failed} failed (pipeline pm-sync)")
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_doctor.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add pipeline/cli.py pipeline/doctor.py pipeline/pm_handoff.py tests/test_cli.py tests/test_doctor.py
git commit -m "Surface the PM handoff queue in pm-sync, status and doctor"
```

---

### Task 7: Make local ASR unreachable by accident

**Files:**
- Modify: `pipeline/config.py:76-78`
- Modify: `pipeline/asr.py:475-489`
- Modify: `tests/test_asr.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_replicate_is_selected_when_a_token_is_present(monkeypatch):
    from pipeline import asr, config

    monkeypatch.setattr(config, "ASR_BACKEND", "replicate")
    monkeypatch.setattr(config, "REPLICATE_API_TOKEN", "r8_test")
    monkeypatch.setattr(asr, "REPLICATE_API_TOKEN", "r8_test")

    assert type(asr.default_backend()).__name__ == "ReplicateBackend"


def test_a_missing_token_fails_loudly_instead_of_starting_a_local_model(monkeypatch):
    """This laptop measures 1.54 tok/s on CPU. A silent local fallback turns a
    two-minute transcription into an unbounded job."""
    from pipeline import asr, config

    monkeypatch.setattr(config, "ASR_BACKEND", "replicate")
    monkeypatch.setattr(config, "REPLICATE_API_TOKEN", "")
    monkeypatch.setattr(asr, "REPLICATE_API_TOKEN", "")

    with pytest.raises(asr.ASRError) as excinfo:
        asr.default_backend()

    assert "REPLICATE_API_TOKEN" in str(excinfo.value)
    assert "MMC_ASR_BACKEND=whisperx" in str(excinfo.value)


def test_explicit_whisperx_still_works(monkeypatch):
    from pipeline import asr, config

    monkeypatch.setattr(config, "ASR_BACKEND", "whisperx")
    monkeypatch.setattr(asr, "ASR_BACKEND", "whisperx")

    assert type(asr.default_backend()).__name__ == "WhisperXBackend"


def test_auto_no_longer_silently_selects_a_local_model(monkeypatch):
    from pipeline import asr, config

    monkeypatch.setattr(config, "ASR_BACKEND", "auto")
    monkeypatch.setattr(asr, "ASR_BACKEND", "auto")
    monkeypatch.setattr(config, "REPLICATE_API_TOKEN", "")
    monkeypatch.setattr(asr, "REPLICATE_API_TOKEN", "")

    with pytest.raises(asr.ASRError):
        asr.default_backend()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_asr.py -k backend -q`
Expected: FAIL — `default_backend()` returns `WhisperXBackend` instead of raising.

- [ ] **Step 3: Change the default in `config.py`**

```python
# ASR backend: "replicate" (serverless GPU, the default), or "whisperx" (local
# CPU, opt-in only). There is deliberately no mode that picks the local model
# when a token is absent: this machine measures ~1.5 tok/s on CPU, so a silent
# fallback converts a two-minute transcription into an unbounded job that looks
# like a hang.
ASR_BACKEND = os.environ.get("MMC_ASR_BACKEND", "replicate").lower().strip()
```

- [ ] **Step 4: Change the selection in `asr.py`**

```python
def default_backend() -> Backend:
    """Select the active ASR backend.

    Replicate unless the operator explicitly asks for local WhisperX. `auto` is
    accepted for compatibility and behaves as `replicate`; it no longer selects
    a local model when the token is missing, because that failure mode is
    indistinguishable from a hang.
    """
    if ASR_BACKEND == "whisperx":
        return WhisperXBackend()

    if not REPLICATE_API_TOKEN:
        raise ASRError(
            "REPLICATE_API_TOKEN is not set, so transcription cannot run. "
            "Set it in .env, or run local CPU transcription explicitly with "
            "MMC_ASR_BACKEND=whisperx (expect hours per meeting on this hardware)."
        )

    from pipeline.replicate_asr import ReplicateBackend

    return ReplicateBackend()
```

If `asr.ASRError` does not exist, add `class ASRError(RuntimeError)` and confirm `cmd_transcribe` reports it as a stage failure rather than a traceback.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_asr.py -q`
Expected: PASS

- [ ] **Step 6: Update `.env.example`**

```
# Transcription runs on Replicate serverless GPU. Required: without it the
# transcribe stage fails loudly rather than starting a local CPU model.
REPLICATE_API_TOKEN=

# Optional. "replicate" (default) or "whisperx" for explicit local CPU.
MMC_ASR_BACKEND=replicate
```

- [ ] **Step 7: Commit**

```bash
git add pipeline/config.py pipeline/asr.py tests/test_asr.py .env.example
git commit -m "Fail loudly instead of falling back to local transcription"
```

---

### Task 8: Backfill the corpus and confirm PM holds it

**Authorization:** this task writes to the Product Manager repository. Confirm with the owner before running it. It does not modify the compiler's manifest, minutes, or transcripts.

- [ ] **Step 1: Configure the handoff**

```bash
./.venv/Scripts/python.exe -c "
from pipeline import config, pm_handoff
print('PM_REPO :', config.PM_REPO or '(unset)')
print('PM_PYTHON:', config.pm_python() or '(unset)')
print('configured:', pm_handoff.is_configured())
"
```

Set `MMC_PM_REPO` in `.env` to the Product Manager repository path if unset.

- [ ] **Step 2: Record PM's catalogue size before**

```bash
./.venv/Scripts/python.exe -m pipeline.cli pm-sync --limit 0
```

Then, in the PM repository, record the catalogue count using its own status command.

- [ ] **Step 3: Run the backfill**

```bash
./.venv/Scripts/python.exe -m pipeline.cli pm-sync --backfill
```

- [ ] **Step 4: Verify PM holds the corpus**

Compare PM's catalogue count against the minutes count recorded in Task 0. Confirm the meetings whose minutes the merge repair rewrote on 2026-08-26 show post-repair spellings in PM, not `Faraz`, `Christine`, or `Paul` alone.

- [ ] **Step 5: Drain anything the outbox still holds**

```bash
./.venv/Scripts/python.exe -m pipeline.cli pm-sync
./.venv/Scripts/python.exe -m pipeline.cli doctor
```

Expected: `pm handoff  queue drained`.

**Checkpoint:** no commit. Do not proceed to Task 9 until this task confirms PM holds the corpus.

---

### Task 9: Delete retrieval and Q&A

**Files:**
- Delete: `pipeline/index.py`, `pipeline/graph_sync.py`, `pipeline/answer.py`
- Delete: `tests/test_index.py`, `tests/test_graph_sync.py`, `tests/test_answer.py`
- Modify: `tests/conftest.py:74-90`, `pipeline/cli.py`, `pipeline/dashboard.py`, `pipeline/doctor.py`, `pipeline/static/*`, `tests/test_dashboard.py`

The `conftest.py` autouse fixture imports `graph_sync` and `index`. It must be removed in the **same commit** as the modules, or every test in the suite errors at collection.

- [ ] **Step 1: Write the failing absence test**

```python
# tests/test_no_local_rag.py
"""The compiler keeps no retrieval stack. Verified by absence."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

PIPELINE = Path(__file__).resolve().parent.parent / "pipeline"

FORBIDDEN = ("11434", "9621", "qwen3", "mxbai", "lightrag", "LIGHTRAG")


@pytest.mark.parametrize("needle", FORBIDDEN)
def test_no_module_references_the_retired_stack(needle):
    hits = [
        f"{path.name}:{i}"
        for path in PIPELINE.glob("*.py")
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if needle in line and "lightrag_doc_id" not in line
    ]
    assert hits == [], f"{needle} still referenced at {hits}"


@pytest.mark.parametrize("module", ["index", "graph_sync", "answer"])
def test_the_retired_modules_are_gone(module):
    assert not (PIPELINE / f"{module}.py").exists()


@pytest.mark.parametrize("command", ["index", "graph-sync", "query"])
def test_the_retrieval_commands_are_gone(command):
    from pipeline import cli

    with pytest.raises(SystemExit):
        cli.main([command, "--help"])
```

`lightrag_doc_id` is excluded because the inert column stays, per the global constraints.

- [ ] **Step 2: Run it to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_no_local_rag.py -q`
Expected: FAIL — all three modules exist and are referenced.

- [ ] **Step 3: Remove the conftest fixture**

Delete the whole `block_live_lightrag_in_unit_tests` fixture (`tests/conftest.py:74-90`) and the `httpx` stub above it if nothing else uses it. Nothing left in the suite makes an outbound call to a graph server.

- [ ] **Step 4: Delete the modules and their tests**

```bash
git rm pipeline/index.py pipeline/graph_sync.py pipeline/answer.py
git rm tests/test_index.py tests/test_graph_sync.py tests/test_answer.py
```

- [ ] **Step 5: Remove the CLI commands**

Delete `cmd_index`, `cmd_graph_sync`, `cmd_query`, their `add_parser` registrations, their lines in the module docstring's command list, and the `index.health()` preflight at `cli.py:124` and `cli.py:614`. Remove `index`/`graph_sync` from `cmd_run`'s stage sequence.

- [ ] **Step 6: Remove the dashboard ask box**

- `pipeline/dashboard.py`: delete `ask()` (line 949) and its route registration; drop `index` from the import at line 26.
- `pipeline/static/index.html`, `app.js`, `style.css`: remove the ask form, its handler, and its styles.
- `tests/test_dashboard.py`: delete the six `answer_module` tests (lines 440-497).
- Leave `db.append_chat_turn`, `db.recent_chat_turns`, and the `chat_turns` table in place, per the global constraints.

- [ ] **Step 7: Remove the doctor checks**

Delete `check_ollama` (line 435) and the LightRAG/graph checks (the block around lines 263-355, including the `documents/status_counts` request and `graph_sync` import), and their entries in the check list at line 729. Delete their tests in `tests/test_doctor.py` (lines 114, 129 region).

- [ ] **Step 8: Run the full suite**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: PASS except the pre-existing failures recorded in Task 0. `tests/test_answer.py` is deleted, so its four known failures disappear — note that as expected, not as a fix.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "Delete the compiler's retrieval stack and Q&A surface"
```

---

### Task 10: Remove the containers and the dead configuration

**Files:**
- Delete: `docker-compose.yml`
- Modify: `pipeline/config.py`, `.env.example`, `AGENTS.md`, `README.md`, `docs/ARCHITECTURE.md`

- [ ] **Step 1: Stop and remove the containers and volumes**

```bash
docker compose down -v
docker ps -a --filter name=mmc-
```

Expected: no `mmc-lightrag`, `mmc-ollama`, or `mmc-postgres`.

- [ ] **Step 2: Delete the compose file and configuration**

```bash
git rm docker-compose.yml
```

From `pipeline/config.py`, remove `LIGHTRAG_URL`, `LIGHTRAG_API_KEY`, `LIGHTRAG_TIMEOUT`, and any `POSTGRES_*` and Ollama settings. From `.env.example`, remove the same, plus `MMC_LIGHTRAG_API_KEY` and the Postgres block.

- [ ] **Step 3: Update the documentation**

- `AGENTS.md`: replace the local-Ollama section with the handoff. State that the compiler keeps no retrieval, that `pm_handoff` is the only seam to Product Manager, and that adapters must not invoke PM directly.
- `README.md`: remove Docker from the setup instructions; document `MMC_PM_REPO`, `pipeline pm-sync`, and the `--backfill` repair path.
- `docs/ARCHITECTURE.md`: redraw the boundary — capture through minutes here, knowledge and Q&A in Product Manager.
- Note that `docs/superpowers/specs/2026-08-14-desktop-app-design.md` and the voice specs may reference the retired stack; update only statements that are now false.

- [ ] **Step 4: Verify the documented commands exist**

```bash
./.venv/Scripts/python.exe -m pipeline.cli --help
./.venv/Scripts/python.exe -m pipeline.cli pm-sync --help
```

Every command named in the docs must appear here, and no command that no longer exists may remain documented.

- [ ] **Step 5: Prove the pipeline runs with no Docker daemon**

Stop Docker Desktop, then:

```bash
./.venv/Scripts/python.exe -m pipeline.cli doctor
./.venv/Scripts/python.exe -m pipeline.cli status
```

Expected: no check fails because a container is unreachable.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Remove the container stack the compiler no longer needs"
```

---

### Task 11: Verification and independent self-review

- [ ] **Step 1: Run the focused suites**

```bash
./.venv/Scripts/python.exe -m pytest tests/test_prior_context.py tests/test_pm_handoff.py tests/test_pm_handoff_schema.py tests/test_no_local_rag.py -q
./.venv/Scripts/python.exe -m pytest tests/test_compile_minutes.py tests/test_people_merge.py tests/test_cli.py tests/test_doctor.py tests/test_asr.py tests/test_dashboard.py -q
```

- [ ] **Step 2: Run the complete suite**

```bash
./.venv/Scripts/python.exe -m pytest -q
```

- [ ] **Step 3: Lint and syntax-check**

```bash
uvx ruff check pipeline/ tests/
node --check pipeline/static/app.js
```

- [ ] **Step 4: Inspect for the prohibited shortcuts**

- No test invokes Product Manager, LightRAG, Ollama, or Replicate.
- No `enqueue` call sits outside the transaction that recorded the minutes.
- No adapter builds a PM command itself; only `pm_handoff.build_command` does.
- No code path selects `WhisperXBackend` without `MMC_ASR_BACKEND=whisperx`.
- `drain()` has no path that raises into a caller's stage.
- No secret-bearing environment variable reaches the child process.

- [ ] **Step 5: Re-read the spec against the plan**

Map each spec section (A through H, and the completion criteria) to a passing test or an explicitly deferred operational step. Report anything unmapped.

- [ ] **Step 6: Report**

State: tests passing, pre-existing failures carried forward from Task 0, PM catalogue count before and after the backfill, and the outbox's final pending/failed counts.

**Checkpoint:** one verification commit only if the review itself requires fixes.

---

## Completion criteria

The implementation is complete only when:

1. `pipeline run` completes end to end with no Docker daemon present;
2. `tests/test_no_local_rag.py` passes, so no module references the retired stack;
3. every minutes write — created, recompiled, or merge-rewritten — enqueues exactly one handoff job per distinct content;
4. a PM outage leaves minutes intact, the queue pending, and the backlog visible in both `status` and `doctor`;
5. `pm-sync --backfill` has landed the existing corpus in PM's catalogue, verified against the Task 0 count;
6. a missing `REPLICATE_API_TOKEN` fails transcription with an actionable message and never starts a local model;
7. no unit test contacts PM, LightRAG, Ollama, or Replicate; and
8. the full offline suite passes, with the Task 0 pre-existing failures reported separately.
