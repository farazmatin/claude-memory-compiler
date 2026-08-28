"""Push the manifest's entities and relations straight into LightRAG's graph.

LightRAG can discover a graph through a model-backed document route, but this
build does not use it. Earlier document attempts failed asynchronously while the
index stage treated enqueue acknowledgements as completion.

Re-running that extraction is not the fix. The pipeline has already extracted
this graph once, with a frontier model on the user's subscription, during the
minutes stage. We author those records directly; LightRAG is storage and graph
traversal only, and no model-backed LightRAG route is part of this build.

The manifest stays the source of truth: a full rebuild is one command, and
nothing here is load-bearing for the minutes themselves.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from pipeline import db
from pipeline.config import LIGHTRAG_API_KEY, LIGHTRAG_URL

# One HTTP round trip per entity and per relation, against a loopback service.
REQUEST_TIMEOUT_SEC = 120.0


class GraphSyncError(RuntimeError):
    """Raised when LightRAG rejects a graph write."""


# Outcome of one graph write.
#
# DROPPED exists because "refused" and "failed" are not the same thing. LightRAG's
# naming contract filters digits-and-dots tokens, so an extracted entity named
# "1.0" is refused with HTTP 400 on every run, forever. Counting that as a write
# failure wedged publication for the whole corpus: once every other entity
# already exists, `entities_written` is 0 on every later run, so the "wrote
# nothing" guard fired on the one permanent refusal and eight meetings sat at
# minutes_compiled with their minutes already written to disk.
WRITTEN = "written"
DUPLICATE = "duplicate"
DROPPED = "dropped"
FAILED = "failed"

# Refusals a later run cannot fix, because the payload itself is unacceptable.
# 401/403/429 are deliberately absent: a dead key or a rate limit is recoverable,
# and silently dropping those would publish a corpus with holes in it.
_PERMANENT_REFUSAL = frozenset({400, 409, 422})


@dataclass
class SyncReport:
    entities_written: int = 0
    entities_skipped: int = 0
    entities_dropped: int = 0
    relations_written: int = 0
    relations_skipped: int = 0
    relations_dropped: int = 0
    errors: list[str] = field(default_factory=list)
    drops: list[str] = field(default_factory=list)

    def summary(self) -> str:
        line = (
            f"{self.entities_written} entities, {self.relations_written} relations written; "
            f"{self.entities_skipped} entities and {self.relations_skipped} relations skipped"
        )
        dropped = self.entities_dropped + self.relations_dropped
        if dropped:
            line += f"; {dropped} unpublishable record(s) dropped"
        if self.errors:
            line += f"; {len(self.errors)} error(s)"
        return line


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if LIGHTRAG_API_KEY:
        headers["X-API-Key"] = LIGHTRAG_API_KEY
    return headers


def _speaker_names(conn: sqlite3.Connection) -> dict[str, str]:
    """Resolved diarization labels, keyed "<meeting_id>:<label>".

    Entity extraction runs on minutes that still carry raw SPEAKER_00 labels
    wherever naming failed, so those labels reach the entity table as if they
    were people. Anything still unresolved is deliberately absent here and gets
    dropped rather than published: "SPEAKER_00" is not a person, and a graph
    node by that name merges unrelated strangers across every meeting.
    """
    names: dict[str, str] = {}
    for row in conn.execute(
        "SELECT meeting_id, label, name FROM speakers WHERE name IS NOT NULL AND name <> ''"
    ):
        names[f"{row['meeting_id']}:{row['label']}"] = str(row["name"]).strip()
    return names


def _resolve(name: str, meeting_id: str, speakers: dict[str, str]) -> str | None:
    """Map a raw entity name to what should appear in the graph, or None to drop."""
    clean = (name or "").strip()
    if not clean:
        return None
    if clean.upper().startswith("SPEAKER_"):
        return speakers.get(f"{meeting_id}:{clean}")
    return clean


def collect(conn: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Fold the per-meeting entity and relation rows into graph-shaped records.

    Entities are per-meeting in the manifest, so the same person appears once per
    meeting they were mentioned in. The graph wants one node carrying every
    description and every meeting it came from, which is also what makes the node
    worth retrieving.
    """
    speakers = _speaker_names(conn)

    entities: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT meeting_id, name, kind, description FROM entities"):
        resolved = _resolve(row["name"], row["meeting_id"], speakers)
        if resolved is None:
            continue
        bucket = entities.setdefault(
            resolved,
            {"entity_type": row["kind"] or "other", "descriptions": [], "meetings": set()},
        )
        bucket["meetings"].add(row["meeting_id"])
        description = (row["description"] or "").strip()
        if description and description not in bucket["descriptions"]:
            bucket["descriptions"].append(description)
        # "other" is the extractor's fallback, so let any specific kind win.
        if bucket["entity_type"] == "other" and row["kind"]:
            bucket["entity_type"] = row["kind"]

    seen: set[tuple[str, str, str]] = set()
    relations: list[dict[str, Any]] = []
    for row in conn.execute("SELECT meeting_id, subject, predicate, object FROM relations"):
        subject = _resolve(row["subject"], row["meeting_id"], speakers)
        obj = _resolve(row["object"], row["meeting_id"], speakers)
        predicate = (row["predicate"] or "").strip()
        if not subject or not obj or not predicate or subject == obj:
            continue
        # A relation is only retrievable if both endpoints exist as nodes.
        if subject not in entities or obj not in entities:
            continue
        key = (subject, predicate, obj)
        if key in seen:
            continue
        seen.add(key)
        relations.append(
            {
                "source": subject,
                "target": obj,
                "predicate": predicate,
                "meeting_id": row["meeting_id"],
            }
        )

    return entities, relations


