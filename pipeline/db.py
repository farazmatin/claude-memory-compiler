"""SQLite manifest: the state machine that drives every stage.

Each meeting is one row that advances through statuses. Stages are independent
and resumable, which is the whole point: ASR costs 30-50 CPU-minutes per meeting
and must never be redone. Improving the minutes template later means re-running
`minutes` + `index` over years of retained transcripts without re-transcribing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import DB_PATH, now_iso, today_iso

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
    -- Set when the stage failure was a network fault rather than a bad input, so
    -- the next pipeline run can requeue it without a human reading the manifest.
    retryable       INTEGER NOT NULL DEFAULT 0,
    -- Auto-requeues spent on the current failure. Bounded so a real outage
    -- cannot spin, and cleared once the meeting clears the failing stage.
    retry_count     INTEGER NOT NULL DEFAULT 0,
    -- LightRAG's id for the indexed copy of this meeting's minutes. Required to
    -- DELETE the stale version before re-indexing a recompiled document;
    -- without it a recompile leaves the old entities in the graph forever.
    lightrag_doc_id TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status);
-- Stages walk pending work in meeting-date order, never discovery order: the
-- minutes compiler reads prior related minutes to flag reversed decisions, and
-- that context is only correct if meetings compile chronologically.
CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(meeting_date, meeting_time);

CREATE TABLE IF NOT EXISTS minute_rewrite_jobs (
    id             TEXT PRIMARY KEY,
    operation_id   TEXT NOT NULL,
    meeting_id     TEXT NOT NULL,
    minutes_path   TEXT,
    mappings_json  TEXT NOT NULL,
    before_sha256  TEXT,
    after_sha256   TEXT,
    before_text    TEXT,
    after_text     TEXT,
    state          TEXT NOT NULL,  -- pending|applied|unchanged|missing|conflict
    error          TEXT,
    created_at     TEXT NOT NULL,
    finished_at    TEXT,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

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

CREATE TABLE IF NOT EXISTS merged_names (
    old_key      TEXT PRIMARY KEY,
    old_spelling TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    merged_at    TEXT NOT NULL,
    FOREIGN KEY (canonical) REFERENCES people(canonical) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_merged_names_canonical ON merged_names(canonical);

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

-- Commitments, decisions and open questions emitted by the minutes compiler.
-- Same reasoning as entities/relations above: 279 action items and 41 meetings'
-- worth of decisions existed only as prose the compiler wrote and nothing ever
-- read back, so "what did I commit to?" had no answer short of grepping 45
-- markdown files by hand. Parsed out of the same document, in the same pass.
CREATE TABLE IF NOT EXISTS commitments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      TEXT NOT NULL,
    owner           TEXT,               -- canonicalized through people/person_aliases
    text            TEXT NOT NULL,
    due_date        TEXT,               -- raw, as written: "unspecified", "before 2026-08-17", ...
    -- A second column, not in the original table sketch: "raw AND normalised"
    -- cannot both live in one TEXT column. NULL whenever due_date has no literal
    -- YYYY-MM-DD in it ("unspecified" and prose-only dates both land here).
    due_date_iso    TEXT,
    timestamp_cite  TEXT,               -- verbatim [H:MM:SS] citation(s), never parsed
    state           TEXT NOT NULL DEFAULT 'open',   -- open | done, from "- [ ]" / "- [x]"
    created_at      TEXT NOT NULL,
    UNIQUE(meeting_id, text),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_commitments_owner_state ON commitments(owner, state);
-- Powers ?overdue=1: filtering/sorting a raw "unspecified"/"before X" string is
-- useless, so this indexes the normalised column rather than due_date itself.
CREATE INDEX IF NOT EXISTS idx_commitments_due_date ON commitments(due_date_iso);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      TEXT NOT NULL,
    text            TEXT NOT NULL,
    decided_by      TEXT,               -- canonicalized; NULL when the prose names no one
    rationale       TEXT,
    timestamp_cite  TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(meeting_id, text),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS open_questions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      TEXT NOT NULL,
    text            TEXT NOT NULL,
    owner           TEXT,               -- canonicalized; NULL when the prose names no one
    created_at      TEXT NOT NULL,
    UNIQUE(meeting_id, text),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

-- Inbox files already seen, keyed by identity rather than content. Lets ingest
-- skip hashing a file it has already hashed: the inbox is never emptied (it is a
-- synced folder), so by year five a routine run would otherwise re-read ~165 GB
-- just to rediscover known duplicates.
CREATE TABLE IF NOT EXISTS seen_files (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime       INTEGER NOT NULL,
    meeting_id  TEXT,               -- NULL when the file was a duplicate
    seen_at     TEXT NOT NULL
);

-- ── Voice enrollment ────────────────────────────────────────────────
--
-- Diarization only separates voices WITHIN one recording; SPEAKER_00 means
-- nothing in the next file. These tables give the pipeline a memory of what
-- people sound like, so a voice named once is recognised forever after.

-- One row per enrolled utterance. A person's voiceprint is the duration-weighted
-- mean of their samples, computed on read rather than stored: a confirmation the
-- owner later regrets is one DELETE, and the voiceprint corrects itself.
--
-- meeting_id is ON DELETE SET NULL, deliberately NOT CASCADE. Cascading would
-- mean deleting one old meeting silently degrades the voiceprint of everyone who
-- spoke in it, and enrollment evidence disappearing unnoticed is exactly the
-- failure nobody catches until names start going wrong. The embedding is the
-- asset; its provenance is merely nice to have.
CREATE TABLE IF NOT EXISTS voice_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical   TEXT NOT NULL,
    meeting_id  TEXT,
    label       TEXT,
    embedding   BLOB NOT NULL,      -- float32 little-endian, np.ndarray.tobytes()
    dim         INTEGER NOT NULL,
    model       TEXT NOT NULL,      -- embeddings from different models never mix
    speech_sec  REAL NOT NULL,
    source      TEXT NOT NULL,      -- confirmed | merged | bootstrap
    created_at  TEXT NOT NULL,
    FOREIGN KEY (canonical)  REFERENCES people(canonical) ON DELETE CASCADE,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id)      ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_voice_samples_canonical ON voice_samples(canonical);
CREATE INDEX IF NOT EXISTS idx_voice_samples_model ON voice_samples(model);

-- Every diarized label with its embedding and clips, named or not. Retaining
-- unnamed rows is what lets a voice named today be matched against last spring's
-- meetings, long after the source audio was deleted.
CREATE TABLE IF NOT EXISTS speaker_matches (
    meeting_id      TEXT NOT NULL,
    label           TEXT NOT NULL,
    embedding       BLOB,
    dim             INTEGER,
    model           TEXT,
    speech_sec      REAL,
    snippet_paths   TEXT,               -- JSON array, relative to SNIPPETS_DIR
    snippet_quality TEXT,               -- ok | low
    best_canonical  TEXT,
    best_score      REAL,
    next_canonical  TEXT,               -- runner-up, for the margin test
    next_score      REAL,
    llm_name        TEXT,               -- what the transcript pass concluded
    band            TEXT,               -- auto | review | new
    state           TEXT NOT NULL,      -- pending | resolved | dismissed
    cluster_id      TEXT,
    resolved_as     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (meeting_id, label),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_speaker_matches_state ON speaker_matches(state);
CREATE INDEX IF NOT EXISTS idx_speaker_matches_cluster ON speaker_matches(cluster_id);

-- Rebuilt on demand. One row per group of pending labels believed to be the same
-- person, and the unit the owner is actually asked about: a voice appearing in
-- twelve meetings is one question, not twelve.
CREATE TABLE IF NOT EXISTS voice_clusters (
    id              TEXT PRIMARY KEY,
    size            INTEGER NOT NULL,
    total_speech    REAL NOT NULL,      -- drives queue order: most history first
    best_canonical  TEXT,
    best_score      REAL,
    next_canonical  TEXT,
    next_score      REAL,
    band            TEXT NOT NULL,      -- review | new
    created_at      TEXT NOT NULL
);

-- ── Ask AI conversation history ─────────────────────────────────────
--
-- One row per question/answer turn. Keyed by a client-held session id rather
-- than a login, since the dashboard has no accounts - the browser mints one
-- with localStorage and a "New conversation" click clears it. answer.py stays
-- storage-agnostic (it takes `history` as a plain list of tuples); this table
-- is what the dashboard route reads and writes around that call.
CREATE TABLE IF NOT EXISTS chat_turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    turn_index      INTEGER NOT NULL,   -- 0-based, per session; see append_chat_turn
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    mode            TEXT,
    provider        TEXT,
    synthesized     INTEGER NOT NULL,
    context_chars   INTEGER,
    retrieval_sec   REAL,
    synthesis_sec   REAL,
    created_at      TEXT NOT NULL,
    UNIQUE(session_id, turn_index)
);

-- DESC because every read is "the last N turns of this session".
CREATE INDEX IF NOT EXISTS idx_chat_turns_session ON chat_turns(session_id, turn_index DESC);

-- ── BM25 chunk index ────────────────────────────────────────────────
--
-- The searchable copy of the minutes. Minutes live on disk and are the record;
-- these rows are a derived, disposable projection of them, which is why nothing
-- here is authored and the whole table can be rebuilt from the files in seconds.
-- See pipeline/chunk_index.py for the chunk geometry.
--
-- Every provenance field is denormalized from `meetings` on purpose. A hit is
-- handed to a caller as an excerpt plus a citation, and the citation has to name
-- the meeting this text actually came from; carrying it on the row makes that a
-- property of the data rather than of whoever remembered to join.
-- meeting_id CASCADEs, unlike voice_samples above. The opposite reasoning
-- applies: an embedding is an irreplaceable asset whose provenance is merely
-- nice to have, whereas a chunk IS its provenance - text with no meeting behind
-- it is an excerpt that cites a meeting the manifest cannot produce, and it
-- costs one second to rebuild from the file on disk.
CREATE TABLE IF NOT EXISTS minute_chunks (
    chunk_id      TEXT PRIMARY KEY,   -- "<meeting_id>:<ordinal, 4 digits>"
    meeting_id    TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    meeting_date  TEXT,
    source_path   TEXT NOT NULL,
    ordinal       INTEGER NOT NULL,   -- position in the file; stable across reindex
    heading       TEXT,               -- nearest enclosing markdown heading
    text          TEXT NOT NULL,
    -- Written by a later retrieval stage, NULL until then. Declared and indexed
    -- now so adding it needs no second migration over a live manifest.
    context_header TEXT,
    char_count    INTEGER NOT NULL,
    -- SHA-256 of `text`. Reindex compares hashes and skips a meeting whose
    -- chunks all match, so re-running over the whole corpus is cheap and does
    -- not churn ids that downstream artifacts key on.
    content_hash  TEXT NOT NULL,
    indexed_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_minute_chunks_meeting ON minute_chunks(meeting_id);
-- One chunk per position per meeting. Guards the reindex path: a replace that
-- failed to clear the old rows would otherwise double a meeting's text in the
-- index and let one meeting outvote itself.
CREATE UNIQUE INDEX IF NOT EXISTS idx_minute_chunks_pos ON minute_chunks(meeting_id, ordinal);

-- External-content FTS5: the index stores only the inverted index, and the text
-- is read back from minute_chunks. Two copies of an 18,000-character minutes
-- file per meeting would double the manifest for nothing.
CREATE VIRTUAL TABLE IF NOT EXISTS minute_chunks_fts USING fts5(
    text,
    context_header,
    heading,
    content='minute_chunks',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- An external-content table is not maintained by SQLite; these triggers are the
-- only thing keeping it in step. Without them a reindex leaves the FTS index
-- pointing at rowids that no longer hold the text it indexed, and searches
-- start returning other meetings' passages under the wrong citation.
CREATE TRIGGER IF NOT EXISTS minute_chunks_ai AFTER INSERT ON minute_chunks BEGIN
    INSERT INTO minute_chunks_fts(rowid, text, context_header, heading)
    VALUES (new.rowid, new.text, new.context_header, new.heading);
END;

CREATE TRIGGER IF NOT EXISTS minute_chunks_ad AFTER DELETE ON minute_chunks BEGIN
    INSERT INTO minute_chunks_fts(minute_chunks_fts, rowid, text, context_header, heading)
    VALUES ('delete', old.rowid, old.text, old.context_header, old.heading);
END;

CREATE TRIGGER IF NOT EXISTS minute_chunks_au AFTER UPDATE ON minute_chunks BEGIN
    INSERT INTO minute_chunks_fts(minute_chunks_fts, rowid, text, context_header, heading)
    VALUES ('delete', old.rowid, old.text, old.context_header, old.heading);
    INSERT INTO minute_chunks_fts(rowid, text, context_header, heading)
    VALUES (new.rowid, new.text, new.context_header, new.heading);
END;

-- ── Dense vectors over the same chunks ──────────────────────────────
--
-- The semantic half of retrieval. The FTS index above finds the passage that
-- uses the caller's words; these vectors find the one that means the same thing
-- in different words, which is most of what a question about a meeting needs.
-- See pipeline/dense_index.py.
--
-- No vector extension and no index structure. The corpus is 2,809 chunks at 384
-- float32 each - about 4 MB - and scoring a query against all of them is one
-- numpy matrix-vector product. An approximate index at this size would cost a
-- native dependency and an approximation to save nothing measurable.
--
-- chunk_id alone is the primary key, so the store holds one model's vectors at
-- a time and switching models replaces the corpus rather than doubling it.
-- `model` is still carried per row and every search filters on it: vectors from
-- two models are not on the same scale, and a search that mixed them would rank
-- on noise. Same namespacing rule as voice_samples, for the same reason.
--
-- content_hash is the invalidation, and the foreign key does not replace it.
-- reindex_meeting deletes and re-inserts a changed meeting's chunks, so
-- "m1:0003" after an edit is a different passage at the same id - and the
-- cascade only fires on a connection with `PRAGMA foreign_keys = ON`, which
-- SQLite defaults to OFF. A vector whose content_hash no longer matches its
-- chunk describes text nobody can be shown; embed_chunks rebuilds it and
-- search_dense refuses to score it.
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id     TEXT PRIMARY KEY REFERENCES minute_chunks(chunk_id) ON DELETE CASCADE,
    model        TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    vector       BLOB NOT NULL,      -- float32 little-endian, np.ndarray.tobytes()
    content_hash TEXT NOT NULL,      -- the minute_chunks.content_hash this was built from
    embedded_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunk_vectors_model ON chunk_vectors(model);
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
    error: str | None
    created_at: str
    updated_at: str
    # Defaulted and last so callers that build a Meeting by hand - tests, and the
    # dashboard's synthetic rows - do not have to know about the retry budget.
    retryable: int = 0
    retry_count: int = 0

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
    # .keys() is required: iterating a sqlite3.Row yields VALUES, not keys, so
    # SIM118's suggested rewrite would silently build a broken mapping.
    return DriveSource(**{k: row[k] for k in row.keys()})  # noqa: SIM118


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the manifest, creating it if needed."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


#  column -> DDL. Applied to manifests created before the column existed.
MIGRATIONS: dict[str, str] = {
    "lightrag_doc_id": "ALTER TABLE meetings ADD COLUMN lightrag_doc_id TEXT",
    "retryable":
        "ALTER TABLE meetings ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0",
    "retry_count":
        "ALTER TABLE meetings ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
}


def _drop_prefk_chunk_index(conn: sqlite3.Connection) -> None:
    """Discard a `minute_chunks` built before it referenced `meetings`.

    SQLite cannot add a foreign key to an existing table, and
    `CREATE TABLE IF NOT EXISTS` would leave the old shape in place forever
    while every test passed against a freshly created one. Dropping is the
    right migration for this table specifically: it holds nothing authored -
    only a projection of minutes files that `pipeline chunk-index` rebuilds in
    about a second.

    Both the FTS table and its triggers go with it, and in that order. The FTS
    table is external-content, so leaving it behind would point it at a table
    that no longer exists; the triggers reference the same rowids and would be
    recreated stale. Must run BEFORE the SCHEMA script, which puts all four
    back.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'minute_chunks'"
    ).fetchone()
    if not table or conn.execute("PRAGMA foreign_key_list(minute_chunks)").fetchone():
        return
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS minute_chunks_ai;
        DROP TRIGGER IF EXISTS minute_chunks_ad;
        DROP TRIGGER IF EXISTS minute_chunks_au;
        DROP TABLE IF EXISTS minute_chunks_fts;
        DROP TABLE IF EXISTS minute_chunks;
        """
    )


def init_db(db_path: Path | None = None) -> None:
    """Create tables and apply pending migrations. Idempotent."""
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        _drop_prefk_chunk_index(conn)
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
        "lightrag_doc_id", "error", "retryable", "retry_count",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown meeting columns: {sorted(unknown)}")

    if status != FAILED:
        fields.setdefault("error", None)

    # Clearing the failing stage settles the old failure, so next month's blip
    # starts with a full retry budget instead of this month's leftovers. A
    # requeue back to DISCOVERED is not progress and must keep the count.
    if status in STATUS_ORDER and status != DISCOVERED:
        fields.setdefault("retryable", 0)
        fields.setdefault("retry_count", 0)

    assignments = ", ".join(f"{k} = ?" for k in fields)
    prefix = f"{assignments}, " if assignments else ""
    conn.execute(
        f"UPDATE meetings SET {prefix}status = ?, updated_at = ? WHERE id = ?",
        (*fields.values(), status, now_iso(), meeting_id),
    )


def mark_failed(
    conn: sqlite3.Connection,
    meeting_id: str,
    error: str,
    *,
    retryable: bool = False,
) -> None:
    """Park a meeting with its error message.

    Kept out of the status ladder deliberately - a failed row is retried by
    resetting it with `reset_to`, so a transient ffmpeg or HF-token problem
    never silently loses the recording.

    ``retryable`` marks a network fault rather than a bad input, which lets
    ``requeue_transient`` pick the row up on the next run. Default False: a
    missing file or a rejected token repeats identically forever, and
    auto-retrying it would spin without ever producing minutes.
    """
    advance(conn, meeting_id, FAILED, error=error[:4000], retryable=int(retryable))


def requeue_transient(
    conn: sqlite3.Connection, status: str, max_retries: int
) -> list[str]:
    """Rewind network-failed meetings to `status`, within their retry budget.

    Recovery used to depend on a person reading the manifest and running
    `pipeline retry`: six recordings sat parked at FAILED for a day with no
    minutes compiled. Bounded by `max_retries` so a genuine multi-day outage
    still comes to rest at FAILED where `pipeline status` reports it.
    """
    if status not in STATUS_ORDER:
        raise ValueError(f"Not a pipeline status: {status}")

    rows = conn.execute(
        """
        SELECT id FROM meetings
        WHERE status = ? AND retryable = 1 AND retry_count < ?
        ORDER BY meeting_date IS NULL, meeting_date, meeting_time, created_at
        """,
        (FAILED, max_retries),
    ).fetchall()

    requeued: list[str] = []
    for row in rows:
        conn.execute(
            "UPDATE meetings SET retry_count = retry_count + 1 WHERE id = ?",
            (row["id"],),
        )
        advance(conn, row["id"], status)
        requeued.append(row["id"])
    return requeued


def reset_to(conn: sqlite3.Connection, meeting_id: str, status: str) -> None:
    """Rewind a meeting so a stage will pick it up again.

    Restores the auto-requeue budget: every caller is a person deciding to retry
    - `pipeline retry`, or a dashboard review action - and they have usually just
    fixed the cause, so the row should not inherit a budget spent unattended.
    """
    if status not in STATUS_ORDER:
        raise ValueError(f"Not a pipeline status: {status}")
    advance(conn, meeting_id, status, retryable=0, retry_count=0)


def refreshable_meetings(conn: sqlite3.Connection, meeting_ids: list[str]) -> int:
    """How many of these `queue_minutes_refresh` would actually requeue.

    The read-only half of the pair, so a dry run can report the real recompile
    cost before anything is committed rather than guessing at it.
    """
    ids = sorted({meeting_id for meeting_id in meeting_ids if meeting_id})
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM meetings WHERE id IN ({placeholders}) "
        "AND status IN (?, ?)",
        (*ids, MINUTES_COMPILED, INDEXED),
    ).fetchone()
    return int(row["n"]) if row else 0


def queue_minutes_refresh(conn: sqlite3.Connection, meeting_ids: list[str]) -> int:
    """Requeue completed meetings whose confirmed speaker names changed.

    Keep ``lightrag_doc_id`` intact. The index stage needs the old id so it can
    delete the stale search document before inserting regenerated minutes.
    Meetings that have not reached minutes yet stay at their current stage.
    """
    ids = sorted({meeting_id for meeting_id in meeting_ids if meeting_id})
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    cursor = conn.execute(
        f"""
        UPDATE meetings
        SET status = ?, error = NULL, updated_at = ?
        WHERE id IN ({placeholders}) AND status IN (?, ?)
        """,
        (SPEAKERS_RESOLVED, now_iso(), *ids, MINUTES_COMPILED, INDEXED),
    )
    return cursor.rowcount


def clear_audio_path(conn: sqlite3.Connection, meeting_id: str) -> None:
    """Clear audio_path when the local recording is deleted to save space."""
    conn.execute(
        "UPDATE meetings SET audio_path = NULL, updated_at = ? WHERE id = ?",
        (now_iso(), meeting_id),
    )


def delete_meeting(conn: sqlite3.Connection, meeting_id: str) -> bool:
    """Delete a meeting and all its related records completely from the database."""
    conn.execute("DELETE FROM speakers WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM stage_runs WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM drive_sources WHERE meeting_id = ?", (meeting_id,))
    # seen_files is keyed by inbox path, not meeting_id, and ingest.file_unchanged
    # matches on path/size/mtime alone - it has no idea the meeting behind that
    # path was deleted. Leaving this row behind means a source file still sitting
    # in inbox/ can never be re-ingested: every future `pipeline ingest` sees the
    # same path/size/mtime and skips it, silently and permanently.
    conn.execute("DELETE FROM seen_files WHERE meeting_id = ?", (meeting_id,))
    # Belt and braces: minute_chunks CASCADEs, but only on a connection with
    # `PRAGMA foreign_keys = ON`, which SQLite defaults to OFF and every caller
    # has to set for itself. `connect()` sets it; a maintenance script opening
    # the file with a bare sqlite3.connect() would not, and the chunks left
    # behind would keep answering searches citing a meeting that is gone.
    #
    # The vectors go first, and have to: they are identified by joining through
    # minute_chunks, so once the chunk rows are gone there is nothing left to
    # name them by and they would sit orphaned until a meeting reusing the id
    # minted a colliding chunk_id.
    conn.execute(
        "DELETE FROM chunk_vectors WHERE chunk_id IN "
        "(SELECT chunk_id FROM minute_chunks WHERE meeting_id = ?)",
        (meeting_id,),
    )
    conn.execute("DELETE FROM minute_chunks WHERE meeting_id = ?", (meeting_id,))
    cur = conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    return cur.rowcount > 0


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


def label_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Diarized label count per meeting, in one query.

    `speakers` holds a row per label whether or not it was ever named, so this
    is the label count without touching a transcript. Callers use it to spot
    over-segmentation across a whole rematch run rather than per row.

    The union with `speaker_matches` is what makes the answer independent of
    which pass ran first. Both tables carry one row per (meeting, label), and on
    the current corpus they agree on every meeting - but a meeting known only to
    `speaker_matches` would otherwise count zero labels, and `over_segmented(0)`
    is False, so the veto would switch itself off silently rather than fail.
    `UNION` rather than `UNION ALL`, so a label both tables know is counted once.
    """
    return {
        r["meeting_id"]: int(r["n"])
        for r in conn.execute(
            """
            SELECT meeting_id, COUNT(*) AS n FROM (
                SELECT meeting_id, label FROM speakers
                UNION
                SELECT meeting_id, label FROM speaker_matches
            ) AS labels
            GROUP BY meeting_id
            """
        )
    }


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


