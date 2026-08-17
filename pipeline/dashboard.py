"""Local, read-only and interactive meeting-memory control room."""

from __future__ import annotations

import argparse
import contextlib
import json
import mimetypes
import threading
import webbrowser
from collections import deque
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pipeline import db, index
from pipeline.config import (
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    MINUTES_DIR,
    OWNER_NAME,
    TEMPLATE_VERSION,
    TZ,
    now_iso,
)

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
        def write(self, s: str) -> int:
            for line in s.splitlines():
                if line.strip():
                    _log_buffer.append(f"[{now_iso()}] {line.rstrip()}")
            return len(s)

        def flush(self) -> None:
            pass

    log_stream = _LogCapture()
    success = False
    error_msg: str | None = None
    try:
        with contextlib.redirect_stdout(log_stream), contextlib.redirect_stderr(log_stream):
            cli.ensure_dirs()
            db.init_db()
            rc = 0
            if stage == "all":
                rc = cli.cmd_run(argparse.Namespace(limit=limit, no_llm=False, owner=OWNER_NAME))
            elif stage == "ingest":
                rc = cli.cmd_ingest(argparse.Namespace(then_run=False))
            elif stage == "capture":
                rc = cli.cmd_capture(argparse.Namespace(dry_run=False, complete_backfill=False))
            elif stage == "transcribe":
                rc = cli.cmd_transcribe(argparse.Namespace(limit=limit, keep_going=False, traceback=False))
            elif stage == "speakers":
                rc = cli.cmd_speakers(argparse.Namespace(limit=limit, owner=OWNER_NAME, no_llm=False, traceback=False))
            elif stage == "minutes":
                rc = cli.cmd_minutes(argparse.Namespace(limit=limit, recompile=False, traceback=False))
            elif stage == "index":
                rc = cli.cmd_index(argparse.Namespace(limit=limit))
            elif stage == "recompile":
                rc = cli.cmd_minutes(argparse.Namespace(limit=limit, recompile=True, traceback=False))
            else:
                raise ValueError(f"Unknown pipeline stage: {stage}")
            success = (rc == 0)
            _log_buffer.append(f"[{now_iso()}] Pipeline stage '{stage}' finished with exit code {rc}")
    except Exception as exc:
        success = False
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
        return db.list_people(conn)


def add_person(canonical: str, role: str | None = None, aliases: list[str] | None = None) -> None:
    """Register or update a canonical person with optional aliases."""
    db.init_db()
    with db.connect() as conn:
        db.add_person(conn, canonical=canonical, role=role, aliases=aliases)


def merge_people(from_name: str, into: str) -> int:
    """Merge one person/alias into another and rewrite historical records."""
    db.init_db()
    with db.connect() as conn:
        return db.merge_person(conn, from_name=from_name, into=into)


def set_meeting_speaker(
    meeting_id: str, label: str, name: str, confidence: str = "confirmed"
) -> None:
    """Set or override a speaker mapping for a specific meeting."""
    db.init_db()
    cleaned = name.strip()
    with db.connect() as conn:
        db.set_speaker(conn, meeting_id=meeting_id, label=label, name=cleaned or None, confidence=confidence)
        if cleaned:
            db.add_person(conn, canonical=cleaned)


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
            SELECT m.*, ds.web_view_link,
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
            "web_view_link": source.web_view_link if source else None,
            "speaker_count": len(speakers),
            "unresolved_count": sum(1 for speaker in speakers if speaker["name"] is None),
        }
    )
    record["minutes"] = _read_minutes(meeting.minutes_path)
    record["speakers"] = speakers
    record["entities"] = entities
    return record


def ask(question: str, mode: str | None = None) -> dict[str, Any]:
    """Ask the existing RAG pipeline and return the answer in JSON-safe form."""
    cleaned = question.strip()
    if not cleaned:
        raise ValueError("Ask a question about your meeting record.")
    if len(cleaned) > MAX_QUERY_CHARS:
        raise ValueError(f"Questions are limited to {MAX_QUERY_CHARS} characters.")
    selected_mode = mode if mode in VALID_QUERY_MODES else None
    from pipeline import answer

    result = answer.ask(cleaned, mode=selected_mode)
    return {
        "answer": result.text,
        "retrieval_sec": result.retrieval_sec,
        "synthesis_sec": result.synthesis_sec,
        "provider": result.provider,
        "context_chars": result.context_chars,
        "synthesized": result.synthesized,
    }


