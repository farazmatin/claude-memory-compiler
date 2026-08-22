"""LightRAG document identity and replace-on-recompile.

The recompile path is the justification for retaining transcripts. If a
recompiled document is inserted without removing its predecessor, both copies
live in the graph and retrieval starts returning contradictory duplicates - which
would quietly invalidate the whole three-tier design.
"""

from __future__ import annotations

from pipeline import index


def test_doc_id_is_stable_and_strip_insensitive():
    """Mirrors LightRAG's own compute_mdhash_id(content.strip(), "doc-")."""
    assert index.compute_doc_id("hello") == index.compute_doc_id("  hello\n\n")
    assert index.compute_doc_id("hello").startswith("doc-")


def test_doc_id_changes_with_content():
    assert index.compute_doc_id("a") != index.compute_doc_id("b")


def test_unchanged_content_is_not_reinserted(tmp_path, monkeypatch):
    """Re-indexing identical content must be a no-op.

    Insertion triggers CPU-bound entity extraction; repeating it to reach the
    same state would waste minutes per document on every batch.
    """
    path = tmp_path / "m.md"
    path.write_text("---\ndate: 2026-08-10\n---\nbody", encoding="utf-8")
    existing = index.compute_doc_id(path.read_text(encoding="utf-8"))

    calls: list[str] = []
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
    monkeypatch.setattr(index, "insert_text", lambda *a, **k: calls.append("insert"))
    monkeypatch.setattr(
        index, "delete_document", lambda doc_id: calls.append(f"delete:{doc_id}") or True
    )

    doc_id, replaced = index.replace_minutes(path, "doc-stale")
    assert replaced is True
    assert calls == ["delete:doc-stale", "insert"], "old version must go first"
    assert doc_id == index.compute_doc_id("new content")


def test_first_insert_has_nothing_to_delete(tmp_path, monkeypatch):
    path = tmp_path / "m.md"
    path.write_text("first", encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(index, "insert_text", lambda *a, **k: calls.append("insert"))
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
    monkeypatch.setattr(index, "delete_document", lambda *a: False)
    monkeypatch.setattr(index, "insert_text", lambda *a, **k: calls.append("insert"))

    _, replaced = index.replace_minutes(path, "doc-stale")
    assert replaced is False
    assert calls == [], "must not insert when the stale copy could not be removed"


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
    assert index.delete_document("doc-abc") is False
