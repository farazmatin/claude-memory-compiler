"""Commitments, decisions and open questions emitted by the minutes compiler.

Three sections of every meeting's minutes exist only as prose today: 279 action
items across the corpus, decisions in 41 of 45 meetings, open questions in 36.
Nothing has ever parsed them - "what did I commit to?" and "what did we decide
and why?" have no answer short of grepping 45 markdown files by hand. This module
is the shared parser behind both the commitment register and the decision store;
compile_minutes.compile_meeting calls it once, alongside entities.extract, and
persists all three tables in the same pass.

Same reasoning as entities.py, and deliberately reusing its `_section` helper
rather than a second copy of it: models produce these sections with the same
kind of loose, inconsistent formatting they produce entities with, so a tolerant
line parser that recovers most of a messy block beats a strict one that discards
all of a slightly-malformed one. See templates/minutes.md for the shapes this is
parsing.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from pipeline.entities import _section

# "- [ ] **owner** — task. Due: date. [ts]" - the Action Items shape. Owner is
# always bold in the corpus; the separator after it varies (em dash, en dash,
# hyphen, colon) the same way entities.py tolerates several separators.
_ACTION_LINE = re.compile(
    r"^[-*]\s*\[(?P<mark>[ xX])\]\s*\*\*(?P<owner>[^*]+?)\*\*\s*[-–—:]\s*(?P<body>.+)$"
)

# "- **what was decided** — decided by X. Rationale: why. [ts]"
_DECISION_LINE = re.compile(r"^[-*]\s*\*\*(?P<what>[^*]+?)\*\*\s*[-–—:]\s*(?P<body>.+)$")

# One or more trailing "[...]" citation groups, e.g. "[0:04:50]" or
# "[0:15:05] [0:16:57]". Matched generically rather than validated against an
# H:MM:SS shape, because "[?:??]" (an unaligned turn) is a legitimate citation
# too - see asr.format_timestamp.
_TRAILING_CITE = re.compile(r"^(?P<rest>.*?)\s*(?P<cite>(?:\[[^\[\]]*\]\s*)+)$", re.DOTALL)

_DUE_CLAUSE = re.compile(r"^(?P<task>.*?)\s*Due:\s*(?P<due>.+?)\.?\s*$", re.IGNORECASE | re.DOTALL)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

_RATIONALE_SPLIT = re.compile(r"\bRationale:\s*", re.IGNORECASE)
# Attribution prose varies more than any other field in this template - "decided
# by X", "decided jointly by X and Y", "proposed by X and accepted by Y", "agreed
# by X", "stated by X" are all real examples from this corpus. Rather than
# enumerate every verb, take whatever proper-noun-looking text follows the last
# "by" in the clause; requiring an initial capital keeps it off of "decided by
# the meeting group, led by Unknown speaker ...", which should attribute to the
# named person, not "the meeting group".
_DECIDED_BY = re.compile(r"\bby\s+(?P<who>[A-Z][^.,]*?)(?=[.,]|$)")

# The Open Questions template line is "Question, and who needs to resolve it" -
# but in practice most questions in this corpus never name a resolver at all.
# Only the literal template shape is trusted for an owner; anything else is
# left unattributed rather than guessed at.
_RESOLVER = re.compile(r"(?P<who>[A-Z][\w'&/.\s-]*?)\s+needs?\s+to\s+resolve\s+(?:this|it|that)\b")

# Connectors this corpus uses between names in a compound attribution: "X and
# Y", "X, Y, and Z", "X & Y", "X / Y". Splitting on these is what lets
# canonicalization reach "Faraz Matin and Yuliya" -> "Faraz and Yuliya" - a
# whole-string alias lookup only ever matches a single registered name verbatim.
_NAME_SPLIT = re.compile(r"\s*,\s*(?:and\s+)?|\s+and\s+|\s*[&/]\s*")


def _strip_trailing_citations(text: str) -> tuple[str, str | None]:
    """Split "...task. Due: date. [0:04:50]" into (task, "[0:04:50]").

    Citations are stored verbatim, never parsed - a range like
    "[0:19:49-0:20:12]" or the unaligned placeholder "[?:??]" are both valid
    and neither should be forced into a stricter shape than the transcript
    actually produced.
    """
    stripped = text.strip()
    match = _TRAILING_CITE.match(stripped)
    if not match:
        return stripped, None
    return match.group("rest").strip(), match.group("cite").strip()


def _split_due(body: str) -> tuple[str, str | None]:
    """Split "task. Due: 2026-08-14" into ("task.", "2026-08-14")."""
    match = _DUE_CLAUSE.match(body)
    if not match:
        return body.strip(), None
    return match.group("task").strip(), match.group("due").strip()


def _due_date_iso(due_raw: str | None) -> str | None:
    """First ISO date literally present in the due-date prose, or None.

    "2026-08-14 (40 minutes after standup)" and "before 2026-08-17" both carry a
    real date buried in freeform text; "unspecified" carries none. Never raises:
    a malformed date fails isoformat and is treated the same as no date at all,
    rather than crashing the compile over a field nothing depends on for
    correctness.
    """
    if not due_raw:
        return None
    found = _ISO_DATE.search(due_raw)
    if not found:
        return None
    try:
        return date.fromisoformat(found.group()).isoformat()
    except ValueError:
        return None


def parse_action_items(document: str) -> list[dict[str, object]]:
    """Extract the Action Items section as commitments.

    `- [ ]` is open, `- [x]` is done - checkbox state the model already writes,
    not something inferred.
    """
    found: list[dict[str, object]] = []
    for line in _section(document, "Action Items").splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith(("-", "*")):
            continue
        match = _ACTION_LINE.match(stripped)
        if not match:
            continue
        body, cite = _strip_trailing_citations(match.group("body"))
        task, due_raw = _split_due(body)
        if not task:
            continue
        found.append(
            {
                "owner": match.group("owner").strip().strip("*_`") or None,
                "text": task,
                "due_date": due_raw,
                "due_date_iso": _due_date_iso(due_raw),
                "timestamp_cite": cite,
                "state": "done" if match.group("mark").lower() == "x" else "open",
            }
        )
    return found


def parse_decisions(document: str) -> list[dict[str, object]]:
    """Extract the Decisions section."""
    found: list[dict[str, object]] = []
    for line in _section(document, "Decisions").splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith(("-", "*")):
            continue
        match = _DECISION_LINE.match(stripped)
        if not match:
            continue
        text = match.group("what").strip().strip("*_`")
        if not text:
            continue
        body, cite = _strip_trailing_citations(match.group("body"))
        parts = _RATIONALE_SPLIT.split(body, maxsplit=1)
        attribution = parts[0].strip()
        rationale = parts[1].strip() if len(parts) > 1 else None
        who_match = _DECIDED_BY.search(attribution)
        found.append(
            {
                "text": text,
                "decided_by": who_match.group("who").strip() if who_match else None,
                "rationale": rationale,
                "timestamp_cite": cite,
            }
        )
    return found


def parse_open_questions(document: str) -> list[dict[str, object]]:
    """Extract the Open Questions section."""
    found: list[dict[str, object]] = []
    for line in _section(document, "Open Questions").splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith(("-", "*")):
            continue
        body, cite = _strip_trailing_citations(stripped[1:].strip())
        if not body:
            continue
        who_match = _RESOLVER.search(body)
        found.append(
            {
                "text": body,
                "owner": who_match.group("who").strip() if who_match else None,
                "timestamp_cite": cite,
            }
        )
    return found


def extract(document: str) -> dict[str, list[dict[str, object]]]:
    """Parse all three sections out of a compiled minutes document."""
    return {
        "commitments": parse_action_items(document),
        "decisions": parse_decisions(document),
        "open_questions": parse_open_questions(document),
    }


def _canonicalize_compound(conn, raw: str) -> str:
    """Canonicalize every name in a possibly-compound attribution.

    An attribution clause routinely names more than one person - "decided
    jointly by Faraz Matin and Yuliya" - and db.canonical_name only ever
    matches a whole string against a registered alias verbatim, so it cannot
    fold the "Faraz Matin" half of that pair on its own. Splitting on the
    connectors this corpus actually uses and canonicalizing each piece
    independently gets both without registering every combination as its own
    alias. A non-person fragment like "Neil's team" or "the meeting group"
    matches no alias and passes through unchanged, same as canonical_name
    would do for it whole.
    """
    from pipeline import db

    parts = [p.strip() for p in _NAME_SPLIT.split(raw.strip()) if p.strip()]
    if not parts:
        return raw
    resolved = [db.canonical_name(conn, p) or p for p in parts]
    return " and ".join(_normalize_unresolved(p) for p in resolved)


# The template asks for `Unknown speaker (SPEAKER_xx)` and the model also writes a
# bare `SPEAKER_xx`, so the same anonymous person arrived under two owner strings
# and grouped as two people - measured on the real corpus, SPEAKER_00 alone split
# 11/5 across the two forms. Reduce both to the label, which is the only part that
# actually identifies anyone, so that naming the label later fixes every row at once.
_UNRESOLVED_LABEL = re.compile(
    r"^(?:unknown\s+speaker\s*\(\s*)?(SPEAKER_\d+)\s*\)?$", re.I
)


def _normalize_unresolved(name: str) -> str:
    match = _UNRESOLVED_LABEL.match(name.strip())
    return match.group(1).upper() if match else name


def canonicalize(
    conn, parsed: dict[str, list[dict[str, object]]]
) -> dict[str, list[dict[str, object]]]:
    """Normalize owner / decided_by names through the people registry.

    Same idiom as entities.canonicalize: "Faraz Matin" and "Faraz" must collapse
    to one identity here for the same reason they must in the knowledge graph.
    """
    for item in parsed["commitments"]:
        if item.get("owner"):
            item["owner"] = _canonicalize_compound(conn, item["owner"])
    for item in parsed["decisions"]:
        if item.get("decided_by"):
            item["decided_by"] = _canonicalize_compound(conn, item["decided_by"])
    for item in parsed["open_questions"]:
        if item.get("owner"):
            item["owner"] = _canonicalize_compound(conn, item["owner"])
    return parsed


def backfill_from_disk(conn) -> dict[str, int]:
    """Populate commitments/decisions/open_questions for every meeting that
    already has minutes written to disk.

    For meetings compiled before these tables existed - at introduction, all of
    them. Never calls the LLM: it re-parses text a frontier model already
    produced, the same reasoning that makes `pipeline minutes --recompile`
    unnecessary just to pick up a template addition that did not change what the
    model was asked to say. Safe to run repeatedly - replace_commitments and
    friends replace each meeting's rows rather than duplicating them.
    """
    from pipeline import db

    counts = {"meetings": 0, "commitments": 0, "decisions": 0, "open_questions": 0}
    rows = conn.execute(
        "SELECT id, minutes_path FROM meetings WHERE minutes_path IS NOT NULL"
    ).fetchall()
    for row in rows:
        path = Path(row["minutes_path"])
        if not path.is_file():
            continue
        document = path.read_text(encoding="utf-8")
        parsed = canonicalize(conn, extract(document))
        db.replace_commitments(conn, row["id"], parsed["commitments"])
        db.replace_decisions(conn, row["id"], parsed["decisions"])
        db.replace_open_questions(conn, row["id"], parsed["open_questions"])
        counts["meetings"] += 1
        counts["commitments"] += len(parsed["commitments"])
        counts["decisions"] += len(parsed["decisions"])
        counts["open_questions"] += len(parsed["open_questions"])
    return counts