# ── People registry ───────────────────────────────────────────────────
#
# The point of this table is determinism. Asking a model to spell a name the same
# way it did four months ago is not a reliable strategy, and every variant it
# invents becomes a separate node in the knowledge graph.


def person_key(name: str) -> str:
    """Return the stable, case-insensitive identity key for a person's name."""
    return name.strip().lower()


def flatten_and_record_merge(conn: sqlite3.Connection, old_spelling: str, canonical: str) -> None:
    """Record a merge and redirect older tombstones straight to its target."""
    old_spelling = old_spelling.strip()
    canonical = canonical.strip()
    old_key = person_key(old_spelling)
    target_key = person_key(canonical)
    if not old_key or not target_key:
        raise ValueError("merge names must not be blank")
    if old_key == target_key:
        raise ValueError("a person cannot be merged into itself")

    target = conn.execute(
        "SELECT canonical FROM people WHERE canonical = ?", (canonical,)
    ).fetchone()
    if target is None:
        raise ValueError(f"merge target is not a living person: {canonical!r}")

    if conn.execute("SELECT 1 FROM merged_names WHERE old_key = ?", (target_key,)).fetchone():
        raise ValueError(f"merge target is already tombstoned: {canonical!r}")

    timestamp = now_iso()
    conn.execute(
        "UPDATE merged_names SET canonical = ?, merged_at = ? WHERE canonical = ?",
        (canonical, timestamp, old_spelling),
    )
    conn.execute(
        """
        INSERT INTO merged_names (old_key, old_spelling, canonical, merged_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(old_key) DO UPDATE SET
            old_spelling = excluded.old_spelling,
            canonical = excluded.canonical,
            merged_at = excluded.merged_at
        """,
        (old_key, old_spelling, canonical, timestamp),
    )


