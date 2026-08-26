"""Preview-bound people merges and recoverable minutes rewrites.

This module is the single workflow seam used by CLI and dashboard adapters.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pipeline import config, db
from pipeline.rename_minutes import plan_text

_COMMON_WORD_NAMES = frozenset({"may"})


@dataclass(frozen=True)
class MergePreview:
    digest: str
    requested_target: str
    actual_target: str
    source_names: tuple[str, ...]
    speaker_rows: int
    affected_meetings: int
    files_changed: int
    literal_matches: int
    missing_files: tuple[str, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class MergeResult:
    target: str
    speaker_rows: int
    minutes_rewritten: int
    minutes_unchanged: int
    minutes_missing: int
    rewrite_conflicts: int
    pending_rewrites: int


class PreviewDriftError(ValueError):
    """The approved preview no longer describes the current merge inputs."""


@dataclass(frozen=True)
class _FileRewrite:
    meeting_id: str
    minutes_path: str | None
    before_sha256: str | None
    after_sha256: str | None
    before_text: str | None
    after_text: str | None
    state: str
    matched_spellings: tuple[str, ...] = ()
    match_count: int = 0

    def digest_payload(self) -> dict[str, object]:
        return {
            "meeting_id": self.meeting_id,
            "minutes_path": self.minutes_path,
            "state": self.state,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "matched_spellings": self.matched_spellings,
            "match_count": self.match_count,
        }


@dataclass(frozen=True)
class _MergePlan:
    public: MergePreview
    absorbed_names: tuple[str, ...]
    affected_ids: tuple[str, ...]
    mappings: dict[str, str]
    file_rewrites: tuple[_FileRewrite, ...]


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _build_plan(
    conn: sqlite3.Connection,
    database_path: Path,
    names: Iterable[str],
    into: str,
) -> _MergePlan:
    source_names = tuple(name.strip() for name in names)
    requested_target = into.strip()
    if not source_names:
        raise ValueError("at least one source is required")
    if not requested_target:
        raise ValueError("target must not be blank")
    if len({db.person_key(name) for name in source_names}) != len(source_names):
        raise ValueError("sources must be unique")

    for source_name in source_names:
        source = conn.execute(
            "SELECT canonical FROM people WHERE canonical = ?", (source_name,)
        ).fetchone()
        if source is None:
            raise ValueError(f"source does not exist: {source_name!r}")

    target = conn.execute(
        "SELECT canonical FROM people WHERE canonical = ?", (requested_target,)
    ).fetchone()
    if target:
        actual_target = target["canonical"]
    else:
        actual_target = db.resolve_merged_name(conn, requested_target) or requested_target
    absorbed_names = tuple(name for name in source_names if name != actual_target)
    if not absorbed_names:
        raise ValueError("merge would not change any source")
    affected_ids = tuple(sorted(db.affected_meeting_ids(conn, absorbed_names)))
    placeholders = ", ".join("?" for _ in absorbed_names)
    speaker_rows = conn.execute(
        f"SELECT COUNT(*) AS count FROM speakers WHERE name IN ({placeholders})",
        absorbed_names,
    ).fetchone()["count"]

    mappings = {name: actual_target for name in absorbed_names}
    file_rewrites: list[_FileRewrite] = []
    missing_files: list[str] = []
    files_changed = 0
    literal_matches = 0
    for meeting_id in affected_ids:
        row = conn.execute(
            "SELECT minutes_path FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        raw_path = row["minutes_path"] if row else None
        minutes_path = Path(raw_path) if raw_path else None
        if minutes_path is None or not minutes_path.is_file():
            missing_files.append(meeting_id)
            file_rewrites.append(
                _FileRewrite(
                    meeting_id=meeting_id,
                    minutes_path=str(minutes_path) if minutes_path else None,
                    before_sha256=None,
                    after_sha256=None,
                    before_text=None,
                    after_text=None,
                    state="missing",
                )
            )
            continue

        before_bytes = minutes_path.read_bytes()
        before_text = before_bytes.decode("utf-8")
        rewrite = plan_text(before_text, mappings)
        after_bytes = rewrite.after.encode("utf-8")
        literal_matches += rewrite.match_count
        state = "changed" if after_bytes != before_bytes else "unchanged"
        if state == "changed":
            files_changed += 1
        file_rewrites.append(
            _FileRewrite(
                meeting_id=meeting_id,
                minutes_path=str(minutes_path.resolve()),
                before_sha256=_sha256_bytes(before_bytes),
                after_sha256=_sha256_bytes(after_bytes),
                before_text=before_text,
                after_text=rewrite.after,
                state=state,
                matched_spellings=rewrite.matched_spellings,
                match_count=rewrite.match_count,
            )
        )

    conflicts = tuple(
        f"Common-word source {name!r} matched {literal_matches} literals"
        for name in absorbed_names
        if db.person_key(name) in _COMMON_WORD_NAMES and literal_matches
    )

    participant_names = tuple(sorted({*source_names, actual_target}))
    participant_placeholders = ", ".join("?" for _ in participant_names)
    people_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT canonical, role, notes, created_at
            FROM people
            WHERE canonical IN ({participant_placeholders})
            ORDER BY canonical
            """,
            participant_names,
        )
    ]
    alias_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT alias, canonical, created_at
            FROM person_aliases
            WHERE canonical IN ({participant_placeholders})
            ORDER BY alias
            """,
            participant_names,
        )
    ]
    participant_keys = tuple(sorted({db.person_key(name) for name in participant_names}))
    key_placeholders = ", ".join("?" for _ in participant_keys)
    tombstone_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT old_key, old_spelling, canonical, merged_at
            FROM merged_names
            WHERE old_key IN ({key_placeholders})
               OR canonical IN ({participant_placeholders})
            ORDER BY old_key
            """,
            (*participant_keys, *participant_names),
        )
    ]

    payload: dict[str, object] = {
        "database_path": str(database_path.resolve()),
        "requested_target": requested_target,
        "actual_target": actual_target,
        "source_names": source_names,
        "absorbed_names": absorbed_names,
        "affected_meeting_ids": affected_ids,
        "mappings": mappings,
        "files": [file_rewrite.digest_payload() for file_rewrite in file_rewrites],
        "people": people_rows,
        "aliases": alias_rows,
        "tombstones": tombstone_rows,
        "conflicts": conflicts,
    }
    public = MergePreview(
        digest=_digest(payload),
        requested_target=requested_target,
        actual_target=actual_target,
        source_names=source_names,
        speaker_rows=speaker_rows,
        affected_meetings=len(affected_ids),
        files_changed=files_changed,
        literal_matches=literal_matches,
        missing_files=tuple(missing_files),
        conflicts=conflicts,
    )
    return _MergePlan(
        public=public,
        absorbed_names=absorbed_names,
        affected_ids=affected_ids,
        mappings=mappings,
        file_rewrites=tuple(file_rewrites),
    )


