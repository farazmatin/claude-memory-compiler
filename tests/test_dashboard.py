"""Coverage for the read-only local Meeting Memory dashboard."""

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
        byte_size=100,
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
        (meeting_id, "SPEAKER_00", "Faraz", 0.98),
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
        {"label": "SPEAKER_00", "name": "Faraz", "confidence": "0.98"}
    ]
    assert detail["entities"][0]["name"] == "September"
    assert "ship in September" in detail["minutes"]


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