def resolve_merged_name(conn: sqlite3.Connection, name: str) -> str | None:
    """Resolve a retired spelling to its living canonical target."""
    key = person_key(name)
    if not key:
        return None

    resolved: str | None = None
    visited: set[str] = set()
    while key not in visited:
        visited.add(key)
        row = conn.execute(
            "SELECT canonical FROM merged_names WHERE old_key = ?", (key,)
        ).fetchone()
        if row is None:
            if resolved is None:
                return None
            living = conn.execute(
                "SELECT 1 FROM people WHERE canonical = ?", (resolved,)
            ).fetchone()
            return resolved if living is not None else None
        resolved = row["canonical"]
        key = person_key(resolved)
    return None


def affected_meeting_ids(conn: sqlite3.Connection, names: Iterable[str]) -> set[str]:
    """Return meetings containing any supplied name in a person-bearing field."""
    values = tuple({name.strip() for name in names if name and name.strip()})
    if not values:
        return set()

    placeholders = ", ".join("?" for _ in values)
    rows = conn.execute(
        f"""
        SELECT meeting_id FROM speakers WHERE name IN ({placeholders})
        UNION SELECT meeting_id FROM entities WHERE name IN ({placeholders})
        UNION SELECT meeting_id FROM relations
            WHERE subject IN ({placeholders}) OR object IN ({placeholders})
        UNION SELECT meeting_id FROM commitments WHERE owner IN ({placeholders})
        UNION SELECT meeting_id FROM decisions WHERE decided_by IN ({placeholders})
        UNION SELECT meeting_id FROM open_questions WHERE owner IN ({placeholders})
        """,
        values * 7,
    ).fetchall()
    return {row["meeting_id"] for row in rows}


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
    if conn.execute(
        "SELECT 1 FROM merged_names WHERE old_key = ?", (person_key(canonical),)
    ).fetchone():
        raise ValueError(f"cannot register tombstoned person: {canonical!r}")
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
    if row:
        return row["canonical"]
    return resolve_merged_name(conn, name) or name.strip()


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


