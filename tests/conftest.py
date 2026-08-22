"""Test fixtures.

Every test runs against a throwaway working tree, configured via the MMC_*
environment variables BEFORE `pipeline.config` is imported - the module reads
them at import time.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_WORK = Path(tempfile.mkdtemp(prefix="mmc-tests-"))
os.environ.update(
    MMC_INBOX=str(_WORK / "inbox"),
    MMC_AUDIO=str(_WORK / "audio"),
    MMC_TRANSCRIPTS=str(_WORK / "transcripts"),
    MMC_MINUTES=str(_WORK / "minutes"),
    MMC_DB_DIR=str(_WORK / "db"),
    MMC_TIMEZONE="America/Toronto",
    # Blanked explicitly. config.py loads .env with override=False, so anything
    # set here wins - but anything NOT set here would be inherited from a
    # developer's real .env, making the suite pass locally and fail in CI (or
    # worse, reach a real service). These are the credential-bearing values.
    HF_TOKEN="",
    MMC_LIGHTRAG_API_KEY="test-key",
    MMC_DRIVE_FUTURE_FOLDER_ID="",
    MMC_DRIVE_BACKFILL_FOLDER_ID="",
)

# httpx is only needed by the index module's HTTP calls, which these tests never
# make. Stubbing it keeps the suite runnable without the optional dependency.
if "httpx" not in sys.modules:
    try:
        import httpx  # noqa: F401
    except ImportError:
        stub = types.ModuleType("httpx")

        class _HTTPError(Exception):
            pass

        class _HTTPStatusError(_HTTPError):
            pass

        class _Client:
            """Refuses to connect, like an absent LightRAG server.

            Constructing this raises HTTPError, so index calls take exactly the
            degradation path they take in production when the server is down:
            `query_context` returns "" and the compile continues without topical
            prior context.
            """

            def __init__(self, *args, **kwargs):
                raise _HTTPError("httpx stub: no server in tests")

        stub.HTTPError = _HTTPError
        stub.HTTPStatusError = _HTTPStatusError
        stub.Client = _Client
        sys.modules["httpx"] = stub

import pytest

from pipeline import db
from pipeline.config import ensure_dirs


@pytest.fixture(autouse=True)
def block_live_lightrag_in_unit_tests(request, monkeypatch):
    """Keep unit tests offline even when httpx is installed locally.

    Functional tests provide a LightRAG-shaped HTTP server explicitly. Unit tests
    must not accidentally query the developer's real archive while compiling
    minutes; that makes the suite slow, non-deterministic, and privacy-unsafe.
    """
    if "e2e" not in request.keywords:
        from pipeline import graph_sync, index

        monkeypatch.setattr(index, "query_context", lambda *args, **kwargs: "")
        # graph_sync.retrieve_context() otherwise makes a real httpx call to
        # LIGHTRAG_URL on every answer.ask(); default it to "no match" so unit
        # tests exercise the normal (local-fallback) path deterministically
        # unless a test opts into real graph content explicitly.
        monkeypatch.setattr(graph_sync, "retrieve_context", lambda *args, **kwargs: "")


@pytest.fixture()
def manifest(tmp_path, monkeypatch):
    """A fresh manifest database per test."""
    db_path = tmp_path / "manifest.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        yield conn


@pytest.fixture()
def inbox(tmp_path, monkeypatch):
    """An isolated inbox/audio pair, with the manifest pointed at tmp_path."""
    from pipeline import config, ingest

    inbox_dir = tmp_path / "inbox"
    audio_dir = tmp_path / "audio"
    inbox_dir.mkdir()
    audio_dir.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(config, "AUDIO_DIR", audio_dir)
    monkeypatch.setattr(ingest, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(ingest, "AUDIO_DIR", audio_dir)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "manifest.db")
    db.init_db(tmp_path / "manifest.db")
    return inbox_dir


def make_meeting(conn, meeting_id: str, date: str, time: str = "09:00", **kwargs):
    """Insert a minimal meeting row for tests."""
    db.insert_meeting(
        conn,
        meeting_id=meeting_id,
        source_path=f"/inbox/{meeting_id}.m4a",
        source_name=f"{meeting_id}.m4a",
        audio_path=f"/audio/{meeting_id}.m4a",
        meeting_date=date,
        meeting_time=time,
        title_hint=kwargs.pop("title_hint", None),
        duration_sec=kwargs.pop("duration_sec", 3600.0),
    )
    if kwargs:
        db.advance(conn, meeting_id, kwargs.pop("status", db.DISCOVERED), **kwargs)
    return db.get_meeting(conn, meeting_id)


ensure_dirs()
