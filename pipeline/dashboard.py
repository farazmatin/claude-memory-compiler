"""Local, read-only and interactive meeting-memory control room."""

from __future__ import annotations

import argparse
import contextlib
import json
import mimetypes
import re
import subprocess
import threading
import uuid
import webbrowser
from collections import Counter, deque
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pipeline import dashboard_auth, db, index, voices
from pipeline.config import (
    ANTIGRAVITY_MODEL,
    AUDIO_DIR,
    CHAT_HISTORY_TURNS,
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    DRIVE_CREDENTIALS_FILE,
    DRIVE_TOKEN_FILE,
    GEMINI_MODEL,
    INBOX_DIR,
    MINUTES_DIR,
    OWNER_NAME,
    SNIPPET_SEC,
    SNIPPETS_DIR,
    TEMPLATE_VERSION,
    TRANSCRIPTS_DIR,
    TZ,
    now_iso,
)
from pipeline.titles import clean_meeting_title

STATIC_DIR = Path(__file__).with_name("static")
MAX_QUERY_CHARS = 4_000
VALID_QUERY_MODES = {"hybrid", "global", "local", "naive", "mix"}

# ── Pipeline background runner state ──────────────────────────────────
_pipeline_lock = threading.Lock()
_pipeline_state: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "started_at": None,
    "finished_at": None,
    "success": None,
    "error": None,
}
_log_buffer: deque[str] = deque(maxlen=200)


def get_pipeline_status() -> dict[str, Any]:
    """Return the current background pipeline execution state and recent logs."""
    with _pipeline_lock:
        return {
            "running": _pipeline_state["running"],
            "stage": _pipeline_state["stage"],
            "started_at": _pipeline_state["started_at"],
            "finished_at": _pipeline_state["finished_at"],
            "success": _pipeline_state["success"],
            "error": _pipeline_state["error"],
            "logs": list(_log_buffer),
        }


def trigger_pipeline_run(stage: str = "all", limit: int | None = None) -> dict[str, Any]:
    """Start an asynchronous pipeline execution in a background worker thread."""
    with _pipeline_lock:
        if _pipeline_state["running"]:
            raise ValueError("A pipeline operation is already in progress.")
        _pipeline_state["running"] = True
        _pipeline_state["stage"] = stage
        _pipeline_state["started_at"] = now_iso()
        _pipeline_state["finished_at"] = None
        _pipeline_state["success"] = None
        _pipeline_state["error"] = None

    _log_buffer.append(f"[{now_iso()}] Initiating background pipeline stage: '{stage}'...")

    worker = threading.Thread(
        target=_run_pipeline_worker,
        args=(stage, limit),
        daemon=True,
        name="PipelineWorkerThread",
    )
    worker.start()
    return {"status": "started", "stage": stage, "started_at": _pipeline_state["started_at"]}


def _run_pipeline_worker(stage: str, limit: int | None) -> None:
    from pipeline import cli

    class _LogCapture:
        def write(self, text: str) -> int:
            for line in text.splitlines():
                if line.strip():
                    _log_buffer.append(f"[{now_iso()}] {line.rstrip()}")
            return len(text)

        def flush(self) -> None:
            pass

    log_stream = _LogCapture()
    success = False
    error_msg: str | None = None
    try:
        with contextlib.redirect_stdout(log_stream), contextlib.redirect_stderr(log_stream):
            cli.ensure_dirs()
            db.init_db()
            if stage == "all":
                rc = cli.cmd_run(argparse.Namespace(limit=limit, no_llm=False, owner=OWNER_NAME))
            elif stage == "ingest":
                rc = cli.cmd_ingest(argparse.Namespace(then_run=False))
            elif stage == "capture":
                rc = cli.cmd_capture(argparse.Namespace(dry_run=False, complete_backfill=False))
            elif stage == "transcribe":
                rc = cli.cmd_transcribe(
                    argparse.Namespace(limit=limit, keep_going=False, traceback=False)
                )
            elif stage == "speakers":
                rc = cli.cmd_speakers(
                    argparse.Namespace(
                        limit=limit, owner=OWNER_NAME, no_llm=False, traceback=False, all=False
                    )
                )
            elif stage == "minutes":
                rc = cli.cmd_minutes(
                    argparse.Namespace(limit=limit, recompile=False, traceback=False, force=False)
                )
            elif stage == "index":
                rc = cli.cmd_index(argparse.Namespace(limit=limit))
            elif stage == "recompile":
                rc = cli.cmd_minutes(
                    argparse.Namespace(limit=limit, recompile=True, traceback=False, force=False)
                )
            else:
                raise ValueError(f"Unknown pipeline stage: {stage}")
            success = rc == 0
            _log_buffer.append(f"[{now_iso()}] Pipeline stage '{stage}' finished with exit code {rc}")
    except Exception as exc:
        error_msg = str(exc)
        _log_buffer.append(f"[{now_iso()}] Pipeline stage '{stage}' crashed: {exc}")
    finally:
        with _pipeline_lock:
            _pipeline_state["running"] = False
            _pipeline_state["stage"] = "idle"
            _pipeline_state["finished_at"] = now_iso()
            _pipeline_state["success"] = success
            _pipeline_state["error"] = error_msg


def retry_failed(target_status: str = db.DISCOVERED) -> int:
    """Requeue all failed meetings to the target status."""
    db.init_db()
    with db.connect() as conn:
        failed = db.pending(conn, db.FAILED)
        for meeting in failed:
            db.reset_to(conn, meeting.id, target_status)
        return len(failed)


def retry_meeting(meeting_id: str, target_status: str = db.DISCOVERED) -> bool:
    """Requeue a specific meeting to the target status."""
    db.init_db()
    with db.connect() as conn:
        meeting = db.get_meeting(conn, meeting_id)
        if not meeting:
            return False
        db.reset_to(conn, meeting.id, target_status)
        return True


def people() -> list[dict[str, Any]]:
    """Return all canonical people and their aliases."""
    db.init_db()
    with db.connect() as conn:
        records = db.list_people(conn)

    for person in records:
        aliases = person.get("aliases")
        if aliases is None:
            person["aliases"] = []
        elif isinstance(aliases, str):
            person["aliases"] = [alias.strip() for alias in aliases.split(",") if alias.strip()]
        elif not isinstance(aliases, list):
            person["aliases"] = list(aliases)
    return records


def add_person(canonical: str, role: str | None = None, aliases: list[str] | None = None) -> None:
    """Register or update a canonical person with optional aliases."""
    db.init_db()
    with db.connect() as conn:
        db.add_person(conn, canonical=canonical, role=role, aliases=aliases)


def merge_people(from_name: str, into: str) -> int:
    """Merge one person/alias into another and rewrite historical records."""
    db.init_db()
    with db.connect() as conn:
        rewritten = db.merge_person(conn, from_name=from_name, into=into)
        voices.merge_people(conn, from_name, into)
        return rewritten


