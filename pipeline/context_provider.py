"""Read-only, bounded context interface over the private meeting archive.

The provider deliberately emits background context rather than claims. Product
Manager owns QA status and evidence authority; this module only supplies
provenance-bearing graph records and minute excerpts through a stable contract.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pipeline import db, graph_sync, index

CONTEXT_QUERY_TIMEOUT_SEC = 6.0


class ContextQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4_000)
    limit: int = Field(default=8, ge=1, le=50)
    max_chars: int = Field(default=5_000, ge=256, le=20_000)
    as_of: str | None = None

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value

    @field_validator("as_of")
    @classmethod
    def _valid_as_of(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("as_of must be YYYY-MM-DD")
        return value


class ContextItem(BaseModel):
    kind: Literal["entity", "relation", "minute_excerpt"]
    text: str
    meeting_id: str
    meeting_date: str | None
    source_path: str
    score: float = Field(ge=0.0, le=1.0)
    classification: Literal["background"] = "background"


class ContextIndexState(BaseModel):
    backend: Literal["lightrag-postgres", "manifest-fallback"]
    status: Literal["ready", "partial", "unavailable"]
    fresh_through: str | None
    partial: bool
    detail: str = ""


class ContextResult(BaseModel):
    items: list[ContextItem]
    index: ContextIndexState


class ContextProvider(Protocol):
    def search(self, request: ContextQuery) -> ContextResult: ...


def _meeting_rows(conn: sqlite3.Connection, as_of: str | None) -> dict[str, sqlite3.Row]:
    sql = """
        SELECT id, meeting_date, minutes_path
        FROM meetings
        WHERE minutes_path IS NOT NULL
    """
    params: tuple[object, ...] = ()
    if as_of:
        sql += " AND meeting_date IS NOT NULL AND meeting_date <= ?"
        params = (as_of,)
    return {str(row["id"]): row for row in conn.execute(sql, params)}


def _source_ids(value: object) -> set[str]:
    return {part.strip() for part in str(value or "").split("|") if part.strip()}


def _query_words(query: str) -> list[str]:
    return [
        word
        for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", query.lower())
        if len(word) > 2 and word not in graph_sync.STOPWORDS
    ]


def _minute_excerpt(path: str, words: list[str], max_chars: int = 600) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    ranked = sorted(
        enumerate(blocks),
        key=lambda pair: (-sum(word in pair[1].lower() for word in words), pair[0]),
    )
    if not ranked or (words and not any(word in ranked[0][1].lower() for word in words)):
        return ""
    return ranked[0][1][:max_chars]


def _index_state(graph_available: bool, fresh_through: str | None) -> ContextIndexState:
    if not graph_available:
        return ContextIndexState(
            backend="manifest-fallback",
            status="partial",
            fresh_through=fresh_through,
            partial=True,
            detail="LightRAG graph unavailable; serving manifest-derived background only.",
        )
    try:
        health = index.document_health()
    except index.IndexError_ as exc:
        return ContextIndexState(
            backend="lightrag-postgres",
            status="partial",
            fresh_through=fresh_through,
            partial=True,
            detail=f"Graph available; document readiness unavailable: {exc}",
        )
    partial = bool(
        health.failed
        or health.active
        or health.recovery_required
        or health.documents_processed < health.documents_stored
    )
    return ContextIndexState(
        backend="lightrag-postgres",
        status="partial" if partial else "ready",
        fresh_through=fresh_through,
        partial=partial,
        detail=(
            f"{health.documents_processed}/{health.documents_stored} documents processed; "
            f"{health.vector_chunks_ready} have vector chunks; {health.failed} failed."
        ),
    )


def _bounded(items: list[ContextItem], limit: int, max_chars: int) -> list[ContextItem]:
    selected: list[ContextItem] = []
    used = 0
    for item in sorted(
        items,
        key=lambda value: (-(value.score), value.meeting_date or "", value.meeting_id, value.kind),
    ):
        if len(selected) >= limit:
            break
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(item.text) > remaining:
            if remaining < 40:
                break
            item = item.model_copy(update={"text": item.text[:remaining]})
        selected.append(item)
        used += len(item.text)
    return selected


class ManifestContextProvider:
    """Production adapter backed by the manifest and LightRAG graph traversal."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path
        self._health_cache: ContextIndexState | None = None

    def health(self) -> ContextIndexState:
        graph_available = bool(graph_sync.graph_labels(timeout_sec=2.0))
        with db.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(meeting_date) AS fresh_through FROM meetings "
                "WHERE minutes_path IS NOT NULL"
            ).fetchone()
        state = _index_state(graph_available, row["fresh_through"] if row else None)
        self._health_cache = state
        return state

    def _search_index_state(
        self,
        graph_available: bool,
        fresh_through: str | None,
    ) -> ContextIndexState:
        if not graph_available:
            return _index_state(False, fresh_through)
        if self._health_cache and self._health_cache.backend == "lightrag-postgres":
            return self._health_cache.model_copy(update={"fresh_through": fresh_through})
        # Document enumeration can take longer than the whole search budget.
        # Health refreshes it separately; an unprimed search reports the graph
        # conservatively instead of extending a request past its hard timeout.
        return ContextIndexState(
            backend="lightrag-postgres",
            status="partial",
            fresh_through=fresh_through,
            partial=True,
            detail="Graph available; document readiness not checked in bounded search.",
        )

    def search(self, request: ContextQuery) -> ContextResult:
        graph = graph_sync.retrieve_graph(
            request.query,
            max_nodes=max(8, min(24, request.limit * 3)),
            timeout_sec=CONTEXT_QUERY_TIMEOUT_SEC,
        )
        matched = list(graph.get("matched_labels") or [])
        graph_available = bool(graph.get("available"))

        with db.connect(self.db_path) as conn:
            meetings = _meeting_rows(conn, request.as_of)
            entities, _ = graph_sync.collect(conn)
            if not matched:
                matched = graph_sync._match_labels(request.query, list(entities))

            graph_names = {
                str(node.get("name") or "") for node in graph.get("nodes", []) if node.get("name")
            }
            relevant_names = set(matched) | graph_names
            relevant_meetings: set[str] = set()
            for node in graph.get("nodes", []):
                relevant_meetings.update(_source_ids(node.get("source_id")))
            for edge in graph.get("edges", []):
                relevant_meetings.update(_source_ids(edge.get("source_id")))

            items: list[ContextItem] = []
            for name in sorted(relevant_names):
                entity = entities.get(name)
                if not entity:
                    continue
                for meeting_id in sorted(entity["meetings"]):
                    row = meetings.get(meeting_id)
                    if not row:
                        continue
                    relevant_meetings.add(meeting_id)
                    description = " ".join(entity["descriptions"]).strip()
                    text = f"{name} ({entity['entity_type']})"
                    if description:
                        text += f": {description}"
                    items.append(
                        ContextItem(
                            kind="entity",
                            text=text,
                            meeting_id=meeting_id,
                            meeting_date=row["meeting_date"],
                            source_path=str(row["minutes_path"]),
                            score=1.0 if name in matched else 0.9,
                        )
                    )

            if relevant_names:
                for row in conn.execute(
                    "SELECT meeting_id, subject, predicate, object FROM relations ORDER BY meeting_id"
                ):
                    if row["subject"] not in relevant_names and row["object"] not in relevant_names:
                        continue
                    meeting = meetings.get(str(row["meeting_id"]))
                    if not meeting:
                        continue
                    relevant_meetings.add(str(row["meeting_id"]))
                    items.append(
                        ContextItem(
                            kind="relation",
                            text=f"{row['subject']} {row['predicate']} {row['object']}",
                            meeting_id=str(row["meeting_id"]),
                            meeting_date=meeting["meeting_date"],
                            source_path=str(meeting["minutes_path"]),
                            score=0.9,
                        )
                    )

            words = _query_words(request.query)
            for meeting_id in sorted(relevant_meetings):
                row = meetings.get(meeting_id)
                if not row:
                    continue
                excerpt = _minute_excerpt(str(row["minutes_path"]), words)
                if excerpt:
                    items.append(
                        ContextItem(
                            kind="minute_excerpt",
                            text=excerpt,
                            meeting_id=meeting_id,
                            meeting_date=row["meeting_date"],
                            source_path=str(row["minutes_path"]),
                            score=0.8,
                        )
                    )

        fresh_through = max(
            (item.meeting_date for item in items if item.meeting_date), default=None
        )
        return ContextResult(
            items=_bounded(items, request.limit, request.max_chars),
            index=self._search_index_state(graph_available, fresh_through),
        )


class InMemoryContextProvider:
    """Deterministic adapter for Product Manager and contract tests."""

    def __init__(self, result: ContextResult) -> None:
        self.result = result
        self.requests: list[ContextQuery] = []

    def search(self, request: ContextQuery) -> ContextResult:
        self.requests.append(request)
        items = [
            item
            for item in self.result.items
            if request.as_of is None or (item.meeting_date and item.meeting_date <= request.as_of)
        ]
        return self.result.model_copy(
            update={"items": _bounded(items, request.limit, request.max_chars)}
        )
