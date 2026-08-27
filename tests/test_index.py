"""LightRAG document identity and replace-on-recompile.

The recompile path is the justification for retaining transcripts. If a
recompiled document is inserted without removing its predecessor, both copies
live in the graph and retrieval starts returning contradictory duplicates - which
would quietly invalidate the whole three-tier design.
"""

from __future__ import annotations

import pytest

from pipeline import index


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.text = str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


class _PaginatedClient:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, path, json):
        self.requests.append((path, json))
        return _Response(self.pages[json["page"] - 1])


def test_doc_id_is_stable_and_strip_insensitive():
    """Mirrors LightRAG's own compute_mdhash_id(content.strip(), "doc-")."""
    assert index.compute_doc_id("hello") == index.compute_doc_id("  hello\n\n")
    assert index.compute_doc_id("hello").startswith("doc-")


def test_doc_id_changes_with_content():
    assert index.compute_doc_id("a") != index.compute_doc_id("b")


def test_document_records_replays_queued_processed_and_failed_pages(monkeypatch):
    """The status fixture mirrors LightRAG's paginated control-plane response."""
    client = _PaginatedClient(
        [
            {
                "documents": [
                    {"id": "doc-queued", "file_path": "queued.md", "status": "pending"},
                    {
                        "id": "doc-ready",
                        "file_path": "ready.md",
                        "status": "PROCESSED",
                        "chunks_count": 4,
                    },
                ],
                "total_count": 3,
            },
            {
                "documents": [
                    {"id": "doc-failed", "file_path": "failed.md", "status": "failed"}
                ],
                "total_count": 3,
            },
        ]
    )
    monkeypatch.setattr(index, "_client", lambda: client)

    records = index._document_records(page_size=2)

    assert [(record.id, record.status, record.chunks_count) for record in records] == [
        ("doc-queued", "pending", None),
        ("doc-ready", "processed", 4),
        ("doc-failed", "failed", None),
    ]
    assert [request[1]["page"] for request in client.requests] == [1, 2]


def test_duplicate_canonical_source_is_refused(monkeypatch):
    monkeypatch.setattr(
        index,
        "_document_records",
        lambda: [
            index.DocumentRecord("doc-a", "folder/meeting.md", "failed", 0),
            index.DocumentRecord("doc-b", r"other\meeting.md", "processed", 2),
        ],
    )

    with pytest.raises(index.IndexError_, match="multiple LightRAG documents own source"):
        index.find_document_by_source("meeting.md")


def test_wait_for_document_processed_handles_queue_to_terminal(monkeypatch):
    records = iter(
        [
            index.DocumentRecord("doc-a", "meeting.md", "pending", 0),
            index.DocumentRecord("doc-a", "meeting.md", "processing", 1),
            index.DocumentRecord("doc-a", "meeting.md", "processed", 2),
        ]
    )
    monkeypatch.setattr(index, "find_document_by_id", lambda *a: next(records))
    monkeypatch.setattr(index.time, "sleep", lambda *a: None)

    assert index.wait_for_document_processed("doc-a", timeout_seconds=1) is True


def test_wait_for_document_processed_stops_on_failed(monkeypatch):
    monkeypatch.setattr(
        index,
        "find_document_by_id",
        lambda *a: index.DocumentRecord("doc-a", "meeting.md", "failed", 0),
    )

    assert index.wait_for_document_processed("doc-a", timeout_seconds=1) is False


def test_unchanged_content_is_not_reinserted(tmp_path, monkeypatch):
    """Re-indexing identical content must be a no-op.

    Insertion triggers CPU-bound entity extraction; repeating it to reach the
    same state would waste minutes per document on every batch.
    """
    path = tmp_path / "m.md"
    path.write_text("---\ndate: 2026-08-10\n---\nbody", encoding="utf-8")
    existing = index.compute_doc_id(path.read_text(encoding="utf-8"))

    calls: list[str] = []
    monkeypatch.setattr(
        index,
        "find_document_by_source",
        lambda *a: index.DocumentRecord(existing, "m.md", "processed", 1),
    )
    monkeypatch.setattr(index, "insert_text", lambda *a, **k: calls.append("insert"))
    monkeypatch.setattr(index, "delete_document", lambda *a: calls.append("delete") or True)

    doc_id, replaced = index.replace_minutes(path, existing)
    assert doc_id == existing
    assert replaced is True
    assert calls == [], "identical content must not be deleted or reinserted"


