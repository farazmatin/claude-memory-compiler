"""Filename parsing and content-hash dedup."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pipeline import db, ingest
from pipeline.config import TZ

MTIME = datetime(2026, 8, 10, 14, 30, tzinfo=TZ)


@pytest.mark.parametrize(
    "filename,date,time,hint",
    [
        # The real Pixel Recorder convention.
        ("Ali Aug 10 at 11-12 a.m..m4a", "2026-08-10", "11:00", "Ali"),
        ("Natalie Mar 13 at 2-3 p.m..m4a", "2026-03-13", "14:00", "Natalie"),
        # ISO form a configurable recorder can emit. The "T" separator is
        # preceded by a digit, which is why the date span must be masked before
        # the time is matched.
        ("2026-08-10T1100_roadmap-review.m4a", "2026-08-10", "11:00", "roadmap review"),
        ("20260810_1430_standup.m4a", "2026-08-10", "14:30", "standup"),
        # Meridiem edges.
        ("standup Jan 5 at 12-1 p.m..m4a", "2026-01-05", "12:00", "standup"),
        ("late Jan 5 at 12-1 a.m..m4a", "2026-01-05", "00:00", "late"),
        # Unparseable names still land, using mtime.
        ("voice 0042.m4a", "2026-08-10", "14:30", "voice 0042"),
    ],
)
def test_parse_filename(filename, date, time, hint):
    parsed = ingest.parse_filename(Path(filename), MTIME)
    assert parsed.date == date
    assert parsed.time == time
    assert parsed.title_hint == hint


def test_month_name_date_uses_mtime_year():
    """Month-name dates carry no year, so mtime supplies it."""
    parsed = ingest.parse_filename(
        Path("Natalie Mar 13.m4a"), datetime(2025, 3, 13, 8, 0, tzinfo=TZ)
    )
    assert parsed.date == "2025-03-13"


def test_dedup_by_content_not_filename(inbox):
    """Byte-identical files under different names are one meeting.

    The source Drive folder genuinely contains duplicate recordings. Without
    this, each one costs a 40-minute transcription and injects a second copy of
    the same minutes, doubling entities in the graph.
    """
    payload = b"FAKE-AUDIO" * 2000
    (inbox / "Ali Aug 10 at 11-12 a.m..m4a").write_bytes(payload)
    assert ingest.run(verbose=False)["ingested"] == 1

    (inbox / "Ali Aug 10 at 11-12 a.m. (1).m4a").write_bytes(payload)
    counts = ingest.run(verbose=False)
    assert counts["ingested"] == 0
    assert counts["duplicate"] == 2

    with db.connect() as conn:
        assert sum(db.status_counts(conn).values()) == 1


def test_rescan_is_idempotent(inbox):
    (inbox / "a Aug 10 at 9-10 a.m..m4a").write_bytes(b"one")
    ingest.run(verbose=False)
    assert ingest.run(verbose=False)["ingested"] == 0


def test_distinct_content_is_a_new_meeting(inbox):
    (inbox / "a Aug 10 at 9-10 a.m..m4a").write_bytes(b"one")
    (inbox / "b Aug 11 at 9-10 a.m..m4a").write_bytes(b"two")
    assert ingest.run(verbose=False)["ingested"] == 2


def test_inbox_files_are_never_removed(inbox):
    """The inbox is a cloud-synced folder; deleting would destroy the original."""
    (inbox / "a Aug 10 at 9-10 a.m..m4a").write_bytes(b"one")
    ingest.run(verbose=False)
    assert len(list(inbox.glob("*.m4a"))) == 1


def test_pending_is_ordered_chronologically(inbox):
    """Stages must process oldest-first: the minutes compiler compares each
    meeting against genuinely earlier ones."""
    (inbox / "c Aug 12 at 9-10 a.m..m4a").write_bytes(b"three")
    (inbox / "a Aug 10 at 9-10 a.m..m4a").write_bytes(b"one")
    (inbox / "b Aug 11 at 9-10 a.m..m4a").write_bytes(b"two")
    ingest.run(verbose=False)

    with db.connect() as conn:
        dates = [m.meeting_date for m in db.pending(conn, db.DISCOVERED)]
    assert dates == ["2026-08-10", "2026-08-11", "2026-08-12"]