def run(host: str = DASHBOARD_HOST, port: int = DASHBOARD_PORT, open_browser: bool = False) -> None:
    """Serve the local control room until interrupted."""
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


class DashboardHandler(BaseHTTPRequestHandler):
    """Small dependency-free HTTP surface for the local dashboard."""

    server_version = "MeetingMemory/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/overview" or path == "/api/metrics":
            self._json(HTTPStatus.OK, overview())
            return
        if path == "/api/pipeline/status":
            self._json(HTTPStatus.OK, get_pipeline_status())
            return
        if path == "/api/people":
            self._json(HTTPStatus.OK, {"people": people()})
            return
        if path == "/api/meetings":
            search = parse_qs(parsed.query).get("search", [""])[0]
            self._json(HTTPStatus.OK, {"meetings": meetings(search)})
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
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/query":
            try:
                payload = self._payload()
                self._json(HTTPStatus.OK, ask(str(payload.get("question", "")), payload.get("mode")))
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Query failed: {exc}"})
            return

        if path == "/api/pipeline/run":
            try:
                payload = self._payload()
                stage = payload.get("stage", "all")
                limit = payload.get("limit")
                res = trigger_pipeline_run(stage=stage, limit=limit)
                self._json(HTTPStatus.ACCEPTED, res)
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
                res = trigger_pipeline_run(stage="recompile")
                self._json(HTTPStatus.ACCEPTED, res)
            except ValueError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        if path.startswith("/api/meetings/") and path.endswith("/retry"):
            parts = path.split("/")
            meeting_id = parts[3]
            try:
                payload = self._payload()
                target_status = payload.get("status", db.DISCOVERED)
                ok = retry_meeting(meeting_id, target_status=target_status)
                if ok:
                    self._json(HTTPStatus.OK, {"meeting_id": meeting_id, "requeued": True, "target_status": target_status})
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Meeting not found."})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path.startswith("/api/meetings/") and path.endswith("/speakers"):
            parts = path.split("/")
            meeting_id = parts[3]
            try:
                payload = self._payload()
                label = payload.get("label")
                name = payload.get("name", "")
                confidence = payload.get("confidence", "confirmed")
                if not label:
                    raise ValueError("Speaker 'label' is required.")
                set_meeting_speaker(meeting_id, label=label, name=name, confidence=confidence)
                self._json(HTTPStatus.OK, {"meeting_id": meeting_id, "label": label, "name": name, "confidence": confidence})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/people":
            try:
                payload = self._payload()
                canonical = payload.get("canonical", "").strip()
                if not canonical:
                    raise ValueError("Canonical name is required.")
                role = payload.get("role")
                aliases = payload.get("aliases")
                add_person(canonical, role=role, aliases=aliases)
                self._json(HTTPStatus.OK, {"canonical": canonical, "role": role, "aliases": aliases})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path == "/api/people/merge":
            try:
                payload = self._payload()
                from_name = payload.get("from_name", "").strip()
                into = payload.get("into", "").strip()
                if not from_name or not into:
                    raise ValueError("Both 'from_name' and 'into' are required.")
                rewritten = merge_people(from_name, into)
                self._json(HTTPStatus.OK, {"from_name": from_name, "into": into, "rewritten": rewritten})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
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

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _meeting_summary(row: dict[str, Any]) -> dict[str, Any]:
    minutes = _read_minutes(row.get("minutes_path"))
    speaker_count = int(row.get("speaker_count") or 0)
    unresolved_count = int(row.get("unresolved_count") or 0)
    return {
        "id": row["id"],
        "short_id": row["id"][:8],
        "date": row.get("meeting_date"),
        "time": row.get("meeting_time"),
        "title": row.get("title_hint") or row.get("source_name") or "Untitled meeting",
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


def _review_state(status: str | None, speaker_count: int, unresolved_count: int) -> str:
    if status == db.FAILED:
        return "Needs attention"
    if speaker_count == 0 and status in {db.SPEAKERS_RESOLVED, db.MINUTES_COMPILED, db.INDEXED}:
        return "No diarization"
    return "Speaker review" if unresolved_count else "Ready"


def _excerpt(minutes: str, limit: int = 260) -> str:
    body = minutes.split("---", 2)[-1] if minutes.startswith("---") else minutes
    condensed = " ".join(body.split())
    return condensed[:limit].rstrip() + ("…" if len(condensed) > limit else "")
