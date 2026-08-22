"""Backup: safe SQLite snapshotting and incremental tree sync."""

from __future__ import annotations

import sqlite3

import pytest

from pipeline import backup, db


def test_sqlite_backup_is_consistent_and_verified(tmp_path):
    """A file copy of a live SQLite database can capture a torn page or miss the
    WAL. The online backup API cannot, and the snapshot is integrity-checked -
    a backup that will not restore is worse than none."""
    source = tmp_path / "live.db"
    db.init_db(source)
    with db.connect(source) as conn:
        db.insert_meeting(
            conn, meeting_id="m1", source_path="/a.m4a", source_name="a.m4a",
            audio_path="/audio/a.m4a", meeting_date="2026-08-10", meeting_time="09:00",
            title_hint="Ali", duration_sec=3600.0,
        )

    dest = tmp_path / "out" / "manifest.db"
    backup.backup_sqlite(source, dest)

    assert dest.exists()
    restored = sqlite3.connect(dest)
    try:
        restored.row_factory = sqlite3.Row
        rows = restored.execute("SELECT id, title_hint FROM meetings").fetchall()
        assert [(r["id"], r["title_hint"]) for r in rows] == [("m1", "Ali")]
    finally:
        restored.close()


def test_sqlite_backup_snapshots_while_connection_open(tmp_path):
    """The whole point: the source may be in use when the backup runs."""
    source = tmp_path / "live.db"
    db.init_db(source)
    held = sqlite3.connect(source)
    try:
        held.execute("PRAGMA journal_mode = WAL")
        backup.backup_sqlite(source, tmp_path / "out" / "manifest.db")
        assert (tmp_path / "out" / "manifest.db").exists()
    finally:
        held.close()


def test_invalid_source_raises_rather_than_writing_a_dud(tmp_path):
    """A non-database source must fail loudly.

    Silently producing an unusable snapshot is the worst outcome: it removes the
    pressure to have a real backup while providing none.
    """
    source = tmp_path / "not-a-db.db"
    source.write_bytes(b"this is not a sqlite file, not even close")

    with pytest.raises(sqlite3.DatabaseError):
        backup.backup_sqlite(source, tmp_path / "out" / "manifest.db")


def test_report_flags_manifest_failure_without_aborting(tmp_path, monkeypatch):
    """A broken manifest must not lose the transcript backup that already ran."""
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "a.json").write_text("{}", encoding="utf-8")
    bad_db = tmp_path / "bad.db"
    bad_db.write_bytes(b"garbage")

    for name in ("MINUTES_DIR", "AUDIO_DIR", "TEMPLATES_DIR"):
        monkeypatch.setattr(backup, name, tmp_path / "missing")
    monkeypatch.setattr(backup, "TRANSCRIPTS_DIR", transcripts)
    monkeypatch.setattr(backup, "DB_PATH", bad_db)
    monkeypatch.setattr(backup, "GLOSSARY_FILE", tmp_path / "missing.md")
    monkeypatch.setattr(backup, "SPEAKER_OVERRIDES_FILE", tmp_path / "missing.yaml")
    # SNIPPETS_DIR was left unpatched, so backup.run() walked the developer's
    # real snippets/ tree. Invisible until voice enrollment put 54 files in it,
    # then this test started counting them.
    monkeypatch.setattr(backup, "SNIPPETS_DIR", tmp_path / "missing-snippets")

    report = backup.run(tmp_path / "backup")
    assert not report.ok
    assert any("manifest.db" in e for e in report.errors)
    assert report.copied["transcripts"] == 1, "the rest of the backup must still land"


