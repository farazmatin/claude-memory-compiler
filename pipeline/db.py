"""SQLite manifest: the state machine that drives every stage.

Each meeting is one row that advances through statuses. Stages are independent
and resumable, which is the whole point: ASR costs 30-50 CPU-minutes per meeting
and must never be redone. Improving the minutes template later means re-running
`minutes` + `index` over years of retained transcripts without re-transcribing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

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
    -- LightRAG's id for the indexed copy of this meeting's minutes. Required to
    -- DELETE the stale version before re-indexing a recompiled document;
    -- without it a recompile leaves the old entities in the graph forever.
    lightrag_doc_id TEXT,
    -- Set when a human has confirmed this meeting's speakers and minutes. NULL
    -- means the record is still whatever the compiler guessed.
    reviewed_at     TEXT,
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

-- Canonical people. Speaker resolution and entity extraction both normalize
-- through this, so "Mike", "Michael" and "michael" become one graph node instead
-- of three. Without it, cross-year spelling consistency depends on the LLM
-- re-picking the same string every time, which it will not.
CREATE TABLE IF NOT EXISTS people (
    canonical   TEXT PRIMARY KEY,
    role        TEXT,               -- optional: "PM", "engineering", "customer"
    notes       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_aliases (
    alias       TEXT PRIMARY KEY,   -- lowercased for case-insensitive matching
    canonical   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (canonical) REFERENCES people(canonical) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_person_aliases_canonical ON person_aliases(canonical);

-- Entities and relations emitted by the minutes compiler. Kept in the manifest as
-- well as in the indexed document so they survive independently of LightRAG - the
-- corpus must never be hostage to the index.
CREATE TABLE IF NOT EXISTS entities (
    meeting_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT,               -- person | feature | customer | release | other
    description TEXT,
    PRIMARY KEY (meeting_id, name),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS relations (
    meeting_id  TEXT NOT NULL,
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    object      TEXT NOT NULL,
    PRIMARY KEY (meeting_id, subject, predicate, object),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject);

-- Inbox files already seen, keyed by identity rather than content. Lets ingest
-- skip hashing a file it has already hashed: the inbox is never emptied (it is a
-- synced folder), so by year five a nightly run would otherwise re-read ~165 GB
-- just to rediscover known duplicates.
CREATE TABLE IF NOT EXISTS seen_files (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime       INTEGER NOT NULL,
    meeting_id  TEXT,               -- NULL when the file was a duplicate
    seen_at     TEXT NOT NULL
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
    lightrag_doc_id: str | None
    reviewed_at: str | None
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
    # .keys() is required here: iterating a sqlite3.Row yields VALUES, not keys,
    # so SIM118's suggested rewrite would silently build a broken mapping.
    return Meeting(**{k: row[k] for k in row.keys()})  # noqa: SIM118


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


#  column -> DDL. Applied to manifests created before the column existed.
MIGRATIONS: dict[str, str] = {
    "lightrag_doc_id": "ALTER TABLE meetings ADD COLUMN lightrag_doc_id TEXT",
    "reviewed_at": "ALTER TABLE meetings ADD COLUMN reviewed_at TEXT",
}


def init_db(db_path: Path | None = None) -> None:
    """Create tables and apply pending migrations. Idempotent."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS silently skips an existing table, so new
        # columns must be added explicitly or an upgraded install keeps running
        # against the old shape and fails at the first write.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(meetings)")}
        for column, ddl in MIGRATIONS.items():
            if column not in existing:
                conn.execute(ddl)


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


def list_meetings(
    conn: sqlite3.Connection,
    statuses: list[str] | None = None,
    limit: int | None = None,
) -> list[Meeting]:
    """Meetings for display, most recent first.

    The inverse of `pending`'s ordering: stages want the oldest unprocessed work,
    a reader wants the newest meeting.
    """
    sql = "SELECT * FROM meetings"
    params: list[object] = []
    if statuses:
        sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    sql += " ORDER BY meeting_date IS NULL, meeting_date DESC, meeting_time DESC, created_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_meeting(r) for r in conn.execute(sql, params).fetchall()]


def meetings_by_minutes_names(
    conn: sqlite3.Connection, names: list[str]
) -> dict[str, Meeting]:
    """Resolve minutes filenames back to the meetings that produced them.

    Retrieval cites the filename it indexed; a citation the reader can open needs
    the meeting row behind it. Names that match nothing are simply absent from the
    result - minutes deleted off disk should degrade a citation, not fail a query.
    """
    found: dict[str, Meeting] = {}
    for name in names:
        # `_` is a single-character wildcard in LIKE, and minutes filenames can
        # contain one, so an unescaped pattern would match a neighbouring meeting.
        pattern = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        row = conn.execute(
            "SELECT * FROM meetings WHERE minutes_path LIKE ? ESCAPE '\\' "
            "ORDER BY updated_at DESC LIMIT 1",
            (f"%{pattern}",),
        ).fetchone()
        if row is not None:
            found[name] = _row_to_meeting(row)
    return found


