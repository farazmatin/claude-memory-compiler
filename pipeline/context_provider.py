"""Read-only, bounded context interface over the private meeting archive.

The provider deliberately emits background context rather than claims. Product
Manager owns QA status and evidence authority; this module only supplies
provenance-bearing minute excerpts and graph records through a stable contract.

`search` is hybrid retrieval over the compiled minutes:

1. BM25 over `minute_chunks` (`chunk_index.search_chunks`), 20 candidates.
2. Dense cosine over the same chunks (`dense_index.search_dense`), 20 more.
3. Reciprocal Rank Fusion of the two lists - on rank, never on score.
4. A cross-encoder pass over the fused candidates (`dense_index.rerank`).
5. The LightRAG graph, best-effort, on a 1.5s leash and off the critical path.

Every stage degrades to empty rather than raising and every reason lands in
`index.detail`, so BM25-only, dense-only and graph-only are all working modes
and an empty answer always says which one it was (GC5).

Four things about the retrieval this replaces, because most of them were wrong
rather than merely slow:

* **There was no relevance signal at all.** Entity items scored a fixed 1.0 or
  0.9, relations 0.9, minute excerpts 0.8, and the result was sorted by that
  score - so a verbatim excerpt could never outrank an entity summary, whatever
  the query said. A live search for "USC control inventory" returned five items,
  every one of them an entity. The excerpts are the thing an artifact quotes.
* **Excerpts came from a keyword scan of the minutes file**, one per meeting the
  graph happened to touch, so a meeting the graph never named was unreachable.
* **An entity's text was `" ".join(entity["descriptions"])`** - every meeting's
  description of that name, merged - and the merged blob was then emitted once
  per meeting, each copy citing that meeting alone. Every such citation
  attributed to one meeting words that were said in another (GC3).
* **The graph ran synchronously** with a 6s budget and was measured at 6.07s of
  a 6.44s request, against 0.007s for all of SQLite. It is also empty: LightRAG
  reports 0 of 129 documents processed, 129 failed.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pipeline import chunk_index, db, dense_index, graph_sync, index
from pipeline.chunk_index import ChunkHit

# The graph gets a leash rather than the request's whole budget. Measured on the
# real archive, `retrieve_graph` is 6.07s of a 6.44s request while SQLite is
# 0.007s and every file read together is 0.317s: it *is* the latency. It also
# cannot answer on its own, so an artifact request no longer waits on it. 1.5s
# is enough for a warm traversal and cheap enough that a cold or wedged one
# costs the caller almost nothing.
#
# `/api/query`'s `ask()` path is deliberately untouched and keeps the full
# traversal: a human asked a question there and is waiting for an answer.
GRAPH_TIMEOUT_SEC = 1.5

# Candidates drawn from each retrieval half before fusion. Deliberately larger
# than any `limit`: fusion, the rerank pass and the diversity rules all discard,
# and the pool has to still hold enough after they have.
CANDIDATE_LIMIT = 20

# Reciprocal Rank Fusion's damping constant, at the standard value from the
# original TREC work. Large relative to the list length, so the gap between
# rank 1 and rank 2 stays small and being found by both halves matters more than
# being found first by one.
RRF_K = 60

# Chunks kept after the cross-encoder pass. It costs a model pass per candidate,
# so it runs over the fused shortlist and its output is the excerpt pool the
# diversity rules then draw from. A caller asking for more than this gets a
# proportionally larger shortlist rather than a set padded out with entities.
RERANK_TOP_N = 8

# Excerpts admitted before the first entity summary is considered, when there
# are that many to admit. Entity summaries were structurally crowding out the
# verbatim detail an artifact needs; this is the floor that stops it.
EXCERPT_FLOOR = 2

# Items any one meeting may contribute, across every kind. Without it a single
# verbose meeting - and this corpus has 18,000-character minutes - fills the
# whole result and the background is one conversation.
MAX_ITEMS_PER_MEETING = 2

# Round-robin order once the excerpt floor is met. Excerpts first because they
# are the evidence; entity and relation records are the shape of the archive
# around them.
_KIND_ORDER = ("minute_excerpt", "entity", "relation")

# Both halves require a character budget and both trim a hit that will not fit.
# The GC4 budget belongs on the final selection, not on a candidate pool that is
# mostly discarded - and a trimmed candidate would be reranked on its own
# truncation. Sized so the pool never trims: twice the chunker's ceiling per
# candidate leaves room for the oversized chunks it deliberately allows.
_POOL_MAX_CHARS = CANDIDATE_LIMIT * chunk_index.MAX_CHUNK_CHARS * 2


class ContextQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4_000)
    limit: int = Field(default=8, ge=1, le=50)
    max_chars: int = Field(default=5_000, ge=256, le=20_000)
    as_of: str | None = None
    # Meetings the caller is already grounded in - in practice the one meeting an
    # artifact is being written from. The provider cannot know what grounds a
    # request, so the caller passes it and gets background that is genuinely
    # prior context rather than the source minutes read back at it. Bounded like
    # every other field: the exclusion becomes one SQL parameter per id.
    exclude_meeting_ids: frozenset[str] = Field(default_factory=frozenset, max_length=100)

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
    """Score-ordered bounding, for the in-memory adapter only.

    `ManifestContextProvider.search` uses `_select` instead: its candidates
    arrive in fused-and-reranked order, and re-sorting them by score would throw
    that ranking away and starve the excerpts all over again.
    """
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


# ── Fusion ────────────────────────────────────────────────────────────

def _fuse(lexical: list[ChunkHit], semantic: list[ChunkHit]) -> list[ChunkHit]:
    """The two candidate lists as one, ordered by Reciprocal Rank Fusion.

        score = Σ 1 / (RRF_K + rank within that list)

    Rank, not score, and that is not a stylistic preference. Dense cosines
    measured across four meetings of this corpus were 0.719, 0.699, 0.683 and
    0.682 - a 0.037 spread - while BM25's normalised scores over the same corpus
    run from 0.43 to 0.77. A weighted sum of the two would be BM25 plus a
    rounding error, and putting them on one axis honestly needs a labelled set
    nobody has. RRF needs neither: it only asks each half which of its own hits
    it liked best.

    Both halves trim to their character budget, so the same chunk can arrive
    twice at different lengths. Deduped on `chunk_id` keeping the longest text,
    because the reranker scores the text it is shown and a truncation is not the
    passage. BM25 is folded in first, so a chunk both halves found keeps the
    lexical score: it is calibrated in absolute terms, where a cosine that reads
    0.68 for an unrelated passage is not.
    """
    fused: dict[str, ChunkHit] = {}
    rrf: dict[str, float] = {}
    for hits in (lexical, semantic):
        for rank, hit in enumerate(hits, start=1):
            rrf[hit.chunk_id] = rrf.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            kept = fused.get(hit.chunk_id)
            if kept is None:
                fused[hit.chunk_id] = hit
            elif len(hit.text) > len(kept.text):
                fused[hit.chunk_id] = replace(kept, text=hit.text)
    return sorted(
        fused.values(),
        key=lambda hit: (
            -rrf[hit.chunk_id],
            hit.meeting_date or "",
            hit.meeting_id,
            hit.ordinal,
        ),
    )


def _excerpt_item(hit: ChunkHit) -> ContextItem:
    """One ranked chunk as a wire item.

    `heading` and `chunk_id` are dropped: the `ContextItem` shape is consumed by
    a sibling product and this rebuild does not change it. The heading is not
    lost with them - `chunk_index` folds the heading line into the text of any
    chunk that needs it to be read correctly.
    """
    return ContextItem(
        kind="minute_excerpt",
        text=hit.text,
        meeting_id=hit.meeting_id,
        meeting_date=hit.meeting_date,
        source_path=hit.source_path,
        score=hit.score,
    )


# ── Graph, best-effort ────────────────────────────────────────────────

class _GraphProbe:
    """One `retrieve_graph` call on a daemon thread, abandoned at the deadline.

    Two bounds, because `retrieve_graph`'s own `timeout_sec` is applied to each
    label's HTTP request in turn and does not bound the traversal as a whole:
    the timeout goes in so an abandoned call stops touching the network, and the
    join is what actually bounds `search`.

    Started at construction and joined last, so the leash overlaps the local
    work instead of following it. A daemon thread rather than a pooled one so a
    wedged traversal cannot hold the interpreter open at exit; nothing is shared
    with it - no connection, and its result comes back through two attributes -
    so abandoning it is safe.
    """

    def __init__(self, query: str, *, max_nodes: int) -> None:
        # Read from the module rather than defaulted into the signature, so the
        # leash is one constant a test can shorten and production cannot drift
        # from.
        self.timeout_sec = GRAPH_TIMEOUT_SEC
        self._graph: dict[str, Any] | None = None
        self._error: Exception | None = None
        self._started = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, args=(query, max_nodes), daemon=True
        )
        self._thread.start()

    def _run(self, query: str, max_nodes: int) -> None:
        try:
            self._graph = graph_sync.retrieve_graph(
                query, max_nodes=max_nodes, timeout_sec=self.timeout_sec
            )
        except Exception as exc:  # noqa: BLE001 - a read-only search must not raise
            self._error = exc

    def result(self) -> tuple[dict[str, Any], str]:
        """(graph, detail). An empty graph and a reason when it did not land."""
        self._thread.join(max(0.0, self.timeout_sec - (time.monotonic() - self._started)))
        if self._thread.is_alive():
            return {}, f"Graph traversal skipped: no answer within {self.timeout_sec:.1f}s."
        if self._error is not None:
            return {}, (
                "Graph traversal skipped: "
                f"{type(self._error).__name__}: {self._error}."
            )
        return self._graph or {}, ""


# ── Graph records, per-meeting ────────────────────────────────────────

def _entity_descriptions(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """(entity name, meeting id) -> the description that meeting actually gave it.

    GC3. `graph_sync.collect` folds every meeting's description of a name into
    one list, because a graph node wants exactly that. An emitted item is a
    citation of one meeting, and quoting the fold under a single meeting's id
    attributes to it words that were said in a different room. This re-reads the
    same rows without the fold, through graph_sync's own name resolution so that
    a `SPEAKER_02` row lands under the person `collect` would have named.

    Two rows can share one key only when resolution maps two raw names in the
    same meeting onto one person. Joining those is not the GC3 defect: they were
    both said in that meeting.
    """
    speakers = graph_sync._speaker_names(conn)
    descriptions: dict[tuple[str, str], list[str]] = {}
    for row in conn.execute(
        "SELECT meeting_id, name, description FROM entities ORDER BY meeting_id, name"
    ):
        name = graph_sync._resolve(row["name"], row["meeting_id"], speakers)
        description = (row["description"] or "").strip()
        if name is None or not description:
            continue
        bucket = descriptions.setdefault((name, str(row["meeting_id"])), [])
        if description not in bucket:
            bucket.append(description)
    return {key: " ".join(parts) for key, parts in descriptions.items()}


def _entity_items(
    conn: sqlite3.Connection,
    names: set[str],
    matched: set[str],
    entities: dict[str, dict[str, Any]],
    meetings: dict[str, sqlite3.Row],
    excluded: frozenset[str],
) -> list[ContextItem]:
    """One item per entity name, carrying one meeting's own description.

    One per name rather than one per (name, meeting) pair, which is what makes
    the GC3 fix possible: a name that five meetings described used to become
    five items whose text was identical and whose citations disagreed. The
    meeting chosen is the most recent that named it and is still eligible, since
    the last thing said about a control is the thing an artifact needs.
    """
    descriptions = _entity_descriptions(conn)
    items: list[ContextItem] = []
    for name in sorted(names):
        entity = entities.get(name)
        if not entity:
            continue
        eligible = sorted(
            (
                meeting_id
                for meeting_id in (str(value) for value in entity["meetings"])
                if meeting_id in meetings and meeting_id not in excluded
            ),
            key=lambda meeting_id: (meetings[meeting_id]["meeting_date"] or "", meeting_id),
        )
        if not eligible:
            continue
        meeting_id = eligible[-1]
        row = meetings[meeting_id]
        text = f"{name} ({entity['entity_type']})"
        description = descriptions.get((name, meeting_id), "")
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
    return sorted(items, key=lambda item: (-item.score, item.text))


def _relation_items(
    conn: sqlite3.Connection,
    names: set[str],
    meetings: dict[str, sqlite3.Row],
    excluded: frozenset[str],
) -> list[ContextItem]:
    """Relations touching a relevant name, each attributed to its own meeting row.

    Read straight from the table rather than from `graph_sync.collect`, whose
    relation list is deduped across meetings on (subject, predicate, object) in
    SQL order - which would attribute a repeated relation to whichever meeting
    the query planner happened to return first.
    """
    if not names:
        return []
    items: list[ContextItem] = []
    for row in conn.execute(
        "SELECT meeting_id, subject, predicate, object FROM relations "
        "ORDER BY meeting_id, subject, predicate, object"
    ):
        if row["subject"] not in names and row["object"] not in names:
            continue
        meeting_id = str(row["meeting_id"])
        if meeting_id in excluded:
            continue
        meeting = meetings.get(meeting_id)
        if not meeting:
            continue
        items.append(
            ContextItem(
                kind="relation",
                text=f"{row['subject']} {row['predicate']} {row['object']}",
                meeting_id=meeting_id,
                meeting_date=meeting["meeting_date"],
                source_path=str(meeting["minutes_path"]),
                score=0.9,
            )
        )
    return items


# ── Selection ─────────────────────────────────────────────────────────

def _fit(kind: str, text: str, remaining: int) -> str | None:
    """`text` for the remaining budget, or None when it cannot be served honestly."""
    if remaining <= 0:
        return None
    if kind == "minute_excerpt":
        # The same rule both retrieval halves apply, so a trimmed excerpt
        # carries the same ellipsis whichever path produced it and nothing
        # downstream quotes a severed sentence as if it were whole.
        return chunk_index.fit_excerpt(text, remaining)
    # An entity summary or a relation is one line that reads as a statement
    # about the meeting; half of "Atlas depends on Beacon" is a different
    # statement. Served whole or not at all.
    return text if len(text) <= remaining else None


def _select(candidates: list[ContextItem], limit: int, max_chars: int) -> list[ContextItem]:
    """The diversity rules and the GC4 budget over ranked candidates.

    Candidates arrive already ranked within their kind - excerpts in fused,
    reranked order - and this preserves that order. It does not re-sort by
    score, which is what starved the excerpts before: a single ordering by score
    cannot express "at least two excerpts" or "one of each kind", and with fixed
    scores per kind it could not express relevance either.

    Two phases. The excerpt floor first, so the verbatim detail gets first
    refusal on the opening slots; then a round-robin across the kinds, which is
    how an entity summary and a relation both reach a result that excerpts could
    otherwise fill. "When candidates allow" is what draining the queue means: if
    the excerpts run out, or every remaining one fails a cap, the other kinds
    take the slots rather than the result going short.
    """
    queues: dict[str, list[ContextItem]] = {kind: [] for kind in _KIND_ORDER}
    for candidate in candidates:
        queues[candidate.kind].append(candidate)

    selected: list[ContextItem] = []
    per_meeting: dict[str, int] = {}
    used = 0

    def admit(item: ContextItem) -> None:
        nonlocal used
        if per_meeting.get(item.meeting_id, 0) >= MAX_ITEMS_PER_MEETING:
            return
        text = _fit(item.kind, item.text, max_chars - used)
        if text is None:
            return
        if text != item.text:
            item = item.model_copy(update={"text": text})
        selected.append(item)
        per_meeting[item.meeting_id] = per_meeting.get(item.meeting_id, 0) + 1
        used += len(text)

    excerpts = queues["minute_excerpt"]
    while len(selected) < min(limit, EXCERPT_FLOOR) and excerpts:
        admit(excerpts.pop(0))

    while len(selected) < limit and any(queues.values()):
        for kind in _KIND_ORDER:
            if len(selected) >= limit:
                break
            if queues[kind]:
                admit(queues[kind].pop(0))
    return selected


class ManifestContextProvider:
    """Production adapter over the chunk indexes and the LightRAG graph."""

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
        graph_detail: str,
        fresh_through: str | None,
        retrieval_detail: str,
    ) -> ContextIndexState:
        if graph_available:
            if self._health_cache and self._health_cache.backend == "lightrag-postgres":
                base = self._health_cache.model_copy(update={"fresh_through": fresh_through})
            else:
                # Document enumeration can take longer than the whole search
                # budget. Health refreshes it separately; an unprimed search
                # reports the graph conservatively instead of extending a
                # request past its hard timeout.
                base = ContextIndexState(
                    backend="lightrag-postgres",
                    status="partial",
                    fresh_through=fresh_through,
                    partial=True,
                    detail="Graph available; document readiness not checked in bounded search.",
                )
        elif graph_detail:
            # Abandoned, not absent. Saying "unavailable" here would be a claim
            # about a server this request never waited long enough to judge.
            base = ContextIndexState(
                backend="manifest-fallback",
                status="partial",
                fresh_through=fresh_through,
                partial=True,
                detail=graph_detail,
            )
        else:
            base = _index_state(False, fresh_through)
        detail = " ".join(part for part in (base.detail, retrieval_detail) if part)
        return base.model_copy(update={"detail": detail})

    def search(self, request: ContextQuery) -> ContextResult:
        # Started before the local work and joined after it, so the graph's
        # best-effort window overlaps SQLite rather than following it.
        probe = _GraphProbe(request.query, max_nodes=max(8, min(24, request.limit * 3)))
        excluded = frozenset(request.exclude_meeting_ids)

        with db.connect(self.db_path) as conn:
            bm25_ready, bm25_reason = chunk_index.index_status(conn)
            dense_ready, dense_reason = dense_index.dense_status(conn)
            bounds = {
                "limit": CANDIDATE_LIMIT,
                "max_chars": _POOL_MAX_CHARS,
                "as_of": request.as_of,
                "exclude_meeting_ids": excluded,
            }
            fused = _fuse(
                chunk_index.search_chunks(conn, request.query, **bounds),
                dense_index.search_dense(conn, request.query, **bounds),
            )
            # The rerank pass runs on the fused candidates' full text; the
            # character budget is applied in _select, after the shortlist is
            # chosen, so nothing is scored on its own truncation.
            excerpts = [
                _excerpt_item(hit)
                for hit in dense_index.rerank(
                    request.query, fused, top_n=max(RERANK_TOP_N, request.limit)
                )
            ]

            graph, graph_detail = probe.result()
            matched = [str(label) for label in (graph.get("matched_labels") or [])]
            meetings = _meeting_rows(conn, request.as_of)
            entities, _relations = graph_sync.collect(conn)
            if not matched:
                # The graph is the better matcher when it answers. When it does
                # not, the same string matching runs against the manifest's own
                # entity names, which cost 0.007s and are always there.
                matched = graph_sync._match_labels(request.query, list(entities))
            names = {
                str(node.get("name") or "")
                for node in graph.get("nodes") or []
                if node.get("name")
            } | set(matched)
            names.discard("")

            items = _select(
                [
                    *excerpts,
                    *_entity_items(conn, names, set(matched), entities, meetings, excluded),
                    *_relation_items(conn, names, meetings, excluded),
                ],
                request.limit,
                request.max_chars,
            )

        fresh_through = max(
            (item.meeting_date for item in items if item.meeting_date), default=None
        )
        state = self._search_index_state(
            bool(graph.get("available")),
            graph_detail,
            fresh_through,
            f"BM25: {bm25_reason}. Dense: {dense_reason}.",
        )
        if not items and not bm25_ready and not dense_ready:
            # Nothing was served and neither half could have served anything.
            # "Nothing matched" and "nothing is built" are different answers and
            # only one of them is actionable (GC5).
            state = state.model_copy(update={"status": "unavailable"})
        return ContextResult(items=items, index=state)


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
