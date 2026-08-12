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
from pathlib import Path

import httpx

from pipeline.config import (
    LIGHTRAG_API_KEY,
    LIGHTRAG_DEFAULT_MODE,
    LIGHTRAG_TIMEOUT,
    LIGHTRAG_URL,
)


class IndexError_(RuntimeError):
    """LightRAG rejected a request or is unreachable."""


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if LIGHTRAG_API_KEY:
        headers["X-API-Key"] = LIGHTRAG_API_KEY
    return headers


def _client() -> httpx.Client:
    # Generous timeout: entity extraction on a CPU-bound Ollama model can take
    # many minutes per document, and the insert call blocks until it finishes.
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


def delete_document(doc_id: str) -> bool:
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
                return True
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

    if previous_doc_id and previous_doc_id == doc_id:
        # Byte-identical content is already indexed under this id. Re-inserting
        # would burn minutes of CPU-bound extraction to reach the same state.
        return doc_id, True

    if previous_doc_id and not delete_document(previous_doc_id):
        # Bail out BEFORE inserting. Inserting anyway is what creates the
        # duplicate this function exists to prevent - the caller cannot undo it
        # after the fact.
        return doc_id, False

    insert_text(text, file_source=path.name)
    return doc_id, True


def insert_minutes(path: Path) -> dict:
    """Insert a minutes file, keyed by its filename for citation.

    Prefer `replace_minutes` in the pipeline; this stays for one-off manual
    inserts where no previous version exists.
    """
    return insert_text(path.read_text(encoding="utf-8"), file_source=path.name)


def query_context(question: str, mode: str | None = None, top_k: int | None = None) -> str:
    """Retrieve context for a question WITHOUT generating an answer.

    Two uses. First, topical prior-decision lookup during minutes compilation -
    retrieval is what is wanted there, not prose. Second, it is the hook for
    splitting retrieval from synthesis: the local model retrieves, a subscription
    writes the answer.

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
        with _client() as client:
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