def stale_template(conn: sqlite3.Connection, current_version: str) -> list[Meeting]:
    """Meetings whose minutes were built by an older template.

    This is the recompilation path: bump TEMPLATE_VERSION, re-run the minutes
    stage over these, and years of history gets rebuilt from retained
    transcripts with no ASR cost.

    Restricted to meetings that have already cleared speaker resolution. A
    meeting still at `transcribed` also has a transcript and a NULL
    template_version, and compiling it here would jump it straight to
    minutes_compiled - skipping stage 3 entirely and producing minutes whose
    action items are owned by "SPEAKER_01".
    """
    sql = """
        SELECT * FROM meetings
        WHERE transcript_path IS NOT NULL
          AND status IN (?, ?, ?)
          AND (template_version IS NULL OR template_version != ?)
        ORDER BY meeting_date IS NULL, meeting_date, meeting_time, created_at
    """
    params = (SPEAKERS_RESOLVED, MINUTES_COMPILED, INDEXED, current_version)
    return [_row_to_meeting(r) for r in conn.execute(sql, params).fetchall()]


def recent_indexed_before(
    conn: sqlite3.Connection,
    meeting_date: str | None,
    limit: int,
    meeting_time: str | None = None,
    exclude_id: str | None = None,
) -> list[Meeting]:
    """Most recent meetings with minutes that precede this one.

    Feeds prior-decision context into the minutes compiler so it can flag when a
    decision reverses an earlier position.

    Compares (date, time) as a pair, not date alone. At five meetings a day a
    date-only comparison makes every meeting blind to the other four from the
    same day, so a decision reversed after lunch would never be flagged.
    """
    if not meeting_date:
        return []
    # Empty string sorts below any "HH:MM", so an unknown time places the meeting
    # at the start of its day rather than excluding it.
    time_key = meeting_time or ""
    sql = """
        SELECT * FROM meetings
        WHERE minutes_path IS NOT NULL
          AND meeting_date IS NOT NULL
          AND (meeting_date < ?
               OR (meeting_date = ? AND COALESCE(meeting_time, '') < ?))
          AND id != COALESCE(?, '')
        ORDER BY meeting_date DESC, meeting_time DESC
        LIMIT ?
    """
    params = (meeting_date, meeting_date, time_key, exclude_id, limit)
    return [_row_to_meeting(r) for r in conn.execute(sql, params).fetchall()]


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
        "asr_model", "template_version", "transcript_path", "minutes_path",
        "lightrag_doc_id", "error",
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


def speaker_rows(conn: sqlite3.Connection, meeting_id: str) -> list[dict[str, object]]:
    """Every diarized label for a meeting, resolved or not.

    `get_speakers` omits unresolved labels because the compiler wants a name map.
    Review wants the opposite: an unresolved label is the thing most worth
    showing, since it is an action item whose owner was lost.
    """
    rows = conn.execute(
        "SELECT label, name, confidence FROM speakers WHERE meeting_id = ? ORDER BY label",
        (meeting_id,),
    ).fetchall()
    return [
        {"label": r["label"], "name": r["name"], "confidence": r["confidence"]} for r in rows
    ]


def mark_reviewed(conn: sqlite3.Connection, meeting_id: str, when: str | None = None) -> None:
    """Record that a human has confirmed this meeting. `None` clears it."""
    conn.execute(
        "UPDATE meetings SET reviewed_at = ?, updated_at = ? WHERE id = ?",
        (when, now_iso(), meeting_id),
    )


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


# ── People registry ───────────────────────────────────────────────────
#
# The point of this table is determinism. Asking a model to spell a name the same
# way it did four months ago is not a reliable strategy, and every variant it
# invents becomes a separate node in the knowledge graph.