def set_meeting_speaker(
    meeting_id: str, label: str, name: str, confidence: str = "confirmed"
) -> None:
    """Set or override a speaker mapping for a specific meeting."""
    db.init_db()
    cleaned = name.strip()
    with db.connect() as conn:
        db.set_speaker(
            conn,
            meeting_id=meeting_id,
            label=label,
            name=cleaned or None,
            confidence=confidence,
        )
        if cleaned:
            db.add_person(conn, canonical=cleaned)


def get_voice_clusters() -> list[dict[str, Any]]:
    """Return pending voice review clusters with member meeting details."""
    db.init_db()
    with db.connect() as conn:
        voices.cluster_pending(conn)
        clusters = db.pending_clusters(conn)

        result = []
        for c in clusters:
            labels = db.cluster_labels(conn, c["id"])
            members = []
            llm_votes: Counter[str] = Counter()
            for row in labels:
                m = db.get_meeting(conn, row["meeting_id"])
                # How much audio the card can actually play. Clips are cut once,
                # at enrollment, and transcription deletes the source audio in
                # the same loop - so this count is fixed for good and a card that
                # promises "listen" without it is promising audio that may not
                # exist.
                try:
                    snippets = json.loads(row["snippet_paths"] or "[]")
                except (TypeError, ValueError):
                    snippets = []
                llm_name = (row["llm_name"] or "").strip()
                if llm_name:
                    llm_votes[llm_name] += 1
                members.append({
                    "meeting_id": row["meeting_id"],
                    "label": row["label"],
                    # Raw title_hint is a mangled Drive id; the voice card was
                    # labelling every meeting chip with one.
                    "meeting_title": (
                        clean_meeting_title(m.source_name, m.title_hint, m.minutes_path)
                        if m
                        else "Meeting"
                    ),
                    "meeting_date": m.meeting_date if m else None,
                    "speech_sec": float(row["speech_sec"] or 0),
                    "snippet_count": len(snippets) if isinstance(snippets, list) else 0,
                    "llm_name": llm_name or None,
                })
            result.append({
                "id": c["id"],
                "size": c["size"],
                "total_speech": c["total_speech"],
                "best_canonical": c["best_canonical"],
                "best_score": round(float(c["best_score"] or 0), 2) if c["best_score"] is not None else None,
                "next_canonical": c["next_canonical"],
                "band": c["band"],
                # A name heard in the room is independent evidence from a
                # voiceprint score, and it is strongest exactly where the
                # voiceprint is weakest. Offer it alongside, never instead.
                "llm_suggestion": llm_votes.most_common(1)[0][0] if llm_votes else None,
                "clip_seconds": round(sum(m["snippet_count"] for m in members) * SNIPPET_SEC),
                "members": members,
            })
        return result


def confirm_voice_cluster(cluster_id: str, canonical: str) -> int:
    """Confirm all appearances in a voice cluster as a canonical person name."""
    db.init_db()
    with db.connect() as conn:
        return voices.confirm(conn, cluster_id=cluster_id, canonical=canonical.strip())


def confirm_confident_clusters(threshold: float = 0.85) -> dict[str, int]:
    """Confirm named voice clusters at or above a confidence threshold."""
    clusters = get_voice_clusters()
    candidates = [
        cluster
        for cluster in clusters
        if cluster.get("best_canonical") and float(cluster.get("best_score") or 0) >= threshold
    ]
    meetings = sum(
        confirm_voice_cluster(str(cluster["id"]), str(cluster["best_canonical"]))
        for cluster in candidates
    )
    return {
        "clusters": len(candidates),
        "meetings": meetings,
        "skipped": len(clusters) - len(candidates),
    }


def dismiss_voice_cluster(cluster_id: str) -> int:
    """Dismiss a cluster as non-speaker noise or crosstalk."""
    db.init_db()
    with db.connect() as conn:
        return voices.dismiss(conn, cluster_id=cluster_id)


def split_voice_cluster(cluster_id: str) -> list[str]:
    """Split a cluster back into individual pending speaker matches."""
    db.init_db()
    with db.connect() as conn:
        return voices.split_cluster(conn, cluster_id=cluster_id)


