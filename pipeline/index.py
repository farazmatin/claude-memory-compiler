"""Stage 5 and 6: push minutes into LightRAG, and query it.

Only minutes go in. Transcripts stay out of the index by design - see the module
docstring in compile_minutes.py for why.

LightRAG builds two things from each document: a knowledge graph of entities and
relations, and a vector index. That combination is the reason it was chosen over
plain vector RAG: PM questions are entity-centric and aggregative ("why did we
deprioritize X", "everything customer Y has told us"), and those answers span
dozens of meetings rather than the top five chunks.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from pipeline.config import (
    LIGHTRAG_API_KEY,
    LIGHTRAG_DEFAULT_MODE,
    LIGHTRAG_TIMEOUT,
    LIGHTRAG_URL,
)


class IndexError_(RuntimeError):
    """LightRAG rejected a request or is unreachable."""


@dataclass(frozen=True)
class DocumentRecord:
    """The small, non-content subset of a LightRAG document we need here."""

    id: str
    file_path: str
    status: str
    chunks_count: int | None = None


@dataclass(frozen=True)
class RepairTarget:
    """One manifest document whose LightRAG ownership needs reconciliation."""

    meeting_id: str
    file_source: str
    desired_doc_id: str
    manifest_doc_id: str | None


@dataclass(frozen=True)
class RepairOperation:
    """A read-only repair decision bound to exact current document IDs."""

    meeting_id: str
    file_source: str
    desired_doc_id: str
    manifest_doc_id: str | None
    current_doc_id: str | None
    current_status: str | None
    action: str
    delete_doc_id: str | None = None
    candidate_doc_ids: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class RepairPreview:
    """Deterministic, mutation-free plan for reconciling document ownership."""

    fingerprint: str
    pipeline_busy: bool
    recovery_required: bool
    latest_message: str
    items: tuple[RepairOperation, ...]


@dataclass(frozen=True)
class DocumentHealth:
    """Three distinct index readiness signals plus terminal failure counts."""

    documents_stored: int
    documents_processed: int
    vector_chunks_ready: int
    failed: int
    active: int
    status_counts: dict[str, int]
    pipeline_busy: bool
    recovery_required: bool
    latest_message: str


ACTIVE_DOCUMENT_STATUSES = {
    "pending",
    "parsing",
    "analyzing",
    "processing",
    "preprocessed",
}


def _headers() -> dict[str, str]:
    """Build request headers, refusing to proceed without an API key.

    This used to degrade silently: with no key the header was simply omitted, the
    request succeeded against an unauthenticated server, and nothing anywhere
    reported a problem. That is how a LightRAG instance holding every meeting
    record ran open to the network for a day without being noticed. Failing here
    means a missing key surfaces as an error at the first call instead of as an
    index that quietly accepts anonymous requests.
    """
    if not LIGHTRAG_API_KEY:
        raise IndexError_(
            "MMC_LIGHTRAG_API_KEY is not set. Refusing to send unauthenticated "
            "requests to LightRAG - set it in .env and restart the stack."
        )
    return {"Content-Type": "application/json", "X-API-Key": LIGHTRAG_API_KEY}


def _client() -> httpx.Client:
    # Legacy document operations retain their configured timeout. The active
    # context build does not call this model-backed route.
    return httpx.Client(base_url=LIGHTRAG_URL, timeout=LIGHTRAG_TIMEOUT, headers=_headers())


def health() -> dict:
    """Server status. Used by `pipeline status` to fail fast with a clear message."""
    try:
        with _client() as client:
            response = client.get("/health")
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise IndexError_(f"LightRAG unreachable at {LIGHTRAG_URL}: {exc}") from exc


def pipeline_status() -> dict[str, Any]:
    """Return the bounded LightRAG control-plane state, never document content."""
    try:
        with _client() as client:
            response = client.get("/documents/pipeline_status")
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise IndexError_(f"pipeline status lookup failed: {exc}") from exc
    if not isinstance(body, dict):
        raise IndexError_("pipeline status lookup returned an invalid response")
    return body


def document_health(
    *,
    records: Iterable[DocumentRecord] | None = None,
    pipeline_state: Mapping[str, Any] | None = None,
) -> DocumentHealth:
    """Report storage, processing, and vector readiness without conflating them."""
    current_records = tuple(records if records is not None else _document_records())
    state = dict(pipeline_state if pipeline_state is not None else pipeline_status())
    counts = Counter(record.status for record in current_records)
    return DocumentHealth(
        documents_stored=len(current_records),
        documents_processed=counts.get("processed", 0),
        vector_chunks_ready=sum(
            record.status == "processed" and bool(record.chunks_count)
            for record in current_records
        ),
        failed=counts.get("failed", 0),
        active=sum(counts.get(status, 0) for status in ACTIVE_DOCUMENT_STATUSES),
        status_counts=dict(sorted(counts.items())),
        pipeline_busy=bool(
            state.get("busy")
            or state.get("destructive_busy")
            or int(state.get("pending_enqueues") or 0)
        ),
        recovery_required=bool(state.get("recovery_required")),
        latest_message=str(state.get("latest_message") or ""),
    )


def compute_doc_id(text: str) -> str:
    """LightRAG's document id for this content.

    Mirrors LightRAG's own `compute_mdhash_id(content.strip(), prefix="doc-")`.
    Deriving it locally rather than parsing it out of the insert response means
    the id is knowable before insert and stable across restarts, which is what
    makes replace-on-recompile possible.

    If a LightRAG release ever changes this convention, deletes will start
    missing and `index` will warn - see `replace_minutes`.
    """
    digest = hashlib.md5(text.strip().encode("utf-8")).hexdigest()
    return f"doc-{digest}"


def insert_text(text: str, file_source: str) -> dict:
    """Insert one document.

    `file_source` is carried through to LightRAG's citations so a retrieved fact
    can be traced back to the meeting it came from.
    """
    payload = {"text": text, "file_source": file_source}
    try:
        with _client() as client:
            response = client.post("/documents/text", json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise IndexError_(
            f"insert failed ({exc.response.status_code}): {exc.response.text[:500]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise IndexError_(f"insert failed: {exc}") from exc


def _document_records(page_size: int = 200) -> list[DocumentRecord]:
    """List LightRAG records without exposing stored meeting text.

    The endpoint is paginated even on small installations.  Read every page so
    canonical-source lookup cannot silently miss an older record and create a
    duplicate.
    """
    records: list[DocumentRecord] = []
    page = 1
    while True:
        try:
            with _client() as client:
                response = client.post(
                    "/documents/paginated",
                    json={"page": page, "page_size": page_size},
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise IndexError_(f"document lookup failed: {exc}") from exc

        if not isinstance(body, dict):
            raise IndexError_("document lookup returned an invalid response")
        items = body.get("documents") or body.get("items") or body.get("data") or []
        if not isinstance(items, list):
            raise IndexError_("document lookup returned an invalid document list")

        for item in items:
            if not isinstance(item, dict):
                continue
            doc_id = str(item.get("id") or item.get("doc_id") or "")
            file_path = str(item.get("file_path") or item.get("file_source") or "")
            if not doc_id:
                continue
            raw_chunks = item.get("chunks_count")
            try:
                chunks_count = int(raw_chunks) if raw_chunks is not None else None
            except (TypeError, ValueError):
                chunks_count = None
            records.append(
                DocumentRecord(
                    id=doc_id,
                    file_path=file_path,
                    status=str(item.get("status") or "unknown").lower(),
                    chunks_count=chunks_count,
                )
            )

        total = body.get("total_count", body.get("total"))
        if len(items) < page_size:
            break
        if isinstance(total, int) and page * page_size >= total:
            break
        page += 1
    return records


def _source_name(value: str) -> str:
    """Return a filename for either Windows- or POSIX-shaped source paths."""
    return value.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _meeting_hash_suffix(value: str) -> str | None:
    """Return the stable id8 suffix carried by generated minutes filenames."""
    stem = _source_name(value).rsplit(".", 1)[0]
    suffix = stem.rsplit("-", 1)[-1]
    if len(suffix) == 8 and all(character in "0123456789abcdef" for character in suffix):
        return suffix
    return None


def find_document_by_source(file_source: str) -> DocumentRecord | None:
    """Resolve the one LightRAG record that currently owns a citation source."""
    wanted = _source_name(file_source)
    matches = [record for record in _document_records() if _source_name(record.file_path) == wanted]
    if len(matches) > 1:
        ids = ", ".join(sorted(record.id for record in matches))
        raise IndexError_(
            f"multiple LightRAG documents own source {file_source!r}: {ids}; "
            "repair the source conflict before indexing"
        )
    return matches[0] if matches else None


def find_document_by_id(doc_id: str) -> DocumentRecord | None:
    """Find a document by its actual LightRAG identifier."""
    return next((record for record in _document_records() if record.id == doc_id), None)


def build_repair_preview(
    targets: Iterable[RepairTarget],
    *,
    records: Iterable[DocumentRecord] | None = None,
    pipeline_state: Mapping[str, Any] | None = None,
) -> RepairPreview:
    """Plan exact index reconciliation without changing LightRAG or the manifest.

    Source ownership outranks the manifest's cached document id because it is the
    canonical source key that triggers LightRAG's duplicate-source refusal.  A
    conflict is deliberately left for an operator; this function never chooses a
    winner among multiple primary candidates.
    """
    current_records = tuple(records if records is not None else _document_records())
    state = dict(pipeline_state if pipeline_state is not None else pipeline_status())
    by_source: dict[str, list[DocumentRecord]] = {}
    for record in current_records:
        by_source.setdefault(_source_name(record.file_path), []).append(record)

    operations: list[RepairOperation] = []
    for target in targets:
        owners = sorted(by_source.get(_source_name(target.file_source), []), key=lambda row: row.id)
        candidate_ids = tuple(owner.id for owner in owners)
        if len(owners) > 1:
            operations.append(
                RepairOperation(
                    meeting_id=target.meeting_id,
                    file_source=target.file_source,
                    desired_doc_id=target.desired_doc_id,
                    manifest_doc_id=target.manifest_doc_id,
                    current_doc_id=None,
                    current_status=None,
                    action="resolve_source_conflict",
                    candidate_doc_ids=candidate_ids,
                    reason="multiple primary documents own the canonical source",
                )
            )
            continue
        if not owners:
            operations.append(
                RepairOperation(
                    meeting_id=target.meeting_id,
                    file_source=target.file_source,
                    desired_doc_id=target.desired_doc_id,
                    manifest_doc_id=target.manifest_doc_id,
                    current_doc_id=None,
                    current_status=None,
                    action="insert",
                    reason="canonical source has no current document",
                )
            )
            continue

        owner = owners[0]
        if owner.id == target.desired_doc_id and owner.status == "processed":
            action = "none"
            delete_doc_id = None
            reason = "desired document is already processed"
        elif owner.status in ACTIVE_DOCUMENT_STATUSES:
            action = "wait"
            delete_doc_id = None
            reason = f"current document is still {owner.status}"
        else:
            action = "delete_then_insert"
            delete_doc_id = owner.id
            reason = (
                f"canonical source is owned by {owner.status} document {owner.id}; "
                f"replace with {target.desired_doc_id}"
            )
        operations.append(
            RepairOperation(
                meeting_id=target.meeting_id,
                file_source=target.file_source,
                desired_doc_id=target.desired_doc_id,
                manifest_doc_id=target.manifest_doc_id,
                current_doc_id=owner.id,
                current_status=owner.status,
                action=action,
                delete_doc_id=delete_doc_id,
                candidate_doc_ids=candidate_ids,
                reason=reason,
            )
        )

    fingerprint_payload = {
        "pipeline": {
            "busy": bool(state.get("busy")),
            "destructive_busy": bool(state.get("destructive_busy")),
            "pending_enqueues": int(state.get("pending_enqueues") or 0),
            "recovery_required": bool(state.get("recovery_required")),
        },
        "operations": [operation.__dict__ for operation in operations],
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return RepairPreview(
        fingerprint=fingerprint,
        pipeline_busy=bool(
            state.get("busy")
            or state.get("destructive_busy")
            or int(state.get("pending_enqueues") or 0)
        ),
        recovery_required=bool(state.get("recovery_required")),
        latest_message=str(state.get("latest_message") or ""),
        items=tuple(operations),
    )


def wait_for_document_processed(
    doc_id: str,
    timeout_seconds: float = LIGHTRAG_TIMEOUT,
    poll_seconds: float = 2.0,
) -> bool:
    """Wait for an enqueued insert and accept only terminal `processed` state."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = find_document_by_id(doc_id)
        if record is not None:
            if record.status == "processed":
                return True
            if record.status == "failed":
                return False
        time.sleep(poll_seconds)
    return False


