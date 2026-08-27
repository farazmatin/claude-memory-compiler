"""Entities and relations emitted by the minutes compiler.

The minutes compiler runs on the subscription provider chain once per meeting,
so it emits entities and relations explicitly rather than asking a model-backed
LightRAG route to rediscover them.
Two things then happen:

1. They are stored in the manifest, independent of LightRAG. The corpus is never
   hostage to the index.
2. They can be published directly to the LightRAG graph without a second model
   call.

Format is deliberately line-based rather than JSON. Models emit stray prose around
JSON often enough that a tolerant line parser recovers more of the output than a
strict parse that throws the whole block away.
"""

from __future__ import annotations

import re

# Recognised entity kinds. Anything else is normalized to "other" rather than
# rejected - an unexpected kind is still a real entity.
KINDS = frozenset({"person", "feature", "customer", "release", "team", "metric", "other"})

_ENTITY_LINE = re.compile(
    r"^\s*[-*]?\s*(?P<name>[^|:(]+?)\s*[(\[]\s*(?P<kind>[\w -]+?)\s*[)\]]\s*"
    r"(?:[:\-–]\s*(?P<description>.+))?$"
)
_RELATION_LINE = re.compile(
    r"^\s*[-*]?\s*(?P<subject>.+?)\s*(?:->|→|\|)\s*(?P<predicate>.+?)\s*"
    r"(?:->|→|\|)\s*(?P<object>.+?)\s*$"
)


def normalize_kind(raw: str | None) -> str:
    if not raw:
        return "other"
    cleaned = raw.strip().lower().replace(" ", "_")
    return cleaned if cleaned in KINDS else "other"


def _section(document: str, heading: str) -> str:
    """Body of a markdown section, up to the next heading of any level."""
    pattern = re.compile(
        rf"^#{{1,6}}\s*{re.escape(heading)}\s*$(?P<body>.*?)(?=^#{{1,6}}\s|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(document)
    return match.group("body").strip() if match else ""


def parse_entities(document: str) -> list[dict[str, str]]:
    """Extract the Entities section.

    Accepts `Name (kind): description`, with `-` bullets, `[]` brackets, and an
    optional description - because that is the range of shapes models actually
    produce for the same instruction.
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    for line in _section(document, "Entities").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ">", "|")):
            continue
        match = _ENTITY_LINE.match(stripped)
        if not match:
            continue
        name = match.group("name").strip().strip("*_`")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        found.append(
            {
                "name": name,
                "kind": normalize_kind(match.group("kind")),
                "description": (match.group("description") or "").strip(),
            }
        )
    return found


def parse_relations(document: str) -> list[dict[str, str]]:
    """Extract the Relations section as subject -> predicate -> object triples."""
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for line in _section(document, "Relations").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ">")):
            continue
        match = _RELATION_LINE.match(stripped)
        if not match:
            continue
        triple = tuple(
            match.group(part).strip().strip("*_`")
            for part in ("subject", "predicate", "object")
        )
        if not all(triple) or triple in seen:
            continue
        seen.add(triple)  # type: ignore[arg-type]
        found.append({"subject": triple[0], "predicate": triple[1], "object": triple[2]})
    return found


def canonicalize(
    conn,
    entities: list[dict[str, str]],
    relations: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Normalize person names through the people registry.

    Applied to entities and to both ends of every relation, so "Mike" and "Michael"
    collapse to one node instead of two disconnected ones.
    """
    from pipeline import db

    for entity in entities:
        if entity.get("kind") == "person":
            entity["name"] = db.canonical_name(conn, entity["name"]) or entity["name"]

    known = {e["name"].lower(): e["name"] for e in entities if e.get("kind") == "person"}
    for relation in relations:
        for end in ("subject", "object"):
            lowered = relation[end].lower()
            if lowered in known:
                relation[end] = known[lowered]
            else:
                relation[end] = db.canonical_name(conn, relation[end]) or relation[end]

    return entities, relations


def render_for_index(
    entities: list[dict[str, str]], relations: list[dict[str, str]]
) -> str:
    """A labelled block appended to the indexed document.

    Stated outright rather than left implicit in prose: this is the whole point of
    emitting entities upstream, because a small extraction model succeeds at reading
    an explicit list and fails at discovering the same facts from narrative.
    """
    if not entities and not relations:
        return ""

    lines = ["", "## Knowledge Graph", ""]
    if entities:
        lines.append("Entities:")
        lines += [
            f"- {e['name']} ({e.get('kind', 'other')})"
            + (f": {e['description']}" if e.get("description") else "")
            for e in entities
        ]
        lines.append("")
    if relations:
        lines.append("Relations:")
        lines += [
            f"- {r['subject']} -> {r['predicate']} -> {r['object']}" for r in relations
        ]
        lines.append("")
    return "\n".join(lines)


def extract(document: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Parse both sections out of a compiled minutes document."""
    return parse_entities(document), parse_relations(document)
