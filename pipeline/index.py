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


def insert_minutes(path: Path) -> dict:
    """Insert a minutes file, keyed by its filename for citation."""
    return insert_text(path.read_text(encoding="utf-8"), file_source=path.name)


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