# ── Commitments, decisions and open questions ─────────────────────────

def replace_commitments(
    conn: sqlite3.Connection, meeting_id: str, commitments: list[dict[str, object]]
) -> None:
    """Store a meeting's parsed action items, replacing any prior set.

    Replace rather than append, for the same reason replace_entities does: a
    recompile must not leave the previous run's commitments duplicated
    underneath the new ones.
    """
    conn.execute("DELETE FROM commitments WHERE meeting_id = ?", (meeting_id,))
    ts = now_iso()
    for item in commitments:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO commitments
                (meeting_id, owner, text, due_date, due_date_iso, timestamp_cite, state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id, item.get("owner"), text, item.get("due_date"),
                item.get("due_date_iso"), item.get("timestamp_cite"),
                item.get("state") or "open", ts,
            ),
        )


def replace_decisions(
    conn: sqlite3.Connection, meeting_id: str, decisions: list[dict[str, object]]
) -> None:
    """Store a meeting's parsed decisions, replacing any prior set."""
    conn.execute("DELETE FROM decisions WHERE meeting_id = ?", (meeting_id,))
    ts = now_iso()
    for item in decisions:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO decisions
                (meeting_id, text, decided_by, rationale, timestamp_cite, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (meeting_id, text, item.get("decided_by"), item.get("rationale"),
             item.get("timestamp_cite"), ts),
        )


