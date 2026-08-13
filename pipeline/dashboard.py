"""Local, read-only meeting-memory control room."""

from __future__ import annotations

import json
import mimetypes
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pipeline import db, index
from pipeline.config import DASHBOARD_HOST, DASHBOARD_PORT, MINUTES_DIR

STATIC_DIR = Path(__file__).with_name("static")
MAX_QUERY_CHARS = 4_000
VALID_QUERY_MODES = {"hybrid", "global", "local", "naive", "mix"}


def overview() -> dict[str, Any]:
    """Return counts and local-index health for the dashboard header."""
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
    try:
        health = index.health().get("status", "healthy")
    except index.IndexError_ as exc:
        health = f"unavailable: {exc}"
    return {
        "meetings": sum(statuses.values()),
        "indexed": statuses.get(db.INDEXED, 0),
        "pending": sum(
            count for status, count in statuses.items() if status not in {db.INDEXED, db.FAILED}
        ),
        "failed": statuses.get(db.FAILED, 0),
        "speaker_review": speaker_review + no_diarization,
        "lightrag": health,
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
    print("It is read-only and bound to the local machine.")
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
        if parsed.path == "/api/overview":
            self._json(HTTPStatus.OK, overview())
            return
        if parsed.path == "/api/meetings":
            search = parse_qs(parsed.query).get("search", [""])[0]
            self._json(HTTPStatus.OK, {"meetings": meetings(search)})
            return
        if parsed.path.startswith("/api/meetings/"):
            detail = meeting_detail(parsed.path.rsplit("/", 1)[-1])
            if detail:
                self._json(HTTPStatus.OK, detail)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Meeting not found."})
            return
        self._asset(parsed.path)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/query":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        try:
            payload = self._payload()
            self._json(HTTPStatus.OK, ask(str(payload.get("question", "")), payload.get("mode")))
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Query failed: {exc}"})

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
        "source_name": row.get("source_name"), "status": row.get("status"),
        "duration_sec": row.get("duration_sec"), "minutes_path": row.get("minutes_path"),
        "drive_url": row.get("web_view_link"), "speaker_count": speaker_count,
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
