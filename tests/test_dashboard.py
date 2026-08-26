"""Coverage for the local Meeting Memory dashboard, metrics, and operations."""

from __future__ import annotations

import uuid

import pytest

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

    # The minutes document's own heading wins over the filename-derived hint.
    # The compiler writes that heading from the transcript, so it is the most
    # specific name available; title_hint is a fallback scraped off the audio
    # filename and is only reached when the document offers nothing better.
    assert library[0]["title"] == "Roadmap"
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


def test_stage_failures_is_its_own_endpoint_not_folded_into_overview(manifest):
    """Deliberately not a key on overview(): overview is polled every 8s and
    already makes a network call, so this history is fetched separately, only
    when the diagnostics drawer is opened."""
    make_meeting(manifest, "m1", "2026-08-14", title_hint="Sprint Planning")
    run_id = db.start_stage(manifest, "m1", "transcribe")
    db.finish_stage(manifest, run_id, False, "OSError: ffmpeg not found")
    manifest.commit()

    assert "failures" not in dashboard.overview()

    result = dashboard.stage_failures()
    assert len(result["failures"]) == 1
    assert result["failures"][0]["stage"] == "transcribe"
    assert result["failures"][0]["detail"] == "OSError: ffmpeg not found"


def test_stage_failures_respects_limit(manifest):
    make_meeting(manifest, "m1", "2026-08-14")
    for i in range(3):
        run_id = db.start_stage(manifest, "m1", "transcribe")
        db.finish_stage(manifest, run_id, False, f"failure {i}")
    manifest.commit()

    assert len(dashboard.stage_failures(limit=1)["failures"]) == 1


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
    approved = dashboard.preview_people_merge(["Alice"], "Bob")
    result = dashboard.merge_people("Alice", "Bob", expected_digest=approved.digest)
    assert result.speaker_rows >= 1
    with db.connect() as conn:
        speakers = db.get_speakers(conn, meeting_id)
        assert speakers.get("SPEAKER_00") == "Bob"


def test_merging_people_queues_completed_minutes_for_refresh(manifest):
    meeting_id = "merge-refresh"
    make_meeting(manifest, meeting_id, "2026-08-14", status=db.INDEXED)
    db.set_speaker(manifest, meeting_id, "SPEAKER_00", "Ali", "confirmed")
    db.add_person(manifest, "Ali")
    db.add_person(manifest, "Ali Hilal")
    manifest.commit()

    approved = dashboard.preview_people_merge(["Ali"], "Ali Hilal")
    dashboard.merge_people("Ali", "Ali Hilal", expected_digest=approved.digest)

    with db.connect() as conn:
        meeting = db.get_meeting(conn, meeting_id)
        assert meeting.status == db.SPEAKERS_RESOLVED
        assert db.get_speakers(conn, meeting_id)["SPEAKER_00"] == "Ali Hilal"


def test_merge_many_people_keeps_one_spelling_and_all_history(manifest):
    meeting_ids = []
    for index, name in enumerate(("Ru", "Roo", "Roe")):
        meeting_id = f"merge-many-{index}"
        meeting_ids.append(meeting_id)
        make_meeting(manifest, meeting_id, "2026-08-14", status=db.INDEXED)
        db.add_person(manifest, name, role="Designer" if name == "Ru" else None)
        db.set_speaker(manifest, meeting_id, "SPEAKER_00", name, "confirmed")
    manifest.commit()

    approved = dashboard.preview_people_merge(["Ru", "Roo", "Roe"], "Roo")
    result = dashboard.merge_many_people(
        ["Ru", "Roo", "Roe"], "Roo", expected_digest=approved.digest
    )

    assert result.speaker_rows == 2
    with db.connect() as conn:
        people = {person["canonical"]: person for person in db.list_people(conn)}
        assert set(people) == {"Roo"}
        assert people["Roo"]["role"] == "Designer"
        assert db.canonical_name(conn, "Ru") == "Roo"
        assert db.canonical_name(conn, "Roe") == "Roo"
        for meeting_id in meeting_ids:
            assert db.get_speakers(conn, meeting_id)["SPEAKER_00"] == "Roo"
        assert db.get_meeting(conn, meeting_ids[0]).status == db.SPEAKERS_RESOLVED
        assert db.get_meeting(conn, meeting_ids[1]).status == db.INDEXED
        assert db.get_meeting(conn, meeting_ids[2]).status == db.SPEAKERS_RESOLVED


