from __future__ import annotations

import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline import chunk_index, context_provider, db, dense_index, graph_sync, index


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


# ── Hybrid retrieval ──────────────────────────────────────────────────
#
# Fusion, diversity and provenance are context_provider's own logic, so most of
# these drive it through the two retrieval halves rather than through a model.
# `chunk_index.search_chunks` and `dense_index.search_dense` are pinned by their
# own suites; stubbing them is what makes a rank assertion an assertion about
# RRF rather than about BM25's opinion of a fixture. The tests that need the
# wiring proven - the excerpt floor, BM25-only, the exclusion - run the real
# BM25 index over a real chunked corpus.
#
# No test may reach a model. `dense_index.rerank` catches a failed load and
# returns the order it was given, so a test that produces excerpts without
# installing a cross-encoder would silently try to fetch a 130 MB one (GC1).
# `_reranker` is mandatory wherever excerpts exist.

FILLER = (
    "The team reviewed the control inventory and agreed the evidence trail has to "
    "survive an audit without anyone reconstructing it from memory afterwards. "
    "Ownership stays with the first line while the second line reviews sampling. "
)


def _minutes(manifest, tmp_path: Path, meeting_id: str, date: str, body: str) -> Path:
    """A meeting with minutes on disk, advanced to minutes_compiled."""
    minutes = tmp_path / f"{meeting_id}.md"
    minutes.write_text(body, encoding="utf-8")
    db.insert_meeting(
        manifest,
        meeting_id=meeting_id,
        source_path=str(tmp_path / f"{meeting_id}.m4a"),
        source_name=f"{meeting_id}.m4a",
        audio_path=str(tmp_path / f"{meeting_id}.m4a"),
        meeting_date=date,
        meeting_time="09:00",
        title_hint=meeting_id,
        duration_sec=60.0,
    )
    db.advance(
        manifest,
        meeting_id,
        db.MINUTES_COMPILED,
        transcript_path=str(tmp_path / "private-transcript.json"),
        minutes_path=str(minutes),
    )
    manifest.commit()
    return minutes


def _body(marker: str) -> str:
    """Minutes that match the fixture query and chunk to exactly one passage."""
    return (
        f"# Control inventory review\n\n{marker} The control inventory was discussed. "
        + FILLER * 2
    )


def _entity(manifest, meeting_id: str, name: str, description: str, kind: str = "feature") -> None:
    manifest.execute(
        "INSERT INTO entities (meeting_id, name, kind, description) VALUES (?, ?, ?, ?)",
        (meeting_id, name, kind, description),
    )
    manifest.commit()


def _hit(
    meeting_id: str,
    ordinal: int = 0,
    *,
    text: str | None = None,
    score: float = 0.5,
    date: str = "2026-06-09",
) -> chunk_index.ChunkHit:
    return chunk_index.ChunkHit(
        chunk_id=f"{meeting_id}:{ordinal:04d}",
        meeting_id=meeting_id,
        meeting_date=date,
        source_path=f"/minutes/{meeting_id}.md",
        ordinal=ordinal,
        heading=None,
        text=text if text is not None else f"{meeting_id}:{ordinal} {FILLER}",
        score=score,
    )


def _halves(monkeypatch, lexical=(), semantic=()) -> list[tuple[str, dict]]:
    """Pin both retrieval halves. Returns the keyword arguments each was called with."""
    calls: list[tuple[str, dict]] = []

    def _lexical(_conn, _query, **kwargs):
        calls.append(("bm25", kwargs))
        return list(lexical)

    def _semantic(_conn, _query, **kwargs):
        calls.append(("dense", kwargs))
        return list(semantic)

    monkeypatch.setattr(chunk_index, "search_chunks", _lexical)
    monkeypatch.setattr(dense_index, "search_dense", _semantic)
    return calls


class _FlatReranker:
    """A cross-encoder with no opinion: every document scores the same.

    `dense_index.rerank` breaks ties on the incoming order, so this leaves the
    fused ranking intact while still exercising the real rerank call - and it
    records what it was shown, which is how the "rerank the untrimmed text" rule
    gets checked.
    """

    def __init__(self) -> None:
        self.documents: list[str] = []

    def rerank(self, query: str, documents):
        self.documents = list(documents)
        return [0.0] * len(self.documents)


def _reranker(monkeypatch, fake=None):
    fake = fake or _FlatReranker()
    monkeypatch.setattr(dense_index, "_load_reranker", lambda model: fake)
    monkeypatch.setattr(dense_index, "_RERANKER", None)
    monkeypatch.setattr(dense_index, "_RERANKER_MODEL", None)
    return fake


def _search(**kwargs):
    return context_provider.ManifestContextProvider(db.DB_PATH).search(
        context_provider.ContextQuery(**kwargs)
    )


