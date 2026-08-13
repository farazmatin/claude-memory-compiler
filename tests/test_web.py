"""The HTTP layer and the citation path behind it.

The retrieval and synthesis calls are stubbed throughout: these tests are about
whether a citation names a meeting that was actually retrieved and whether that
meeting can be opened, not about LightRAG.
"""

from __future__ import annotations

import pytest

from pipeline import answer, db

from .conftest import make_meeting

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from web import app as web_app

# ── source extraction ────────────────────────────────────────────────

def test_extract_sources_finds_minutes_filenames():
    context = (
        "id,content,file_path\n"
        "1,We chose Postgres,2026-08-10-kafka-acl-a1b2c3d4.md\n"
        "2,Owner is Faraz,2026-08-11-drive-verification-e5f6a7b8.md\n"
    )
    assert answer.extract_sources(context) == [
        "2026-08-10-kafka-acl-a1b2c3d4.md",
        "2026-08-11-drive-verification-e5f6a7b8.md",
    ]


def test_extract_sources_dedupes_preserving_order():
    context = "a.md then b.md then a.md again"
    assert answer.extract_sources(context) == ["a.md", "b.md"]


def test_extract_sources_empty_context():
    assert answer.extract_sources("") == []


# ── manifest lookup ──────────────────────────────────────────────────

def test_meetings_by_minutes_names_resolves_filenames(manifest, tmp_path):
    make_meeting(manifest, "m1", "2026-08-10", minutes_path=str(tmp_path / "2026-08-10-kafka.md"))
    make_meeting(manifest, "m2", "2026-08-11", minutes_path=str(tmp_path / "2026-08-11-drive.md"))

    found = db.meetings_by_minutes_names(manifest, ["2026-08-11-drive.md", "nope.md"])

    assert set(found) == {"2026-08-11-drive.md"}
    assert found["2026-08-11-drive.md"].id == "m2"


def test_meetings_by_minutes_names_treats_underscore_literally(manifest, tmp_path):
    """`_` is a LIKE wildcard; an unescaped pattern would match the wrong meeting."""
    make_meeting(manifest, "m1", "2026-08-10", minutes_path=str(tmp_path / "2026-08-10-aXb.md"))

    assert db.meetings_by_minutes_names(manifest, ["2026-08-10-a_b.md"]) == {}


def test_list_meetings_is_newest_first(manifest):
    make_meeting(manifest, "old", "2026-08-01")
    make_meeting(manifest, "new", "2026-08-12")

    assert [m.id for m in db.list_meetings(manifest)] == ["new", "old"]


# ── HTTP ─────────────────────────────────────────────────────────────

@pytest.fixture()
def client(manifest, tmp_path, monkeypatch):
    """A test client against the temp manifest and minutes directory.

    The `manifest` fixture already points `db.DB_PATH` at tmp_path; the app opens
    its own connection to that same file, so tests must commit their inserts
    (`manifest.commit()`) before the request can see them.
    """
    monkeypatch.setattr(web_app, "MINUTES_DIR", tmp_path)
    return TestClient(web_app.create_app())


def _stub_answer(monkeypatch, **overrides):
    result = answer.Answer(
        text=overrides.pop("text", "We chose Postgres because the team already runs it."),
        retrieval_sec=1.234,
        synthesis_sec=2.345,
        provider="gemini",
        context_chars=512,
        synthesized=True,
        sources=overrides.pop("sources", []),
    )
    monkeypatch.setattr(answer, "ask", lambda *args, **kwargs: result)
    return result


def test_ask_returns_answer_and_timing(client, monkeypatch):
    _stub_answer(monkeypatch)

    response = client.post("/api/ask", json={"question": "why postgres?"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"].startswith("We chose Postgres")
    assert body["provider"] == "gemini"
    assert body["retrieval_sec"] == 1.23
    assert body["synthesized"] is True


def test_ask_citations_carry_meeting_metadata(client, manifest, tmp_path, monkeypatch):
    minutes = tmp_path / "2026-08-10-kafka-acl.md"
    minutes.write_text("---\ntitle: Kafka ACL production change order\n---\n# body", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-10", status=db.INDEXED, minutes_path=str(minutes))
    manifest.commit()
    _stub_answer(monkeypatch, sources=["2026-08-10-kafka-acl.md"])

    citations = client.post("/api/ask", json={"question": "kafka?"}).json()["citations"]

    assert len(citations) == 1
    assert citations[0]["meeting_id"] == "m1"
    assert citations[0]["title"] == "Kafka ACL production change order"
    assert citations[0]["date"] == "2026-08-10"


def test_ask_keeps_unmatched_sources_as_unlinked_citations(client, monkeypatch):
    """A retrieved file with no manifest row is still what the answer was built on."""
    _stub_answer(monkeypatch, sources=["vanished.md"])

    citations = client.post("/api/ask", json={"question": "anything?"}).json()["citations"]

    assert citations == [
        {"source": "vanished.md", "meeting_id": None, "date": None, "title": None, "status": None}
    ]


def test_ask_rejects_unknown_mode(client):
    response = client.post("/api/ask", json={"question": "hi", "mode": "telepathic"})
    assert response.status_code == 422


def test_ask_rejects_empty_question(client):
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


def test_ask_reports_unreachable_index(client, monkeypatch):
    from pipeline import index

    def explode(*args, **kwargs):
        raise index.IndexError_("LightRAG unreachable at http://localhost:9621")

    monkeypatch.setattr(answer, "ask", explode)

    response = client.post("/api/ask", json={"question": "why postgres?"})

    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]


def test_minutes_returns_markdown(client, manifest, tmp_path):
    minutes = tmp_path / "2026-08-10-kafka-acl.md"
    minutes.write_text("---\ntitle: Kafka ACL\n---\n## Decisions\n- Ship it", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-10", status=db.INDEXED, minutes_path=str(minutes))
    manifest.commit()

    body = client.get("/api/meetings/m1/minutes").json()

    assert body["title"] == "Kafka ACL"
    assert "## Decisions" in body["markdown"]


def test_minutes_404_for_unknown_meeting(client):
    assert client.get("/api/meetings/nope/minutes").status_code == 404


def test_minutes_404_when_never_compiled(client, manifest):
    make_meeting(manifest, "m1", "2026-08-10")
    manifest.commit()
    assert client.get("/api/meetings/m1/minutes").status_code == 404


def test_minutes_refuses_path_outside_minutes_dir(client, manifest, tmp_path):
    """A tampered manifest row must not turn this into arbitrary file disclosure."""
    outside = tmp_path.parent / "secret.md"
    outside.write_text("not minutes", encoding="utf-8")
    make_meeting(manifest, "m1", "2026-08-10", status=db.INDEXED, minutes_path=str(outside))
    manifest.commit()

    assert client.get("/api/meetings/m1/minutes").status_code == 403
