"""Stage 1: discover audio in the inbox, dedup it, register it.

Dedup is content-hash based and load-bearing. The source Drive folder is known to
hold byte-identical duplicate recordings (the Pixel Recorder backup produced two
copies of the same file minutes apart). Without dedup each duplicate costs a
30-50 minute CPU transcription and injects a second copy of the same minutes,
which doubles up entities in the knowledge graph.

Files are COPIED out of the inbox, never moved or deleted. The inbox is expected
to be a cloud-synced folder, and deleting from it would propagate the delete
upstream and destroy the original recording.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pipeline import db
from pipeline.config import AUDIO_DIR, AUDIO_EXTENSIONS, INBOX_DIR, TZ

# ── Filename parsing ──────────────────────────────────────────────────

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Names arrive with spaces OR underscores. Google Drive substitutes "_" for
# every space on download, so the Pixel Recorder's "Ali Aug 10 at 11-12 a.m."
# reaches us as "Ali_Aug_10_at_11-12_a.m.". Both \s and \b fail on that form -
# "_" is itself a word character, so \b never fires beside it - which sent
# every Drive-captured recording down the mtime fallback and stamped 32 of 44
# meetings with their import time rather than when they actually happened.
# Hence an explicit separator class, and alphanumeric lookarounds instead of
# \b so the boundary survives an adjacent underscore.
_SEP = r"[\s_]"
_LB = r"(?<![A-Za-z0-9])"   # left boundary, underscore-safe
_RB = r"(?![A-Za-z0-9])"    # right boundary, underscore-safe
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"

# 2026-08-10, 2026_08_10, 20260810
_ISO_DATE = re.compile(r"(?<!\d)(20\d{2})[-_]?(\d{2})[-_]?(\d{2})(?!\d)")
# "Aug 10", "August 10", "Aug_10", "10 Aug", "10_Aug"
_NAME_DATE = re.compile(_LB + _MONTH + _SEP + r"+(\d{1,2})" + _RB, re.I)
_NAME_DATE_REV = re.compile(_LB + r"(\d{1,2})" + _SEP + r"+" + _MONTH + _RB, re.I)
# "at 11-12 a.m.", "at_11-12_a.m.", "at 2-3 pm", "at 11:30 am", "T1100", "_1430"
_TIME_RANGE = re.compile(
    _LB + r"at" + _SEP + r"+(\d{1,2})(?::(\d{2}))?" + _SEP + r"*"
    r"(?:[-–]" + _SEP + r"*(\d{1,2})(?::\d{2})?)?" + _SEP + r"*"
    r"([ap])\.?" + _SEP + r"*m\.?", re.I
)
_TIME_COMPACT = re.compile(r"(?<!\d)[T_](\d{2})(\d{2})(?!\d)")
_TIME_COLON = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")


@dataclass
class ParsedName:
    """What we could pull out of a filename."""

    date: str | None      # YYYY-MM-DD
    time: str | None      # HH:MM
    title_hint: str | None


def _parse_time(stem: str) -> tuple[str | None, tuple[int, int] | None]:
    """Extract a start time. Returns (HH:MM, (span_start, span_end)) or (None, None)."""
    m = _TIME_RANGE.search(stem)
    if m:
        hour = int(m.group(1))
        # ":" is illegal in a filename, so the recorder writes the minute after a
        # hyphen: "at 11-31 a.m." is 11:31, not an 11-to-31 range. Read a
        # two-digit tail as minutes and keep the range reading only for the
        # one-digit form ("at 2-3 pm"), where an hour span is the honest guess.
        # Treating every tail as a range silently floored 33 meetings to :00.
        tail = m.group(3)
        if m.group(2):
            minute = int(m.group(2))
        elif tail and len(tail) == 2:
            minute = int(tail)
        else:
            minute = 0
        meridiem = m.group(4).lower()
        if meridiem == "p" and hour != 12:
            hour += 12
        elif meridiem == "a" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}", m.span()

    m = _TIME_COMPACT.search(stem)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}", m.span()

    m = _TIME_COLON.search(stem)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}", m.span()

    return None, None


def _parse_date(stem: str, fallback_year: int) -> tuple[str | None, tuple[int, int] | None]:
    """Extract a date. Month-name forms carry no year, so `fallback_year` (the
    file's mtime year) fills it in."""
    m = _ISO_DATE.search(stem)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}", m.span()

    for pattern, month_group, day_group in (
        (_NAME_DATE, 1, 2),
        (_NAME_DATE_REV, 2, 1),
    ):
        m = pattern.search(stem)
        if m:
            month = MONTHS[m.group(month_group)[:3].lower()]
            day = int(m.group(day_group))
            if 1 <= day <= 31:
                return f"{fallback_year:04d}-{month:02d}-{day:02d}", m.span()

    return None, None


