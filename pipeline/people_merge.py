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
from pipeline.rename_minutes import discover_spellings, plan_text

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


@dataclass(frozen=True)
class LegacyRepairPreview:
    digest: str
    database_path: str
    mappings: tuple[tuple[str, str], ...]
    excluded_aliases: tuple[str, ...]
    alias_rows: tuple[dict[str, object], ...]
    targets: tuple[dict[str, object], ...]
    suggestion_updates: tuple[dict[str, object], ...]
    proposed_clears: tuple[dict[str, object], ...]
    file_rewrites: tuple[dict[str, object], ...]
    files_changed: int
    literal_matches: int
    missing_files: tuple[str, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class LegacyRepairResult:
    digest: str
    aliases_deleted: int
    suggestions_rewritten: int
    suggestions_cleared: int
    minutes_rewritten: int
    minutes_unchanged: int
    minutes_missing: int
    rewrite_conflicts: int
    pending_rewrites: int
    already_applied: bool = False


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


def _digest_rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, object]]:
    """Make exact database inputs JSON-safe without embedding voice blobs."""
    result: list[dict[str, object]] = []
    for row in rows:
        item: dict[str, object] = {}
        for key, value in dict(row).items():
            if isinstance(value, (bytes, bytearray, memoryview)):
                content = bytes(value)
                item[key] = {
                    "bytes": len(content),
                    "sha256": _sha256_bytes(content),
                }
            else:
                item[key] = value
        result.append(item)
    return result


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

    repeated = absorbed_names * 3
    source_row_queries: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        (
            "speakers",
            f"SELECT * FROM speakers WHERE name IN ({placeholders}) "
            "ORDER BY meeting_id, label",
            absorbed_names,
        ),
        (
            "speaker_matches",
            f"""
            SELECT * FROM speaker_matches
            WHERE resolved_as IN ({placeholders})
               OR best_canonical IN ({placeholders})
               OR next_canonical IN ({placeholders})
            ORDER BY meeting_id, label
            """,
            repeated,
        ),
        (
            "voice_clusters",
            f"""
            SELECT * FROM voice_clusters
            WHERE best_canonical IN ({placeholders})
               OR next_canonical IN ({placeholders})
            ORDER BY id
            """,
            absorbed_names * 2,
        ),
        (
            "voice_samples",
            f"SELECT * FROM voice_samples WHERE canonical IN ({placeholders}) "
            "ORDER BY id",
            absorbed_names,
        ),
        (
            "entities",
            f"SELECT * FROM entities WHERE name IN ({placeholders}) "
            "ORDER BY meeting_id, name",
            absorbed_names,
        ),
        (
            "relations",
            f"""
            SELECT * FROM relations
            WHERE subject IN ({placeholders}) OR object IN ({placeholders})
            ORDER BY meeting_id, subject, predicate, object
            """,
            absorbed_names * 2,
        ),
        (
            "commitments",
            f"SELECT * FROM commitments WHERE owner IN ({placeholders}) ORDER BY id",
            absorbed_names,
        ),
        (
            "decisions",
            f"SELECT * FROM decisions WHERE decided_by IN ({placeholders}) ORDER BY id",
            absorbed_names,
        ),
        (
            "open_questions",
            f"SELECT * FROM open_questions WHERE owner IN ({placeholders}) ORDER BY id",
            absorbed_names,
        ),
    )
    source_rows = {
        name: _digest_rows(conn.execute(sql, params))
        for name, sql, params in source_row_queries
    }

    mappings = {name: actual_target for name in absorbed_names}
    file_rewrites: list[_FileRewrite] = []
    missing_files: list[str] = []
    files_changed = 0
    literal_matches = 0
    common_word_matches = {
        name: 0
        for name in absorbed_names
        if db.person_key(name) in _COMMON_WORD_NAMES
    }
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
        for source_name in common_word_matches:
            common_word_matches[source_name] += plan_text(
                before_text, {source_name: actual_target}
            ).match_count
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
        f"Common-word source {name!r} matched {count} literals"
        for name, count in common_word_matches.items()
        if count
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
        "source_rows": source_rows,
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


def _legacy_suggestion_changes(
    conn: sqlite3.Connection,
    alias_map: dict[str, str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    living = {
        row["canonical"] for row in conn.execute("SELECT canonical FROM people")
    }
    updates: list[dict[str, object]] = []
    clears: list[dict[str, object]] = []
    shapes = (
        ("speaker_matches", ("meeting_id", "label")),
        ("voice_clusters", ("id",)),
    )
    for table, key_columns in shapes:
        for column in ("best_canonical", "next_canonical"):
            selected = ", ".join((*key_columns, column))
            for row in conn.execute(
                f"SELECT {selected} FROM {table} WHERE {column} IS NOT NULL"
            ):
                before = row[column]
                if before in living:
                    continue
                change = {
                    "table": table,
                    "keys": {key: row[key] for key in key_columns},
                    "column": column,
                    "before": before,
                }
                target = alias_map.get(db.person_key(before))
                if target:
                    updates.append({**change, "after": target})
                else:
                    clears.append({**change, "after": None})
    def key(item: dict[str, object]) -> str:
        return json.dumps(item, sort_keys=True, separators=(",", ":"))

    return sorted(updates, key=key), sorted(clears, key=key)


def _build_legacy_preview(
    conn: sqlite3.Connection,
    database_path: Path,
    excluded_aliases: Iterable[str],
) -> LegacyRepairPreview:
    excluded = tuple(
        sorted({db.person_key(alias) for alias in excluded_aliases if alias.strip()})
    )
    excluded_set = set(excluded)
    alias_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT alias, canonical, created_at
            FROM person_aliases
            WHERE alias != LOWER(canonical)
            ORDER BY alias
            """
        )
        if row["alias"] not in excluded_set
    ]
    mappings = tuple((row["alias"], row["canonical"]) for row in alias_rows)
    alias_map = dict(mappings)
    suggestion_updates, proposed_clears = _legacy_suggestion_changes(conn, alias_map)

    file_rewrites: list[dict[str, object]] = []
    missing_files: list[str] = []
    conflicts: list[str] = []
    literal_matches = 0
    alias_match_counts = {alias: 0 for alias in alias_map}
    rows = conn.execute(
        "SELECT id, status, minutes_path FROM meetings ORDER BY id"
    ).fetchall()
    for row in rows:
        raw_path = row["minutes_path"]
        path = Path(raw_path) if raw_path else None
        if path is None or not path.is_file():
            if row["status"] == db.SPEAKERS_RESOLVED:
                missing_files.append(row["id"])
            continue
        before_bytes = path.read_bytes()
        try:
            before_text = before_bytes.decode("utf-8")
        except UnicodeDecodeError:
            conflicts.append(f"Minutes file for {row['id']} is not valid UTF-8")
            continue

        exact_mappings: dict[str, str] = {}
        for alias, target in mappings:
            spellings = discover_spellings(before_text, alias)
            for spelling in spellings:
                exact_mappings[spelling] = target
            if spellings:
                alias_match_counts[alias] += plan_text(
                    before_text, {spelling: target for spelling in spellings}
                ).match_count
        rewrite = plan_text(before_text, exact_mappings)
        if not rewrite.match_count:
            continue
        after_bytes = rewrite.after.encode("utf-8")
        literal_matches += rewrite.match_count
        file_rewrites.append(
            {
                "meeting_id": row["id"],
                "minutes_path": str(path.resolve()),
                "mappings": tuple(sorted(exact_mappings.items())),
                "matched_spellings": rewrite.matched_spellings,
                "match_count": rewrite.match_count,
                "before_sha256": _sha256_bytes(before_bytes),
                "after_sha256": _sha256_bytes(after_bytes),
            }
        )

    conflicts.extend(
        f"Common-word alias {alias!r} matched {count} literals"
        for alias, count in sorted(alias_match_counts.items())
        if alias in _COMMON_WORD_NAMES and count
    )
    targets = [
        dict(row)
        for row in conn.execute(
            """
            SELECT canonical, role, notes, created_at
            FROM people
            WHERE canonical IN (
                SELECT DISTINCT canonical FROM person_aliases
                WHERE alias != LOWER(canonical)
            )
            ORDER BY canonical
            """
        )
    ]
    payload: dict[str, object] = {
        "database_path": str(database_path.resolve()),
        "excluded_aliases": excluded,
        "alias_rows": alias_rows,
        "mappings": mappings,
        "targets": targets,
        "suggestion_updates": suggestion_updates,
        "proposed_clears": proposed_clears,
        "file_rewrites": file_rewrites,
        "missing_files": missing_files,
        "conflicts": conflicts,
    }
    return LegacyRepairPreview(
        digest=_digest(payload),
        database_path=str(database_path.resolve()),
        mappings=mappings,
        excluded_aliases=excluded,
        alias_rows=tuple(alias_rows),
        targets=tuple(targets),
        suggestion_updates=tuple(suggestion_updates),
        proposed_clears=tuple(proposed_clears),
        file_rewrites=tuple(file_rewrites),
        files_changed=len(file_rewrites),
        literal_matches=literal_matches,
        missing_files=tuple(missing_files),
        conflicts=tuple(conflicts),
    )


def preview_legacy_repair(
    *, excluded_aliases: Iterable[str] = ()
) -> LegacyRepairPreview:
    """Inventory legacy merge damage without mutating the manifest or minutes."""
    database_path = Path(config.DB_PATH)
    with db.connect(database_path) as conn:
        return _build_legacy_preview(conn, database_path, excluded_aliases)


def legacy_repair_artifact(preview_result: LegacyRepairPreview) -> dict[str, object]:
    """Serialize the complete proposal that the preview digest binds."""
    proposal: dict[str, object] = {
        "database_path": preview_result.database_path,
        "excluded_aliases": preview_result.excluded_aliases,
        "alias_rows": preview_result.alias_rows,
        "mappings": preview_result.mappings,
        "targets": preview_result.targets,
        "suggestion_updates": preview_result.suggestion_updates,
        "proposed_clears": preview_result.proposed_clears,
        "file_rewrites": preview_result.file_rewrites,
        "missing_files": preview_result.missing_files,
        "conflicts": preview_result.conflicts,
    }
    if _digest(proposal) != preview_result.digest:
        raise ValueError("legacy repair preview payload does not match its digest")
    return {"version": 1, "digest": preview_result.digest, "proposal": proposal}


def write_legacy_repair_preview(
    preview_result: LegacyRepairPreview, path: Path
) -> Path:
    """Atomically write one private, digest-bound legacy repair artifact."""
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        legacy_repair_artifact(preview_result),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    _atomic_replace(resolved, "preview", content + "\n")
    return resolved


def _load_legacy_repair_artifact(path: Path) -> tuple[str, dict[str, object]]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read legacy repair preview: {exc}") from exc
    if artifact.get("version") != 1 or not isinstance(artifact.get("proposal"), dict):
        raise ValueError("unsupported legacy repair preview format")
    digest = str(artifact.get("digest", ""))
    proposal = artifact["proposal"]
    if not digest or _digest(proposal) != digest:
        raise PreviewDriftError("legacy repair artifact digest is invalid")
    return digest, proposal


def _legacy_file_jobs(
    conn: sqlite3.Connection, preview_result: LegacyRepairPreview
) -> tuple[_FileRewrite, ...]:
    jobs: list[_FileRewrite] = []
    for item in preview_result.file_rewrites:
        path = Path(str(item["minutes_path"]))
        before_bytes = path.read_bytes()
        before_text = before_bytes.decode("utf-8")
        mappings = {str(source): str(target) for source, target in item["mappings"]}
        rewrite = plan_text(before_text, mappings)
        after_bytes = rewrite.after.encode("utf-8")
        if (
            _sha256_bytes(before_bytes) != item["before_sha256"]
            or _sha256_bytes(after_bytes) != item["after_sha256"]
        ):
            raise PreviewDriftError(
                f"minutes changed after preview: {item['meeting_id']}"
            )
        jobs.append(
            _FileRewrite(
                meeting_id=str(item["meeting_id"]),
                minutes_path=str(path.resolve()),
                before_sha256=str(item["before_sha256"]),
                after_sha256=str(item["after_sha256"]),
                before_text=before_text,
                after_text=rewrite.after,
                state="changed" if before_text != rewrite.after else "unchanged",
                matched_spellings=tuple(str(value) for value in item["matched_spellings"]),
                match_count=int(item["match_count"]),
            )
        )

    for meeting_id in preview_result.missing_files:
        row = conn.execute(
            "SELECT minutes_path FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        raw_path = row["minutes_path"] if row else None
        jobs.append(
            _FileRewrite(
                meeting_id=meeting_id,
                minutes_path=raw_path,
                before_sha256=None,
                after_sha256=None,
                before_text=None,
                after_text=None,
                state="missing",
            )
        )
    return tuple(jobs)


def _update_legacy_suggestion(
    conn: sqlite3.Connection, change: dict[str, object]
) -> None:
    table = str(change["table"])
    column = str(change["column"])
    if table not in {"speaker_matches", "voice_clusters"}:
        raise ValueError(f"unsupported suggestion table: {table}")
    if column not in {"best_canonical", "next_canonical"}:
        raise ValueError(f"unsupported suggestion column: {column}")
    keys = change["keys"]
    if not isinstance(keys, dict) or not keys:
        raise ValueError("suggestion change is missing its row key")
    key_names = tuple(sorted(keys))
    if table == "speaker_matches" and key_names != ("label", "meeting_id"):
        raise ValueError("speaker match repair has an invalid row key")
    if table == "voice_clusters" and key_names != ("id",):
        raise ValueError("voice cluster repair has an invalid row key")
    where = " AND ".join(f"{key} = ?" for key in key_names)
    score_column = "best_score" if column == "best_canonical" else "next_score"
    conn.execute(
        f"""
        UPDATE {table}
        SET {column} = ?, {score_column} = CASE WHEN ? IS NULL THEN NULL ELSE {score_column} END
        WHERE {where} AND {column} = ?
        """,
        (
            change["after"],
            change["after"],
            *(keys[key] for key in key_names),
            change["before"],
        ),
    )


def _promote_valid_runner_ups(conn: sqlite3.Connection) -> None:
    for table in ("speaker_matches", "voice_clusters"):
        conn.execute(
            f"""
            UPDATE {table}
            SET best_canonical = next_canonical,
                best_score = next_score,
                next_canonical = NULL,
                next_score = NULL
            WHERE best_canonical IS NULL AND next_canonical IS NOT NULL
            """
        )
    _deduplicate_match_suggestions(conn)


def apply_legacy_repair(
    path: Path, *, expected_digest: str
) -> LegacyRepairResult:
    """Apply exactly one owner-approved legacy repair artifact."""
    artifact_digest, proposal = _load_legacy_repair_artifact(path.resolve())
    if not expected_digest or expected_digest != artifact_digest:
        raise PreviewDriftError("approved legacy repair digest does not match artifact")

    database_path = Path(config.DB_PATH)
    if str(database_path.resolve()) != str(proposal.get("database_path", "")):
        raise PreviewDriftError("legacy repair preview names a different database")

    marker = f"people.merge_repair.applied.{artifact_digest}"
    with db.connect(database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if db.get_setting(conn, marker) == artifact_digest:
            already_applied = True
            aliases_deleted = 0
            suggestions_rewritten = 0
            suggestions_cleared = 0
        else:
            already_applied = False
            excluded = tuple(str(value) for value in proposal.get("excluded_aliases", ()))
            current = _build_legacy_preview(conn, database_path, excluded)
            if current.digest != artifact_digest:
                raise PreviewDriftError(
                    "legacy repair preview changed; generate and approve a new preview"
                )

            display_spellings: dict[str, str] = {}
            for item in current.file_rewrites:
                for spelling, target in item["mappings"]:
                    alias_key = db.person_key(str(spelling))
                    if dict(current.mappings).get(alias_key) == target:
                        display_spellings.setdefault(alias_key, str(spelling))
            for alias, target in current.mappings:
                db.flatten_and_record_merge(
                    conn, display_spellings.get(alias, alias), target
                )

            for change in current.suggestion_updates:
                _update_legacy_suggestion(conn, change)
            for change in current.proposed_clears:
                _update_legacy_suggestion(conn, change)
            _promote_valid_runner_ups(conn)

            file_jobs = _legacy_file_jobs(conn, current)
            _enqueue_file_rewrites(
                conn,
                operation_id=artifact_digest,
                mappings=dict(current.mappings),
                file_rewrites=file_jobs,
            )

            aliases_deleted = 0
            for alias, target in current.mappings:
                cursor = conn.execute(
                    """
                    DELETE FROM person_aliases
                    WHERE alias = ? AND canonical = ? AND alias != LOWER(canonical)
                    """,
                    (alias, target),
                )
                aliases_deleted += int(cursor.rowcount or 0)
            suggestions_rewritten = len(current.suggestion_updates)
            suggestions_cleared = len(current.proposed_clears)
            db.set_setting(conn, marker, artifact_digest)

    rewrite_result = _resume_pending_rewrites(artifact_digest)
    return LegacyRepairResult(
        digest=artifact_digest,
        aliases_deleted=aliases_deleted,
        suggestions_rewritten=suggestions_rewritten,
        suggestions_cleared=suggestions_cleared,
        minutes_rewritten=rewrite_result.minutes_rewritten,
        minutes_unchanged=rewrite_result.minutes_unchanged,
        minutes_missing=rewrite_result.minutes_missing,
        rewrite_conflicts=rewrite_result.rewrite_conflicts,
        pending_rewrites=rewrite_result.pending_rewrites,
        already_applied=already_applied,
    )


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


def _enqueue_file_rewrites(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    mappings: dict[str, str],
    file_rewrites: tuple[_FileRewrite, ...],
) -> None:
    if not file_rewrites:
        return
    mappings_json = json.dumps(
        mappings, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    created_at = config.now_iso()
    for file_rewrite in file_rewrites:
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

    affected_ids = tuple(file_rewrite.meeting_id for file_rewrite in file_rewrites)
    placeholders = ", ".join("?" for _ in affected_ids)
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
            *affected_ids,
            db.SPEAKERS_RESOLVED,
            db.MINUTES_COMPILED,
            db.INDEXED,
        ),
    )


def _enqueue_rewrite_jobs(conn: sqlite3.Connection, plan: _MergePlan) -> None:
    _enqueue_file_rewrites(
        conn,
        operation_id=plan.public.digest,
        mappings=plan.mappings,
        file_rewrites=plan.file_rewrites,
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
        if target_row is None or (not target_row["role"] and role):
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