def delete_document(doc_id: str, timeout_seconds: float = LIGHTRAG_TIMEOUT) -> bool:
    """Remove a document and its derived graph entities.

    Returns False when the server has no matching document or exposes no delete
    route, rather than raising: the caller decides whether a failed delete should
    block a re-index. Tries the current endpoint first, then the older path, since
    this route has moved between releases.
    """
    attempts = (
        ("DELETE", "/documents/delete_document", {"doc_ids": [doc_id]}),
        ("DELETE", f"/documents/{doc_id}", None),
    )
    for method, url, payload in attempts:
        try:
            with _client() as client:
                response = client.request(method, url, json=payload)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                # A 2xx is not proof of a delete. LightRAG's ingestion pipeline
                # is single-threaded, and a delete issued while it is extracting
                # is declined with 200 + {"status": "busy"} - so trusting the
                # status code alone reports a delete that never happened, and
                # the re-insert behind it then fails 409 on a record the caller
                # believes is gone.
                try:
                    body: Any = response.json()
                except ValueError:
                    body = {}
                status = body.get("status") if isinstance(body, dict) else None
                declined = status in {
                    "busy",
                    "not_allowed",
                }
                if declined:
                    return False

                # Current LightRAG acknowledges the request before its
                # background deletion completes.  Do not let the caller insert
                # the replacement until the old identifier has actually gone.
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline:
                    if find_document_by_id(doc_id) is None:
                        return True
                    time.sleep(2.0)
                return False
        except httpx.HTTPError:
            continue
    return False