def test_rrf_puts_a_hit_both_halves_found_above_one_only_either_found(manifest, monkeypatch):
    """Two reciprocal ranks beat one, which is the whole point of fusing on rank.

    "z-both" is *second* in both the lexical and the dense list, so on either
    half alone it is behind the half's own rank-1 hit - "a-win" in lexical,
    "b-other" in semantic - and never holds the best single rank anywhere.
    Summed, it still wins: 1/62 + 1/62 = 0.03226 against 1/61 = 0.01639 for
    each of the other two. A fusion that took the better single rank instead
    of the sum would score "z-both" at 1/62, the worst of the three, and put
    it last - not merely tied, so no tie-break could rescue the assertion.
    """
    _halves(
        monkeypatch,
        lexical=[_hit("a-win"), _hit("z-both")],
        semantic=[_hit("b-other"), _hit("z-both")],
    )
    _reranker(monkeypatch)

    result = _search(query="control inventory", limit=5, max_chars=20_000)

    assert result.items[0].meeting_id == "z-both"
    assert {item.meeting_id for item in result.items} == {"z-both", "a-win", "b-other"}
    assert all(item.kind == "minute_excerpt" for item in result.items)


def test_the_cross_encoder_reorders_the_fused_candidates(manifest, monkeypatch):
    """Step 4 is wired, not decorative: the rerank pass can overturn the fusion."""
    _halves(monkeypatch, lexical=[_hit("alpha"), _hit("omega")], semantic=[])

    class Opinionated:
        def rerank(self, query, documents):
            return [1.0 if "omega" in document else -1.0 for document in documents]

    _reranker(monkeypatch, Opinionated())

    result = _search(query="control inventory", limit=5, max_chars=20_000)

    assert [item.meeting_id for item in result.items] == ["omega", "alpha"]
    assert result.items[0].score > result.items[1].score


def test_no_meeting_contributes_more_than_two_items(manifest, tmp_path, monkeypatch):
    """The cap is over the whole result set, not per kind.

    One verbose meeting used to be able to fill every slot, and this corpus has
    18,000-character minutes.
    """
    _minutes(manifest, tmp_path, "loud", "2026-06-09", _body("Loud"))
    _entity(manifest, "loud", "Control inventory", "the programme of record")
    _halves(
        monkeypatch,
        lexical=[_hit("loud", 0), _hit("loud", 1), _hit("quiet", 0)],
        semantic=[_hit("loud", 2), _hit("loud", 3)],
    )
    _reranker(monkeypatch)

    result = _search(query="control inventory", limit=8, max_chars=20_000)

    counts = Counter(item.meeting_id for item in result.items)
    assert counts["loud"] == 2
    assert max(counts.values()) <= 2
    # "loud" has an entity item waiting, and it does not get a third slot.
    assert not [i for i in result.items if i.meeting_id == "loud" and i.kind == "entity"]


def test_two_excerpts_are_admitted_before_the_first_entity_summary(
    manifest, tmp_path, monkeypatch
):
    """The starvation this rebuild exists to fix.

    Entity items scored a fixed 1.0/0.9 against an excerpt's fixed 0.8 and the
    result was sorted by that score, so a live search for "USC control
    inventory" returned 5 items, every one an entity and not one a verbatim
    excerpt. This runs the real BM25 index, so it also proves the wiring.
    """
    for meeting_id, date in (("m1", "2026-06-09"), ("m2", "2026-06-10"), ("m3", "2026-06-11")):
        _minutes(manifest, tmp_path, meeting_id, date, _body(meeting_id))
        _entity(manifest, meeting_id, "Control inventory", f"{meeting_id} description")
    chunk_index.reindex_all(manifest)
    _reranker(monkeypatch)

    result = _search(query="control inventory", limit=8, max_chars=20_000)

    kinds = [item.kind for item in result.items]
    assert kinds.count("minute_excerpt") >= 2
    assert kinds[:2] == ["minute_excerpt", "minute_excerpt"]
    # Entities are not banished, just no longer first.
    assert "entity" in kinds


def test_an_entity_name_is_emitted_once_however_many_meetings_named_it(
    manifest, tmp_path, monkeypatch
):
    """One item per name. The old code emitted one per (name, meeting) pair."""
    for meeting_id, date in (("m1", "2026-06-09"), ("m2", "2026-06-10")):
        _minutes(manifest, tmp_path, meeting_id, date, _body(meeting_id))
        _entity(manifest, meeting_id, "Control inventory", f"{meeting_id} description")

    result = _search(query="control inventory", limit=8, max_chars=20_000)

    names = [item.text.split(" (")[0] for item in result.items if item.kind == "entity"]
    assert names == ["Control inventory"]


def test_an_entity_quotes_only_the_meeting_it_is_attributed_to(manifest, tmp_path, monkeypatch):
    """GC3.

    `graph_sync.collect` folds every meeting's description of a name into one
    list, because that is what a graph node wants. An emitted item is a citation
    of one meeting, and the old code put the fold under each meeting's id in
    turn, so every copy claimed words that were said in a different room.
    """
    _minutes(manifest, tmp_path, "june", "2026-06-09", _body("June"))
    _minutes(manifest, tmp_path, "july", "2026-07-09", _body("July"))
    _entity(manifest, "june", "Control inventory", "owned by the first line")
    _entity(manifest, "july", "Control inventory", "sampling moved to the second line")

    result = _search(query="control inventory", limit=8, max_chars=20_000)

    entities = [item for item in result.items if item.kind == "entity"]
    assert len(entities) == 1
    assert entities[0].meeting_id == "july"
    assert "sampling moved to the second line" in entities[0].text
    assert "owned by the first line" not in entities[0].text