def replace_open_questions(
    conn: sqlite3.Connection, meeting_id: str, questions: list[dict[str, object]]
) -> None:
    """Store a meeting's parsed open questions, replacing any prior set."""
    conn.execute("DELETE FROM open_questions WHERE meeting_id = ?", (meeting_id,))
    ts = now_iso()
    for item in questions:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO open_questions (meeting_id, text, owner, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (meeting_id, text, item.get("owner"), ts),
        )


def get_decisions(conn: sqlite3.Connection, meeting_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT * FROM decisions WHERE meeting_id = ? ORDER BY id", (meeting_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_open_questions(conn: sqlite3.Connection, meeting_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT * FROM open_questions WHERE meeting_id = ? ORDER BY id", (meeting_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_commitments(
    conn: sqlite3.Connection, owner: str | None = None, overdue: bool = False
) -> list[dict[str, object]]:
    """Commitments across the corpus, joined with meeting context.

    `overdue` implies open: a completed commitment past its due date is not
    a thing to chase, so it is excluded rather than shown crossed out.
    """
    sql = """
        SELECT c.*, m.meeting_date, m.meeting_time, m.title_hint, m.source_name, m.minutes_path
        FROM commitments c
        JOIN meetings m ON m.id = c.meeting_id
        WHERE 1 = 1
    """
    params: list[object] = []
    if owner:
        sql += " AND LOWER(c.owner) = LOWER(?)"
        params.append(owner)
    if overdue:
        sql += " AND c.state = 'open' AND c.due_date_iso IS NOT NULL AND c.due_date_iso < ?"
        params.append(today_iso())
    sql += " ORDER BY c.state, c.due_date_iso IS NULL, c.due_date_iso, c.owner"
    rows = conn.execute(sql, params).fetchall()
    today = today_iso()
    return [
        {**dict(r), "overdue": bool(r["state"] == "open" and r["due_date_iso"] and r["due_date_iso"] < today)}
        for r in rows
    ]


def list_decisions(conn: sqlite3.Connection, topic: str | None = None) -> list[dict[str, object]]:
    """Decisions across the corpus, joined with meeting context, newest first."""
    sql = """
        SELECT d.*, m.meeting_date, m.meeting_time, m.title_hint, m.source_name, m.minutes_path
        FROM decisions d
        JOIN meetings m ON m.id = d.meeting_id
    """
    params: list[object] = []
    if topic:
        like = f"%{topic}%"
        sql += " WHERE d.text LIKE ? OR d.decided_by LIKE ? OR d.rationale LIKE ?"
        params += [like, like, like]
    sql += " ORDER BY m.meeting_date DESC, m.meeting_time DESC, d.id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_open_questions(
    conn: sqlite3.Connection, topic: str | None = None
) -> list[dict[str, object]]:
    """Open questions across the corpus, joined with meeting context.

    The third register, and the only one with no reader until now: 148 rows were
    parsed at compile time and had neither an endpoint nor a retrieval path. An
    unanswered question is often the most useful thing in a meeting - it is the
    thing still owed.
    """
    sql = """
        SELECT q.*, m.meeting_date, m.meeting_time, m.title_hint, m.source_name, m.minutes_path
        FROM open_questions q
        JOIN meetings m ON m.id = q.meeting_id
    """
    params: list[object] = []
    if topic:
        like = f"%{topic}%"
        sql += " WHERE q.text LIKE ? OR q.owner LIKE ?"
        params += [like, like]
    sql += " ORDER BY m.meeting_date DESC, m.meeting_time DESC, q.id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


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


def recent_stage_failures(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, object]]:
    """Most recent failed stage_runs, each carrying enough meeting context to
    read on its own.

    `detail` is written to stage_runs on every run, but until now it was only
    ever read back in aggregate by `stage_timings`. That means a meeting that
    failed a stage and was later retried successfully leaves no trace anywhere
    a human looks: its CURRENT status is healthy, and `pending(conn, FAILED)`
    only ever sees meetings failing RIGHT NOW. This is that missing history.

    Includes junk-recording parks alongside genuine crashes - both set ok=0 on
    the stage_run - but each row's `detail` says which: a real failure reads
    "TypeError: ..." and a park reads "junk recording: ...". Ordered newest
    first, capped at `limit` so a bad week does not flood the status output.
    """
    rows = conn.execute(
        """
        SELECT sr.stage, sr.started_at, sr.finished_at, sr.detail,
               m.id AS meeting_id, m.meeting_date, m.title_hint, m.source_name
        FROM stage_runs sr
        JOIN meetings m ON m.id = sr.meeting_id
        WHERE sr.ok = 0
        ORDER BY sr.started_at DESC, sr.rowid DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        # Same shape as Meeting.label, computed here because this is a raw join
        # row rather than a Meeting instance.
        date = r["meeting_date"] or "????-??-??"
        hint = r["title_hint"] or r["source_name"]
        out.append(
            {
                "meeting_id": r["meeting_id"],
                "label": f"{date} {hint} ({r['meeting_id'][:12]})",
                "stage": r["stage"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "detail": r["detail"],
            }
        )
    return out


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


# ── Voice enrollment ──────────────────────────────────────────────────

def upsert_speaker_match(conn: sqlite3.Connection, meeting_id: str, label: str, **fields) -> None:
    """Insert or update one diarized label's match row.

    Only the supplied fields are written, so the daytime pass can record the
    embedding and snippets before any matching has happened, and the matcher can
    fill in scores later without clobbering them.
    """
    allowed = {
        "embedding", "dim", "model", "speech_sec", "snippet_paths", "snippet_quality",
        "best_canonical", "best_score", "next_canonical", "next_score", "llm_name",
        "band", "state", "cluster_id", "resolved_as",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown speaker_matches fields: {sorted(unknown)}")

    ts = now_iso()
    conn.execute(
        """
        INSERT INTO speaker_matches (meeting_id, label, state, created_at, updated_at)
        VALUES (?, ?, 'pending', ?, ?)
        ON CONFLICT(meeting_id, label) DO NOTHING
        """,
        (meeting_id, label, ts, ts),
    )
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE speaker_matches SET {assignments}, updated_at = ? "
        "WHERE meeting_id = ? AND label = ?",
        (*fields.values(), ts, meeting_id, label),
    )


def set_match_llm_name(
    conn: sqlite3.Connection, meeting_id: str, label: str, name: str | None
) -> None:
    """Record the transcript pass's conclusion against an existing match row.

    An UPDATE rather than `upsert_speaker_match`, because that would CREATE a row
    for every diarized label. A row with no embedding is invisible to matching,
    clustering and the review queue alike, so manufacturing them would fill the
    table with records nothing can ever act on.
    """
    conn.execute(
        "UPDATE speaker_matches SET llm_name = ?, updated_at = ? "
        "WHERE meeting_id = ? AND label = ?",
        (name, now_iso(), meeting_id, label),
    )


def get_speaker_match(conn: sqlite3.Connection, meeting_id: str, label: str):
    return conn.execute(
        "SELECT * FROM speaker_matches WHERE meeting_id = ? AND label = ?",
        (meeting_id, label),
    ).fetchone()


def pending_matches(conn: sqlite3.Connection, model: str | None = None) -> list[sqlite3.Row]:
    """Every unresolved label that carries an embedding.

    Dismissed rows are excluded but not deleted: over-segmentation means a
    "not a real speaker" fragment is sometimes a real person who barely spoke,
    and destroying that evidence would be unrecoverable.
    """
    sql = "SELECT * FROM speaker_matches WHERE state = 'pending' AND embedding IS NOT NULL"
    params: list[object] = []
    if model:
        sql += " AND model = ?"
        params.append(model)
    return list(conn.execute(sql + " ORDER BY speech_sec DESC", params))


def cluster_labels(conn: sqlite3.Connection, cluster_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM speaker_matches WHERE cluster_id = ? AND state = 'pending' "
            "ORDER BY speech_sec DESC",
            (cluster_id,),
        )
    )


def replace_clusters(
    conn: sqlite3.Connection, clusters: list[dict[str, object]], *, model: str
) -> None:
    """Rebuild one namespace's voice_clusters. Idempotent and safe on demand.

    Scoped to `model` because the unscoped version destroyed every namespace's
    clusters whenever any one of them was reclustered - which is how an empty
    namespace came to wipe the live review queue on each dashboard load.
    `voice_clusters` carries no model column and needs none: membership lives in
    speaker_matches, which does.
    """
    ts = now_iso()
    conn.execute(
        """
        DELETE FROM voice_clusters WHERE id IN (
            SELECT DISTINCT cluster_id FROM speaker_matches
            WHERE model = ? AND cluster_id IS NOT NULL
        )
        """,
        (model,),
    )
    conn.execute(
        "UPDATE speaker_matches SET cluster_id = NULL WHERE state = 'pending' AND model = ?",
        (model,),
    )
    for cluster in clusters:
        conn.execute(
            """
            INSERT INTO voice_clusters
                (id, size, total_speech, best_canonical, best_score,
                 next_canonical, next_score, band, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cluster["id"], cluster["size"], cluster["total_speech"],
                cluster.get("best_canonical"), cluster.get("best_score"),
                cluster.get("next_canonical"), cluster.get("next_score"),
                cluster["band"], ts,
            ),
        )
        for meeting_id, label in cluster["members"]:  # type: ignore[union-attr]
            conn.execute(
                "UPDATE speaker_matches SET cluster_id = ?, updated_at = ? "
                "WHERE meeting_id = ? AND label = ?",
                (cluster["id"], ts, meeting_id, label),
            )

    # Sweep clusters no row points at any more. Without this the wreckage of an
    # earlier unscoped rebuild would sit in the queue forever, offering cards
    # whose members cannot be loaded.
    conn.execute(
        """
        DELETE FROM voice_clusters WHERE id NOT IN (
            SELECT cluster_id FROM speaker_matches
            WHERE cluster_id IS NOT NULL AND state = 'pending'
        )
        """
    )


