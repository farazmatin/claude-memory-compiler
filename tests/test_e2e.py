"""End-to-end tests: the app, not its functions.

Each test drives the real `pipeline` CLI over a throwaway tree with real audio,
real ffmpeg, a real SQLite manifest, and real HTTP to a LightRAG-shaped server.
Only the three things that need a GPU, a subscription or a docker stack are faked
(see `e2e_harness`).

These exist because the unit suite could pass while the app was broken: nothing
tested that stage 2's output is shaped the way stage 3 expects, that the CLI wires
its own commands together, or that the subprocess provider path works at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import asr, cli, compile_minutes, db, index, llm
from tests.e2e_harness import (
    FakeASRBackend,
    FakeLightRAG,
    apply_env,
    install_fake_llm,
    make_audio,
    pipeline_env,
)

pytestmark = pytest.mark.e2e


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """A fully wired pipeline over a throwaway tree."""
    server = FakeLightRAG().start()
    backend = FakeASRBackend()

    env = pipeline_env(tmp_path, server.url)
    env.update(install_fake_llm(tmp_path))
    apply_env(monkeypatch, env, [asr, index, cli, llm, compile_minutes])
    monkeypatch.setattr(asr, "default_backend", lambda: backend)
    # This fixture's audio is ~1s with a fixed ~30-word canned transcript,
    # deliberately tiny for speed - by design that is exactly what the
    # junk-recording floor (MIN_MEETING_SEC/MIN_TRANSCRIPT_WORDS) exists to
    # park. That feature has its own coverage in test_minutes_triage.py; disable
    # it here so it does not interfere with tests that are about pipeline
    # plumbing rather than this feature.
    monkeypatch.setattr(cli, "MIN_MEETING_SEC", 0.0)
    monkeypatch.setattr(cli, "MIN_TRANSCRIPT_WORDS", 0)

    class App:
        root = tmp_path
        inbox = Path(env["MMC_INBOX"])
        minutes_dir = Path(env["MMC_MINUTES"])
        transcripts_dir = Path(env["MMC_TRANSCRIPTS"])
        audio_dir = Path(env["MMC_AUDIO"])
        lightrag = server
        asr_backend = backend

        @staticmethod
        def run(*argv: str) -> int:
            return cli.main(list(argv))

        @staticmethod
        def meetings() -> list[db.Meeting]:
            with db.connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM meetings ORDER BY meeting_date, meeting_time"
                ).fetchall()
                return [db._row_to_meeting(r) for r in rows]

    App.run("init")
    yield App
    server.stop()


def add_meeting(app, name: str = "Ali Aug 10 at 11-12 a.m..m4a", freq: int = 440) -> Path:
    return make_audio(app.inbox / name, seconds=1.0, freq=freq)


# ═══════════════════════════════════════════════════════════════════════
# E2E-1  Happy path: audio in, answer out
# ═══════════════════════════════════════════════════════════════════════

def test_full_pipeline_audio_to_answer(app, capsys):
    """The whole product in one test: a recording becomes a queryable answer."""
    add_meeting(app)

    assert app.run("run", "--owner", "Faraz") == 0

    meetings = app.meetings()
    assert len(meetings) == 1
    meeting = meetings[0]

    # Ingest parsed the real filename and probed the real audio.
    assert meeting.meeting_date == "2026-08-10"
    # "at 11-12 a.m." is 11:12, not an 11-to-12 range: ":" is illegal in a
    # filename so the recorder writes the minute after a hyphen, which is the
    # only reading that makes sense of the rest of the corpus ("at 8-40 p.m.").
    assert meeting.meeting_time == "11:12"
    assert meeting.title_hint == "Ali"
    assert meeting.duration_sec and meeting.duration_sec > 0, "ffprobe read the file"

    # It reached the end of the ladder.
    assert meeting.status == db.INDEXED
    assert meeting.lightrag_doc_id, "doc id recorded for replace-on-recompile"

    # Every artifact exists on disk.
    assert Path(meeting.transcript_path).exists()
    assert Path(meeting.minutes_path).exists()
    assert (app.transcripts_dir / f"{meeting.id[:12]}.md").exists(), "readable transcript"

    # Speakers resolved through the real LLM subprocess.
    with db.connect() as conn:
        assert db.get_speakers(conn, meeting.id) == {
            "SPEAKER_00": "Faraz", "SPEAKER_01": "Ali"
        }

    # Entities were parsed out of the minutes and stored.
    with db.connect() as conn:
        names = {e["name"] for e in db.get_entities(conn, meeting.id)}
        assert {"Atlas", "Faraz", "Northwind"} <= names
        assert db.get_relations(conn, meeting.id), "relations extracted"

    # The document reached the index, carrying the canonicalized graph block.
    assert len(app.lightrag.documents) == 1
    indexed = next(iter(app.lightrag.documents.values()))["text"]
    assert "## Knowledge Graph" in indexed
    assert "Atlas -> part of -> 2026.4" in indexed

    # And a question gets an answer.
    assert app.run("query", "why was Atlas deferred?") == 0
    assert "Atlas" in capsys.readouterr().out


# ═══════════════════════════════════════════════════════════════════════
# E2E-2  Dedup against real duplicate files
# ═══════════════════════════════════════════════════════════════════════

def test_duplicate_audio_is_ingested_once(app):
    """The source Drive folder really does contain byte-identical duplicates."""
    original = add_meeting(app)
    duplicate = app.inbox / "Ali Aug 10 at 11-12 a.m. (1).m4a"
    duplicate.write_bytes(original.read_bytes())

    assert app.run("ingest") == 0
    assert len(app.meetings()) == 1, "identical bytes under a different name is one meeting"

    app.run("run", "--owner", "Faraz")
    assert len(app.lightrag.documents) == 1
    assert len(app.asr_backend.calls) == 1, "the expensive stage ran once"


# ═══════════════════════════════════════════════════════════════════════
# E2E-3  Resumability and idempotence
# ═══════════════════════════════════════════════════════════════════════

def test_stages_advance_one_at_a_time(app):
    """Each stage claims its own status and hands on. This is what makes a crash
    cost minutes rather than the hours of transcription behind it."""
    add_meeting(app)

    for command, expected in [
        ("ingest", db.DISCOVERED),
        ("transcribe", db.TRANSCRIBED),
        ("speakers", db.SPEAKERS_RESOLVED),
        ("minutes", db.MINUTES_COMPILED),
        ("index", db.INDEXED),
    ]:
        args = [command] + (["--owner", "Faraz"] if command == "speakers" else [])
        assert app.run(*args) == 0
        assert app.meetings()[0].status == expected


def test_rerunning_a_completed_batch_does_nothing(app):
    add_meeting(app)
    app.run("run", "--owner", "Faraz")

    calls_before = len(app.asr_backend.calls)
    docs_before = dict(app.lightrag.documents)

    assert app.run("run", "--owner", "Faraz") == 0
    assert len(app.asr_backend.calls) == calls_before, "no re-transcription"
    assert app.lightrag.documents == docs_before, "no re-indexing"


def test_second_scan_skips_known_inbox_files(app):
    """Files already seen are not re-read - the inbox never empties."""
    add_meeting(app)
    app.run("ingest")

    hashed: list[str] = []
    from pipeline import ingest as ingest_module

    original = ingest_module.hash_file
    ingest_module.hash_file = lambda p, **kw: hashed.append(p.name) or original(p, **kw)
    try:
        app.run("ingest")
    finally:
        ingest_module.hash_file = original

    assert hashed == [], "a known file must not be re-hashed"


# ═══════════════════════════════════════════════════════════════════════
# E2E-4  Recompilation — the payoff for retaining transcripts
# ═══════════════════════════════════════════════════════════════════════

def test_recompile_rebuilds_without_retranscribing(app, monkeypatch, tmp_path):
    """Bump the template, rebuild history, pay no ASR cost. This is the property
    the whole three-tier design exists to provide."""
    add_meeting(app)
    app.run("run", "--owner", "Faraz")

    asr_calls = len(app.asr_backend.calls)
    old_doc_id = app.meetings()[0].lightrag_doc_id

    # A new template version produces different minutes. Derive the bumped
    # value from the real one rather than hard-coding "2": the shipping version
    # moves, and a literal here silently stops testing anything the moment it
    # catches up - the corpus recompile that bumped it to "2" did exactly that.
    from pipeline.config import TEMPLATE_VERSION as CURRENT_VERSION

    bumped = f"{CURRENT_VERSION}-next"
    revised = (tmp_path / "minutes_response.md").read_text().replace(
        f'template_version: "{CURRENT_VERSION}"', f'template_version: "{bumped}"'
    ).replace("Atlas Roadmap Review", "Atlas Roadmap Review (revised)")
    (tmp_path / "minutes_response.md").write_text(revised, encoding="utf-8")

    from pipeline import compile_minutes
    monkeypatch.setattr(compile_minutes, "TEMPLATE_VERSION", bumped)
    monkeypatch.setattr(cli, "TEMPLATE_VERSION", bumped)

    assert app.run("minutes", "--recompile") == 0
    assert app.run("index") == 0

    assert len(app.asr_backend.calls) == asr_calls, "ASR must not run again"
    assert app.meetings()[0].template_version == bumped

    # The stale copy was removed rather than left beside the new one.
    assert old_doc_id in app.lightrag.deleted, "old version deleted before re-insert"
    assert len(app.lightrag.documents) == 1, "exactly one copy in the index"
    assert "revised" in next(iter(app.lightrag.documents.values()))["text"]


def test_reindex_is_abandoned_when_the_stale_copy_survives(app):
    """Refusing to insert keeps the corpus consistent. Two contradictory versions
    of one meeting is worse than a meeting temporarily missing."""
    add_meeting(app)
    app.run("run", "--owner", "Faraz")

    # Force new content, then make deletion impossible.
    (app.minutes_dir / next(iter(p.name for p in app.minutes_dir.glob("*.md")))).write_text(
        "---\ndate: 2026-08-10\ntitle: Changed\n---\n# Changed\n", encoding="utf-8"
    )
    with db.connect() as conn:
        db.advance(conn, app.meetings()[0].id, db.MINUTES_COMPILED)
    app.lightrag.refuse_delete = True

    assert app.run("index") == 1, "must report failure"
    assert len(app.lightrag.documents) == 1, "no duplicate was created"


# ═══════════════════════════════════════════════════════════════════════
# E2E-5  Failure handling
# ═══════════════════════════════════════════════════════════════════════

def test_batch_failure_exits_nonzero_and_alerts(app, tmp_path, monkeypatch):
    """A nightly cron must not report success after a failure, and on a headless
    server the exit code alone reaches nobody."""
    add_meeting(app)
    app.asr_backend.fail = True

    alert_log = tmp_path / "alert.txt"
    from pipeline import alert
    monkeypatch.setattr(alert, "ALERT_COMMAND", f"tee {alert_log}")

    assert app.run("run", "--owner", "Faraz") == 1
    assert app.meetings()[0].status == db.FAILED
    assert "simulated ASR failure" in (app.meetings()[0].error or "")

    assert alert_log.exists(), "failure must be pushed somewhere visible"
    body = alert_log.read_text(encoding="utf-8")
    assert "transcribe" in body
    assert "pipeline doctor" in body


def test_failed_meeting_can_be_requeued_and_completed(app):
    """Nothing is lost: the audio is retained and the stage is resumable."""
    add_meeting(app)
    app.asr_backend.fail = True
    app.run("run", "--owner", "Faraz")
    assert app.meetings()[0].status == db.FAILED

    app.asr_backend.fail = False
    assert app.run("retry") == 0
    assert app.run("run", "--owner", "Faraz") == 0
    assert app.meetings()[0].status == db.INDEXED


def test_one_bad_file_does_not_stop_the_batch(app):
    """A single unreadable recording must not cost the rest of the night."""
    add_meeting(app, "good Aug 10 at 9-10 a.m..m4a", freq=440)
    corrupt = app.inbox / "bad Aug 11 at 9-10 a.m..m4a"
    corrupt.write_bytes(b"this is not audio")

    app.run("ingest")
    real_transcribe = app.asr_backend.transcribe

    # Archived audio is renamed to {date}_{hash}, so identify the bad meeting by
    # its manifest row rather than by the filename on disk.
    bad_ids = {m.id for m in app.meetings() if m.title_hint == "bad"}

    def selective(audio_path, meeting_id, prompt):
        if meeting_id in bad_ids:
            raise RuntimeError("unreadable audio")
        return real_transcribe(audio_path, meeting_id, prompt)

    app.asr_backend.transcribe = selective
    # Non-zero is expected: one meeting failed. The point is that the other one
    # still got through rather than the batch aborting at the first error.
    assert app.run("transcribe") == 1

    statuses = {m.title_hint: m.status for m in app.meetings()}
    assert statuses["good"] == db.TRANSCRIBED, "the good file still progressed"
    assert statuses["bad"] == db.FAILED, "the bad one is parked, not silent"


def test_provider_failure_leaves_transcript_intact(app, tmp_path, monkeypatch):
    """Minutes failures are retryable, so the meeting stays in the queue rather
    than being parked - the expensive work is already done."""
    add_meeting(app)
    app.run("ingest")
    app.run("transcribe")
    app.run("speakers", "--owner", "Faraz")

    fail_flag = tmp_path / "fail"
    fail_flag.write_text("x")
    monkeypatch.setenv("FAKE_LLM_FAIL", str(fail_flag))

    assert app.run("minutes") == 1
    meeting = app.meetings()[0]
    assert meeting.status == db.SPEAKERS_RESOLVED, "stays queued for the next batch"
    assert Path(meeting.transcript_path).exists(), "transcript survives"

    # Recovering needs no re-transcription.
    fail_flag.unlink()
    assert app.run("minutes") == 0
    assert app.meetings()[0].status == db.MINUTES_COMPILED


# ═══════════════════════════════════════════════════════════════════════
# E2E-6  Data safety
# ═══════════════════════════════════════════════════════════════════════

def test_inbox_is_never_modified(app):
    """The inbox is a cloud-synced folder; deleting from it destroys the original."""
    audio = add_meeting(app)
    before = audio.read_bytes()

    app.run("run", "--owner", "Faraz")

    assert audio.exists(), "source file must survive"
    assert audio.read_bytes() == before, "and be untouched"


def test_backup_and_restore_round_trip(app, tmp_path):
    """A backup that cannot be restored is worse than none."""
    add_meeting(app)
    app.run("run", "--owner", "Faraz")

    destination = tmp_path / "backup"
    assert app.run("backup", "--to", str(destination)) == 0

    assert (destination / "BACKUP_INFO.txt").exists()
    assert list((destination / "transcripts").glob("*.json")), "source of truth backed up"
    assert list((destination / "minutes").glob("*.md")), "corpus backed up"
    assert list((destination / "audio").glob("*")), "audio backed up"

    # The manifest snapshot must be a real, queryable database.
    import sqlite3

    conn = sqlite3.connect(destination / "db" / "manifest.db")
    try:
        count = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_transcripts_are_retained_and_readable(app):
    """Tier 1 is the source that makes recompilation possible."""
    add_meeting(app)
    app.run("run", "--owner", "Faraz")

    meeting = app.meetings()[0]
    data = json.loads(Path(meeting.transcript_path).read_text())
    assert data["segments"], "word/segment data retained"

    readable = (app.transcripts_dir / f"{meeting.id[:12]}.md").read_text()
    assert "Faraz:" in readable, "resolved names in the readable copy"


# ═══════════════════════════════════════════════════════════════════════
# E2E-7  Cross-meeting behaviour
# ═══════════════════════════════════════════════════════════════════════

def test_meetings_are_processed_oldest_first(app):
    """Out-of-order compilation would compare a meeting against its own future."""
    add_meeting(app, "c Aug 12 at 9-10 a.m..m4a", freq=300)
    add_meeting(app, "a Aug 10 at 9-10 a.m..m4a", freq=440)
    add_meeting(app, "b Aug 11 at 9-10 a.m..m4a", freq=500)

    app.run("ingest")
    app.run("transcribe")

    order = [Path(c).name for c in app.asr_backend.calls]
    assert order == sorted(order), "processed in meeting-date order"


def test_second_meeting_receives_prior_context(app, tmp_path, monkeypatch):
    """The reversal-detection feature depends on earlier minutes reaching the
    compiler prompt."""
    add_meeting(app, "a Aug 10 at 9-10 a.m..m4a", freq=440)
    app.run("run", "--owner", "Faraz")

    captured: list[str] = []
    from pipeline import compile_minutes
    real_build = compile_minutes.build_prompt

    def spy(*args, **kwargs):
        prompt = real_build(*args, **kwargs)
        captured.append(prompt)
        return prompt

    monkeypatch.setattr(compile_minutes, "build_prompt", spy)

    add_meeting(app, "b Aug 11 at 9-10 a.m..m4a", freq=500)
    app.run("run", "--owner", "Faraz")

    assert captured, "the second meeting was compiled"
    assert "Atlas Roadmap Review" in captured[0], "earlier minutes reached the prompt"


def test_people_registry_normalizes_across_meetings(app):
    """One person recorded two ways would be two graph nodes."""
    add_meeting(app)
    app.run("run", "--owner", "Faraz")

    with db.connect() as conn:
        db.merge_person(conn, "Ali", "Alison Chen")
        assert db.canonical_name(conn, "Ali") == "Alison Chen"
        # History was rewritten, not just future meetings.
        assert "Alison Chen" in db.get_speakers(conn, app.meetings()[0].id).values()


# ═══════════════════════════════════════════════════════════════════════
# E2E-8  Operator surface
# ═══════════════════════════════════════════════════════════════════════

def test_status_reports_state_and_measured_timings(app, capsys):
    add_meeting(app)
    app.run("run", "--owner", "Faraz")
    capsys.readouterr()

    assert app.run("status") == 0
    out = capsys.readouterr().out
    assert "indexed" in out
    assert "Stage timings" in out, "measured, not estimated"
    assert "LightRAG: reachable" in out


def test_doctor_passes_against_a_configured_environment(app, capsys):
    """With ffmpeg, a provider and LightRAG all present, the only failures left
    should be the genuinely absent pieces."""
    app.run("doctor")
    out = capsys.readouterr().out

    assert "ffmpeg" in out
    assert "lightrag" in out
    assert "reachable" in out, "should see the running fake server"
    assert "will use gemini" in out, "should see the fake provider"


def test_entities_command_surfaces_graph_health(app, capsys):
    add_meeting(app)
    app.run("run", "--owner", "Faraz")
    capsys.readouterr()

    assert app.run("entities") == 0
    out = capsys.readouterr().out
    assert "Atlas" in out
    assert "feature" in out


def test_query_timing_attributes_latency(app, capsys):
    add_meeting(app)
    app.run("run", "--owner", "Faraz")
    capsys.readouterr()

    assert app.run("query", "what happened?", "--timing") == 0
    out = capsys.readouterr().out
    assert "retrieval" in out and "synthesis" in out
    # Regression: `answer.py` imported `last_provider` by value, so this always
    # read "via None". The unit test patched the same stale name and passed, which
    # is exactly why this assertion lives here instead - on the real CLI output.
    assert "via gemini" in out, "the timing line must name the provider that answered"
    assert "via None" not in out


def test_local_query_uses_lightrag_generation(app, capsys):
    add_meeting(app)
    app.run("run", "--owner", "Faraz")
    capsys.readouterr()

    assert app.run("query", "what happened?", "--local") == 0
    assert "local-model answer" in capsys.readouterr().out