def replace_minutes(
    path: Path, previous_doc_id: str | None, augment: str = ""
) -> tuple[str, bool]:
    """Index a minutes file, replacing any previously indexed version.

    Returns (doc_id, replaced_cleanly).

    `augment` is appended to the indexed text but never written to the file on
    disk. It carries the canonicalized entity and relation block from the manifest:
    the file keeps what the compiler actually wrote, while the index gets names
    normalized through the people registry. Since it is derived deterministically
    from the manifest, the document id stays stable.

    The recompile path depends on this. Inserting a recompiled document without
    deleting its predecessor leaves both copies in the graph, so every entity and
    relation from the old version survives alongside the new one and retrieval
    starts returning contradictory duplicates. That would quietly invalidate the
    whole reason transcripts are retained.
    """
    text = path.read_text(encoding="utf-8") + augment
    doc_id = compute_doc_id(text)
    existing = find_document_by_source(path.name)

    if existing and existing.id == doc_id and existing.status == "processed":
        # Byte-identical content is already indexed under this id. Re-inserting
        # would burn minutes of CPU-bound extraction to reach the same state.
        return doc_id, True

    # The manifest can lag behind LightRAG after a failed/aborted run.  The
    # canonical citation source is authoritative for replacement because it is
    # what causes LightRAG's 409 duplicate-source response.
    delete_id = existing.id if existing else None
    if not existing and previous_doc_id:
        previous = find_document_by_id(previous_doc_id)
        previous_hash = _meeting_hash_suffix(previous.file_path) if previous else None
        desired_hash = _meeting_hash_suffix(path.name)
        if (
            previous
            and _source_name(previous.file_path) != _source_name(path.name)
            and (not previous_hash or previous_hash != desired_hash)
        ):
            raise IndexError_(
                f"manifest document {previous_doc_id} belongs to source "
                f"{previous.file_path!r}, not {path.name!r}; refusing to delete it"
            )
        delete_id = previous.id if previous else None
    if delete_id and not delete_document(delete_id):
        # Bail out BEFORE inserting. Inserting anyway is what creates the
        # duplicate this function exists to prevent - the caller cannot undo it
        # after the fact.
        return doc_id, False

    insert_text(text, file_source=path.name)
    return doc_id, wait_for_document_processed(doc_id)