def decision_timeline(topic: str | None = None) -> dict[str, Any]:
    """Chronological milestones and decisions for a topic or across the corpus.

    Reads the `decisions` / `open_questions` tables rather than re-reading every
    indexed meeting's markdown from disk and keyword-scraping it on every
    request. That used to mean 45 file reads plus a heuristic re-guess of what
    counted as a "decision" line on every single timeline view; now it is one
    query, and it can never surface something the compiler did not actually
    label as a decision.
    """
    db.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, source_name, title_hint, minutes_path, meeting_date, meeting_time,
                   duration_sec
            FROM meetings
            WHERE status = ?
            ORDER BY meeting_date ASC, meeting_time ASC
            """,
            (db.INDEXED,),
        ).fetchall()

        filter_term = (topic or "").strip().lower()
        if filter_term in {"all", "everything", ""}:
            filter_term = ""

        events = []
        for row in rows:
            speakers = [
                s["name"]
                for s in conn.execute(
                    "SELECT name FROM speakers WHERE meeting_id = ? AND name IS NOT NULL",
                    (row["id"],),
                ).fetchall()
            ]
            entity_names = [e["name"] for e in db.get_entities(conn, row["id"])]
            decision_rows = db.get_decisions(conn, row["id"])
            question_rows = db.get_open_questions(conn, row["id"])

            title = clean_meeting_title(
                row["source_name"], row["title_hint"], row["minutes_path"]
            )
            if filter_term:
                searchable = " ".join(
                    [
                        title,
                        *entity_names,
                        *speakers,
                        *(d["text"] for d in decision_rows),
                        *(q["text"] for q in question_rows),
                    ]
                ).lower()
                if filter_term not in searchable:
                    continue

            decisions = [d["text"] for d in decision_rows[:3]]
            if not decisions and question_rows:
                # Nothing was decided, but an open question is still a milestone
                # worth surfacing rather than falling through to a placeholder.
                decisions = [question_rows[0]["text"]]
            if not decisions:
                decisions = ["Meeting indexed and archived in memory."]

            events.append({
                "meeting_id": row["id"],
                "short_id": row["id"][:8],
                "date": row["meeting_date"] or "Undated",
                "time": row["meeting_time"] or "",
                "title": title,
                "headline": decisions[0],
                "decisions": decisions,
                "speakers": speakers,
                "entities": entity_names[:6],
                "duration_sec": row["duration_sec"],
            })

        return {
            "topic": topic or "All Historical Meetings",
            "total_milestones": len(events),
            "events": events,
        }


def stage_failures(limit: int = 20) -> dict[str, Any]:
    """History of failed stage_runs for the diagnostics drawer.

    Deliberately its own endpoint rather than a key on `overview()`: overview
    is polled every 8s by the frontend and already makes a network call
    (`index.health()`), so this is fetched only when the diagnostics drawer is
    actually opened.
    """
    db.init_db()
    with db.connect() as conn:
        return {"failures": db.recent_stage_failures(conn, limit=limit)}


def commitments_list(owner: str | None = None, overdue: bool = False) -> dict[str, Any]:
    """Commitments across the corpus, for the open-commitments panel."""
    db.init_db()
    with db.connect() as conn:
        rows = db.list_commitments(conn, owner=owner or None, overdue=overdue)
    return {"commitments": rows}


def decisions_list(topic: str | None = None) -> dict[str, Any]:
    """Decisions across the corpus, optionally narrowed to a topic."""
    db.init_db()
    with db.connect() as conn:
        rows = db.list_decisions(conn, topic=topic or None)
    return {"decisions": rows}


def overview() -> dict[str, Any]:
    """Return detailed metrics, queue status, activity, and local-index health."""
    db.init_db()
    with db.connect() as conn:
        statuses = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM meetings GROUP BY status"
            ).fetchall()
        }
        speaker_review = conn.execute(
            "SELECT COUNT(DISTINCT meeting_id) AS count FROM speakers WHERE name IS NULL"
        ).fetchone()["count"]
        no_diarization = conn.execute(
            """
            SELECT COUNT(*) AS count FROM meetings m
            WHERE m.status IN (?, ?, ?) AND NOT EXISTS (
                SELECT 1 FROM speakers s WHERE s.meeting_id = m.id
            )
            """,
            (db.SPEAKERS_RESOLVED, db.MINUTES_COMPILED, db.INDEXED),
        ).fetchone()["count"]

        # Audio durations
        dur_row = conn.execute(
            "SELECT COALESCE(SUM(duration_sec), 0) AS total_sec, AVG(duration_sec) AS avg_sec FROM meetings"
        ).fetchone()
        total_duration_sec = float(dur_row["total_sec"] or 0)
        avg_duration_sec = float(dur_row["avg_sec"] or 0)

        # Date calculations (local timezone)
        today = datetime.now(TZ).date()
        yesterday = today - timedelta(days=1)
        week_ago = today - timedelta(days=7)
        today_str = today.isoformat()
        yesterday_str = yesterday.isoformat()
        week_str = week_ago.isoformat()

        def _stats_for_range(start_iso: str, end_iso: str | None = None) -> dict[str, Any]:
            if end_iso:
                r = conn.execute(
                    """
                    SELECT COUNT(*) AS count, COALESCE(SUM(duration_sec), 0) AS sec
                    FROM meetings
                    WHERE status = ? AND updated_at >= ? AND updated_at < ?
                    """,
                    (db.INDEXED, start_iso, end_iso),
                ).fetchone()
            else:
                r = conn.execute(
                    """
                    SELECT COUNT(*) AS count, COALESCE(SUM(duration_sec), 0) AS sec
                    FROM meetings
                    WHERE status = ? AND updated_at >= ?
                    """,
                    (db.INDEXED, start_iso),
                ).fetchone()
            sec = float(r["sec"] or 0)
            return {
                "count": int(r["count"] or 0),
                "duration_sec": sec,
                "duration_min": round(sec / 60, 1),
                "duration_hours": round(sec / 3600, 2),
            }

        activity = {
            "today": _stats_for_range(today_str),
            "yesterday": _stats_for_range(yesterday_str, today_str),
            "last_7_days": _stats_for_range(week_str),
        }

        # Queue breakdown
        queue = {
            status: statuses.get(status, 0)
            for status in [*db.STATUS_ORDER, db.FAILED]
        }

        # Timings
        timings = db.stage_timings(conn)

        # Knowledge & graph stats
        entities_count = conn.execute("SELECT COUNT(*) AS count FROM entities").fetchone()["count"]
        relations_count = conn.execute("SELECT COUNT(*) AS count FROM relations").fetchone()["count"]
        top_entities = db.entity_mentions(conn, limit=8)
        people_count = conn.execute("SELECT COUNT(*) AS count FROM people").fetchone()["count"]
        unresolved_speakers = conn.execute("SELECT COUNT(*) AS count FROM speakers WHERE name IS NULL").fetchone()["count"]

        # Drive stats
        drive_rows = conn.execute(
            "SELECT state, COUNT(*) AS count, COALESCE(SUM(byte_size), 0) AS total_bytes FROM drive_sources GROUP BY state"
        ).fetchall()
        drive_by_state = {r["state"]: {"count": r["count"], "bytes": r["total_bytes"]} for r in drive_rows}
        drive_total_files = sum(r["count"] for r in drive_rows)
        drive_total_bytes = sum(r["total_bytes"] for r in drive_rows)

        # Maintenance
        stale_templates = len(db.stale_template(conn, TEMPLATE_VERSION))
        failed_list = [
            {"id": m.id, "short_id": m.short_id, "label": m.label, "error": (m.error or "").splitlines()[0] if m.error else "Unknown error"}
            for m in db.pending(conn, db.FAILED)
        ]

    try:
        health = index.health().get("status", "healthy")
    except index.IndexError_ as exc:
        health = f"unavailable: {exc}"

    total_meetings = sum(statuses.values())
    indexed_count = statuses.get(db.INDEXED, 0)
    pending_count = sum(
        count for status, count in statuses.items() if status not in {db.INDEXED, db.FAILED}
    )
    failed_count = statuses.get(db.FAILED, 0)

    return {
        # Backward-compatible keys
        "meetings": total_meetings,
        "indexed": indexed_count,
        "pending": pending_count,
        "failed": failed_count,
        "speaker_review": speaker_review + no_diarization,
        "lightrag": health,
        # Expanded metrics
        "durations": {
            "total_sec": total_duration_sec,
            "total_min": round(total_duration_sec / 60, 1),
            "total_hours": round(total_duration_sec / 3600, 1),
            "avg_min": round(avg_duration_sec / 60, 1),
        },
        "activity": activity,
        "queue": queue,
        "timings": timings,
        "knowledge": {
            "entities_count": entities_count,
            "relations_count": relations_count,
            "top_entities": top_entities,
            "people_count": people_count,
            "unresolved_speakers": unresolved_speakers,
        },
        "drive": {
            "total_files": drive_total_files,
            "total_bytes": drive_total_bytes,
            "total_mb": round(drive_total_bytes / (1024 * 1024), 1),
            "by_state": drive_by_state,
            "configured": DRIVE_CREDENTIALS_FILE.exists(),
            "authorized": DRIVE_TOKEN_FILE.exists(),
        },
        "engine": {
            "asr_backend": "Replicate Serverless GPU (Cloud)",
            "asr_model": "victor-upmeet/whisperx (Whisper large-v3 + Pyannote)",
            "asr_speed": "~1-2 min per meeting",
            "minutes_model": ANTIGRAVITY_MODEL or GEMINI_MODEL or "gemini-3.7-flash",
        },
        "maintenance": {
            "stale_templates": stale_templates,
            "template_version": TEMPLATE_VERSION,
            "failed_meetings": failed_list,
        },
    }


def meetings(search: str = "") -> list[dict[str, Any]]:
    """Return the meeting library, newest first, optionally narrowed by metadata."""
    term = f"%{search.strip()}%"
    db.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT m.*, ds.web_view_link, ds.source_name AS original_drive_name,
                   COUNT(s.label) AS speaker_count,
                   SUM(CASE WHEN s.name IS NULL THEN 1 ELSE 0 END) AS unresolved_count
            FROM meetings m
            LEFT JOIN drive_sources ds ON ds.meeting_id = m.id
            LEFT JOIN speakers s ON s.meeting_id = m.id
            WHERE (? = '%%' OR m.source_name LIKE ? OR COALESCE(m.title_hint, '') LIKE ?
                   OR COALESCE(m.meeting_date, '') LIKE ?)
            GROUP BY m.id
            ORDER BY m.meeting_date DESC, m.meeting_time DESC, m.created_at DESC
            """,
            (term, term, term, term),
        ).fetchall()
    return [_meeting_summary(dict(row)) for row in rows]