def test_exclude_meeting_ids_removes_the_meeting_grounding_the_request(
    manifest, tmp_path, monkeypatch
):
    """Background has to be genuinely prior, not the source minutes read back."""
    for meeting_id, date in (("source", "2026-06-10"), ("prior", "2026-06-09")):
        _minutes(manifest, tmp_path, meeting_id, date, _body(meeting_id))
        _entity(manifest, meeting_id, "Control inventory", f"{meeting_id} description")
        manifest.execute(
            "INSERT INTO relations (meeting_id, subject, predicate, object) VALUES (?, ?, ?, ?)",
            (meeting_id, "Control inventory", "is owned by", "the first line"),
        )
    manifest.commit()
    chunk_index.reindex_all(manifest)
    _reranker(monkeypatch)

    result = _search(
        query="control inventory",
        limit=8,
        max_chars=20_000,
        exclude_meeting_ids=["source"],
    )

    assert result.items
    assert {item.meeting_id for item in result.items} == {"prior"}
    assert {item.kind for item in result.items} >= {"minute_excerpt", "entity"}


def test_a_slow_graph_is_abandoned_and_says_so(manifest, tmp_path, monkeypatch):
    """The graph is 6.07s of a 6.44s request and cannot answer on its own.

    LightRAG reports 0 of 129 documents processed, so its vector half is dead
    and traversal is all that is left. It gets a leash, and the answer goes out
    without it (GC5).
    """
    _minutes(manifest, tmp_path, "m1", "2026-06-09", _body("m1"))
    chunk_index.reindex_all(manifest)
    _reranker(monkeypatch)

    # The bound search really runs under, read before the test shortens it.
    assert context_provider.GRAPH_TIMEOUT_SEC == 1.5
    monkeypatch.setattr(context_provider, "GRAPH_TIMEOUT_SEC", 0.05)

    entered = threading.Event()

    def slow(*_args, **_kwargs):
        entered.set()
        time.sleep(1.0)
        return {"available": True, "matched_labels": [], "nodes": [], "edges": []}

    monkeypatch.setattr(graph_sync, "retrieve_graph", slow)

    started = time.monotonic()
    result = _search(query="control inventory", limit=8, max_chars=20_000)
    elapsed = time.monotonic() - started

    assert entered.is_set(), "the graph traversal was never started"
    assert elapsed < 0.5, f"search waited for the graph ({elapsed:.2f}s)"
    assert [item.kind for item in result.items] == ["minute_excerpt"]
    assert "skipped" in result.index.detail


def test_bm25_only_is_a_working_mode_when_the_dense_half_is_not_built(
    manifest, tmp_path, monkeypatch
):
    """`chunk_vectors` is empty on every database that exists today.

    The corpus build was held deliberately: a later stage rewrites
    `minute_chunks.context_header` and invalidates every vector. BM25-only is
    the mode this ships in, not a fallback that logs a warning.
    """
    _minutes(manifest, tmp_path, "m1", "2026-06-09", _body("m1"))
    chunk_index.reindex_all(manifest)
    _reranker(monkeypatch)

    result = _search(query="control inventory", limit=8, max_chars=20_000)

    assert [item.kind for item in result.items] == ["minute_excerpt"]
    assert "chunks indexed" in result.index.detail
    assert "no vectors" in result.index.detail
    assert result.index.status == "partial"


def test_neither_half_built_returns_no_items_and_a_reason(manifest, tmp_path, monkeypatch):
    """Nothing matched and nothing is built are different answers (GC5)."""
    _minutes(manifest, tmp_path, "m1", "2026-06-09", _body("m1"))  # minutes, never chunked

    result = _search(query="control inventory", limit=8, max_chars=20_000)

    assert result.items == []
    assert result.index.status == "unavailable"
    assert "chunk index is empty" in result.index.detail
    assert "no vectors" in result.index.detail


def test_limit_and_max_chars_both_bound_the_fused_set(manifest, monkeypatch):
    """GC4, and the budget is applied last.

    The cross-encoder has to score the passage rather than a truncation of it,
    so the character budget lands after the final set is chosen - which is also
    why the candidate pool is not drawn under the caller's budget.
    """
    text = (FILLER * 3)[:250]
    calls = _halves(
        monkeypatch,
        lexical=[_hit(f"m{i}", text=text) for i in range(6)],
        semantic=[_hit(f"m{i}", text=text) for i in range(6, 12)],
    )
    flat = _reranker(monkeypatch)

    result = _search(query="control inventory", limit=3, max_chars=700)

    assert len(result.items) == 3
    assert sum(len(item.text) for item in result.items) <= 700
    # A trimmed excerpt is marked, the same way both retrieval halves mark one.
    assert any(item.text.endswith("…") for item in result.items)
    assert flat.documents, "the cross-encoder was never called"
    assert not any(document.endswith("…") for document in flat.documents)
    assert all(kwargs["max_chars"] > 700 for _half, kwargs in calls)