def test_merge_many_people_rejects_missing_name_before_writing(manifest):
    for name in ("Ru", "Roo"):
        db.add_person(manifest, name)
    manifest.commit()

    with pytest.raises(ValueError, match="source does not exist: 'Roe'"):
        dashboard.preview_people_merge(["Ru", "Roo", "Roe"], "Roo")

    with db.connect() as conn:
        assert {person["canonical"] for person in db.list_people(conn)} == {"Ru", "Roo"}


def test_merge_many_people_handles_graph_rows_that_already_use_both_names(manifest):
    meeting_id = "merge-graph-collision"
    make_meeting(manifest, meeting_id, "2026-08-14", status=db.INDEXED)
    for name in ("Ru", "Roo"):
        db.add_person(manifest, name)
    db.replace_entities(
        manifest,
        meeting_id,
        [
            {"name": "Ru", "kind": "person", "description": "Short spelling"},
            {"name": "Roo", "kind": "person", "description": "Kept spelling"},
        ],
        [
            {"subject": "Ru", "predicate": "works with", "object": "Roo"},
            {"subject": "Roo", "predicate": "works with", "object": "Roo"},
        ],
    )
    manifest.commit()

    approved = dashboard.preview_people_merge(["Ru", "Roo"], "Roo")
    dashboard.merge_many_people(
        ["Ru", "Roo"], "Roo", expected_digest=approved.digest
    )

    with db.connect() as conn:
        assert [item["name"] for item in db.get_entities(conn, meeting_id)] == ["Roo"]
        assert db.get_relations(conn, meeting_id) == [
            {
                "subject": "Roo",
                "predicate": "works with",
                "object": "Roo",
            }
        ]
        assert db.get_meeting(conn, meeting_id).status == db.SPEAKERS_RESOLVED


def test_manual_speaker_correction_queues_completed_minutes_for_refresh(manifest):
    meeting_id = "manual-refresh"
    make_meeting(manifest, meeting_id, "2026-08-14", status=db.INDEXED)
    manifest.commit()

    dashboard.set_meeting_speaker(meeting_id, "SPEAKER_00", "Ali Hilal")

    with db.connect() as conn:
        assert db.get_meeting(conn, meeting_id).status == db.SPEAKERS_RESOLVED


def test_similar_contact_names_are_suggested_and_no_is_remembered(manifest):
    for name in ("Ru", "Roo", "Roe", "Ruth"):
        db.add_person(manifest, name)
    manifest.commit()

    suggestions = dashboard.people_merge_suggestions()
    groups = {frozenset(item["names"]) for item in suggestions}
    assert frozenset(("Ru", "Roo", "Roe")) in groups
    assert all("Ruth" not in group for group in groups)

    dashboard.dismiss_people_suggestion("Ru", "Roo")
    remaining = dashboard.people_merge_suggestions()
    assert all(not {"Ru", "Roo"} <= set(item["names"]) for item in remaining)


def test_declining_a_similar_name_group_remembers_every_pair(manifest):
    for name in ("Ru", "Roo", "Roe"):
        db.add_person(manifest, name)
    manifest.commit()

    dashboard.dismiss_people_suggestion_group(["Ru", "Roo", "Roe"])

    assert dashboard.people_merge_suggestions() == []


def test_rename_person_changes_spelling_through_the_deep_module(manifest):
    meeting_id = "rename-contact"
    make_meeting(manifest, meeting_id, "2026-08-14", status=db.INDEXED)
    db.add_person(manifest, "Roo", role="Designer")
    db.set_speaker(manifest, meeting_id, "SPEAKER_00", "Roo", "confirmed")
    db.add_voice_sample(
        manifest,
        canonical="Roo",
        meeting_id=meeting_id,
        label="SPEAKER_00",
        embedding=b"voice",
        dim=1,
        model="test-model",
        speech_sec=10.0,
    )
    manifest.commit()

    approved = dashboard.preview_people_merge(["Roo"], "Rue")
    result = dashboard.rename_person(
        "Roo", "Rue", expected_digest=approved.digest
    )

    assert result.target == "Rue"
    with db.connect() as conn:
        people = {person["canonical"]: person for person in db.list_people(conn)}
        assert "Roo" not in people
        assert people["Rue"]["role"] == "Designer"
        assert db.get_speakers(conn, meeting_id)["SPEAKER_00"] == "Rue"
        assert len(db.person_samples(conn, "Rue")) == 1
        assert db.get_meeting(conn, meeting_id).status == db.SPEAKERS_RESOLVED