def pending_clusters(
    conn: sqlite3.Connection, limit: int | None = None
) -> list[sqlite3.Row]:
    """The review queue, most valuable first.

    Ordered by total speaking time so the earliest answers resolve the most
    history: in a personal archive a handful of recurring people dominate.

    Unbounded by default. The old default of 50 sat 5 rows above the real queue
    and had already been worked around at the one call site that noticed
    (voices.py passed 10_000). A cap that silently drops rows turns any filter
    built on top of this into a confident lie, so callers that want a cap now
    say so.
    """
    sql = "SELECT * FROM voice_clusters ORDER BY total_speech DESC"
    if limit is None:
        return list(conn.execute(sql))
    return list(conn.execute(f"{sql} LIMIT ?", (limit,)))


def add_voice_sample(
    conn: sqlite3.Connection,
    *,
    canonical: str,
    meeting_id: str | None,
    label: str | None,
    embedding: bytes,
    dim: int,
    model: str,
    speech_sec: float,
    source: str = "confirmed",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO voice_samples
            (canonical, meeting_id, label, embedding, dim, model, speech_sec, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (canonical, meeting_id, label, embedding, dim, model, speech_sec, source, now_iso()),
    )
    return int(cursor.lastrowid or 0)


def voice_sample_exists(
    conn: sqlite3.Connection, canonical: str, meeting_id: str, label: str, model: str
) -> bool:
    """Whether this exact (person, meeting, label, namespace) is already enrolled.

    `add_voice_sample` has no unique constraint by design - each human
    confirmation is meant to be able to add a fresh row - so the caller doing
    automatic bootstrap enrollment owns not inserting the same evidence twice.
    """
    return (
        conn.execute(
            "SELECT 1 FROM voice_samples WHERE canonical = ? AND meeting_id = ? "
            "AND label = ? AND model = ? LIMIT 1",
            (canonical, meeting_id, label, model),
        ).fetchone()
        is not None
    )


def person_samples(
    conn: sqlite3.Connection, canonical: str, model: str | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM voice_samples WHERE canonical = ?"
    params: list[object] = [canonical]
    if model:
        sql += " AND model = ?"
        params.append(model)
    return list(conn.execute(sql + " ORDER BY created_at", params))


def enrolled_names(conn: sqlite3.Connection, model: str) -> list[str]:
    return [
        r["canonical"]
        for r in conn.execute(
            "SELECT DISTINCT canonical FROM voice_samples WHERE model = ? ORDER BY canonical",
            (model,),
        )
    ]


def sample_meeting_count(conn: sqlite3.Connection, canonical: str, model: str) -> int:
    """Distinct meetings backing a person's voiceprint.

    Gates auto-matching. With one phone on a table, a person enrolled from a
    single meeting is enrolled from a single seat, and the same colleague across
    the table next week embeds differently enough to be mistaken for someone else.
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(meeting_id, CAST(id AS TEXT))) AS n "
        "FROM voice_samples WHERE canonical = ? AND model = ?",
        (canonical, model),
    ).fetchone()
    return int(row["n"]) if row else 0


def delete_voice_sample(conn: sqlite3.Connection, sample_id: int) -> None:
    conn.execute("DELETE FROM voice_samples WHERE id = ?", (sample_id,))


def reassign_voice_samples(conn: sqlite3.Connection, source: str, target: str) -> int:
    cursor = conn.execute(
        "UPDATE voice_samples SET canonical = ? WHERE canonical = ?", (target, source)
    )
    return int(cursor.rowcount or 0)


def delete_person_voice_data(conn: sqlite3.Connection, canonical: str) -> int:
    """Remove every sample for one person. Returns the count deleted.

    Snippet files on disk are the caller's responsibility - see voices.forget().
    """
    cursor = conn.execute("DELETE FROM voice_samples WHERE canonical = ?", (canonical,))
    return int(cursor.rowcount or 0)


def get_setting_float(conn: sqlite3.Connection, key: str, default: float) -> float:
    raw = get_setting(conn, key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ── Ask AI conversation history ──────────────────────────────────────

def append_chat_turn(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    question: str,
    answer: str,
    mode: str | None,
    provider: str | None,
    synthesized: bool,
    context_chars: int | None,
    retrieval_sec: float | None,
    synthesis_sec: float | None,
) -> int:
    """Append one turn to a session and return its turn_index.

    The index is computed here, not trusted from the caller: two requests for
    the same session landing in the same transaction race onto the same
    COALESCE(MAX(...)+1, 0) read, and the UNIQUE(session_id, turn_index)
    constraint turns that race into a clear IntegrityError instead of a
    silently overwritten turn.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(turn_index) + 1, 0) AS next_index FROM chat_turns "
        "WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    turn_index = int(row["next_index"])
    conn.execute(
        """
        INSERT INTO chat_turns (
            session_id, turn_index, question, answer, mode, provider, synthesized,
            context_chars, retrieval_sec, synthesis_sec, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id, turn_index, question, answer, mode, provider,
            1 if synthesized else 0, context_chars, retrieval_sec, synthesis_sec, now_iso(),
        ),
    )
    return turn_index


def recent_chat_turns(
    conn: sqlite3.Connection, session_id: str, limit: int = 6
) -> list[dict[str, object]]:
    """The most recent turns of a session, oldest first.

    Oldest-first because this feeds straight into a prompt in conversation
    order; the DESC index that makes "most recent N" cheap to fetch is undone
    with a single reverse here rather than in SQL.
    """
    rows = conn.execute(
        "SELECT * FROM chat_turns WHERE session_id = ? ORDER BY turn_index DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def clear_chat_session(conn: sqlite3.Connection, session_id: str) -> int:
    """Delete every turn of a session ("New conversation"). Returns rows removed."""
    cursor = conn.execute("DELETE FROM chat_turns WHERE session_id = ?", (session_id,))
    return int(cursor.rowcount or 0)
