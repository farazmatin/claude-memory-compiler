"""Back up the irreplaceable data.

What actually matters, in order:

1. `transcripts/` - the immutable source. Everything downstream can be rebuilt
   from these without re-running ASR, which is the expensive step. Losing these
   costs 30-50 CPU-minutes per meeting to recreate, and only if the audio survives.
2. `audio/` - recreates transcripts, but only at that cost. Irreplaceable if the
   phone copy is gone.
3. `minutes/` - the corpus. Rebuildable from transcripts, but only by spending
   LLM quota again.
4. `db/manifest.db` - rebuildable in principle, painful in practice.

Deliberately NOT backed up: `rag_storage/` and the Postgres volume. The whole
index is derived data - it re-indexes from `minutes/`, so backing it up wastes
space on something reconstructible.

**SQLite cannot be copied with `cp` while in use.** A plain file copy of a live
database can capture a torn page or miss the WAL entirely, producing a backup that
restores as corrupt. This module uses SQLite's own online backup API instead, which
is why `pipeline backup` exists rather than a one-line rsync in the README.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.config import (
    AUDIO_DIR,
    DB_PATH,
    GLOSSARY_FILE,
    MINUTES_DIR,
    SNIPPETS_DIR,
    SPEAKER_OVERRIDES_FILE,
    TEMPLATES_DIR,
    TRANSCRIPTS_DIR,
    now_iso,
)


@dataclass
class BackupReport:
    destination: Path
    copied: dict[str, int] = field(default_factory=dict)
    bytes_written: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [f"{name}: {count} file(s)" for name, count in sorted(self.copied.items())]
        size_mb = self.bytes_written / (1024 * 1024)
        return f"{', '.join(parts)} | {size_mb:.1f} MB -> {self.destination}"


def backup_sqlite(source: Path, dest: Path) -> None:
    """Snapshot a SQLite database safely while it may be in use.

    Uses `Connection.backup()`, which takes a consistent snapshot including
    anything sitting in the WAL. A filesystem copy can silently produce a corrupt
    backup - and a backup you cannot restore is worse than none, because it
    removes the pressure to have a real one.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dest)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    # A snapshot that cannot be opened and queried is not a backup.
    verify = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        result = verify.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError(f"integrity check failed: {result}")
    finally:
        verify.close()


def _sync_tree(source: Path, dest: Path, report: BackupReport, label: str) -> None:
    """Copy new or changed files from `source` into `dest`.

    Incremental by size and mtime rather than content hash: at hundreds of
    gigabytes of audio, re-hashing everything nightly would cost more than the
    backup itself. Nothing is ever deleted from the destination - this is a
    backup, and a file vanishing from the source is exactly when the copy matters.
    """
    if not source.exists():
        return

    count = 0
    for src_file in sorted(source.rglob("*")):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(source)
        dst_file = dest / rel

        try:
            if dst_file.exists():
                src_stat = src_file.stat()
                dst_stat = dst_file.stat()
                if (
                    src_stat.st_size == dst_stat.st_size
                    and int(src_stat.st_mtime) <= int(dst_stat.st_mtime)
                ):
                    continue
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            report.bytes_written += src_file.stat().st_size
            count += 1
        except OSError as exc:
            report.errors.append(f"{label}/{rel}: {exc}")

    if count:
        report.copied[label] = count


def run(destination: Path, include_audio: bool = True) -> BackupReport:
    """Back up everything irreplaceable to `destination`.

    `include_audio=False` skips the bulkiest tier. Reasonable when the phone or
    a cloud folder still holds the originals - but it means a restore can only
    rebuild from transcripts, never re-transcribe.
    """
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    report = BackupReport(destination=destination)

    # Transcripts first: they are the immutable source and the cheapest thing to
    # protect. If the run dies partway, this is what you most want to have copied.
    _sync_tree(TRANSCRIPTS_DIR, destination / "transcripts", report, "transcripts")
    _sync_tree(MINUTES_DIR, destination / "minutes", report, "minutes")
    # Voice clips are tiny but irreplaceable: the source audio is deleted after
    # transcription, so losing these means a speaker can never be labelled by ear
    # again without re-fetching the original from Drive.
    _sync_tree(SNIPPETS_DIR, destination / "snippets", report, "snippets")

    if include_audio:
        _sync_tree(AUDIO_DIR, destination / "audio", report, "audio")

    # Small but load-bearing: the glossary shapes transcription accuracy and the
    # overrides file is the only ground truth for speaker identity.
    for path, label in (
        (GLOSSARY_FILE, "glossary.md"),
        (SPEAKER_OVERRIDES_FILE, "speaker-overrides.yaml"),
    ):
        if path.exists():
            try:
                shutil.copy2(path, destination / label)
                report.bytes_written += path.stat().st_size
                report.copied[label] = 1
            except OSError as exc:
                report.errors.append(f"{label}: {exc}")

    _sync_tree(TEMPLATES_DIR, destination / "templates", report, "templates")

    if DB_PATH.exists():
        try:
            backup_sqlite(DB_PATH, destination / "db" / "manifest.db")
            report.copied["manifest.db"] = 1
            report.bytes_written += (destination / "db" / "manifest.db").stat().st_size
        except (sqlite3.Error, OSError) as exc:
            report.errors.append(f"manifest.db: {exc}")

    (destination / "BACKUP_INFO.txt").write_text(
        "\n".join(
            [
                "Meeting minutes pipeline backup",
                f"Written: {now_iso()}",
                f"Audio included: {include_audio}",
                "",
                "Restore:",
                "  1. Copy transcripts/, minutes/, audio/, templates/, glossary.md",
                "     and speaker-overrides.yaml back into the repo.",
                "  2. Copy db/manifest.db to db/manifest.db.",
                "  3. docker compose up -d",
                "  4. pipeline index          # rebuilds the LightRAG index",
                "",
                "The index is NOT backed up - it is derived from minutes/ and is",
                "rebuilt by step 4. If minutes/ was lost but transcripts/ survived,",
                "run `pipeline minutes --recompile` before step 4.",
                "",
                "Contents:",
                *(f"  {name}: {count}" for name, count in sorted(report.copied.items())),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    return report