def test_changed_content_deletes_old_then_inserts(tmp_path, monkeypatch):
    path = tmp_path / "m.md"
    path.write_text("new content", encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(
        index,
        "find_document_by_source",
        lambda *a: index.DocumentRecord("doc-source-owner", "m.md", "failed", 3),
    )
    monkeypatch.setattr(index, "insert_text", lambda *a, **k: calls.append("insert"))
    monkeypatch.setattr(index, "wait_for_document_processed", lambda *a: True)
    monkeypatch.setattr(
        index, "delete_document", lambda doc_id: calls.append(f"delete:{doc_id}") or True
    )

    doc_id, replaced = index.replace_minutes(path, "doc-stale")
    assert replaced is True
    assert calls == ["delete:doc-source-owner", "insert"], (
        "the current canonical-source owner, not a stale manifest id, must go first"
    )
    assert doc_id == index.compute_doc_id("new content")


def test_first_insert_has_nothing_to_delete(tmp_path, monkeypatch):
    path = tmp_path / "m.md"
    path.write_text("first", encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(index, "find_document_by_source", lambda *a: None)
    monkeypatch.setattr(index, "insert_text", lambda *a, **k: calls.append("insert"))
    monkeypatch.setattr(index, "wait_for_document_processed", lambda *a: True)
    monkeypatch.setattr(index, "delete_document", lambda *a: calls.append("delete") or True)

    _, replaced = index.replace_minutes(path, None)
    assert replaced is True
    assert calls == ["insert"]


def test_failed_delete_is_reported_not_swallowed(tmp_path, monkeypatch):
    """A delete that fails must surface, so the caller can refuse to insert.

    Inserting anyway would leave two versions of the same meeting in the graph.
    """
    path = tmp_path / "m.md"
    path.write_text("changed", encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(
        index,
        "find_document_by_source",
        lambda *a: index.DocumentRecord("doc-source-owner", "m.md", "failed", 3),
    )
    monkeypatch.setattr(index, "delete_document", lambda *a: False)
    monkeypatch.setattr(index, "insert_text", lambda *a, **k: calls.append("insert"))

    _, replaced = index.replace_minutes(path, "doc-stale")
    assert replaced is False
    assert calls == [], "must not insert when the stale copy could not be removed"


def test_failed_processing_is_not_reported_as_indexed(tmp_path, monkeypatch):
    """An enqueue acknowledgement is not a completed index."""
    path = tmp_path / "m.md"
    path.write_text("new content", encoding="utf-8")

    monkeypatch.setattr(index, "find_document_by_source", lambda *a: None)
    monkeypatch.setattr(index, "insert_text", lambda *a, **k: {"status": "success"})
    monkeypatch.setattr(index, "wait_for_document_processed", lambda *a: False)

    _, replaced = index.replace_minutes(path, None)
    assert replaced is False


def test_stale_manifest_id_cannot_delete_a_different_source(tmp_path, monkeypatch):
    path = tmp_path / "wanted.md"
    path.write_text("new content", encoding="utf-8")
    deleted = []

    monkeypatch.setattr(index, "find_document_by_source", lambda *a: None)
    monkeypatch.setattr(
        index,
        "find_document_by_id",
        lambda *a: index.DocumentRecord("doc-stale", "different.md", "processed", 2),
    )
    monkeypatch.setattr(index, "delete_document", lambda doc_id: deleted.append(doc_id) or True)
    monkeypatch.setattr(
        index,
        "insert_text",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not insert")),
    )

    with pytest.raises(index.IndexError_, match="belongs to source"):
        index.replace_minutes(path, "doc-stale")
    assert deleted == []


def test_manifest_predecessor_with_same_meeting_hash_can_be_renamed(
    tmp_path, monkeypatch
):
    path = tmp_path / "2026-08-10-new-title-8476fd87.md"
    path.write_text("new content", encoding="utf-8")
    calls = []

    monkeypatch.setattr(index, "find_document_by_source", lambda *a: None)
    monkeypatch.setattr(
        index,
        "find_document_by_id",
        lambda *a: index.DocumentRecord(
            "doc-old", "2026-08-10-old-title-8476fd87.md", "processed", 2
        ),
    )
    monkeypatch.setattr(index, "delete_document", lambda doc_id: calls.append(doc_id) or True)
    monkeypatch.setattr(index, "insert_text", lambda *a, **k: {"status": "success"})
    monkeypatch.setattr(index, "wait_for_document_processed", lambda *a: True)

    _, processed = index.replace_minutes(path, "doc-old")

    assert processed is True
    assert calls == ["doc-old"]


def test_repair_preview_names_exact_operations_and_is_deterministic():
    targets = [
        index.RepairTarget("meeting-a", "a.md", "doc-new-a", "doc-manifest-a"),
        index.RepairTarget("meeting-b", "b.md", "doc-new-b", None),
        index.RepairTarget("meeting-c", "c.md", "doc-ready-c", "doc-ready-c"),
        index.RepairTarget("meeting-d", "d.md", "doc-new-d", "doc-manifest-d"),
    ]
    records = [
        index.DocumentRecord("doc-owner-a", "a.md", "failed", 0),
        index.DocumentRecord("doc-ready-c", "c.md", "processed", 3),
        index.DocumentRecord("doc-owner-d", "d.md", "processing", 1),
    ]
    status = {"busy": False, "recovery_required": False, "latest_message": "idle"}

    first = index.build_repair_preview(targets, records=records, pipeline_state=status)
    second = index.build_repair_preview(targets, records=records, pipeline_state=status)

    assert first.fingerprint == second.fingerprint
    assert [(item.meeting_id, item.action, item.delete_doc_id) for item in first.items] == [
        ("meeting-a", "delete_then_insert", "doc-owner-a"),
        ("meeting-b", "insert", None),
        ("meeting-c", "none", None),
        ("meeting-d", "wait", None),
    ]
    assert first.items[0].manifest_doc_id == "doc-manifest-a"
    assert first.items[0].current_doc_id == "doc-owner-a"


def test_repair_preview_never_chooses_a_winner_for_duplicate_sources():
    target = index.RepairTarget("meeting-a", "a.md", "doc-new", None)
    records = [
        index.DocumentRecord("doc-a", "a.md", "failed", 0),
        index.DocumentRecord("doc-b", "folder/a.md", "processed", 2),
    ]

    preview = index.build_repair_preview(
        [target],
        records=records,
        pipeline_state={"busy": False, "recovery_required": False},
    )

    item = preview.items[0]
    assert item.action == "resolve_source_conflict"
    assert item.delete_doc_id is None
    assert item.candidate_doc_ids == ("doc-a", "doc-b")


def test_document_health_separates_storage_vectors_and_terminal_failures():
    health = index.document_health(
        records=[
            index.DocumentRecord("doc-a", "a.md", "processed", 3),
            index.DocumentRecord("doc-b", "b.md", "processed", 0),
            index.DocumentRecord("doc-c", "c.md", "failed", 0),
            index.DocumentRecord("doc-d", "d.md", "pending", None),
        ],
        pipeline_state={
            "busy": True,
            "recovery_required": False,
            "latest_message": "processing",
        },
    )

    assert health.documents_stored == 4
    assert health.documents_processed == 2
    assert health.vector_chunks_ready == 1
    assert health.failed == 1
    assert health.active == 1
    assert health.pipeline_busy is True


def test_delete_reports_failure_when_the_server_is_busy(monkeypatch):
    """LightRAG answers a delete it did not perform with 200 + status "busy".

    Its ingestion pipeline is single-threaded, so a delete issued while
    documents are still being extracted is refused - but refused with a 200,
    not a 4xx. Treating any 2xx as success made `delete_document` report
    "deleted 14/14" while deleting none, and the re-insert that followed hit
    409 on every one of them.
    """
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "busy", "message": "Pipeline is busy with another operation."}

        @staticmethod
        def raise_for_status():
            return None

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, *a, **kw):
            return Response()

    monkeypatch.setattr(index, "_client", lambda: Client())
    monkeypatch.setattr(
        index,
        "find_document_by_id",
        lambda *a: index.DocumentRecord("doc-abc", "m.md", "failed", 2),
    )
    assert index.delete_document("doc-abc") is False


def test_delete_waits_for_background_completion(monkeypatch):
    """The current LightRAG route acknowledges before its background delete ends."""
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "deletion_started"}

        @staticmethod
        def raise_for_status():
            return None

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, *a, **kw):
            return Response()

    records = iter(
        [
            index.DocumentRecord("doc-abc", "m.md", "failed", 2),
            None,
        ]
    )
    monkeypatch.setattr(index, "_client", lambda: Client())
    monkeypatch.setattr(index, "find_document_by_id", lambda *a: next(records))
    monkeypatch.setattr(index.time, "sleep", lambda *a: None)

    assert index.delete_document("doc-abc", timeout_seconds=1) is True