def test_people_api_normalizes_aliases_for_the_frontend(manifest, monkeypatch):
    monkeypatch.setattr(
        db,
        "list_people",
        lambda conn: [
            {"canonical": "Michael", "role": "PM", "aliases": "mike, mikey", "meetings": 2},
            {"canonical": "Alice", "role": None, "aliases": None, "meetings": 0},
            {"canonical": "Ruth", "role": None, "aliases": ["ruthie"], "meetings": 1},
        ],
    )

    result = dashboard.people()

    assert result[0]["aliases"] == ["mike", "mikey"]
    assert result[1]["aliases"] == []
    assert result[2]["aliases"] == ["ruthie"]


def test_pipeline_status_and_trigger():
    status = dashboard.get_pipeline_status()
    assert "running" in status
    assert "logs" in status


def test_speaker_refresh_stage_runs_minutes_then_index(manifest, monkeypatch):
    from pipeline import cli

    make_meeting(manifest, "refresh-me", "2026-08-14", status=db.SPEAKERS_RESOLVED)
    manifest.commit()
    calls = []
    monkeypatch.setattr(cli, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli, "cmd_minutes", lambda args: calls.append("minutes") or 0)
    monkeypatch.setattr(cli, "cmd_index", lambda args: calls.append("index") or 0)

    dashboard._run_pipeline_worker("speaker-refresh", None)

    assert calls == ["minutes", "index"]


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
    # The security property is that the file's CONTENT never leaves the archive.
    # The excerpt is a fixed UI placeholder, not a read of the file, so it is
    # allowed to be non-empty - what matters is that it says nothing about the
    # document sitting outside MINUTES_DIR.
    assert detail["minutes"] == ""
    assert detail["excerpt"] == "No executive summary recorded yet."
    assert "secret" not in detail["excerpt"].lower()


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


# ── Ask AI session handling ───────────────────────────────────────────

def _stub_answer(text="ok", provider="gemini", synthesized=True):
    from pipeline import answer as answer_module

    return answer_module.Answer(
        text=text, retrieval_sec=0.1, synthesis_sec=0.2, provider=provider,
        context_chars=10, synthesized=synthesized,
    )


def test_ask_mints_and_returns_a_session_id(manifest, monkeypatch):
    from pipeline import answer as answer_module

    monkeypatch.setattr(
        answer_module, "ask",
        lambda question, mode=None, top_k=None, synthesize=True, history=None: _stub_answer(),
    )

    result = dashboard.ask("what happened?")

    assert dashboard._valid_session_id(result["session_id"]) == result["session_id"]
    assert result["provider"] == "gemini"


def test_ask_persists_and_replays_history_within_a_session(manifest, monkeypatch):
    from pipeline import answer as answer_module

    captured_history: list[list[tuple[str, str]] | None] = []

    def fake_ask(question, mode=None, top_k=None, synthesize=True, history=None):
        captured_history.append(history)
        return _stub_answer(text=f"answer to {question}", provider="codex")

    monkeypatch.setattr(answer_module, "ask", fake_ask)

    first = dashboard.ask("what did we decide?")
    session_id = first["session_id"]
    second = dashboard.ask("why?", session_id=session_id)

    assert second["session_id"] == session_id
    assert captured_history[0] == []
    assert captured_history[1] == [("what did we decide?", "answer to what did we decide?")]


def test_ask_rejects_malformed_session_id_by_minting_a_fresh_one(manifest, monkeypatch):
    from pipeline import answer as answer_module

    monkeypatch.setattr(
        answer_module, "ask",
        lambda question, mode=None, top_k=None, synthesize=True, history=None: _stub_answer(),
    )

    result = dashboard.ask("q", session_id="not-a-valid-session-id")

    assert result["session_id"] != "not-a-valid-session-id"
    assert dashboard._valid_session_id(result["session_id"]) == result["session_id"]