def test_run_copies_the_irreplaceable_tiers(tmp_path, monkeypatch):
    transcripts = tmp_path / "transcripts"
    minutes = tmp_path / "minutes"
    audio = tmp_path / "audio"
    for directory in (transcripts, minutes, audio):
        directory.mkdir()
    (transcripts / "abc.json").write_text('{"meeting_id": "abc"}', encoding="utf-8")
    (transcripts / "abc.md").write_text("# Transcript", encoding="utf-8")
    (minutes / "2026-08-10-standup.md").write_text("---\ndate: x\n---", encoding="utf-8")
    (audio / "2026-08-10_abc.m4a").write_bytes(b"audio")

    monkeypatch.setattr(backup, "TRANSCRIPTS_DIR", transcripts)
    monkeypatch.setattr(backup, "MINUTES_DIR", minutes)
    monkeypatch.setattr(backup, "AUDIO_DIR", audio)
    monkeypatch.setattr(backup, "DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(backup, "GLOSSARY_FILE", tmp_path / "missing.md")
    monkeypatch.setattr(backup, "SPEAKER_OVERRIDES_FILE", tmp_path / "missing.yaml")
    # SNIPPETS_DIR was left unpatched, so backup.run() walked the developer's
    # real snippets/ tree. Invisible until voice enrollment put 54 files in it,
    # then this test started counting them.
    monkeypatch.setattr(backup, "SNIPPETS_DIR", tmp_path / "missing-snippets")
    monkeypatch.setattr(backup, "TEMPLATES_DIR", tmp_path / "missing")

    dest = tmp_path / "backup"
    report = backup.run(dest)

    assert report.ok
    assert report.copied == {"transcripts": 2, "minutes": 1, "audio": 1}
    assert (dest / "transcripts" / "abc.json").exists()
    assert (dest / "audio" / "2026-08-10_abc.m4a").exists()
    assert "Restore:" in (dest / "BACKUP_INFO.txt").read_text(encoding="utf-8")


def test_run_can_skip_audio(tmp_path, monkeypatch):
    transcripts = tmp_path / "transcripts"
    audio = tmp_path / "audio"
    transcripts.mkdir()
    audio.mkdir()
    (transcripts / "a.json").write_text("{}", encoding="utf-8")
    (audio / "a.m4a").write_bytes(b"x" * 1000)

    monkeypatch.setattr(backup, "TRANSCRIPTS_DIR", transcripts)
    monkeypatch.setattr(backup, "AUDIO_DIR", audio)
    monkeypatch.setattr(backup, "MINUTES_DIR", tmp_path / "missing")
    monkeypatch.setattr(backup, "DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(backup, "GLOSSARY_FILE", tmp_path / "missing.md")
    monkeypatch.setattr(backup, "SPEAKER_OVERRIDES_FILE", tmp_path / "missing.yaml")
    # SNIPPETS_DIR was left unpatched, so backup.run() walked the developer's
    # real snippets/ tree. Invisible until voice enrollment put 54 files in it,
    # then this test started counting them.
    monkeypatch.setattr(backup, "SNIPPETS_DIR", tmp_path / "missing-snippets")
    monkeypatch.setattr(backup, "TEMPLATES_DIR", tmp_path / "missing")

    report = backup.run(tmp_path / "backup", include_audio=False)
    assert "audio" not in report.copied
    assert report.copied["transcripts"] == 1


def test_second_run_is_incremental(tmp_path, monkeypatch):
    """Re-hashing hundreds of gigabytes nightly would cost more than the backup."""
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "a.json").write_text("{}", encoding="utf-8")

    for name in ("MINUTES_DIR", "AUDIO_DIR", "TEMPLATES_DIR"):
        monkeypatch.setattr(backup, name, tmp_path / "missing")
    monkeypatch.setattr(backup, "TRANSCRIPTS_DIR", transcripts)
    monkeypatch.setattr(backup, "DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(backup, "GLOSSARY_FILE", tmp_path / "missing.md")
    monkeypatch.setattr(backup, "SPEAKER_OVERRIDES_FILE", tmp_path / "missing.yaml")
    # SNIPPETS_DIR was left unpatched, so backup.run() walked the developer's
    # real snippets/ tree. Invisible until voice enrollment put 54 files in it,
    # then this test started counting them.
    monkeypatch.setattr(backup, "SNIPPETS_DIR", tmp_path / "missing-snippets")

    dest = tmp_path / "backup"
    assert backup.run(dest).copied["transcripts"] == 1
    assert "transcripts" not in backup.run(dest).copied, "unchanged files must be skipped"

    (transcripts / "b.json").write_text("{}", encoding="utf-8")
    assert backup.run(dest).copied["transcripts"] == 1, "only the new file"


def test_backup_never_deletes_from_destination(tmp_path, monkeypatch):
    """A file vanishing from the source is exactly when the backup copy matters."""
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "a.json").write_text("{}", encoding="utf-8")

    for name in ("MINUTES_DIR", "AUDIO_DIR", "TEMPLATES_DIR"):
        monkeypatch.setattr(backup, name, tmp_path / "missing")
    monkeypatch.setattr(backup, "TRANSCRIPTS_DIR", transcripts)
    monkeypatch.setattr(backup, "DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(backup, "GLOSSARY_FILE", tmp_path / "missing.md")
    monkeypatch.setattr(backup, "SPEAKER_OVERRIDES_FILE", tmp_path / "missing.yaml")
    # SNIPPETS_DIR was left unpatched, so backup.run() walked the developer's
    # real snippets/ tree. Invisible until voice enrollment put 54 files in it,
    # then this test started counting them.
    monkeypatch.setattr(backup, "SNIPPETS_DIR", tmp_path / "missing-snippets")

    dest = tmp_path / "backup"
    backup.run(dest)
    (transcripts / "a.json").unlink()
    backup.run(dest)

    assert (dest / "transcripts" / "a.json").exists()
