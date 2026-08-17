"""Coverage for the local Meeting Memory dashboard, metrics, and operations."""

from __future__ import annotations

from pipeline import dashboard, db

from .conftest import make_meeting


def test_library_and_detail_read_minutes_inside_archive(manifest, tmp_path, monkeypatch):
    minutes_dir = tmp_path / "minutes"
    minutes_dir.mkdir()
    monkeypatch.setattr(dashboard, "MINUTES_DIR", minutes_dir)
    meeting_id = "a" * 64
    minutes_path = minutes_dir / "roadmap.md"
    minutes_path.write_text("# Roadmap\n\nWe decided to ship in September.", encoding="utf-8")
    make_meeting(
        manifest,
        meeting_id,
        "2026-08-12",
        title_hint="Roadmap decision",
        duration_sec=1800.0,
        status=db.INDEXED,
        minutes_path=str(minutes_path),
    )
    db.upsert_drive_source(
        manifest,
        drive_file_id="drive-1",
        drive_version="1",
        folder_kind="future",
        source_name="roadmap.m4a",
        mime_type="audio/mp4",
        byte_size=1000,
        md5_checksum="checksum",
        created_time="2026-08-12T09:00:00Z",
        modified_time="2026-08-12T09:00:00Z",
        web_view_link="https://drive.google.com/file/d/drive-1/view",
        recording_date="2026-08-12",
        state="ingested",
    )
    manifest.execute(
        "UPDATE drive_sources SET meeting_id = ? WHERE drive_file_id = ?",
        (meeting_id, "drive-1"),
    )
    manifest.execute(
        "INSERT INTO speakers (meeting_id, label, name, confidence) VALUES (?, ?, ?, ?)",
        (meeting_id, "SPEAKER_00", "Faraz", "confirmed"),
    )
    manifest.execute(
        "INSERT INTO entities (meeting_id, name, kind, description) VALUES (?, ?, ?, ?)",
        (meeting_id, "September", "date", "release target"),
    )
    manifest.commit()

    library = dashboard.meetings("Roadmap")
    detail = dashboard.meeting_detail(meeting_id)

    assert library[0]["title"] == "Roadmap decision"
    assert "ship in September" in library[0]["excerpt"]
    assert detail is not None
    assert detail["drive_url"].startswith("https://drive.google.com/")
    assert detail["speakers"] == [
        {"label": "SPEAKER_00", "name": "Faraz", "confidence": "confirmed"}
    ]
    assert detail["entities"][0]["name"] == "September"
    assert "ship in September" in detail["minutes"]


def test_overview_computes_extended_metrics(manifest):
    meeting_id = "m" * 64
    make_meeting(
        manifest,
        meeting_id,
        "2026-08-14",
        title_hint="Sprint Planning",
        duration_sec=3600.0,
        status=db.INDEXED,
    )
    manifest.execute(
        "INSERT INTO stage_runs (meeting_id, stage, started_at, finished_at, ok, detail) "
        "VALUES (?, ?, '2026-08-14T10:00:00Z', '2026-08-14T10:10:00Z', 1, 'ok')",
        (meeting_id, "transcribe"),
    )
    manifest.execute(
        "INSERT INTO people (canonical, role, created_at) VALUES ('Faraz', 'PM', '2026-08-14T10:00:00Z')"
    )
    manifest.commit()

    data = dashboard.overview()
    assert data["meetings"] >= 1
    assert data["durations"]["total_hours"] >= 1.0
    assert "activity" in data
    assert "today" in data["activity"]
    assert "yesterday" in data["activity"]
    assert "queue" in data
    assert data["queue"][db.INDEXED] >= 1
    assert "timings" in data
    assert "knowledge" in data
    assert data["knowledge"]["people_count"] >= 1


def test_retry_failed_and_retry_meeting(manifest):
    meeting_id = "f" * 64
    make_meeting(
        manifest,
        meeting_id,
        "2026-08-14",
        status=db.FAILED,
    )
    manifest.commit()

    # Retry single meeting
    ok = dashboard.retry_meeting(meeting_id, db.DISCOVERED)
    assert ok is True
    with db.connect() as conn:
        m = db.get_meeting(conn, meeting_id)
        assert m.status == db.DISCOVERED

    # Reset back to failed and test batch retry
    with db.connect() as conn:
        db.mark_failed(conn, meeting_id, "some error")
    count = dashboard.retry_failed(db.DISCOVERED)
    assert count == 1
    with db.connect() as conn:
        m = db.get_meeting(conn, meeting_id)
        assert m.status == db.DISCOVERED


def test_people_management_and_speaker_override(manifest):
    meeting_id = "s" * 64
    make_meeting(
        manifest,
        meeting_id,
        "2026-08-14",
        status=db.TRANSCRIBED,
    )
    manifest.commit()

    # Add person
    dashboard.add_person("Alice", role="Engineering", aliases=["alice_dev"])
    plist = dashboard.people()
    assert any(p["canonical"] == "Alice" for p in plist)

    # Set speaker in meeting
    dashboard.set_meeting_speaker(meeting_id, "SPEAKER_00", "Alice")
    with db.connect() as conn:
        speakers = db.get_speakers(conn, meeting_id)
        assert speakers.get("SPEAKER_00") == "Alice"

    # Merge person
    dashboard.add_person("Bob", role="Design")
    rewritten = dashboard.merge_people("Alice", "Bob")
    assert rewritten >= 1
    with db.connect() as conn:
        speakers = db.get_speakers(conn, meeting_id)
        assert speakers.get("SPEAKER_00") == "Bob"


def test_pipeline_status_and_trigger():
    status = dashboard.get_pipeline_status()
    assert "running" in status
    assert "logs" in status


def test_minutes_outside_archive_are_not_exposed(manifest, tmp_path, monkeypatch):
    minutes_dir = tmp_path / "minutes"
    minutes_dir.mkdir()
    monkeypatch.setattr(dashboard, "MINUTES_DIR", minutes_dir)
    outside = tmp_path / "private.md"
    outside.write_text("not meeting minutes", encoding="utf-8")
    meeting_id = "b" * 64
    make_meeting(
        manifest,
        meeting_id,
        "2026-08-12",
        status=db.INDEXED,
        minutes_path=str(outside),
    )
    manifest.commit()

    detail = dashboard.meeting_detail(meeting_id)

    assert detail is not None
    assert detail["minutes"] == ""
    assert detail["excerpt"] == ""


def test_question_validation_and_static_assets_exist():
    assert dashboard._excerpt("short record") == "short record"
    assert dashboard._excerpt("x" * 261).endswith("…")
    for asset in ("index.html", "app.js", "style.css"):
        assert (dashboard.STATIC_DIR / asset).is_file()
    try:
        dashboard.ask("   ")
    except ValueError as exc:
        assert "Ask a question" in str(exc)
    else:
        raise AssertionError("empty dashboard query should be rejected")


def test_dashboard_command_accepts_local_options():
    args = __import__("pipeline.cli", fromlist=["build_parser"]).build_parser().parse_args(
        ["dashboard", "--host", "127.0.0.1", "--port", "9876"]
    )
    assert args.host == "127.0.0.1"
    assert args.port == 9876