def meeting_detail(meeting_id: str) -> dict[str, Any] | None:
    """Return a single meeting, its readable minutes, and review signals."""
    db.init_db()
    with db.connect() as conn:
        meeting = db.get_meeting(conn, meeting_id)
        if not meeting:
            return None
        source = db.get_drive_source_for_meeting(conn, meeting.id)
        speakers = [
            dict(row)
            for row in conn.execute(
                "SELECT label, name, confidence FROM speakers WHERE meeting_id = ? ORDER BY label",
                (meeting.id,),
            ).fetchall()
        ]
        entities = db.get_entities(conn, meeting.id)
    record = _meeting_summary(
        {
            **meeting.__dict__,
            "original_drive_name": source.source_name if source else None,
            "web_view_link": source.web_view_link if source else None,
            "speaker_count": len(speakers),
            "unresolved_count": sum(1 for speaker in speakers if speaker["name"] is None),
        }
    )
    record["minutes"] = _read_minutes(meeting.minutes_path)
    record["speakers"] = speakers
    record["entities"] = entities
    return record


_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _valid_session_id(value: str | None) -> str | None:
    cleaned = (value or "").strip().lower()
    return cleaned if _SESSION_ID_RE.fullmatch(cleaned) else None


def new_chat_session() -> str:
    return uuid.uuid4().hex