def preview(names: Iterable[str], into: str) -> MergePreview:
    """Return the exact, read-only impact of a proposed people merge."""
    database_path = Path(config.DB_PATH)
    with db.connect(database_path) as conn:
        return _build_plan(conn, database_path, names, into).public


def _rewrite_structured_rows(
    conn: sqlite3.Connection, source: str, target: str
) -> None:
    conn.execute(
        "UPDATE voice_samples SET canonical = ? WHERE canonical = ?", (target, source)
    )
    for column in ("resolved_as", "best_canonical", "next_canonical"):
        conn.execute(
            f"UPDATE speaker_matches SET {column} = ? WHERE {column} = ?",
            (target, source),
        )
    for column in ("best_canonical", "next_canonical"):
        conn.execute(
            f"UPDATE voice_clusters SET {column} = ? WHERE {column} = ?",
            (target, source),
        )
    conn.execute("UPDATE speakers SET name = ? WHERE name = ?", (target, source))

    conn.execute(
        """
        INSERT OR IGNORE INTO entities (meeting_id, name, kind, description)
        SELECT meeting_id, ?, kind, description FROM entities WHERE name = ?
        """,
        (target, source),
    )
    conn.execute("DELETE FROM entities WHERE name = ?", (source,))
    conn.execute(
        """
        INSERT OR IGNORE INTO relations (meeting_id, subject, predicate, object)
        SELECT meeting_id,
               CASE WHEN subject = ? THEN ? ELSE subject END,
               predicate,
               CASE WHEN object = ? THEN ? ELSE object END
        FROM relations WHERE subject = ? OR object = ?
        """,
        (source, target, source, target, source, source),
    )
    conn.execute(
        "DELETE FROM relations WHERE subject = ? OR object = ?", (source, source)
    )
    conn.execute(
        "UPDATE commitments SET owner = ? WHERE owner = ?", (target, source)
    )
    conn.execute(
        "UPDATE decisions SET decided_by = ? WHERE decided_by = ?", (target, source)
    )
    conn.execute(
        "UPDATE open_questions SET owner = ? WHERE owner = ?", (target, source)
    )