def test_clear_chat_session_removes_history_and_returns_new_id(manifest, monkeypatch):
    from pipeline import answer as answer_module

    monkeypatch.setattr(
        answer_module, "ask",
        lambda question, mode=None, top_k=None, synthesize=True, history=None: _stub_answer(),
    )
    session_id = dashboard.ask("q", session_id=None)["session_id"]

    result = dashboard.clear_chat_session(session_id)

    assert result["cleared"] == 1
    assert result["session_id"] != session_id
    with db.connect() as conn:
        assert db.recent_chat_turns(conn, session_id) == []


def test_clear_chat_session_with_invalid_id_clears_nothing():
    result = dashboard.clear_chat_session("garbage")
    assert result["cleared"] == 0
    assert dashboard._valid_session_id(result["session_id"]) == result["session_id"]


def test_valid_session_id_accepts_only_32_hex_chars():
    good = uuid.uuid4().hex
    assert dashboard._valid_session_id(good) == good
    assert dashboard._valid_session_id("not-hex-and-wrong-length") is None
    assert dashboard._valid_session_id("a" * 31) is None
    assert dashboard._valid_session_id("g" * 32) is None  # not hex
    assert dashboard._valid_session_id(None) is None


def test_dashboard_command_accepts_local_options():
    args = __import__("pipeline.cli", fromlist=["build_parser"]).build_parser().parse_args(
        ["dashboard", "--host", "127.0.0.1", "--port", "9876"]
    )
    assert args.host == "127.0.0.1"
    assert args.port == 9876


def test_classifier_does_not_read_software_vocabulary_as_personal():
    """Substring matching filed seven work meetings as Personal.

    "lease" matched *re*lease, "tenant" matched multi-tenant, "separation"
    matched separation of concerns, and "personal" matched personal data - all
    ordinary vocabulary in a security-software corpus.
    """
    for text, title in (
        ("We shipped the release notes for the multi-tenant refactor.", "USC Release"),
        ("Separation of concerns between the query and control services.", "Architecture"),
        ("Personal data handling and the health check endpoint.", "API Security"),
    ):
        assert dashboard.classify_meeting_category(text, title, None)["domain"] == "Professional"


def test_classifier_still_catches_genuinely_personal_meetings():
    strong = dashboard.classify_meeting_category(
        "Discussion about the rental property and the tenant.", "Rental property", None
    )
    assert strong["domain"] == "Personal"

    # A weak term in the title is enough on its own.
    titled = dashboard.classify_meeting_category("Notes.", "Landlord call", None)
    assert titled["domain"] == "Personal"


def test_declared_frontmatter_category_beats_keyword_guessing():
    """An explicit category is deliberate; heuristics must not override it."""
    minutes = "---\ndate: 2026-08-12\ncategory: professional\n---\n\nRental property and the landlord."
    assert dashboard.classify_meeting_category(minutes, "Ops", None)["domain"] == "Professional"


def test_transcript_body_drops_the_file_header():
    rendered = "# Transcript ab\n\n- Model: `x`\n- Language: en\n\n---\n\n**[0:45] A:** hello"
    assert dashboard._transcript_body(rendered) == "**[0:45] A:** hello"
    assert dashboard._transcript_body("**[0:01] A:** only turns") == "**[0:01] A:** only turns"


# ── Decision timeline (reads decisions/open_questions, not markdown) ───

def test_decision_timeline_reads_the_decisions_table_without_touching_disk(manifest):
    """No minutes_path is ever set here, and MINUTES_DIR is never monkeypatched -
    if this passes, the timeline did not read a markdown file to build it."""
    make_meeting(manifest, "m1", "2026-08-10", title_hint="Roadmap Review", status=db.INDEXED)
    db.replace_decisions(
        manifest, "m1",
        [{"text": "Ship Atlas in Q1", "decided_by": "Faraz", "rationale": "capacity"}],
    )
    manifest.commit()  # dashboard.decision_timeline opens its own connection

    result = dashboard.decision_timeline()

    assert result["total_milestones"] == 1
    event = result["events"][0]
    assert event["meeting_id"] == "m1"
    assert event["headline"] == "Ship Atlas in Q1"
    assert event["decisions"] == ["Ship Atlas in Q1"]