def insert_minutes(path: Path) -> dict:
    """Insert a minutes file, keyed by its filename for citation.

    Prefer `replace_minutes` in the pipeline; this stays for one-off manual
    inserts where no previous version exists.
    """
    return insert_text(path.read_text(encoding="utf-8"), file_source=path.name)


def query_context(question: str, mode: str | None = None, top_k: int | None = None) -> str:
    """Retrieve context for a question WITHOUT generating an answer.

    Legacy compatibility wrapper for the model-backed LightRAG query route.
    Active retrieval uses graph_sync.retrieve_context() instead.

    Returns "" rather than raising when the server is unreachable or does not
    support the flag - a missing nice-to-have must not fail a compile.
    """
    payload: dict[str, object] = {
        "query": question,
        "mode": mode or LIGHTRAG_DEFAULT_MODE,
        "only_need_context": True,
    }
    if top_k is not None:
        payload["top_k"] = top_k

    try:
        with httpx.Client(base_url=LIGHTRAG_URL, timeout=10.0, headers=_headers()) as client:
            response = client.post("/query", json=payload)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        return ""

    if isinstance(data, dict):
        return str(data.get("response") or data.get("context") or "")
    return str(data)


def query(question: str, mode: str | None = None, top_k: int | None = None) -> str:
    """Ask the knowledge base.

    Modes: `hybrid` (graph + vector, the default), `global` for aggregative
    questions whose answer spans many meetings, `local` for tightly scoped
    entity lookups, `naive` for plain vector search.
    """
    payload: dict[str, object] = {"query": question, "mode": mode or LIGHTRAG_DEFAULT_MODE}
    if top_k is not None:
        payload["top_k"] = top_k

    try:
        with _client() as client:
            response = client.post("/query", json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise IndexError_(
            f"query failed ({exc.response.status_code}): {exc.response.text[:500]}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise IndexError_(f"query failed: {exc}") from exc

    if isinstance(data, dict):
        return str(data.get("response") or data.get("answer") or data)
    return str(data)