def _deduplicate_match_suggestions(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE speaker_matches
        SET next_canonical = NULL, next_score = NULL
        WHERE best_canonical IS NOT NULL AND best_canonical = next_canonical
        """
    )
    conn.execute(
        """
        UPDATE voice_clusters
        SET next_canonical = NULL, next_score = NULL
        WHERE best_canonical IS NOT NULL AND best_canonical = next_canonical
        """
    )


def _enqueue_rewrite_jobs(conn: sqlite3.Connection, plan: _MergePlan) -> None:
    if not plan.file_rewrites:
        return
    operation_id = plan.public.digest
    mappings_json = json.dumps(
        plan.mappings, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    created_at = config.now_iso()
    for file_rewrite in plan.file_rewrites:
        job_id = _digest(
            {"operation_id": operation_id, "meeting_id": file_rewrite.meeting_id}
        )
        conn.execute(
            """
            INSERT INTO minute_rewrite_jobs
                (id, operation_id, meeting_id, minutes_path, mappings_json,
                 before_sha256, after_sha256, before_text, after_text,
                 state, error, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)
            """,
            (
                job_id,
                operation_id,
                file_rewrite.meeting_id,
                file_rewrite.minutes_path,
                mappings_json,
                file_rewrite.before_sha256,
                file_rewrite.after_sha256,
                file_rewrite.before_text,
                file_rewrite.after_text,
                created_at,
            ),
        )

    placeholders = ", ".join("?" for _ in plan.affected_ids)
    conn.execute(
        f"""
        UPDATE meetings
        SET status = ?, updated_at = ?
        WHERE id IN ({placeholders})
          AND status IN (?, ?, ?)
        """,
        (
            db.SPEAKERS_RESOLVED,
            created_at,
            *plan.affected_ids,
            db.SPEAKERS_RESOLVED,
            db.MINUTES_COMPILED,
            db.INDEXED,
        ),
    )


def _atomic_replace(path: Path, job_id: str, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{job_id}.rewrite.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _resume_pending_rewrites(operation_id: str | None = None) -> MergeResult:
    database_path = Path(config.DB_PATH)
    rewritten = 0
    unchanged = 0
    missing = 0
    conflicts = 0
    targets: set[str] = set()
    with db.connect(database_path) as conn:
        sql = """
            SELECT * FROM minute_rewrite_jobs
            WHERE state IN ('pending', 'missing', 'conflict')
        """
        params: tuple[str, ...] = ()
        if operation_id is not None:
            sql += " AND operation_id = ?"
            params = (operation_id,)
        sql += " ORDER BY created_at, id"
        jobs = conn.execute(sql, params).fetchall()

        for job in jobs:
            with suppress(AttributeError, json.JSONDecodeError):
                targets.update(json.loads(job["mappings_json"]).values())

            raw_path = job["minutes_path"]
            path = Path(raw_path) if raw_path else None
            if path is None or not path.is_file():
                missing += 1
                conn.execute(
                    """
                    UPDATE minute_rewrite_jobs
                    SET state = 'missing', error = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    ("minutes file is missing", config.now_iso(), job["id"]),
                )
                continue

            current_sha256 = _sha256_bytes(path.read_bytes())
            before_sha256 = job["before_sha256"]
            after_sha256 = job["after_sha256"]
            if current_sha256 == after_sha256:
                state = "unchanged" if before_sha256 == after_sha256 else "applied"
            elif current_sha256 != before_sha256:
                conflicts += 1
                conn.execute(
                    """
                    UPDATE minute_rewrite_jobs
                    SET state = 'conflict', error = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (
                        "current file hash differs from approved before and after hashes",
                        config.now_iso(),
                        job["id"],
                    ),
                )
                continue
            elif before_sha256 == after_sha256:
                state = "unchanged"
            elif job["after_text"] is None:
                conflicts += 1
                conn.execute(
                    """
                    UPDATE minute_rewrite_jobs
                    SET state = 'conflict', error = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    ("approved rewrite has no after text", config.now_iso(), job["id"]),
                )
                continue
            else:
                try:
                    _atomic_replace(path, job["id"], job["after_text"])
                except OSError as exc:
                    conn.execute(
                        """
                        UPDATE minute_rewrite_jobs
                        SET state = 'pending', error = ?, finished_at = NULL
                        WHERE id = ?
                        """,
                        (str(exc), job["id"]),
                    )
                    continue
                state = "applied"

            if state == "applied":
                rewritten += 1
            else:
                unchanged += 1
            finished_at = config.now_iso()
            conn.execute(
                """
                UPDATE minute_rewrite_jobs
                SET state = ?, error = NULL, finished_at = ?
                WHERE id = ?
                """,
                (state, finished_at, job["id"]),
            )
            conn.execute(
                """
                UPDATE meetings
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (db.MINUTES_COMPILED, finished_at, job["meeting_id"]),
            )

        pending_sql = """
            SELECT COUNT(*) AS count FROM minute_rewrite_jobs
            WHERE state = 'pending'
        """
        pending_params: tuple[str, ...] = ()
        if operation_id is not None:
            pending_sql += " AND operation_id = ?"
            pending_params = (operation_id,)
        pending = conn.execute(pending_sql, pending_params).fetchone()["count"]

    return MergeResult(
        target=next(iter(targets)) if len(targets) == 1 else "",
        speaker_rows=0,
        minutes_rewritten=rewritten,
        minutes_unchanged=unchanged,
        minutes_missing=missing,
        rewrite_conflicts=conflicts,
        pending_rewrites=pending,
    )


def resume_pending_rewrites() -> MergeResult:
    """Safely drain every incomplete minutes rewrite job."""
    return _resume_pending_rewrites()


def merge(
    names: Iterable[str], into: str, *, expected_digest: str
) -> MergeResult:
    """Apply only the exact merge impact approved through :func:`preview`."""
    source_names = tuple(names)
    database_path = Path(config.DB_PATH)
    with db.connect(database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        plan = _build_plan(conn, database_path, source_names, into)
        if not expected_digest or plan.public.digest != expected_digest:
            raise PreviewDriftError(
                "merge preview changed; generate and approve a new preview"
            )

        target_row = conn.execute(
            "SELECT role FROM people WHERE canonical = ?", (plan.public.actual_target,)
        ).fetchone()
        if target_row is None:
            role = next(
                (
                    row["role"]
                    for source_name in source_names
                    if (
                        row := conn.execute(
                            "SELECT role FROM people WHERE canonical = ?",
                            (source_name,),
                        ).fetchone()
                    )
                    and row["role"]
                ),
                None,
            )
            db.add_person(conn, plan.public.actual_target, role=role)

        for source_name in plan.absorbed_names:
            db.flatten_and_record_merge(conn, source_name, plan.public.actual_target)
            _rewrite_structured_rows(conn, source_name, plan.public.actual_target)
            source_key = db.person_key(source_name)
            conn.execute(
                """
                UPDATE person_aliases SET canonical = ?
                WHERE canonical = ? AND alias != ?
                """,
                (plan.public.actual_target, source_name, source_key),
            )
            conn.execute("DELETE FROM person_aliases WHERE alias = ?", (source_key,))
            conn.execute("DELETE FROM people WHERE canonical = ?", (source_name,))
        _deduplicate_match_suggestions(conn)
        _enqueue_rewrite_jobs(conn, plan)

    rewrite_result = _resume_pending_rewrites(plan.public.digest)
    return MergeResult(
        target=plan.public.actual_target,
        speaker_rows=plan.public.speaker_rows,
        minutes_rewritten=rewrite_result.minutes_rewritten,
        minutes_unchanged=rewrite_result.minutes_unchanged,
        minutes_missing=rewrite_result.minutes_missing,
        rewrite_conflicts=rewrite_result.rewrite_conflicts,
        pending_rewrites=rewrite_result.pending_rewrites,
    )