def ask(
    question: str,
    mode: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Ask the existing RAG pipeline and persist bounded conversation history."""
    cleaned = question.strip()
    if not cleaned:
        raise ValueError("Ask a question about your meeting record.")
    if len(cleaned) > MAX_QUERY_CHARS:
        raise ValueError(f"Questions are limited to {MAX_QUERY_CHARS} characters.")
    selected_mode = mode if mode in VALID_QUERY_MODES else None
    active_session = _valid_session_id(session_id) or new_chat_session()
    from pipeline import answer

    db.init_db()
    with db.connect() as conn:
        history_rows = db.recent_chat_turns(conn, active_session, limit=CHAT_HISTORY_TURNS)
    history = [(str(row["question"]), str(row["answer"])) for row in history_rows]
    result = answer.ask(cleaned, mode=selected_mode, history=history)

    with db.connect() as conn:
        db.append_chat_turn(
            conn,
            active_session,
            question=cleaned,
            answer=result.text,
            mode=selected_mode,
            provider=result.provider,
            synthesized=result.synthesized,
            context_chars=result.context_chars,
            retrieval_sec=result.retrieval_sec,
            synthesis_sec=result.synthesis_sec,
        )

    return {
        "answer": result.text,
        "retrieval_sec": result.retrieval_sec,
        "synthesis_sec": result.synthesis_sec,
        "provider": result.provider,
        "context_chars": result.context_chars,
        "synthesized": result.synthesized,
        "session_id": active_session,
    }


def clear_chat_session(session_id: str | None) -> dict[str, Any]:
    active_session = _valid_session_id(session_id)
    cleared = 0
    if active_session:
        db.init_db()
        with db.connect() as conn:
            cleared = db.clear_chat_session(conn, active_session)
    return {"cleared": cleared, "session_id": new_chat_session()}


def extract_speaker_snippet(
    meeting_id: str,
    label: str | None = None,
    start_sec: float | None = None,
    duration_sec: float = 10.0,
) -> tuple[bytes, str] | None:
    """Extract a short MP3 snippet for a given speaker label or timestamp."""
    db.init_db()
    with db.connect() as conn:
        meeting = db.get_meeting(conn, meeting_id)
        if not meeting:
            row = conn.execute(
                "SELECT id FROM meetings WHERE id LIKE ? LIMIT 1", (f"{meeting_id}%",)
            ).fetchone()
            if row:
                meeting = db.get_meeting(conn, row["id"])
        if not meeting:
            return None

    from pipeline import asr

    actual_start = max(0.0, float(start_sec or 0.0))
    snippet_text = ""
    try:
        transcript = asr.load_transcript(meeting.id)
        if label:
            matching = [segment for segment in transcript.segments if segment.speaker == label]
            if matching:
                chosen = max(matching, key=lambda segment: segment.end - segment.start)
                actual_start = max(0.0, chosen.start)
                snippet_text = f'"{chosen.text}"'
    except Exception:
        pass

    from pipeline import capture

    audio_path: Path | None = None
    if meeting.audio_path and Path(meeting.audio_path).is_file():
        audio_path = Path(meeting.audio_path)
    else:
        for extension in (".mp3", ".m4a", ".wav", ".mp4"):
            candidate = AUDIO_DIR / f"{meeting.id}{extension}"
            if candidate.is_file():
                audio_path = candidate
                break
        if not audio_path:
            for candidate in INBOX_DIR.rglob(f"*{meeting.source_name}*"):
                if candidate.is_file():
                    audio_path = candidate
                    break
        if not audio_path:
            try:
                audio_path = capture.rehydrate_audio(meeting)
            except Exception:
                return None

    if not audio_path or not audio_path.is_file():
        return None

    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-ss",
        f"{actual_start:.2f}",
        "-t",
        f"{duration_sec:.2f}",
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        "-f",
        "mp3",
        "pipe:1",
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=True)
        return result.stdout, snippet_text
    except Exception:
        return None


def export_product_manager() -> dict[str, Any]:
    """Sync eligible professional minutes into the Product Manager repository."""
    from pipeline.compile_minutes import is_professional_minute
    from pipeline.config import ENABLE_PM_EXPORT, EXPORT_PM_MINUTES_DIR

    if not ENABLE_PM_EXPORT:
        return {"ok": False, "error": "Product Manager export is disabled."}

    EXPORT_PM_MINUTES_DIR.mkdir(parents=True, exist_ok=True)
    synced = 0
    quarantined = 0
    for path in sorted(MINUTES_DIR.glob("*.md")):
        document = path.read_text(encoding="utf-8")
        eligible, _reason = is_professional_minute(path, document)
        if not eligible:
            quarantined += 1
            continue
        (EXPORT_PM_MINUTES_DIR / path.name).write_text(document, encoding="utf-8")
        synced += 1
    return {"ok": True, "synced": synced, "quarantined_personal": quarantined}


def delete_meeting_audio(meeting_id: str) -> bool:
    """Delete a meeting's local audio while preserving its compiled archive."""
    db.init_db()
    with db.connect() as conn:
        meeting = db.get_meeting(conn, meeting_id)
        if not meeting:
            return False
        audio_path = Path(meeting.audio_path) if meeting.audio_path else None
        if audio_path and audio_path.is_file():
            audio_path.unlink()
        db.clear_audio_path(conn, meeting_id)
    return True


def delete_entire_meeting(meeting_id: str) -> bool:
    """Delete a meeting, its local artifacts, and its search document."""
    db.init_db()
    with db.connect() as conn:
        meeting = db.get_meeting(conn, meeting_id)
        if not meeting:
            return False
        for raw_path in (meeting.audio_path, meeting.transcript_path, meeting.minutes_path):
            if raw_path:
                path = Path(raw_path)
                if path.is_file():
                    path.unlink()
        if meeting.lightrag_doc_id:
            with contextlib.suppress(Exception):
                index.delete_document(meeting.lightrag_doc_id)
        return db.delete_meeting(conn, meeting_id)


def set_meeting_category(meeting_id: str, domain: str, category_type: str | None = None) -> bool:
    """Update the category frontmatter on a meeting's compiled minutes."""
    db.init_db()
    with db.connect() as conn:
        meeting = db.get_meeting(conn, meeting_id)
    if not meeting or not meeting.minutes_path:
        return False
    path = Path(meeting.minutes_path)
    if not path.is_file():
        return False
    document = path.read_text(encoding="utf-8")
    category = category_type or domain
    if re.search(r"^category:\s*.*$", document, flags=re.MULTILINE | re.IGNORECASE):
        document = re.sub(
            r"^category:\s*.*$",
            f"category: {category}",
            document,
            count=1,
            flags=re.MULTILINE | re.IGNORECASE,
        )
    elif document.startswith("---\n"):
        document = document.replace("---\n", f"---\ncategory: {category}\n", 1)
    else:
        document = f"---\ncategory: {category}\n---\n\n{document}"
    path.write_text(document, encoding="utf-8")
    return True


def run(host: str = DASHBOARD_HOST, port: int = DASHBOARD_PORT, open_browser: bool = False) -> None:
    """Serve the local control room until interrupted."""
    # Fail before binding rather than after: a dashboard that serves the whole
    # archive to a network is worse than one that refuses to start.
    dashboard_auth.check_startup(host)
    DashboardHandler.bind_host = host
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    address = f"http://{host}:{port}"
    print(f"Meeting Memory is available at {address}")
    print("It is bound to the local machine.")
    if open_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMeeting Memory stopped.")
    finally:
        server.server_close()



# Deliberately self-contained and styled inline: it must render before any
# authenticated asset can be fetched, so it cannot depend on style.css.
LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Meeting Memory - Sign in</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:#f3efe5; color:#1d2923;
         font:16px/1.5 Georgia,'Times New Roman',serif; }
  form { background:#fbf8f0; border:1px solid #ddd6c6; border-radius:10px;
         padding:28px 30px; width:min(92vw,380px);
         box-shadow:0 1px 3px rgba(0,0,0,.06); }
  h1 { margin:0 0 4px; font-size:1.5rem; }
  p  { margin:0 0 18px; color:#5c655e; font-size:.85rem; }
  label { display:block; font-size:.75rem; letter-spacing:.08em;
          text-transform:uppercase; color:#5c655e; margin-bottom:6px; }
  input { width:100%; box-sizing:border-box; padding:9px 11px; font-size:1rem;
          border:1px solid #ddd6c6; border-radius:5px; background:#fff;
          color:#1d2923; font-family:inherit; }
  input:focus-visible, button:focus-visible { outline:2px solid #9c3c22;
          outline-offset:2px; }
  button { margin-top:16px; width:100%; padding:10px; font-size:.95rem;
           font-family:inherit; cursor:pointer; border:0; border-radius:5px;
           background:#9c3c22; color:#fbf8f0; }
  .err { margin-top:12px; color:#7d2d1d; font-size:.85rem; min-height:1.2em; }
  @media (prefers-color-scheme: dark) {
    body { background:#1a1a18; color:#eee8dc; }
    form { background:#232320; border-color:#3a3a34; }
    input { background:#1a1a18; color:#eee8dc; border-color:#3a3a34; }
    p, label { color:#a9a49a; }
  }
</style></head><body>
<form id="f" autocomplete="off">
  <h1>Meeting Memory</h1>
  <p>This archive is private. Enter the dashboard token to continue.</p>
  <label for="t">Access token</label>
  <input id="t" name="token" type="password" required autofocus
         autocomplete="current-password">
  <button type="submit">Sign in</button>
  <div class="err" id="e" role="alert"></div>
</form>
<script>
document.getElementById('f').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const err = document.getElementById('e');
  err.textContent = '';
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: document.getElementById('t').value }),
    });
    if (res.ok) { location.replace('/'); return; }
    err.textContent = 'That token was not accepted.';
  } catch (e) {
    err.textContent = 'Could not reach the dashboard.';
  }
});
</script></body></html>
"""

class DashboardHandler(BaseHTTPRequestHandler):
    """Small dependency-free HTTP surface for the local dashboard."""

    server_version = "MeetingMemory/1.0"
    # Set by run() before the server binds. Class-level default keeps the
    # handler usable in tests that construct it without going through run().
    bind_host = "127.0.0.1"

    def do_GET(self) -> None:
        if not dashboard_auth.authorized(
            urlparse(self.path).path.rstrip("/") or "/", self.headers, self.bind_host
        ):
            self._unauthorized()
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/overview" or path == "/api/metrics":
            self._json(HTTPStatus.OK, overview())
            return
        if path == "/api/pipeline/status":
            self._json(HTTPStatus.OK, get_pipeline_status())
            return
        if path == "/api/diagnostics/stage-failures":
            limit_raw = parse_qs(parsed.query).get("limit", ["20"])[0]
            try:
                limit = max(1, int(limit_raw))
            except ValueError:
                limit = 20
            self._json(HTTPStatus.OK, stage_failures(limit=limit))
            return
        if path == "/api/people":
            self._json(HTTPStatus.OK, {"people": people()})
            return
        if path == "/api/meetings":
            search = parse_qs(parsed.query).get("search", [""])[0]
            self._json(HTTPStatus.OK, {"meetings": meetings(search)})
            return
        if path.startswith("/api/meetings/") and path.endswith("/transcript"):
            meeting_id = path[len("/api/meetings/") : -len("/transcript")]
            record = meeting_transcript(meeting_id)
            if record is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "No transcript for that meeting."})
            else:
                self._json(HTTPStatus.OK, record)
            return
        if path == "/api/voices/clusters":
            self._json(HTTPStatus.OK, {"clusters": get_voice_clusters()})
            return
        if path == "/api/timeline":
            topic = parse_qs(parsed.query).get("topic", [""])[0]
            self._json(HTTPStatus.OK, decision_timeline(topic))
            return
        if path == "/api/commitments":
            qs = parse_qs(parsed.query)
            owner = qs.get("owner", [""])[0]
            overdue = qs.get("overdue", ["0"])[0] in {"1", "true", "yes"}
            self._json(HTTPStatus.OK, commitments_list(owner=owner or None, overdue=overdue))
            return
        if path == "/api/decisions":
            topic = parse_qs(parsed.query).get("topic", [""])[0]
            self._json(HTTPStatus.OK, decisions_list(topic=topic or None))
            return
        if path == "/api/voices/snippet":
            qs = parse_qs(parsed.query)
            self._serve_voice_snippet(
                qs.get("meeting_id", [""])[0],
                qs.get("label", [""])[0],
                int(qs.get("index", ["0"])[0] or 0),
            )
            return
        if path == "/api/audio/snippet":
            qs = parse_qs(parsed.query)
            meeting_id = qs.get("meeting_id", [""])[0]
            label = qs.get("label", [None])[0]
            start_sec = float(qs.get("start", [0])[0]) if qs.get("start") else None
            duration = float(qs.get("duration", [10])[0])
            snippet = extract_speaker_snippet(
                meeting_id,
                label=label,
                start_sec=start_sec,
                duration_sec=duration,
            )
            if not snippet:
                self.send_error(HTTPStatus.NOT_FOUND, "Audio snippet could not be generated.")
                return
            audio_bytes, snippet_text = snippet
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.send_header("X-Snippet-Text", snippet_text)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(audio_bytes)
            return
        if path.startswith("/api/meetings/"):
            detail = meeting_detail(path.rsplit("/", 1)[-1])
            if detail:
                self._json(HTTPStatus.OK, detail)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Meeting not found."})
            return
        self._asset(parsed.path)

    def do_POST(self) -> None:
        if not dashboard_auth.authorized(
            urlparse(self.path).path.rstrip("/") or "/", self.headers, self.bind_host
        ):
            self._unauthorized()
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/login":
            try:
                self._handle_login(self._payload())
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/logout":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", dashboard_auth.clear_cookie_header())
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/timeline":
            try:
                payload = self._payload()
                self._json(HTTPStatus.OK, decision_timeline(payload.get("topic", "")))
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/voices/confirm":
            try:
                payload = self._payload()
                cluster_id = payload.get("cluster_id")
                canonical = str(payload.get("canonical", "")).strip()
                if not cluster_id or not canonical:
                    raise ValueError("Both 'cluster_id' and 'canonical' name are required.")
                count = confirm_voice_cluster(str(cluster_id), canonical)
                self._json(
                    HTTPStatus.OK,
                    {"confirmed": count, "cluster_id": cluster_id, "canonical": canonical},
                )
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/voices/confirm-confident":
            try:
                payload = self._payload()
                self._json(
                    HTTPStatus.OK,
                    confirm_confident_clusters(float(payload.get("threshold", 0.85))),
                )
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/voices/dismiss":
            try:
                payload = self._payload()
                cluster_id = payload.get("cluster_id")
                if not cluster_id:
                    raise ValueError("'cluster_id' is required.")
                count = dismiss_voice_cluster(str(cluster_id))
                self._json(HTTPStatus.OK, {"dismissed": count, "cluster_id": cluster_id})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/voices/split":
            try:
                payload = self._payload()
                cluster_id = payload.get("cluster_id")
                if not cluster_id:
                    raise ValueError("'cluster_id' is required.")
                result = split_voice_cluster(str(cluster_id))
                self._json(HTTPStatus.OK, {"split": result, "cluster_id": cluster_id})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/query":
            try:
                payload = self._payload()
                self._json(
                    HTTPStatus.OK,
                    ask(
                        str(payload.get("question", "")),
                        payload.get("mode"),
                        payload.get("session_id"),
                    ),
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Query failed: {exc}"})
            return
        if path == "/api/query/new":
            self._json(HTTPStatus.OK, {"session_id": new_chat_session()})
            return
        if path == "/api/query/clear":
            try:
                payload = self._payload()
                self._json(HTTPStatus.OK, clear_chat_session(payload.get("session_id")))
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/export/product-manager":
            try:
                self._json(HTTPStatus.OK, export_product_manager())
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return
        if path == "/api/pipeline/run":
            try:
                payload = self._payload()
                result = trigger_pipeline_run(
                    stage=payload.get("stage", "all"),
                    limit=payload.get("limit"),
                )
                self._json(HTTPStatus.ACCEPTED, result)
            except ValueError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/pipeline/retry":
            try:
                payload = self._payload()
                target_status = payload.get("status", db.DISCOVERED)
                count = retry_failed(target_status=target_status)
                self._json(HTTPStatus.OK, {"requeued": count, "target_status": target_status})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/pipeline/recompile":
            try:
                self._json(HTTPStatus.ACCEPTED, trigger_pipeline_run(stage="recompile"))
            except ValueError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path.startswith("/api/meetings/") and path.endswith("/retry"):
            meeting_id = path.split("/")[3]
            try:
                payload = self._payload()
                target_status = payload.get("status", db.DISCOVERED)
                if retry_meeting(meeting_id, target_status=target_status):
                    self._json(
                        HTTPStatus.OK,
                        {
                            "meeting_id": meeting_id,
                            "requeued": True,
                            "target_status": target_status,
                        },
                    )
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Meeting not found."})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path.startswith("/api/meetings/") and path.endswith("/speakers"):
            meeting_id = path.split("/")[3]
            try:
                payload = self._payload()
                label = payload.get("label")
                name = str(payload.get("name", ""))
                confidence = payload.get("confidence", "confirmed")
                if not label:
                    raise ValueError("Speaker 'label' is required.")
                set_meeting_speaker(meeting_id, str(label), name, str(confidence))
                self._json(
                    HTTPStatus.OK,
                    {
                        "meeting_id": meeting_id,
                        "label": label,
                        "name": name,
                        "confidence": confidence,
                    },
                )
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path.startswith("/api/meetings/") and path.endswith("/delete-audio"):
            meeting_id = path.split("/")[3]
            if delete_meeting_audio(meeting_id):
                self._json(HTTPStatus.OK, {"meeting_id": meeting_id, "deleted": "audio"})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Meeting not found."})
            return
        if path.startswith("/api/meetings/") and path.endswith("/category"):
            meeting_id = path.split("/")[3]
            try:
                payload = self._payload()
                if set_meeting_category(meeting_id, payload.get("domain", ""), payload.get("type")):
                    self._json(HTTPStatus.OK, {"meeting_id": meeting_id, "updated": True})
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Meeting not found."})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/people":
            try:
                payload = self._payload()
                canonical = str(payload.get("canonical", "")).strip()
                if not canonical:
                    raise ValueError("Canonical name is required.")
                add_person(canonical, role=payload.get("role"), aliases=payload.get("aliases"))
                self._json(HTTPStatus.OK, {"canonical": canonical})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if path == "/api/people/merge":
            try:
                payload = self._payload()
                from_name = str(payload.get("from_name", "")).strip()
                into = str(payload.get("into", "")).strip()
                if not from_name or not into:
                    raise ValueError("Both 'from_name' and 'into' are required.")
                rewritten = merge_people(from_name, into)
                self._json(
                    HTTPStatus.OK,
                    {"from_name": from_name, "into": into, "rewritten": rewritten},
                )
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})

    def do_DELETE(self) -> None:
        if not dashboard_auth.authorized(
            urlparse(self.path).path.rstrip("/") or "/", self.headers, self.bind_host
        ):
            self._unauthorized()
            return
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/api/meetings/"):
            meeting_id = path.rsplit("/", 1)[-1]
            if delete_entire_meeting(meeting_id):
                self._json(HTTPStatus.OK, {"meeting_id": meeting_id, "deleted": True})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Meeting not found."})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep routine browser requests out of the operator's terminal."""

    def _asset(self, path: str) -> None:
        name = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_QUERY_CHARS * 2:
            raise ValueError("Request is too large.")
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("Request body must be an object.")
        return payload

    def _serve_voice_snippet(self, meeting_id: str, label: str, index: int) -> None:
        """Serve a retained voice clip for the labelling card.

        The card previously played `/api/audio/snippet`, which cuts a fresh clip out
        of the meeting's live audio on every request. That works for the handful of
        meetings that still have audio and silently returns nothing for the rest -
        and going forward that is every meeting, because transcription deletes the
        source. These clips are the durable answer: ~30 KB each, written once at
        enrollment, and the whole reason SNIPPETS_DIR exists.
        """
        if not meeting_id or not label:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "meeting_id and label required"})
            return

        db.init_db()
        with db.connect() as conn:
            row = conn.execute(
                "SELECT snippet_paths FROM speaker_matches WHERE meeting_id = ? AND label = ?",
                (meeting_id, label),
            ).fetchone()
        try:
            relatives = json.loads(row["snippet_paths"]) if row and row["snippet_paths"] else []
        except ValueError:
            relatives = []
        if not isinstance(relatives, list) or index >= len(relatives) or index < 0:
            self._json(HTTPStatus.NOT_FOUND, {"error": "No retained clip for that speaker."})
            return

        # The stored value is relative, but it arrived from a database row rather
        # than from code, so treat it as untrusted: resolve it and require that it
        # still sits inside SNIPPETS_DIR.
        try:
            clip = (SNIPPETS_DIR / str(relatives[index])).resolve()
            if not clip.is_relative_to(SNIPPETS_DIR.resolve()) or not clip.is_file():
                raise OSError("outside the snippet store")
            payload = clip.read_bytes()
        except OSError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Clip missing from disk."})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/ogg")
        self.send_header("Content-Length", str(len(payload)))
        # Immutable once written, so let the browser keep it between card views.
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(payload)

    def _unauthorized(self) -> None:
        """401 for the API, the login page for a browser.

        Distinguished by Accept rather than by path: a fetch() from the page and a
        person typing the URL want different things, and redirecting an XHR to HTML
        just produces a confusing parse error in the console.
        """
        wants_html = "text/html" in (self.headers.get("Accept") or "")
        if wants_html:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            body = LOGIN_PAGE.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required."})

    def _handle_login(self, payload: dict[str, Any]) -> None:
        if not dashboard_auth.token_matches(str(payload.get("token", ""))):
            # No detail about why. A message distinguishing "wrong token" from
            # "no token configured" is a hint worth withholding.
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid token."})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", dashboard_auth.session_cookie_header())
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        try:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass


def classify_meeting_category(
    minutes: str | None,
    title: str | None,
    source_name: str | None,
) -> dict[str, str]:
    """Classify into Personal vs Professional and extract semantic type."""
    text = (minutes or "").lower()
    combined = f"{title or ''} {source_name or ''} {text[:2000]}".lower()

    sub_type = "General"
    domain = "Professional"
    domain_declared = False

    if minutes and minutes.startswith("---"):
        parts = minutes.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                if line.startswith("category:"):
                    raw_cat = line.split("category:", 1)[-1].strip().strip('"\'').lower()
                    if raw_cat in {"personal", "household"}:
                        domain = "Personal"
                        domain_declared = True
                    elif raw_cat in {"professional", "work"}:
                        domain = "Professional"
                        domain_declared = True
                elif line.startswith("type:"):
                    raw_type = line.split("type:", 1)[-1].strip().strip('"\'').lower()
                    if raw_type in {"standup", "daily"}:
                        sub_type = "Standup"
                    elif raw_type in {"one-on-one", "1:1", "1-on-1"}:
                        sub_type = "1:1 Meeting"
                    elif raw_type in {"review", "stakeholder"}:
                        sub_type = "Review"
                    elif raw_type in {"planning", "discovery"}:
                        sub_type = "Planning"
                    elif raw_type in {"personal", "household"}:
                        sub_type = "Personal"
                        domain = "Personal"

    # Keywords for Personal classification.
    #
    # These are matched on word boundaries and scored, because the previous
    # substring test misfiled every one of the seven meetings it flagged in a
    # security-software corpus: "lease" matched *re*lease, "tenant" matched
    # multi-tenant, "separation" matched separation of concerns, and "personal"
    # matched personal data. Bare "health" and "personal" are gone entirely -
    # in this corpus they are ordinary technical vocabulary - and what remains
    # needs either a hit in the title, an unambiguous phrase, or two independent
    # hits before it can outweigh the default.
    strong_personal = [
        r"rental property", r"personal matter", r"personal appointment",
        r"separation agreement", r"car repair", r"family member",
        r"doctor'?s? appointment", r"health insurance", r"blacklock",
    ]
    weak_personal = [
        r"tenant", r"lease", r"landlord", r"mortgage", r"household",
        r"family", r"doctor",
    ]
    title_text = (title or "").lower()

    def _hits(patterns: list[str], text: str) -> int:
        return sum(1 for p in patterns if re.search(rf"\b{p}\b", text))

    strong = _hits(strong_personal, combined)
    weak = _hits(weak_personal, combined)
    weak_in_title = _hits(weak_personal, title_text)
    # An explicit frontmatter category is a human or the compiler being
    # deliberate; keyword guessing must never override it.
    if not domain_declared and (strong or weak_in_title or weak >= 2):
        domain = "Personal"
        if sub_type == "General":
            sub_type = "Personal & Household"

    # Specific professional sub-types
    if domain == "Professional" and sub_type == "General":
        if "standup" in combined:
            sub_type = "Standup"
        elif any(k in combined for k in ["1:1", "one on one", "one-on-one", "career", "check-in"]):
            sub_type = "1:1 Meeting"
        elif any(k in combined for k in ["roadmap", "planning", "delivery", "sprint"]):
            sub_type = "Planning & Delivery"
        elif any(k in combined for k in ["architecture", "integration", "discovery", "crowdstrike", "fabric", "kafka"]):
            sub_type = "Architecture & Discovery"
        elif any(k in combined for k in ["measurement", "review", "audit", "governance", "uce", "top 15"]):
            sub_type = "Governance & Review"

    return {
        "domain": domain,
        "type": sub_type,
    }