def _post(client: httpx.Client, path: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Write one record. Returns the outcome and a detail string for reporting."""
    try:
        resp = client.post(
            f"{LIGHTRAG_URL}{path}", headers=_headers(), json=payload, timeout=REQUEST_TIMEOUT_SEC
        )
    except httpx.HTTPError as exc:
        return FAILED, f"{type(exc).__name__}: {exc}"
    if resp.status_code == 200:
        return WRITTEN, ""
    # A duplicate is not a failure: re-running the sync must be safe.
    body = resp.text[:200]
    if resp.status_code in (400, 409) and "exist" in body.lower():
        return DUPLICATE, ""
    if resp.status_code in _PERMANENT_REFUSAL:
        return DROPPED, f"HTTP {resp.status_code}: {body}"
    return FAILED, f"HTTP {resp.status_code}: {body}"


def sync(conn: sqlite3.Connection | None = None) -> SyncReport:
    """Write every manifest entity and relation into the LightRAG graph."""
    if conn is None:
        with db.connect() as owned:
            return sync(owned)

    report = SyncReport()
    entities, relations = collect(conn)
    with httpx.Client() as client:
        for name, data in entities.items():
            payload = {
                "entity_name": name,
                "entity_data": {
                    "entity_type": data["entity_type"],
                    "description": " ".join(data["descriptions"])[:2000]
                    or f"{data['entity_type']} mentioned in the meeting record",
                    "source_id": "|".join(sorted(data["meetings"])),
                },
            }
            outcome, detail = _post(client, "/graph/entity/create", payload)
            if outcome == WRITTEN:
                report.entities_written += 1
            elif outcome == DROPPED:
                report.entities_dropped += 1
                report.drops.append(f"entity {name}: {detail}")
            else:
                report.entities_skipped += 1
                if detail:
                    report.errors.append(f"entity {name}: {detail}")

        for rel in relations:
            payload = {
                "source_entity": rel["source"],
                "target_entity": rel["target"],
                "relation_data": {
                    "description": f"{rel['source']} {rel['predicate']} {rel['target']}",
                    "keywords": rel["predicate"],
                    "weight": 1.0,
                    "source_id": rel["meeting_id"],
                },
            }
            outcome, detail = _post(client, "/graph/relation/create", payload)
            label = f"relation {rel['source']}->{rel['target']}"
            if outcome == WRITTEN:
                report.relations_written += 1
            elif outcome == DROPPED:
                report.relations_dropped += 1
                report.drops.append(f"{label}: {detail}")
            else:
                report.relations_skipped += 1
                if detail:
                    report.errors.append(f"{label}: {detail}")

    return report


def graph_labels(timeout_sec: float = 30.0) -> list[str]:
    """Entity labels currently in the graph. Empty means retrieval is dead."""
    try:
        resp = httpx.get(
            f"{LIGHTRAG_URL}/graph/label/list",
            headers=_headers(),
            timeout=timeout_sec,
        )
        resp.raise_for_status()
        labels = resp.json()
        return labels if isinstance(labels, list) else []
    except httpx.HTTPError:
        return []


# ── Retrieval ─────────────────────────────────────────────────────────
# LightRAG's own /query path is model-backed, so this build does not call it.
# The graph itself answers a traversal directly. We retrieve here - label match,
# then subgraph - and leave synthesis to the subscription-backed providers in
# pipeline.llm.

STOPWORDS = {
    "what",
    "when",
    "who",
    "why",
    "how",
    "which",
    "where",
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "do",
    "does",
    "did",
    "we",
    "i",
    "you",
    "they",
    "it",
    "of",
    "on",
    "in",
    "to",
    "for",
    "and",
    "or",
    "about",
    "with",
    "that",
    "this",
    "there",
    "any",
    "some",
    "our",
    "my",
    "me",
    "be",
    "been",
    "have",
    "has",
    "had",
    "decided",
    "decision",
    "decisions",
    "made",
    "discuss",
    "discussed",
    "said",
    "tell",
    "show",
    "give",
    "list",
    "summary",
    "summarise",
    "summarize",
    "recent",
    "recently",
    "last",
    "week",
    "meeting",
    "meetings",
}


def _match_labels(question: str, labels: list[str], limit: int = 4) -> list[str]:
    """Entity labels a question is plausibly about, best first.

    Deliberately dumb string matching rather than an LLM call: an exact or
    substring hit on a proper noun is what actually carries a question like
    "what is Yulia responsible for", and it costs nothing.
    """
    words = {w.strip(".,?!'\"").lower() for w in question.split()}
    words = {w for w in words if len(w) > 2 and w not in STOPWORDS}
    if not words:
        return []

    scored: list[tuple[int, int, str]] = []
    for label in labels:
        low = label.lower()
        tokens = {t for t in low.replace("-", " ").split() if t}
        exact = len(words & tokens)
        partial = sum(1 for w in words if w in low)
        if exact or partial:
            # Prefer a whole-token hit, then a shorter label: "USC" beats
            # "USC roadmap discussion" for the query "USC".
            scored.append((exact, partial, label))
    scored.sort(key=lambda s: (-s[0], -s[1], len(s[2])))
    return [label for _, _, label in scored[:limit]]


def _subgraph(
    client: httpx.Client,
    label: str,
    max_nodes: int = 24,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    try:
        resp = client.get(
            f"{LIGHTRAG_URL}/graphs",
            headers=_headers(),
            params={"label": label, "max_depth": 2, "max_nodes": max_nodes},
            timeout=timeout_sec,
        )
        return resp.json() if resp.status_code == 200 else {}
    except httpx.HTTPError:
        return {}


def retrieve_graph(
    question: str,
    max_nodes: int = 24,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """Return one normalized graph traversal without invoking an LLM.

    This is the retrieval seam shared by answer synthesis and the local context
    API. Keeping label matching and traversal here prevents the two consumers
    from developing subtly different rankings for the same question.
    """
    deadline = time.monotonic() + max(0.1, timeout_sec)
    labels = graph_labels(timeout_sec=min(30.0, timeout_sec))
    if not labels:
        return {"available": False, "matched_labels": [], "nodes": [], "edges": []}
    matched = _match_labels(question, labels)
    if not matched:
        return {"available": True, "matched_labels": [], "nodes": [], "edges": []}

    seen_nodes: dict[str, dict[str, Any]] = {}
    seen_edges: set[tuple[str, str, str]] = set()
    edges: list[dict[str, Any]] = []

    with httpx.Client() as client:
        for label in matched:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            data = _subgraph(
                client,
                label,
                max_nodes=max_nodes,
                timeout_sec=max(0.1, remaining),
            )
            for node in data.get("nodes", []):
                props = node.get("properties", node)
                name = str(props.get("entity_id") or props.get("id") or "").strip()
                desc = str(props.get("description") or "").strip()
                if name and name not in seen_nodes:
                    seen_nodes[name] = {
                        "name": name,
                        "description": desc,
                        "source_id": str(props.get("source_id") or ""),
                    }
            for edge in data.get("edges", []):
                props = edge.get("properties", {})
                src = str(edge.get("source") or "").strip()
                dst = str(edge.get("target") or "").strip()
                rel = str(props.get("keywords") or props.get("description") or "relates to")
                key = (src, rel, dst)
                if src and dst and key not in seen_edges:
                    seen_edges.add(key)
                    edges.append(
                        {
                            "source": src,
                            "target": dst,
                            "relationship": rel,
                            "source_id": str(props.get("source_id") or ""),
                        }
                    )

    return {
        "available": True,
        "matched_labels": matched,
        "nodes": list(seen_nodes.values()),
        "edges": edges,
    }


def retrieve_context(question: str, max_chars: int = 6000) -> str:
    """Graph context for a question, or "" when nothing matches.

    Returned as plain prose because that is what the synthesis prompt consumes;
    the caller still supplies the minutes themselves, so this adds the shape of
    the record - who owns what, what depends on what - rather than replacing it.
    """
    graph = retrieve_graph(question)
    matched = graph["matched_labels"]
    if not matched:
        return ""

    if not graph["nodes"] and not graph["edges"]:
        return ""

    parts = [f"## Knowledge graph (matched: {', '.join(matched)})", "", "### Entities"]
    parts.extend(
        f"- **{node['name']}**: {node['description']}"
        for node in graph["nodes"]
        if node["description"]
    )
    if graph["edges"]:
        parts += ["", "### Relationships"]
        parts.extend(
            f"- {edge['source']} {edge['relationship']} {edge['target']}" for edge in graph["edges"]
        )
    return "\n".join(parts)[:max_chars]
