from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline import context_provider, db, graph_sync, index


def _meeting_with_context(manifest, tmp_path: Path, meeting_id: str, date: str) -> Path:
    minutes = tmp_path / f"{meeting_id}.md"
    minutes.write_text(
        "# Architecture review\n\nAtlas depends on the Beacon launch.\n\n## Decisions\nShip later.",
        encoding="utf-8",
    )
    db.insert_meeting(
        manifest,
        meeting_id=meeting_id,
        source_path=str(tmp_path / f"{meeting_id}.m4a"),
        source_name=f"{meeting_id}.m4a",
        audio_path=str(tmp_path / f"{meeting_id}.m4a"),
        meeting_date=date,
        meeting_time="09:00",
        title_hint="Architecture review",
        duration_sec=60.0,
    )
    db.advance(
        manifest,
        meeting_id,
        db.MINUTES_COMPILED,
        transcript_path=str(tmp_path / "private-transcript.json"),
        minutes_path=str(minutes),
    )
    manifest.execute(
        "INSERT INTO entities (meeting_id, name, kind, description) VALUES (?, ?, ?, ?)",
        (meeting_id, "Atlas", "feature", "Migration program"),
    )
    manifest.execute(
        "INSERT INTO entities (meeting_id, name, kind, description) VALUES (?, ?, ?, ?)",
        (meeting_id, "Beacon", "release", "Launch milestone"),
    )
    manifest.execute(
        "INSERT INTO relations (meeting_id, subject, predicate, object) VALUES (?, ?, ?, ?)",
        (meeting_id, "Atlas", "depends on", "Beacon"),
    )
    manifest.commit()
    return minutes


def test_context_query_rejects_unbounded_or_unknown_input():
    with pytest.raises(ValidationError):
        context_provider.ContextQuery(query=" ")
    with pytest.raises(ValidationError):
        context_provider.ContextQuery(query="Atlas", limit=51)
    with pytest.raises(ValidationError):
        context_provider.ContextQuery(query="Atlas", surprise=True)
    with pytest.raises(ValidationError):
        context_provider.ContextQuery(query="Atlas", as_of="August 27")


def test_search_returns_bounded_background_with_minutes_provenance(manifest, tmp_path, monkeypatch):
    minutes = _meeting_with_context(manifest, tmp_path, "meeting-1", "2026-08-25")
    monkeypatch.setattr(
        graph_sync,
        "retrieve_graph",
        lambda *_args, **_kwargs: {
            "available": True,
            "matched_labels": ["Atlas"],
            "nodes": [
                {
                    "name": "Atlas",
                    "description": "Migration program",
                    "source_id": "meeting-1",
                }
            ],
            "edges": [
                {
                    "source": "Atlas",
                    "target": "Beacon",
                    "relationship": "depends on",
                    "source_id": "meeting-1",
                }
            ],
        },
    )
    monkeypatch.setattr(
        index,
        "document_health",
        lambda: index.DocumentHealth(1, 1, 1, 0, 0, {"processed": 1}, False, False, ""),
    )

    result = context_provider.ManifestContextProvider(db.DB_PATH).search(
        context_provider.ContextQuery(query="What does Atlas depend on?", limit=5, max_chars=256)
    )

    assert result.index.status == "partial"
    assert result.index.fresh_through == "2026-08-25"
    assert result.items
    assert len(result.items) <= 5
    assert sum(len(item.text) for item in result.items) <= 256
    assert all(item.classification == "background" for item in result.items)
    assert all(item.meeting_id == "meeting-1" for item in result.items)
    assert all(item.source_path == str(minutes) for item in result.items)
    assert all("transcript" not in item.source_path for item in result.items)
    assert {item.kind for item in result.items} >= {"entity", "relation"}


def test_search_falls_back_to_manifest_and_marks_partial(manifest, tmp_path, monkeypatch):
    _meeting_with_context(manifest, tmp_path, "meeting-1", "2026-08-25")
    monkeypatch.setattr(
        graph_sync,
        "retrieve_graph",
        lambda *_args, **_kwargs: {
            "available": False,
            "matched_labels": [],
            "nodes": [],
            "edges": [],
        },
    )

    result = context_provider.ManifestContextProvider(db.DB_PATH).search(
        context_provider.ContextQuery(query="Atlas", as_of="2026-08-25")
    )

    assert result.items
    assert result.index.backend == "manifest-fallback"
    assert result.index.status == "partial"
    assert result.index.partial is True
    assert "unavailable" in result.index.detail


def test_search_honors_as_of(manifest, tmp_path, monkeypatch):
    _meeting_with_context(manifest, tmp_path, "older", "2026-08-20")
    _meeting_with_context(manifest, tmp_path, "newer", "2026-08-26")
    monkeypatch.setattr(
        graph_sync,
        "retrieve_graph",
        lambda *_args, **_kwargs: {
            "available": False,
            "matched_labels": [],
            "nodes": [],
            "edges": [],
        },
    )

    result = context_provider.ManifestContextProvider(db.DB_PATH).search(
        context_provider.ContextQuery(query="Atlas", as_of="2026-08-21")
    )

    assert result.items
    assert {item.meeting_id for item in result.items} == {"older"}


def test_wire_models_publish_json_schema():
    schema = context_provider.ContextResult.model_json_schema()
    assert schema["properties"]["items"]
    assert schema["properties"]["index"]


def test_health_refreshes_document_readiness(manifest, tmp_path, monkeypatch):
    _meeting_with_context(manifest, tmp_path, "meeting-1", "2026-08-25")
    monkeypatch.setattr(graph_sync, "graph_labels", lambda *args, **kwargs: ["Atlas"])
    monkeypatch.setattr(
        index,
        "document_health",
        lambda: index.DocumentHealth(1, 1, 1, 0, 0, {"processed": 1}, False, False, ""),
    )

    state = context_provider.ManifestContextProvider(db.DB_PATH).health()

    assert state.status == "ready"
    assert state.fresh_through == "2026-08-25"


def test_dashboard_context_route_validates_and_serializes(monkeypatch):
    from http import HTTPStatus

    from pipeline import dashboard

    expected = context_provider.ContextResult(
        items=[],
        index=context_provider.ContextIndexState(
            backend="manifest-fallback",
            status="partial",
            fresh_through=None,
            partial=True,
        ),
    )
    provider = context_provider.InMemoryContextProvider(expected)
    monkeypatch.setattr(dashboard, "_context_provider", provider)
    monkeypatch.setattr(dashboard.dashboard_auth, "authorized", lambda *_: True)

    class FakeHandler(dashboard.DashboardHandler):
        def __init__(self, path, payload, host="127.0.0.1"):
            self.path = path
            self.headers = {}
            self.bind_host = host
            self.payload = payload
            self.response = None

        def _payload(self):
            return self.payload

        def _json(self, status, payload):
            self.response = (status, payload)

    valid = FakeHandler("/api/context/search", {"query": "Atlas"})
    valid.do_POST()
    assert valid.response == (HTTPStatus.OK, expected.model_dump(mode="json"))
    assert provider.requests[0].query == "Atlas"

    invalid = FakeHandler("/api/context/search", {"query": "", "limit": 500})
    invalid.do_POST()
    assert invalid.response[0] == HTTPStatus.UNPROCESSABLE_ENTITY

    exposed = FakeHandler("/api/context/search", {"query": "Atlas"}, host="0.0.0.0")
    exposed.do_POST()
    assert exposed.response == (
        HTTPStatus.FORBIDDEN,
        {"error": "Context API is loopback-only."},
    )
