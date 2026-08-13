"""SQLite manifest: the state machine that drives every stage.

Each meeting is one row that advances through statuses. Stages are independent
and resumable, which is the whole point: ASR costs 30-50 CPU-minutes per meeting
and must never be redone. Improving the minutes template later means re-running
`minutes` + `index` over years of retained transcripts without re-transcribing.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from pipeline.config import DB_PATH, now_iso

# ── Status ladder ─────────────────────────────────────────────────────
# Ordered. Each stage claims rows at status N and advances them to N+1.
DISCOVERED = "discovered"
TRANSCRIBED = "transcribed"
SPEAKERS_RESOLVED = "speakers_resolved"
MINUTES_COMPILED = "minutes_compiled"
INDEXED = "indexed"
FAILED = "failed"

STATUS_ORDER = [DISCOVERED, TRANSCRIBED, SPEAKERS_RESOLVED, MINUTES_COMPILED, INDEXED]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id              TEXT PRIMARY KEY,   -- full sha256 of the audio bytes
    source_path     TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    audio_path      TEXT,
    meeting_date    TEXT,               -- YYYY-MM-DD, local timezone
    meeting_time    TEXT,               -- HH:MM or NULL when unparseable
    title_hint      TEXT,               -- participant/subject guess from filename
    duration_sec    REAL,
    status          TEXT NOT NULL,
    asr_model       TEXT,
    template_version TEXT,
    transcript_path TEXT,
    minutes_path    TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status);
-- Stages walk pending work in meeting-date order, never discovery order: the
-- minutes compiler reads prior related minutes to flag reversed decisions, and
-- that context is only correct if meetings compile chronologically.
CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(meeting_date, meeting_time);

CREATE TABLE IF NOT EXISTS speakers (
    meeting_id  TEXT NOT NULL,
    label       TEXT NOT NULL,          -- SPEAKER_00
    name        TEXT,                   -- resolved human name, NULL if unknown
    confidence  TEXT,                   -- inferred | confirmed | unknown
    PRIMARY KEY (meeting_id, label),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stage_runs (
    meeting_id  TEXT NOT NULL,
    stage       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_stage_runs_meeting ON stage_runs(meeting_id, stage);

CREATE TABLE IF NOT EXISTS drive_sources (
    drive_file_id   TEXT NOT NULL,
    drive_version   TEXT NOT NULL,
    folder_kind     TEXT NOT NULL,      -- future | backfill
    source_name     TEXT NOT NULL,
    mime_type       TEXT,
    byte_size       INTEGER,
    md5_checksum    TEXT,
    created_time    TEXT,
    modified_time   TEXT,
    web_view_link   TEXT,
    recording_date  TEXT,
    state           TEXT NOT NULL,      -- staged | ingested | excluded | ambiguous
    local_path      TEXT,
    meeting_id      TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (drive_file_id, drive_version),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_drive_sources_state ON drive_sources(state);
CREATE INDEX IF NOT EXISTS idx_drive_sources_meeting ON drive_sources(meeting_id);

CREATE TABLE IF NOT EXISTS pipeline_settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass
class Meeting:
    """One recording as it moves through the pipeline."""

    id: str
    source_path: str
    source_name: str
    audio_path: str | None
    meeting_date: str | None
    meeting_time: str | None
    title_hint: str | None
    duration_sec: float | None
    status: str
    asr_model: str | None
    template_version: str | None
    transcript_path: str | None
    minutes_path: str | None
    error: str | None
    created_at: str
    updated_at: str

    @property
    def short_id(self) -> str:
        return self.id[:12]

    @property
    def label(self) -> str:
        """Human-readable identifier for logs."""
        date = self.meeting_date or "????-??-??"
        hint = self.title_hint or self.source_name
        return f"{date} {hint} ({self.short_id})"


@dataclass
class DriveSource:
    """One immutable Drive file version tracked by the capture stage."""

    drive_file_id: str
    drive_version: str
    folder_kind: str
    source_name: str
    mime_type: str | None
    byte_size: int | None
    md5_checksum: str | None
    created_time: str | None
    modified_time: str | None
    web_view_link: str | None
    recording_date: str | None
    state: str
    local_path: str | None
    meeting_id: str | None
    error: str | None
    created_at: str
    updated_at: str


def _row_to_meeting(row: sqlite3.Row) -> Meeting:
    return Meeting(**{k: row[k] for k in row.keys()})


def _row_to_drive_source(row: sqlite3.Row) -> DriveSource:
    return DriveSource(**{k: row[k] for k in row.keys()})


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the manifest, creating it if needed."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # The nightly batch is a single writer, but ASR holds the process for tens of
    # minutes; WAL keeps a concurrent `pipeline status` from blocking.
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    """Create tables. Idempotent."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


# ── Meeting rows ──────────────────────────────────────────────────────

