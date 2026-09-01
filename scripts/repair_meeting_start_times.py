"""One-off repair: refile meetings by their START time, not their end time.

Every recording captured from Drive arrives as "..._My_recording_74.mp3" - no
date, no time - so ingest fell back to the file mtime. mtime is when the
recording ENDED: the recorder writes the file on stop, and the Drive capture
stamps the copy with Drive's createdTime, which is the upload that follows the
stop. A 60-minute call that began at 16:32 was therefore filed at 17:32.

ingest.parse_filename now rewinds the fallback by the measured duration. This
script applies the same correction to rows already in the manifest.

Only rows whose FILENAME carries no explicit time are touched - a
filename-encoded time is already a start time and is authoritative.

The minutes frontmatter carries the same date/time as a plain metadata field, so
it is rewritten in place. No minutes are recompiled: the body never quotes the
meeting time, and a recompile would spend a subscription CLI call and a reindex
per meeting to change one header line.

    ./.venv/Scripts/python.exe scripts/repair_meeting_start_times.py
    ./.venv/Scripts/python.exe scripts/repair_meeting_start_times.py --apply
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import ingest
from pipeline.config import DB_PATH, TZ


def _frontmatter_line(text: str, key: str, value: str) -> str:
    """Replace `key:` inside the leading frontmatter block only."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    head, tail = text[: end], text[end:]
    quoted = f'"{value}"' if key == "time" else value
    return re.sub(rf"(?m)^{key}:.*$", f"{key}: {quoted}", head, count=1) + tail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="commit; otherwise dry run")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT m.id, m.source_name, m.meeting_date, m.meeting_time,
               m.duration_sec, m.minutes_path, s.mtime
        FROM meetings m
        LEFT JOIN seen_files s ON s.meeting_id = m.id
        ORDER BY m.meeting_date, m.meeting_time
        """
    ).fetchall()

    changes: list[tuple[str, str, str, str | None]] = []
    unfixable = 0
    for row in rows:
        stamped, _ = ingest._parse_time(Path(row["source_name"]).stem)
        if stamped is not None:
            continue  # filename time is already a start time
        if row["mtime"] is None or not row["duration_sec"]:
            unfixable += 1
            continue
        end = datetime.fromtimestamp(row["mtime"], tz=TZ)
        start = end - timedelta(seconds=row["duration_sec"])
        date, time = start.strftime("%Y-%m-%d"), start.strftime("%H:%M")
        if (date, time) == (row["meeting_date"], row["meeting_time"]):
            continue
        print(
            f"  {row['meeting_date']} {row['meeting_time']} -> {date} {time}"
            f"  ({row['duration_sec'] / 60:.0f}m)  {row['source_name'][:44]}"
        )
        changes.append((row["id"], date, time, row["minutes_path"]))

    print(f"\n{len(changes)} meetings to refile; {unfixable} lack an mtime or duration.")
    if not args.apply:
        print("Dry run. Re-run with --apply to commit.")
        return 0

    backup = DB_PATH.parent / f"backups/manifest-{datetime.now(TZ):%Y%m%d-%H%M%S}-pre-starttime.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DB_PATH, backup)
    print(f"Manifest backed up to {backup}")

    rewritten = 0
    with conn:
        for meeting_id, date, time, minutes_path in changes:
            conn.execute(
                "UPDATE meetings SET meeting_date = ?, meeting_time = ?, updated_at = ?"
                " WHERE id = ?",
                (date, time, datetime.now(TZ).isoformat(timespec="seconds"), meeting_id),
            )
            if not minutes_path:
                continue
            path = Path(minutes_path)
            if not path.is_absolute():
                path = DB_PATH.parent.parent / minutes_path
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            updated = _frontmatter_line(_frontmatter_line(text, "date", date), "time", time)
            if updated != text:
                path.write_text(updated, encoding="utf-8")
                rewritten += 1

    print(f"Applied. {len(changes)} rows updated, {rewritten} minutes headers rewritten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
