"""The dashboard must stay responsive while a minutes job is slow.

Minute authoring can legitimately spend minutes inside a subscription CLI.  The
HTTP request that starts it must return immediately, and later status requests
must be served by another request thread rather than queueing behind that work.
"""

from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from pipeline import dashboard


def test_pipeline_status_is_responsive_while_background_worker_is_blocked(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow_minutes_worker(_stage: str, _limit: int | None) -> None:
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(dashboard, "_run_pipeline_worker", slow_minutes_worker)
    monkeypatch.setattr(dashboard, "_queue_detail", lambda: {
        "queue": {"speakers_resolved": 1},
        "queue_total": 1,
        "in_flight": {"stage": "minutes", "label": "fixture meeting"},
    })
    monkeypatch.setattr(
        dashboard.DashboardHandler,
        "_request_authorized",
        lambda _self: True,
    )

    with dashboard._pipeline_lock:
        dashboard._pipeline_state.update(
            running=False,
            stage="idle",
            started_at=None,
            finished_at=None,
            success=None,
            error=None,
            blocked_by=None,
        )

    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)

    try:
        connection.request(
            "POST",
            "/api/pipeline/run",
            body=json.dumps({"stage": "minutes"}),
            headers={"Content-Type": "application/json"},
        )
        start_response = connection.getresponse()
        start_response.read()
        assert start_response.status == 202
        assert entered.wait(timeout=1), "the fake minutes worker never started"

        started = time.perf_counter()
        connection.request("GET", "/api/pipeline/status")
        status_response = connection.getresponse()
        payload = json.loads(status_response.read())
        elapsed = time.perf_counter() - started

        assert status_response.status == 200
        assert payload["running"] is True
        assert payload["stage"] == "minutes"
        assert elapsed < 1.0, f"status request blocked for {elapsed:.2f}s"
    finally:
        release.set()
        connection.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        with dashboard._pipeline_lock:
            dashboard._pipeline_state.update(
                running=False,
                stage="idle",
                started_at=None,
                finished_at=None,
                success=None,
                error=None,
                blocked_by=None,
            )


def test_status_poll_does_not_repeat_schema_initialization(manifest, monkeypatch):
    """A two-second status poll must stay read-only after server startup.

    Schema setup takes SQLite write locks.  Repeating it in every poll can wait
    behind the brief write transaction that follows a minutes model response,
    even though the status payload itself only needs SELECTs.
    """
    called = threading.Event()

    def slow_schema_initialization() -> None:
        called.set()
        time.sleep(1.1)

    monkeypatch.setattr(dashboard.db, "init_db", slow_schema_initialization)

    started = time.perf_counter()
    detail = dashboard._queue_detail()
    elapsed = time.perf_counter() - started

    assert called.is_set() is False, "polling repeated schema initialization"
    assert detail["queue_total"] == 0
    assert elapsed < 0.5, f"read-only status poll blocked for {elapsed:.2f}s"