def meeting_exists(conn: sqlite3.Connection, meeting_id: str) -> bool:
    """Content-hash dedup check. Load-bearing: the source Drive folder is known
    to contain byte-identical duplicate recordings, and without this a 40-minute
    CPU transcription runs twice and duplicate minutes double up graph entities."""
    row = conn.execute("SELECT 1 FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    return row is not None


def insert_meeting(
    conn: sqlite3.Connection,
    *,
    meeting_id: str,
    source_path: str,
    source_name: str,
    audio_path: str,
    meeting_date: str | None,
    meeting_time: str | None,
    title_hint: str | None,
    duration_sec: float | None,
) -> None:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO meetings (
            id, source_path, source_name, audio_path, meeting_date, meeting_time,
            title_hint, duration_sec, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            meeting_id, source_path, source_name, audio_path, meeting_date,
            meeting_time, title_hint, duration_sec, DISCOVERED, ts, ts,
        ),
    )


def get_meeting(conn: sqlite3.Connection, meeting_id: str) -> Meeting | None:
    row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    return _row_to_meeting(row) if row else None


def pending(conn: sqlite3.Connection, status: str, limit: int | None = None) -> list[Meeting]:
    """Meetings sitting at `status`, oldest meeting first.

    Chronological order matters for the minutes stage (prior-decision context)
    and makes backfill build the graph in the order events actually happened.
    """
    sql = """
        SELECT * FROM meetings
        WHERE status = ?
        ORDER BY meeting_date IS NULL, meeting_date, meeting_time, created_at
    """
    params: list[object] = [status]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_meeting(r) for r in conn.execute(sql, params).fetchall()]


def stale_template(conn: sqlite3.Connection, current_version: str) -> list[Meeting]:
    """Meetings whose minutes were built by an older template.

    This is the recompilation path: bump TEMPLATE_VERSION, re-run the minutes
    stage over these, and years of history gets rebuilt from retained
    transcripts with no ASR cost.
    """
    sql = """
        SELECT * FROM meetings
        WHERE transcript_path IS NOT NULL
          AND (template_version IS NULL OR template_version != ?)
        ORDER BY meeting_date IS NULL, meeting_date, meeting_time, created_at
    """
    return [_row_to_meeting(r) for r in conn.execute(sql, (current_version,)).fetchall()]


def recent_indexed_before(
    conn: sqlite3.Connection, meeting_date: str | None, limit: int
) -> list[Meeting]:
    """Most recent meetings with minutes that predate `meeting_date`.

    Feeds prior-decision context into the minutes compiler so it can flag when a
    decision reverses an earlier position.
    """
    if not meeting_date:
        return []
    sql = """
        SELECT * FROM meetings
        WHERE minutes_path IS NOT NULL
          AND meeting_date IS NOT NULL
          AND meeting_date < ?
        ORDER BY meeting_date DESC, meeting_time DESC
        LIMIT ?
    """
    return [_row_to_meeting(r) for r in conn.execute(sql, (meeting_date, limit)).fetchall()]