def format_meeting_datetime(date_str: str | None, time_str: str | None) -> dict[str, str]:
    """Format meeting date & time with high visual clarity."""
    formatted_date = date_str or "Undated"
    formatted_time = ""
    weekday = ""

    if date_str:
        try:
            from datetime import date
            d = date.fromisoformat(date_str)
            weekday = d.strftime("%A")
            formatted_date = d.strftime("%b %d, %Y")
        except Exception:
            pass

    if time_str:
        try:
            parts = time_str.split(":")
            hh = int(parts[0])
            mm = parts[1] if len(parts) > 1 else "00"
            ampm = "AM" if hh < 12 else "PM"
            display_h = 12 if hh in (0, 12) else (hh % 12)
            formatted_time = f"{display_h}:{mm} {ampm}"
        except Exception:
            formatted_time = time_str

    return {
        "formatted_date": formatted_date,
        "formatted_time": formatted_time,
        "weekday": weekday,
    }



def _meeting_summary(row: dict[str, Any]) -> dict[str, Any]:
    minutes = _read_minutes(row.get("minutes_path"))
    speaker_count = int(row.get("speaker_count") or 0)
    unresolved_count = int(row.get("unresolved_count") or 0)
    name_for_title = row.get("original_drive_name") or row.get("source_name")
    title = clean_meeting_title(
        name_for_title,
        row.get("title_hint"),
        row.get("minutes_path"),
        minutes,
    )
    categories = classify_meeting_category(minutes, title, name_for_title)
    dt_info = format_meeting_datetime(row.get("meeting_date"), row.get("meeting_time"))

    audio_path = row.get("audio_path")
    has_audio = bool(audio_path and Path(audio_path).is_file())

    return {
        "id": row["id"],
        "short_id": row["id"][:8],
        "date": row.get("meeting_date"),
        "time": row.get("meeting_time"),
        "formatted_date": dt_info["formatted_date"],
        "formatted_time": dt_info["formatted_time"],
        "weekday": dt_info["weekday"],
        "title": title,
        "category": categories["domain"],
        "category_type": categories["type"],
        "has_audio": has_audio,
        "source_name": row.get("source_name"),
        "status": row.get("status"),
        "error": row.get("error"),
        "duration_sec": row.get("duration_sec"),
        "minutes_path": row.get("minutes_path"),
        "drive_url": row.get("web_view_link"),
        "speaker_count": speaker_count,
        "unresolved_count": unresolved_count,
        "review_state": _review_state(row.get("status"), speaker_count, unresolved_count),
        "excerpt": _excerpt(minutes),
    }