def _mask(text: str, span: tuple[int, int] | None) -> str:
    """Blank out a span, preserving length so later match offsets stay valid."""
    if not span:
        return text
    return text[: span[0]] + " " * (span[1] - span[0]) + text[span[1] :]


def parse_filename(path: Path, mtime: datetime) -> ParsedName:
    """Pull date, time, and a title hint out of a filename.

    Handles both the Pixel Recorder convention ("Ali Aug 10 at 11-12 a.m..m4a")
    and the ISO-ish patterns a configurable recorder can be told to emit
    ("2026-08-10T1100_standup.m4a"). Falls back to file mtime for anything
    missing, so an unparseable name still lands with a usable date.
    """
    stem = path.stem
    date, date_span = _parse_date(stem, mtime.year)

    # Search for the time only outside the date. In "2026-08-10T1100" the "T"
    # separator is legitimately preceded by a digit, which the compact-time
    # lookbehind would otherwise reject, and an unmasked date can also overlap
    # a time pattern ("2026_0810").
    without_date = _mask(stem, date_span)
    time, time_span = _parse_time(without_date)

    # Whatever is left after removing the date/time tokens is the subject hint.
    leftover = _mask(without_date, time_span)
    leftover = re.sub(r"\b(at|on|recording|audio|meeting|rec)\b", " ", leftover, flags=re.I)
    leftover = re.sub(r"[_\-–]+", " ", leftover)
    leftover = re.sub(r"\s+", " ", leftover).strip(" .,")

    return ParsedName(
        date=date or mtime.strftime("%Y-%m-%d"),
        time=time or mtime.strftime("%H:%M"),
        title_hint=leftover or None,
    )


def parse_explicit_filename_date(path: Path, fallback_year: int) -> str | None:
    """Return only an explicitly encoded recording date, never an mtime fallback."""
    date, _ = _parse_date(path.stem, fallback_year)
    return date


# ── Audio probing ─────────────────────────────────────────────────────

def probe_duration(path: Path) -> float | None:
    """Duration in seconds via ffprobe, or None if unavailable.

    Duration is used to sanity-check transcription and to predict batch runtime,
    so a missing value is worth noting but never fatal.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        )
        return float(json.loads(result.stdout)["format"]["duration"])
    except (subprocess.SubprocessError, OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def hash_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Full sha256 of the file contents.

    Full digest, not a truncated prefix - this is the primary key and the dedup
    key, and it is cheap next to the ASR cost it protects.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── Stage entry point ─────────────────────────────────────────────────

def discover(inbox: Path | None = None) -> list[Path]:
    """Audio files in the inbox, recursively, sorted for deterministic order."""
    root = inbox or INBOX_DIR
    if not root.exists():
        return []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


def run(inbox: Path | None = None, verbose: bool = True) -> dict[str, int]:
    """Ingest everything new in the inbox. Returns counts for reporting."""
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    counts = {"scanned": 0, "ingested": 0, "duplicate": 0, "skipped": 0, "failed": 0}

    files = discover(inbox)
    with db.connect() as conn:
        for path in files:
            counts["scanned"] += 1
            try:
                stat = path.stat()

                # Skip files already processed, without reading them. The inbox is
                # a synced folder that is never emptied, so by year five a routine
                # run would otherwise re-hash ~165 GB to rediscover known files.
                if db.file_unchanged(conn, str(path), stat.st_size, int(stat.st_mtime)):
                    counts["skipped"] += 1
                    continue

                meeting_id = hash_file(path)

                if db.meeting_exists(conn, meeting_id):
                    counts["duplicate"] += 1
                    db.mark_seen(conn, str(path), stat.st_size, int(stat.st_mtime), None)
                    if verbose:
                        print(f"  dup   {path.name}")
                    continue

                mtime = datetime.fromtimestamp(stat.st_mtime, tz=TZ)
                parsed = parse_filename(path, mtime)
                duration = probe_duration(path)

                # Copy, never move: the inbox is likely a cloud-synced folder and
                # removing the file would delete the upstream original.
                archived = AUDIO_DIR / f"{parsed.date}_{meeting_id[:12]}{path.suffix.lower()}"
                if not archived.exists():
                    shutil.copy2(path, archived)

                db.insert_meeting(
                    conn,
                    meeting_id=meeting_id,
                    source_path=str(path),
                    source_name=path.name,
                    audio_path=str(archived),
                    meeting_date=parsed.date,
                    meeting_time=parsed.time,
                    title_hint=parsed.title_hint,
                    duration_sec=duration,
                )
                db.mark_seen(conn, str(path), stat.st_size, int(stat.st_mtime), meeting_id)
                counts["ingested"] += 1
                if verbose:
                    mins = f"{duration / 60:.0f}m" if duration else "?"
                    print(f"  new   {parsed.date} {parsed.time} {mins:>4}  {path.name}")

            except OSError as exc:
                counts["failed"] += 1
                print(f"  ERROR {path.name}: {exc}")

    return counts