def advance(
    conn: sqlite3.Connection,
    meeting_id: str,
    status: str,
    **fields: object,
) -> None:
    """Move a meeting to a new status, optionally setting other columns.

    Clears `error` on success so a row that previously failed and was retried
    does not keep a stale message.
    """
    allowed = {
        "audio_path", "meeting_date", "meeting_time", "title_hint", "duration_sec",
        "asr_model", "template_version", "transcript_path", "minutes_path", "error",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown meeting columns: {sorted(unknown)}")

    if status != FAILED:
        fields.setdefault("error", None)

    assignments = ", ".join(f"{k} = ?" for k in fields)
    prefix = f"{assignments}, " if assignments else ""
    conn.execute(
        f"UPDATE meetings SET {prefix}status = ?, updated_at = ? WHERE id = ?",
        (*fields.values(), status, now_iso(), meeting_id),
    )


def mark_failed(conn: sqlite3.Connection, meeting_id: str, error: str) -> None:
    """Park a meeting with its error message.

    Kept out of the status ladder deliberately - a failed row is retried by
    resetting it with `reset_to`, so a transient ffmpeg or HF-token problem
    never silently loses the recording.
    """
    advance(conn, meeting_id, FAILED, error=error[:4000])


def reset_to(conn: sqlite3.Connection, meeting_id: str, status: str) -> None:
    """Rewind a meeting so a stage will pick it up again."""
    if status not in STATUS_ORDER:
        raise ValueError(f"Not a pipeline status: {status}")
    advance(conn, meeting_id, status)


# ── Speakers ──────────────────────────────────────────────────────────

def set_speaker(
    conn: sqlite3.Connection,
    meeting_id: str,
    label: str,
    name: str | None,
    confidence: str,
) -> None:
    conn.execute(
        """
        INSERT INTO speakers (meeting_id, label, name, confidence)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(meeting_id, label) DO UPDATE SET name = ?, confidence = ?
        """,
        (meeting_id, label, name, confidence, name, confidence),
    )


def get_speakers(conn: sqlite3.Connection, meeting_id: str) -> dict[str, str]:
    """Resolved label -> name for one meeting. Unresolved labels are omitted."""
    rows = conn.execute(
        "SELECT label, name FROM speakers WHERE meeting_id = ? AND name IS NOT NULL",
        (meeting_id,),
    ).fetchall()
    return {r["label"]: r["name"] for r in rows}


def known_speaker_names(conn: sqlite3.Connection, limit: int = 50) -> list[str]:
    """Names seen across past meetings, most frequent first.

    Given to the resolver as candidates so recurring attendees get spelled
    consistently - inconsistent spellings fragment graph entities.
    """
    rows = conn.execute(
        """
        SELECT name, COUNT(*) AS n FROM speakers
        WHERE name IS NOT NULL
        GROUP BY name ORDER BY n DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [r["name"] for r in rows]


# ── Stage timing ──────────────────────────────────────────────────────

def start_stage(conn: sqlite3.Connection, meeting_id: str, stage: str) -> int:
    """Open a stage_runs record. Returns its rowid."""
    cur = conn.execute(
        "INSERT INTO stage_runs (meeting_id, stage, started_at) VALUES (?, ?, ?)",
        (meeting_id, stage, now_iso()),
    )
    return int(cur.lastrowid or 0)


def finish_stage(
    conn: sqlite3.Connection, run_id: int, ok: bool, detail: str | None = None
) -> None:
    """Close a stage_runs record. This is how the CPU budget gets validated
    against reality rather than estimates."""
    conn.execute(
        "UPDATE stage_runs SET finished_at = ?, ok = ?, detail = ? WHERE rowid = ?",
        (now_iso(), 1 if ok else 0, detail, run_id),
    )


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM meetings GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def stage_timings(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Mean/max wall-clock per stage, for comparing against the CPU budget."""
    rows = conn.execute(
        """
        SELECT stage,
               COUNT(*) AS runs,
               SUM(ok) AS ok_runs,
               AVG(julianday(finished_at) - julianday(started_at)) * 86400 AS avg_sec,
               MAX(julianday(finished_at) - julianday(started_at)) * 86400 AS max_sec
        FROM stage_runs
        WHERE finished_at IS NOT NULL
        GROUP BY stage
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ── Drive capture ────────────────────────────────────────────────────

def get_drive_source(
    conn: sqlite3.Connection, drive_file_id: str, drive_version: str
) -> DriveSource | None:
    row = conn.execute(
        "SELECT * FROM drive_sources WHERE drive_file_id = ? AND drive_version = ?",
        (drive_file_id, drive_version),
    ).fetchone()
    return _row_to_drive_source(row) if row else None


def get_drive_source_for_meeting(conn: sqlite3.Connection, meeting_id: str) -> DriveSource | None:
    row = conn.execute(
        "SELECT * FROM drive_sources WHERE meeting_id = ? ORDER BY updated_at DESC LIMIT 1",
        (meeting_id,),
    ).fetchone()
    return _row_to_drive_source(row) if row else None


def upsert_drive_source(
    conn: sqlite3.Connection,
    *,
    drive_file_id: str,
    drive_version: str,
    folder_kind: str,
    source_name: str,
    mime_type: str | None,
    byte_size: int | None,
    md5_checksum: str | None,
    created_time: str | None,
    modified_time: str | None,
    web_view_link: str | None,
    recording_date: str | None,
    state: str,
    local_path: str | None = None,
    error: str | None = None,
) -> None:
    """Record capture state without overwriting a linked meeting."""
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO drive_sources (
            drive_file_id, drive_version, folder_kind, source_name, mime_type,
            byte_size, md5_checksum, created_time, modified_time, web_view_link,
            recording_date, state, local_path, error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(drive_file_id, drive_version) DO UPDATE SET
            folder_kind = excluded.folder_kind,
            source_name = excluded.source_name,
            mime_type = excluded.mime_type,
            byte_size = excluded.byte_size,
            md5_checksum = excluded.md5_checksum,
            created_time = excluded.created_time,
            modified_time = excluded.modified_time,
            web_view_link = excluded.web_view_link,
            recording_date = excluded.recording_date,
            state = excluded.state,
            local_path = excluded.local_path,
            error = excluded.error,
            updated_at = excluded.updated_at
        """,
        (
            drive_file_id, drive_version, folder_kind, source_name, mime_type,
            byte_size, md5_checksum, created_time, modified_time, web_view_link,
            recording_date, state, local_path, error, ts, ts,
        ),
    )


def staged_drive_sources(conn: sqlite3.Connection) -> list[DriveSource]:
    rows = conn.execute(
        "SELECT * FROM drive_sources WHERE state = 'staged' ORDER BY created_at"
    ).fetchall()
    return [_row_to_drive_source(row) for row in rows]


def link_drive_source_to_meeting(
    conn: sqlite3.Connection, drive_file_id: str, drive_version: str, meeting_id: str
) -> None:
    conn.execute(
        """
        UPDATE drive_sources
        SET state = 'ingested', meeting_id = ?, local_path = NULL, error = NULL, updated_at = ?
        WHERE drive_file_id = ? AND drive_version = ?
        """,
        (meeting_id, now_iso(), drive_file_id, drive_version),
    )


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM pipeline_settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO pipeline_settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )
