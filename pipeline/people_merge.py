"""Preview-bound people merges and recoverable minutes rewrites.

This module is the single workflow seam used by CLI and dashboard adapters.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
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


def preview(names: Iterable[str], into: str) -> MergePreview:
    """Return the exact, read-only impact of a proposed people merge."""
    source_names = tuple(name.strip() for name in names)
    requested_target = into.strip()
    if not source_names:
        raise ValueError("at least one source is required")
    if not requested_target:
        raise ValueError("target must not be blank")
    if len({db.person_key(name) for name in source_names}) != len(source_names):
        raise ValueError("sources must be unique")

    database_path = Path(config.DB_PATH)
    with db.connect(database_path) as conn:
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
        if absorbed_names:
            placeholders = ", ".join("?" for _ in absorbed_names)
            speaker_rows = conn.execute(
                f"SELECT COUNT(*) AS count FROM speakers WHERE name IN ({placeholders})",
                absorbed_names,
            ).fetchone()["count"]
        else:
            speaker_rows = 0

        mappings = {name: actual_target for name in absorbed_names}
        file_plans: list[dict[str, object]] = []
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
                file_plans.append(
                    {
                        "meeting_id": meeting_id,
                        "minutes_path": str(minutes_path) if minutes_path else None,
                        "state": "missing",
                    }
                )
                continue

            before_bytes = minutes_path.read_bytes()
            rewrite = plan_text(before_bytes.decode("utf-8"), mappings)
            after_bytes = rewrite.after.encode("utf-8")
            literal_matches += rewrite.match_count
            if after_bytes != before_bytes:
                files_changed += 1
            file_plans.append(
                {
                    "meeting_id": meeting_id,
                    "minutes_path": str(minutes_path.resolve()),
                    "state": "changed" if after_bytes != before_bytes else "unchanged",
                    "before_sha256": _sha256_bytes(before_bytes),
                    "after_sha256": _sha256_bytes(after_bytes),
                    "matched_spellings": rewrite.matched_spellings,
                    "match_count": rewrite.match_count,
                }
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
        "files": file_plans,
        "people": people_rows,
        "aliases": alias_rows,
        "tombstones": tombstone_rows,
        "conflicts": conflicts,
    }
    return MergePreview(
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