def add_person(
    conn: sqlite3.Connection,
    canonical: str,
    aliases: list[str] | None = None,
    role: str | None = None,
) -> None:
    """Register a canonical name and any aliases that should map to it.

    The canonical name is always registered as an alias of itself, so lookup has
    a single code path.
    """
    canonical = canonical.strip()
    if not canonical:
        return
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO people (canonical, role, created_at) VALUES (?, ?, ?)
        ON CONFLICT(canonical) DO UPDATE SET role = COALESCE(?, role)
        """,
        (canonical, role, ts, role),
    )
    for alias in {canonical, *(aliases or [])}:
        cleaned = alias.strip().lower()
        if cleaned:
            conn.execute(
                """
                INSERT INTO person_aliases (alias, canonical, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET canonical = ?
                """,
                (cleaned, canonical, ts, canonical),
            )


def canonical_name(conn: sqlite3.Connection, name: str | None) -> str | None:
    """Map any known alias to its canonical spelling.

    Unknown names pass through unchanged rather than being rejected: a new person
    appearing in a meeting is normal, and silently dropping them would be worse
    than an unnormalized spelling.
    """
    if not name:
        return name
    row = conn.execute(
        "SELECT canonical FROM person_aliases WHERE alias = ?", (name.strip().lower(),)
    ).fetchone()
    return row["canonical"] if row else name.strip()


def list_people(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Every canonical person with their aliases and meeting count."""
    rows = conn.execute(
        """
        SELECT p.canonical, p.role,
               (SELECT GROUP_CONCAT(a.alias, ', ') FROM person_aliases a
                 WHERE a.canonical = p.canonical AND a.alias != LOWER(p.canonical)
               ) AS aliases,
               (SELECT COUNT(DISTINCT s.meeting_id) FROM speakers s
                 WHERE s.name = p.canonical) AS meetings
        FROM people p
        ORDER BY meetings DESC, p.canonical
        """
    ).fetchall()
    return [dict(r) for r in rows]


def merge_person(conn: sqlite3.Connection, from_name: str, into: str) -> int:
    """Fold one person into another, rewriting existing rows.

    For when the same human was recorded under two names before the registry knew
    about it. Returns the number of speaker rows rewritten.
    """
    from_name, into = from_name.strip(), into.strip()
    if not from_name or not into or from_name == into:
        return 0

    add_person(conn, into, aliases=[from_name])
    cursor = conn.execute(
        "UPDATE speakers SET name = ? WHERE name = ?", (into, from_name)
    )
    # Historic entities and relations must move too, or the graph keeps both nodes.
    conn.execute("UPDATE entities SET name = ? WHERE name = ?", (into, from_name))
    conn.execute("UPDATE relations SET subject = ? WHERE subject = ?", (into, from_name))
    conn.execute("UPDATE relations SET object = ? WHERE object = ?", (into, from_name))
    conn.execute(
        "UPDATE person_aliases SET canonical = ? WHERE canonical = ?", (into, from_name)
    )
    conn.execute("DELETE FROM people WHERE canonical = ?", (from_name,))
    return cursor.rowcount


# ── Entities and relations ────────────────────────────────────────────

def replace_entities(
    conn: sqlite3.Connection,
    meeting_id: str,
    entities: list[dict[str, str]],
    relations: list[dict[str, str]],
) -> None:
    """Store a meeting's emitted entities and relations, replacing any prior set.

    Replace rather than append: a recompile must not leave the previous run's
    entities behind, for the same reason re-indexing deletes before inserting.
    """
    conn.execute("DELETE FROM entities WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM relations WHERE meeting_id = ?", (meeting_id,))

    for entity in entities:
        name = (entity.get("name") or "").strip()
        if not name:
            continue
        conn.execute(
            """
            INSERT INTO entities (meeting_id, name, kind, description)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(meeting_id, name) DO UPDATE SET kind = ?, description = ?
            """,
            (
                meeting_id, name, entity.get("kind"), entity.get("description"),
                entity.get("kind"), entity.get("description"),
            ),
        )

    for relation in relations:
        subject = (relation.get("subject") or "").strip()
        predicate = (relation.get("predicate") or "").strip()
        obj = (relation.get("object") or "").strip()
        if not (subject and predicate and obj):
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO relations (meeting_id, subject, predicate, object)
            VALUES (?, ?, ?, ?)
            """,
            (meeting_id, subject, predicate, obj),
        )


def get_entities(conn: sqlite3.Connection, meeting_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT name, kind, description FROM entities WHERE meeting_id = ? ORDER BY name",
        (meeting_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_relations(conn: sqlite3.Connection, meeting_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT subject, predicate, object FROM relations
        WHERE meeting_id = ? ORDER BY subject, predicate, object
        """,
        (meeting_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def entity_mentions(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, object]]:
    """Most-mentioned entities across the corpus, for `pipeline entities`."""
    rows = conn.execute(
        """
        SELECT name, kind, COUNT(DISTINCT meeting_id) AS meetings
        FROM entities GROUP BY name, kind
        ORDER BY meetings DESC, name LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Seen-file cache ───────────────────────────────────────────────────

def file_unchanged(conn: sqlite3.Connection, path: str, size: int, mtime: int) -> bool:
    """True if this exact path/size/mtime has already been processed.

    Identity, not content: the point is to avoid reading the file at all. A file
    edited in place with the same size and mtime would be missed, which does not
    happen to finished audio recordings.
    """
    row = conn.execute(
        "SELECT size, mtime FROM seen_files WHERE path = ?", (path,)
    ).fetchone()
    return bool(row and row["size"] == size and int(row["mtime"]) == mtime)


def mark_seen(
    conn: sqlite3.Connection, path: str, size: int, mtime: int, meeting_id: str | None
) -> None:
    conn.execute(
        """
        INSERT INTO seen_files (path, size, mtime, meeting_id, seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            size = ?, mtime = ?, meeting_id = ?, seen_at = ?
        """,
        (path, size, mtime, meeting_id, now_iso(), size, mtime, meeting_id, now_iso()),
    )


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