def test_decision_timeline_falls_back_to_an_open_question_when_undecided(manifest):
    make_meeting(manifest, "m1", "2026-08-10", status=db.INDEXED)
    db.replace_open_questions(manifest, "m1", [{"text": "Who owns onboarding?", "owner": "Faraz"}])
    manifest.commit()

    event = dashboard.decision_timeline()["events"][0]
    assert event["headline"] == "Who owns onboarding?"


def test_decision_timeline_falls_back_to_a_placeholder_when_nothing_recorded(manifest):
    make_meeting(manifest, "m1", "2026-08-10", status=db.INDEXED)
    manifest.commit()
    event = dashboard.decision_timeline()["events"][0]
    assert event["headline"] == "Meeting indexed and archived in memory."


def test_decision_timeline_caps_decisions_at_three(manifest):
    make_meeting(manifest, "m1", "2026-08-10", status=db.INDEXED)
    db.replace_decisions(manifest, "m1", [{"text": f"Decision {i}"} for i in range(5)])
    manifest.commit()
    event = dashboard.decision_timeline()["events"][0]
    assert len(event["decisions"]) == 3


def test_decision_timeline_topic_filter_matches_decision_text(manifest):
    """The topic filter must reach into decision/open-question text, not just
    the meeting title and entities."""
    make_meeting(manifest, "m1", "2026-08-10", title_hint="Standup", status=db.INDEXED)
    make_meeting(manifest, "m2", "2026-08-11", title_hint="Standup", status=db.INDEXED)
    db.replace_decisions(manifest, "m1", [{"text": "Use Kafka for the event bus"}])
    db.replace_decisions(manifest, "m2", [{"text": "Ship the UI redesign"}])
    manifest.commit()

    result = dashboard.decision_timeline("kafka")

    assert result["total_milestones"] == 1
    assert result["events"][0]["meeting_id"] == "m1"


def test_decision_timeline_only_includes_indexed_meetings(manifest):
    make_meeting(manifest, "m1", "2026-08-10", status=db.MINUTES_COMPILED)
    manifest.commit()
    assert dashboard.decision_timeline()["total_milestones"] == 0


# ── Commitments and decisions API-backing functions ─────────────────────

def test_commitments_list_wraps_db_query(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_commitments(
        manifest, "m1",
        [{"text": "task A", "owner": "Faraz"}, {"text": "task B", "owner": "Yuliya"}],
    )
    manifest.commit()

    result = dashboard.commitments_list()
    assert {c["text"] for c in result["commitments"]} == {"task A", "task B"}

    filtered = dashboard.commitments_list(owner="faraz")
    assert [c["text"] for c in filtered["commitments"]] == ["task A"]


def test_commitments_list_overdue_filter(manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    db.replace_commitments(
        manifest, "m1",
        [
            {"text": "overdue", "owner": "A", "due_date_iso": "2000-01-01", "state": "open"},
            {"text": "not due yet", "owner": "A", "due_date_iso": "2999-01-01", "state": "open"},
        ],
    )
    manifest.commit()

    result = dashboard.commitments_list(overdue=True)
    assert [c["text"] for c in result["commitments"]] == ["overdue"]


def test_decisions_list_wraps_db_query_and_topic_filter(manifest):
    make_meeting(manifest, "m1", "2026-08-10", title_hint="Kafka Review")
    db.replace_decisions(manifest, "m1", [{"text": "Use Kafka ACLs"}, {"text": "Ship the UI"}])
    manifest.commit()

    assert len(dashboard.decisions_list()["decisions"]) == 2
    filtered = dashboard.decisions_list(topic="kafka")
    assert [d["text"] for d in filtered["decisions"]] == ["Use Kafka ACLs"]


# ── Voice snippet serving and merge completeness ──────────────────────

def test_merge_people_moves_voice_samples_through_the_deep_module(manifest):
    dashboard.add_person("Michael")
    dashboard.add_person("Mike")
    with db.connect() as conn:
        db.add_voice_sample(
            conn,
            canonical="Mike",
            meeting_id=None,
            label="SPEAKER_00",
            embedding=b"voice",
            dim=1,
            model="test-model",
            speech_sec=12.0,
        )
    approved = dashboard.preview_people_merge(["Mike"], "Michael")

    result = dashboard.merge_people(
        "Mike", "Michael", expected_digest=approved.digest
    )

    assert result.target == "Michael"
    with db.connect() as conn:
        assert db.person_samples(conn, "Mike") == []
        assert len(db.person_samples(conn, "Michael")) == 1


def test_merge_many_people_preserves_voice_samples_before_deleting_sources(manifest):
    for name in ("Ru", "Roo", "Roe"):
        db.add_person(manifest, name)
    for index, name in enumerate(("Ru", "Roe")):
        db.add_voice_sample(
            manifest,
            canonical=name,
            meeting_id=None,
            label=f"SPEAKER_0{index}",
            embedding=b"voice",
            dim=1,
            model="test-model",
            speech_sec=12.0,
        )
    manifest.commit()

    approved = dashboard.preview_people_merge(["Ru", "Roo", "Roe"], "Roo")
    result = dashboard.merge_many_people(
        ["Ru", "Roo", "Roe"], "Roo", expected_digest=approved.digest
    )

    assert result.target == "Roo"
    with db.connect() as conn:
        samples = db.person_samples(conn, "Roo")
        assert len(samples) == 2
        assert {sample["label"] for sample in samples} == {"SPEAKER_00", "SPEAKER_01"}


def test_people_merge_http_preview_and_stale_digest_conflict(manifest):
    from http import HTTPStatus

    db.add_person(manifest, "Mike")
    manifest.commit()

    class FakeHandler(dashboard.DashboardHandler):
        def __init__(self, path, payload):
            self.path = path
            self.headers = {}
            self.bind_host = "127.0.0.1"
            self.payload = payload
            self.response = None

        def _payload(self):
            return self.payload

        def _json(self, status, payload):
            self.response = (status, payload)

    preview_handler = FakeHandler(
        "/api/people/merge-preview", {"names": ["Mike"], "into": "Michael"}
    )
    preview_handler.do_POST()
    status, preview_payload = preview_handler.response
    assert status == HTTPStatus.OK

    db.add_person(manifest, "Mike", aliases=["Mikey"])
    manifest.commit()
    merge_handler = FakeHandler(
        "/api/people/merge",
        {
            "from_name": "Mike",
            "into": "Michael",
            "expected_digest": preview_payload["digest"],
        },
    )
    merge_handler.do_POST()

    assert merge_handler.response[0] == HTTPStatus.CONFLICT
    assert "preview changed" in merge_handler.response[1]["error"]


def test_voice_snippet_requires_both_identifiers():
    """A missing label would otherwise match an arbitrary row."""
    import json as _json
    from http import HTTPStatus

    captured = {}

    class Fake:
        _json = lambda self, status, payload: captured.update(status=status, payload=payload)  # noqa: E731
        _serve_voice_snippet = dashboard.DashboardHandler._serve_voice_snippet

    Fake()._serve_voice_snippet("", "", 0)
    assert captured["status"] == HTTPStatus.BAD_REQUEST
    assert "required" in _json.dumps(captured["payload"])


def _pending_match(manifest, meeting_id, label, cluster_id, **fields):
    """A pending speaker_matches row attached to a cluster."""
    make_meeting(manifest, meeting_id, "2026-08-12", title_hint="Standup")
    db.upsert_speaker_match(
        manifest,
        meeting_id=meeting_id,
        label=label,
        cluster_id=cluster_id,
        state="pending",
        speech_sec=120.0,
        **fields,
    )
    # get_voice_clusters opens its own connection, so these rows have to land.
    manifest.commit()


def test_cluster_members_expose_clip_count_and_inferred_name(manifest, monkeypatch):
    """The review card has to say how much audio it can actually play.

    Clips are cut at enrollment and the source audio is deleted right after
    transcription, so a speaker has whatever was retained and no more: 3 clips
    for some, 1 for others, none for 8 of them. A card that offers "listen"
    without saying how long has to guess, and a 6-second speaker then looks
    broken rather than short.
    """
    import json as _json

    monkeypatch.setattr(dashboard.voices, "cluster_pending", lambda conn: None)
    monkeypatch.setattr(db, "pending_clusters", lambda conn, limit=50: [
        {
            "id": "cluster-1",
            "size": 1,
            "total_speech": 120.0,
            "best_canonical": "Yuliya",
            "best_score": 0.81,
            "next_canonical": None,
            "band": "review",
        }
    ])
    _pending_match(
        manifest,
        "b" * 64,
        "SPEAKER_02",
        "cluster-1",
        snippet_paths=_json.dumps(["m/SPEAKER_02-0.opus", "m/SPEAKER_02-1.opus"]),
        llm_name="Ruth",
    )

    clusters = dashboard.get_voice_clusters()
    member = clusters[0]["members"][0]
    assert member["snippet_count"] == 2
    assert member["llm_name"] == "Ruth"


def test_speaker_resolution_queue_keeps_one_off_labels_visible(manifest, monkeypatch):
    """No-embedding labels still need a way back to a human decision."""
    meeting_id = "speaker-one-off"
    make_meeting(manifest, meeting_id, "2026-08-12", title_hint="Client review")
    db.set_speaker(manifest, meeting_id, "SPEAKER_03", None, "unknown")
    db.upsert_speaker_match(
        manifest,
        meeting_id=meeting_id,
        label="SPEAKER_03",
        state="pending",
        speech_sec=31.0,
        llm_name="Ali",
    )
    manifest.commit()
    monkeypatch.setattr(dashboard, "get_voice_clusters", lambda: [{"id": "recurring"}])

    queue = dashboard.speaker_resolution_queue()

    assert queue["clusters"] == [{"id": "recurring"}]
    assert queue["one_offs"] == [
        {
            "meeting_id": meeting_id,
            "label": "SPEAKER_03",
            "meeting_title": "Client review",
            "meeting_date": "2026-08-12",
            "speech_sec": 31.0,
            "snippet_count": 0,
            "best_canonical": None,
            "best_score": None,
            "llm_suggestion": "Ali",
        }
    ]


def test_cluster_offers_the_transcript_name_as_a_second_suggestion(manifest, monkeypatch):
    """A voiceprint match and a name heard in the room are different evidence.

    The minutes stage already infers names from direct address ("thanks, Ruth")
    and stores them on the match. The card showed only the voiceprint's guess,
    so that second, independent signal was collected and never offered - which
    is exactly the case where the voiceprint is weakest.
    """
    monkeypatch.setattr(dashboard.voices, "cluster_pending", lambda conn: None)
    monkeypatch.setattr(db, "pending_clusters", lambda conn, limit=50: [
        {
            "id": "cluster-2",
            "size": 2,
            "total_speech": 240.0,
            "best_canonical": "Yuliya",
            "best_score": 0.55,
            "next_canonical": None,
            "band": "review",
        }
    ])
    for i, mid in enumerate(("c" * 64, "d" * 64)):
        _pending_match(manifest, mid, f"SPEAKER_0{i}", "cluster-2", llm_name="Ruth")

    cluster = dashboard.get_voice_clusters()[0]
    assert cluster["llm_suggestion"] == "Ruth", "the name heard in the room must reach the card"


def test_confirm_all_only_touches_clusters_above_the_threshold(manifest, monkeypatch):
    """Bulk accept is for the confident tail, not the whole queue.

    Confirming a voice writes a name onto every meeting the cluster appears in,
    so a bulk action that swept up weak matches would spread one wrong guess
    across the archive - and the per-cluster undo is a manual rename per meeting.
    """
    confirmed = []
    monkeypatch.setattr(
        dashboard,
        "get_voice_clusters",
        lambda: [
            {"id": "strong", "best_canonical": "Yuliya", "best_score": 0.91},
            {"id": "weak", "best_canonical": "Catherine", "best_score": 0.62},
            {"id": "nameless", "best_canonical": None, "best_score": 0.99},
        ],
    )
    monkeypatch.setattr(
        dashboard,
        "confirm_voice_cluster",
        lambda cluster_id, canonical: confirmed.append((cluster_id, canonical)) or 3,
    )

    result = dashboard.confirm_confident_clusters(threshold=0.85)

    assert confirmed == [("strong", "Yuliya")], "only the confident, named cluster"
    assert result["clusters"] == 1
    assert result["meetings"] == 3
    assert result["skipped"] == 2