def _read_minutes(path_text: str | None) -> str:
    if not path_text:
        return ""
    try:
        path = Path(path_text).resolve()
        if not path.is_relative_to(MINUTES_DIR.resolve()):
            return ""
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def meeting_transcript(meeting_id: str) -> dict[str, Any] | None:
    """The full speaker-attributed transcript for one meeting.

    Every decision and action item in the minutes cites a [H:MM:SS] timestamp,
    and the alignment stage exists solely to make those citations checkable - but
    the transcripts were retained on disk and never served, so "did we actually
    agree to that?" had no answer short of opening the JSON by hand. Rendered
    through asr.render_markdown so consecutive turns by one speaker read as
    speech rather than as fragments, and with resolved names substituted for
    SPEAKER_xx wherever naming has happened.
    """
    from pipeline import asr

    db.init_db()
    with db.connect() as conn:
        meeting = db.get_meeting(conn, meeting_id)
        if not meeting or not meeting.transcript_path:
            return None
        names = {
            row["label"]: row["name"]
            for row in conn.execute(
                "SELECT label, name FROM speakers WHERE meeting_id = ? AND name IS NOT NULL",
                (meeting.id,),
            )
        }

    # Same containment rule as _read_minutes: a path out of the archive is not
    # ours to read, however it got into the manifest.
    try:
        path = Path(meeting.transcript_path).resolve()
        if not path.is_relative_to(TRANSCRIPTS_DIR.resolve()):
            return None
    except OSError:
        return None

    try:
        transcript = asr.load_transcript(meeting.id)
    except (OSError, ValueError, KeyError):
        return None

    return {
        "meeting_id": meeting.id,
        "model": transcript.model,
        "language": transcript.language,
        "duration_sec": transcript.duration_sec,
        "segment_count": len(transcript.segments),
        "speakers": transcript.speaker_labels,
        "unresolved": [label for label in transcript.speaker_labels if label not in names],
        # render_markdown prefixes a title and a model/language/duration block
        # meant for the standalone .md file. The reader shows those as its own
        # meta line, so serving them again renders the same three facts twice -
        # and the file's backticked `model` reads as literal backticks here.
        "markdown": _transcript_body(asr.render_markdown(transcript, names)),
    }


def _transcript_body(markdown: str) -> str:
    """The turns only, with render_markdown's file header removed."""
    _, separator, body = markdown.partition("\n---\n")
    return body.strip() if separator else markdown.strip()


def _review_state(status: str | None, speaker_count: int, unresolved_count: int) -> str:
    if status == db.FAILED:
        return "Needs attention"
    if speaker_count == 0 and status in {db.SPEAKERS_RESOLVED, db.MINUTES_COMPILED, db.INDEXED}:
        return "No diarization"
    return "Speaker review" if unresolved_count else "Ready"


def _excerpt(minutes: str, limit: int = 220) -> str:
    if not minutes:
        return "No executive summary recorded yet."
    body = minutes.split("---", 2)[-1] if minutes.startswith("---") else minutes
    cleaned_lines = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cleaned_lines.append(line)
    text = " ".join(cleaned_lines)
    text = re.sub(r"[*_`]", "", text)
    text = " ".join(text.split())
    if not text:
        return "Executive brief indexed in memory."
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")
