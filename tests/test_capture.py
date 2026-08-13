"""Fast, offline coverage for the Drive capture policy."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import capture, compile_minutes, db, ingest


class FakeDrive:
    def __init__(self, files: dict[str, list[tuple[capture.DriveFile, bytes]]]) -> None:
        self.files = files
        self.fail_download = False

    def list_files(self, folder_id: str) -> list[capture.DriveFile]:
        return [file for file, _ in self.files.get(folder_id, [])]

    def get_file(self, file_id: str) -> capture.DriveFile:
        return self._entry(file_id)[0]

    def download(self, file_id: str, destination: Path) -> None:
        if self.fail_download:
            raise OSError("network interrupted")
        destination.write_bytes(self._entry(file_id)[1])

    def _entry(self, file_id: str) -> tuple[capture.DriveFile, bytes]:
        for entries in self.files.values():
            for entry in entries:
                if entry[0].file_id == file_id:
                    return entry
        raise KeyError(file_id)


def drive_file(file_id: str, name: str, content: bytes, version: str = "1") -> capture.DriveFile:
    return capture.DriveFile(
        file_id=file_id,
        version=version,
        name=name,
        mime_type="audio/mp4",
        byte_size=len(content),
        md5_checksum=hashlib.md5(content, usedforsecurity=False).hexdigest(),
        created_time="2026-08-12T05:00:00Z",
        modified_time="2026-08-12T05:00:00Z",
        web_view_link=f"https://drive.google.com/file/d/{file_id}/view",
    )


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "manifest.db"
        self.handoff = self.root / "inbox" / "drive"
        db.init_db(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_backfill_accepts_cutoff_date_and_rejects_older_file(self) -> None:
        accepted = b"accepted"
        rejected = b"rejected"
        drive = FakeDrive(
            {
                "backfill": [
                    (drive_file("accepted", "Team Jun 9 at 10-11 a.m..m4a", accepted), accepted),
                    (drive_file("rejected", "Team Jun 8 at 10-11 a.m..m4a", rejected), rejected),
                ]
            }
        )

        counts = capture.run(
            client=drive,
            db_path=self.database,
            inbox=self.handoff,
            sources=[capture.CaptureSource("backfill", "backfill")],
        )

        self.assertEqual(counts["downloaded"], 1)
        self.assertEqual(counts["excluded"], 1)
        with db.connect(self.database) as conn:
            accepted_source = db.get_drive_source(conn, "accepted", "1")
            rejected_source = db.get_drive_source(conn, "rejected", "1")
        self.assertEqual(accepted_source.recording_date, "2026-06-09")
        self.assertEqual(accepted_source.state, "staged")
        self.assertEqual(rejected_source.state, "excluded")

    def test_backfill_holds_filename_without_an_explicit_date(self) -> None:
        content = b"unlabeled"
        drive = FakeDrive({"backfill": [(drive_file("unknown", "meeting.m4a", content), content)]})

        counts = capture.run(
            client=drive,
            db_path=self.database,
            inbox=self.handoff,
            sources=[capture.CaptureSource("backfill", "backfill")],
        )

        self.assertEqual(counts["ambiguous"], 1)
        self.assertFalse(self.handoff.exists())

    def test_interrupted_download_is_recorded_for_retry(self) -> None:
        content = b"audio"
        drive = FakeDrive(
            {"future": [(drive_file("retry", "2026-08-12T1100.m4a", content), content)]}
        )
        drive.fail_download = True

        counts = capture.run(
            client=drive,
            db_path=self.database,
            inbox=self.handoff,
            sources=[capture.CaptureSource("future", "future")],
        )

        self.assertEqual(counts["failed"], 1)
        self.assertFalse(list(self.handoff.glob("*.part")))
        with db.connect(self.database) as conn:
            source = db.get_drive_source(conn, "retry", "1")
        self.assertEqual(source.state, "failed")

    def test_ingest_reconciliation_and_rehydration_preserve_drive_source(self) -> None:
        content = b"meeting audio"
        remote = drive_file("meeting", "2026-08-12T1100_roadmap.m4a", content)
        drive = FakeDrive({"future": [(remote, content)]})
        capture.run(
            client=drive,
            db_path=self.database,
            inbox=self.handoff,
            sources=[capture.CaptureSource("future", "future")],
        )
        archive_dir = self.root / "audio"
        with (
            patch.object(db, "DB_PATH", self.database),
            patch.object(ingest, "AUDIO_DIR", archive_dir),
        ):
            counts = ingest.run(inbox=self.handoff, verbose=False)
        self.assertEqual(counts["ingested"], 1)

        self.assertEqual(
            capture.reconcile_ingested(db_path=self.database, handoff_dir=self.handoff), 1
        )
        with db.connect(self.database) as conn:
            meeting = db.pending(conn, db.DISCOVERED)[0]
            source = db.get_drive_source_for_meeting(conn, meeting.id)
            db.advance(conn, meeting.id, db.TRANSCRIBED, transcript_path="transcript.json")
            source_reference = compile_minutes.source_audio_reference(conn, meeting)
        self.assertEqual(source.state, "ingested")
        self.assertEqual(source_reference, remote.web_view_link)

        archive = Path(meeting.audio_path)
        self.assertTrue(
            capture.cleanup_transcribed_audio(meeting.id, str(archive), db_path=self.database)
        )
        self.assertFalse(archive.exists())
        with patch.object(capture, "AUDIO_DIR", archive_dir):
            restored = capture.rehydrate_audio(meeting, client=drive, db_path=self.database)
        self.assertEqual(restored.read_bytes(), content)

    def test_reconciliation_links_a_new_drive_revision_with_duplicate_content(self) -> None:
        content = b"unchanged meeting audio"
        first = drive_file("meeting", "2026-08-12T1100_roadmap.m4a", content)
        drive = FakeDrive({"future": [(first, content)]})
        archive_dir = self.root / "audio"

        capture.run(
            client=drive,
            db_path=self.database,
            inbox=self.handoff,
            sources=[capture.CaptureSource("future", "future")],
        )
        with (
            patch.object(db, "DB_PATH", self.database),
            patch.object(ingest, "AUDIO_DIR", archive_dir),
        ):
            ingest.run(inbox=self.handoff, verbose=False)
        capture.reconcile_ingested(db_path=self.database, handoff_dir=self.handoff)

        revised = drive_file(
            "meeting", "2026-08-12T1100_roadmap.m4a", content, version="2"
        )
        drive.files = {"future": [(revised, content)]}
        counts = capture.run(
            client=drive,
            db_path=self.database,
            inbox=self.handoff,
            sources=[capture.CaptureSource("future", "future")],
        )
        self.assertEqual(counts["downloaded"], 1)
        with (
            patch.object(db, "DB_PATH", self.database),
            patch.object(ingest, "AUDIO_DIR", archive_dir),
        ):
            counts = ingest.run(inbox=self.handoff, verbose=False)
        self.assertEqual(counts["duplicate"], 1)

        self.assertEqual(
            capture.reconcile_ingested(db_path=self.database, handoff_dir=self.handoff), 1
        )
        with db.connect(self.database) as conn:
            source = db.get_drive_source(conn, revised.file_id, revised.version)
        self.assertEqual(source.state, "ingested")
        self.assertIsNotNone(source.meeting_id)
        self.assertFalse(any(self.handoff.iterdir()))

    def test_rehydration_refuses_a_changed_drive_version(self) -> None:
        content = b"original"
        remote = drive_file("changed", "2026-08-12T1100.m4a", content)
        drive = FakeDrive({"future": [(remote, content)]})
        with db.connect(self.database) as conn:
            db.insert_meeting(
                conn,
                meeting_id="a" * 64,
                source_path="drive-handoff.m4a",
                source_name=remote.name,
                audio_path=None,
                meeting_date="2026-08-12",
                meeting_time="11:00",
                title_hint=None,
                duration_sec=None,
            )
            db.upsert_drive_source(
                conn,
                drive_file_id=remote.file_id,
                drive_version=remote.version,
                folder_kind="future",
                source_name=remote.name,
                mime_type=remote.mime_type,
                byte_size=remote.byte_size,
                md5_checksum=remote.md5_checksum,
                created_time=remote.created_time,
                modified_time=remote.modified_time,
                web_view_link=remote.web_view_link,
                recording_date="2026-08-12",
                state="ingested",
            )
            db.link_drive_source_to_meeting(conn, remote.file_id, remote.version, "a" * 64)
            meeting = db.get_meeting(conn, "a" * 64)

        changed = drive_file("changed", "2026-08-12T1100.m4a", b"replacement", version="2")
        drive.files = {"future": [(changed, b"replacement")]}

        with self.assertRaisesRegex(capture.CaptureError, "source changed"):
            capture.rehydrate_audio(meeting, client=drive, db_path=self.database)


if __name__ == "__main__":
    unittest.main()
