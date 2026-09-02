"""Contextual headers for the chunks both retrieval halves serve.

A chunk is a passage lifted out of the middle of a document, and on its own it
is close to meaningless - "he said push it to Q3" names neither the person, the
initiative, nor the meeting. Neither half of retrieval can reach it: BM25 has no
term to match and the embedder places it nowhere near a question that names the
programme. `minute_chunks.context_header` is the column that fixes that, and
this module is what writes it.

Both indexes already consume it. `dense_index._embedding_text` prefixes the
header to the chunk before embedding, and `minute_chunks_fts` carries it as its
own weighted column, so populating it changes retrieval with no further wiring.

Four properties are load-bearing:

* **A header sits on the chunk it describes.** One call covers a whole meeting
  and returns one clause per passage, matched by ordinal. A count mismatch is
  therefore the failure that matters: accepting a short or long answer would
  shift every header after it onto the wrong passage, and since a header is
  served as evidence with a citation attached, nothing downstream could ever
  detect it. A mismatch fails the meeting and leaves its headers NULL.
* **A header describes; it never adds.** The provenance half - the meeting date
  and title - is composed here from the manifest, so the model is never asked
  for a fact it could invent. Its job is one clause about the passage in front
  of it.
* **The hash is the invalidation.** A header is stored with the
  `content_hash` of the text it describes. Text that has moved on leaves the
  header stale, and stale headers are regenerated rather than trusted.
* **A run is resumable.** One call per meeting, committed per meeting. A
  completed meeting costs nothing to re-run, so an interrupted 117-meeting job
  is restarted by re-issuing the same command.

No metered API. The call goes through `llm.complete`, which is the subscription
CLI chain, and the `complete` seam exists so tests can substitute a fake.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from pipeline import llm, titles
from pipeline.config import now_iso

# ── The contract ──────────────────────────────────────────────────────

# A header is metadata prefixed to a passage, not prose in its own right. Past
# this length it stops being a label: it crowds the passage in the embedded text
# and drowns the body's own terms in the FTS index.
HEADER_MAX_CHARS = 200

# The composed provenance prefix takes what it needs from this and no more, so a
# meeting with a 140-character title cannot leave the model's clause nothing to
# occupy. Real titles in the corpus run 30-90 characters.
TITLE_MAX_CHARS = 70

# Floor on what is left for the clause after the prefix. Reached only by a title
# long enough that the prefix would otherwise eat the whole budget; the prefix is
# what gives way, not the description.
CLAUSE_MIN_CHARS = 90

# Ceiling on one prompt, in characters rather than tokens: what matters is
# whether the provider accepts it, and a 4-characters-per-token estimate is the
# same number scaled. The largest meeting in the corpus assembles to about
# 45,000 characters, so the single-call path is the only one that runs in
# practice - the batch fallback exists for a document that grows past it.
PROMPT_CHAR_BUDGET = 120_000

# A response line: "3: the section where ...". The leading run allows a bullet
# or bold marker the model added, and the separator may be `.`, `)` or `:`.
# A dash is deliberately NOT a separator: "2026-06-09, the team ..." would parse
# as clause 2026 and fail an otherwise good response. Anything that does not
# match is not a clause and is ignored, which is safe only because the ordinals
# are then required to be exactly 1..N in order.
_CLAUSE_LINE = re.compile(r"^[\s>*_-]*(\d{1,4})[\s*_]*[.):]\s*(\S.*)$")

# Markdown and quoting the model wrapped around its own clause.
_CLAUSE_NOISE = re.compile(r"[*`_]+")


class HeaderError(RuntimeError):
    """One meeting's headers could not be produced.

    Always raised before anything is written, so a meeting either gets a full
    set of aligned headers or keeps its NULLs.
    """


@dataclass(frozen=True)
class Header:
    """One composed header, ready to store."""

    text: str
    model: str      # the provider that served the call behind it
    clipped: bool   # the model's clause had to be cut to fit HEADER_MAX_CHARS


Completer = Callable[[str], str]


# ── The prompt ────────────────────────────────────────────────────────

_INSTRUCTIONS = """\
You are labelling passages of one meeting's minutes so that each one can be
retrieved and read on its own, out of the document it came from.

MEETING (given facts, from the archive's manifest - do not restate them)
  date:  {date}
  title: {title}

{scope}

For each numbered passage, write one clause that situates it: what that passage
is about and where it sits in this meeting.

RULES
- One clause per passage, on one line, at most {budget} characters.
- Describe only what that passage itself says. Never state a fact it does not
  contain - no invented owner, date, decision, or number. If a passage is thin,
  say what it covers and no more.
- Do not repeat the meeting date or title. They are added automatically.
- Name the subject the passage sits under, and the people or systems it
  concerns when it names them itself.
- No quotation marks, no markdown, no blank lines, no commentary.

OUTPUT
Exactly {count} lines and nothing else:
1: <clause for passage 1>
2: <clause for passage 2>
...
{count}: <clause for passage {count}>

PASSAGES
"""

_WHOLE_DOCUMENT = (
    "The numbered passages below are the complete minutes of that meeting, in\n"
    "order: together they are the whole document."
)

_ONE_BATCH = (
    "The numbered passages below are part {part} of {parts} of that meeting's\n"
    "minutes, in order. You are not seeing the rest of the document."
)


def build_prompt(
    date: str | None,
    title: str,
    chunks: list[sqlite3.Row],
    *,
    part: int = 1,
    parts: int = 1,
) -> str:
    """The prompt for one call: meeting facts, then the numbered passages.

    The passages *are* the document. Sending the minutes file as well would
    double the prompt to say the same thing twice, and the chunks are the text
    the headers must actually describe - a file that had moved on since the
    index was built would describe the wrong passages.
    """
    scope = (
        _WHOLE_DOCUMENT if parts == 1 else _ONE_BATCH.format(part=part, parts=parts)
    )
    head = _INSTRUCTIONS.format(
        date=date or "unknown",
        title=title,
        scope=scope,
        budget=clause_budget(date, title),
        count=len(chunks),
    )
    body = "".join(
        f"\n--- passage {position} of {len(chunks)} "
        f"(section: {row['heading'] or 'untitled'}) ---\n{row['text']}\n"
        for position, row in enumerate(chunks, start=1)
    )
    return head + body


def _batches(
    chunks: list[sqlite3.Row], date: str | None, title: str
) -> list[list[sqlite3.Row]]:
    """One batch per call. A single batch is the normal case.

    Split only when the assembled prompt would not fit the budget, and then
    greedily, so a meeting that needs splitting still sees as much of itself per
    call as it can. Never empty: a passage larger than the whole budget is sent
    alone and left to the provider, because the alternative is refusing to
    describe it at all.
    """
    if len(build_prompt(date, title, chunks)) <= PROMPT_CHAR_BUDGET:
        return [chunks]

    overhead = len(build_prompt(date, title, [], parts=2))
    room = max(PROMPT_CHAR_BUDGET - overhead, 0)
    batches: list[list[sqlite3.Row]] = []
    used = 0
    for row in chunks:
        size = len(row["text"]) + 80  # the passage plus its delimiter line
        if batches and used + size <= room:
            batches[-1].append(row)
            used += size
        else:
            batches.append([row])
            used = size
    return batches


def clause_budget(date: str | None, title: str) -> int:
    """How many characters the model's clause may use for this meeting."""
    return max(CLAUSE_MIN_CHARS, HEADER_MAX_CHARS - len(_prefix(date, title)) - 2)


# ── Parsing and composing ─────────────────────────────────────────────

def parse_clauses(response: str, expected: int) -> list[str]:
    """`expected` clauses in passage order, or raise.

    Strict on purpose. The ordinals must be exactly 1..expected, in order, with
    nothing missing and nothing extra: they are the only thing tying a clause to
    a passage, and a response that answered nine of ten passages would otherwise
    silently label passage 10's text with passage 9's description. Never guess,
    never pad, never truncate to fit.
    """
    found: list[tuple[int, str]] = []
    for line in response.splitlines():
        match = _CLAUSE_LINE.match(line)
        if match:
            clause = _clean_clause(match.group(2))
            if clause:
                found.append((int(match.group(1)), clause))

    ordinals = [number for number, _clause in found]
    if ordinals != list(range(1, expected + 1)):
        raise HeaderError(
            f"expected clauses numbered 1..{expected}, got "
            f"{_summarise(ordinals)} - headers left unwritten"
        )
    return [clause for _number, clause in found]


def _summarise(ordinals: list[int]) -> str:
    if not ordinals:
        return "no numbered lines"
    if ordinals == sorted(ordinals) and len(set(ordinals)) == len(ordinals):
        return f"{len(ordinals)} lines numbered {ordinals[0]}..{ordinals[-1]}"
    return f"{len(ordinals)} lines out of order or repeated"


def _clean_clause(raw: str) -> str:
    """One line of model output as a plain clause."""
    return _CLAUSE_NOISE.sub("", raw).strip().strip('"').strip("'").strip()


def _prefix(date: str | None, title: str) -> str:
    """The provenance half of a header, composed from the manifest.

    Composed rather than requested. The single most damaging thing a model could
    invent here is a date, and the manifest already knows it - so the model is
    never asked (GC2), and a header always describes the meeting its chunk came
    from (GC3).
    """
    parts = [part for part in (date, _clip(title, TITLE_MAX_CHARS)) if part]
    return ", ".join(parts)


def compose_header(date: str | None, title: str, clause: str) -> tuple[str, bool]:
    """(header, was the clause clipped).

    Clipped rather than rejected when the model overruns: a whole meeting's
    headers are not worth discarding over one long clause, and a header is a
    label rather than quoted evidence. The cut is marked with an ellipsis all
    the same, the same way `chunk_index.fit_excerpt` marks a trimmed excerpt.
    """
    budget = clause_budget(date, title)
    fitted = _clip(clause, budget)
    prefix = _prefix(date, title)
    header = f"{prefix}: {fitted}" if prefix else fitted
    return header, fitted != clause


def _clip(text: str, limit: int) -> str:
    """`text` inside `limit` characters, cut at a word break and marked."""
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1]
    cut = clipped.rfind(" ")
    kept = clipped[:cut] if cut > limit // 2 else clipped
    return kept.rstrip(" ,;:.-") + "…"


# ── Generating one meeting ────────────────────────────────────────────

def headers_for_meeting(
    date: str | None,
    title: str,
    chunks: list[sqlite3.Row],
    *,
    complete: Completer,
) -> tuple[list[Header], bool]:
    """(one header per chunk in ordinal order, whether it took several calls).

    Raises HeaderError if any call fails or any response miscounts, so a meeting
    is written whole or not at all. A meeting with headers on some of its chunks
    would be a hole in the index that nothing reports.
    """
    batches = _batches(chunks, date, title)
    headers: list[Header] = []
    for part, batch in enumerate(batches, start=1):
        prompt = build_prompt(date, title, batch, part=part, parts=len(batches))
        try:
            response = complete(prompt)
        except llm.LLMError as exc:
            raise HeaderError(str(exc)) from exc
        model = llm.last_provider or "unknown"
        for clause in parse_clauses(response, len(batch)):
            text, clipped = compose_header(date, title, clause)
            headers.append(Header(text=text, model=model, clipped=clipped))
    return headers, len(batches) > 1


def _write_headers(
    conn: sqlite3.Connection, chunks: list[sqlite3.Row], headers: list[Header]
) -> None:
    if len(headers) != len(chunks):
        # Unreachable while parse_clauses does its job, and checked anyway: this
        # is the last point before a shifted set of headers becomes thousands of
        # rows nobody can audit. Raising HeaderError rather than letting the
        # zip() below raise ValueError keeps the blast radius at one meeting -
        # a ValueError would escape the loop and abort the whole run.
        raise HeaderError(f"{len(headers)} headers for {len(chunks)} chunks")
    generated_at = now_iso()
    conn.executemany(
        """
        UPDATE minute_chunks
           SET context_header = ?, header_content_hash = ?,
               header_model = ?, headers_generated_at = ?
         WHERE chunk_id = ?
        """,
        [
            # strict=True is a second guard on the count that matters. By here
            # parse_clauses has already checked it; if a future edit routes
            # around that, this raises instead of writing a shifted set.
            (header.text, row["content_hash"], header.model, generated_at, row["chunk_id"])
            for row, header in zip(chunks, headers, strict=True)
        ],
    )


# ── The run ───────────────────────────────────────────────────────────

_MEETING_SQL = """
    SELECT c.meeting_id                       AS meeting_id,
           MIN(c.meeting_date)                AS meeting_date,
           MIN(c.source_path)                 AS source_path,
           MIN(m.source_name)                 AS source_name,
           MIN(m.title_hint)                  AS title_hint,
           COUNT(*)                           AS chunks,
           SUM(CASE WHEN c.context_header IS NOT NULL
                     AND c.header_content_hash = c.content_hash
                    THEN 1 ELSE 0 END)        AS current
      FROM minute_chunks c
      LEFT JOIN meetings m ON m.id = c.meeting_id
     GROUP BY c.meeting_id
     -- Chronological, so an interrupted run has covered a contiguous stretch of
     -- the archive rather than an arbitrary slice of it.
     ORDER BY c.meeting_date, c.meeting_id
"""


def generate_all(
    conn: sqlite3.Connection,
    *,
    complete: Completer | None = None,
    meeting: str | None = None,
    limit: int | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Write context headers for every meeting that needs them.

    Returns counts keyed `meetings` (considered), `headed` (meetings written),
    `chunks` (header rows written), `skipped` (already current), `failed`,
    `batched` (needed more than one call), `clipped` (headers cut to fit) and
    `interrupted`.

    `failed` is the count that matters: a meeting in it is still unheaded and
    still reachable only by its own words, and one failure must not stop the
    other 116 (GC5).

    Ctrl-C stops the run and returns what it has, with `interrupted` set. The
    meeting in flight is lost and every meeting before it is committed, which is
    the whole reason this commits per meeting rather than per run.
    """
    send = complete or llm.complete
    stats = {
        "meetings": 0, "headed": 0, "chunks": 0, "skipped": 0,
        "failed": 0, "batched": 0, "clipped": 0, "interrupted": 0,
    }
    rows = [
        row
        for row in conn.execute(_MEETING_SQL)
        if meeting is None or row["meeting_id"] == meeting
    ]
    stats["meetings"] = len(rows)

    pending = []
    for row in rows:
        if not force and row["current"] == row["chunks"]:
            stats["skipped"] += 1
        else:
            pending.append(row)
    if limit is not None:
        pending = pending[:limit]

    # Not done when the caller already had a transaction open: committing inside
    # somebody else's unit of work is not this function's decision to make, and
    # both sibling index modules draw the same line.
    durable = not conn.in_transaction

    for position, row in enumerate(pending, start=1):
        label = f"[{position}/{len(pending)}] {row['meeting_date'] or '????-??-??'}"
        try:
            written, batched, clipped = _head_one(conn, row, send=send)
        except KeyboardInterrupt:
            # Swallowed deliberately, and only here. The interrupt has already
            # done what the operator asked - the loop stops - and the caller
            # needs the counts to know how much of a 45-minute job survived.
            stats["interrupted"] = 1
            print(f"    {label} interrupted; stopping. Re-run to resume.")
            break
        except HeaderError as exc:
            stats["failed"] += 1
            print(f"    {label} FAILED, headers left unwritten: {exc}")
            continue
        if durable:
            conn.commit()
        stats["headed"] += 1
        stats["chunks"] += written
        stats["batched"] += int(batched)
        stats["clipped"] += clipped
        print(f"    {label} {_short_title(row)}: {written} header(s)")
    return stats


def _head_one(
    conn: sqlite3.Connection, row: sqlite3.Row, *, send: Completer
) -> tuple[int, bool, int]:
    """(headers written, needed batching, headers clipped) for one meeting."""
    chunks = list(_chunks(conn, row["meeting_id"]))
    if not chunks:
        raise HeaderError("no chunks")
    title = _meeting_title(row)
    headers, batched = headers_for_meeting(
        row["meeting_date"], title, chunks, complete=send
    )
    _write_headers(conn, chunks, headers)
    return len(headers), batched, sum(header.clipped for header in headers)


def _chunks(conn: sqlite3.Connection, meeting_id: str) -> Iterator[sqlite3.Row]:
    return conn.execute(
        "SELECT chunk_id, ordinal, heading, text, content_hash FROM minute_chunks "
        "WHERE meeting_id = ? ORDER BY ordinal",
        (meeting_id,),
    )


def _meeting_title(row: sqlite3.Row) -> str:
    """The meeting's human-readable title, through the one resolver for it.

    `meetings.title_hint` holds a mangled Drive file id, so the frontmatter of
    the minutes file is the only place the real title lives. Only its head is
    read - `clean_meeting_title` looks at the first 25 lines - and a file that
    has gone missing falls back to the filename slug rather than failing the
    meeting: a header is still worth having without a title.
    """
    text = None
    path = row["source_path"]
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read(4_000)
        except (OSError, UnicodeDecodeError):
            text = None
    return titles.clean_meeting_title(
        row["source_name"], row["title_hint"], path, text
    )


def _short_title(row: sqlite3.Row) -> str:
    """A short label for the progress line.

    Cut with a plain slice and no ellipsis, unlike `_clip`. This string is
    printed, and a Windows console on a legacy code page raises
    UnicodeEncodeError on U+2026 - which would kill an unattended run partway
    through the corpus over a decoration.
    """
    return (Path(row["source_path"] or "").stem or row["meeting_id"])[:60]


# ── Status, the reason channel ────────────────────────────────────────

def context_status(conn: sqlite3.Connection) -> tuple[bool, str]:
    """(any current headers, reason).

    The only channel for why retrieval is not seeing headers: "nobody has built
    the chunk index", "nobody has run this stage" and "the text moved on
    underneath them" are different problems and all three are actionable (GC5).
    """
    present = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'minute_chunks'"
    ).fetchone()[0]
    if not present:
        return False, "chunk index not built; run `pipeline chunk-index`"
    total, headed, stale = conn.execute(
        """
        SELECT COUNT(*),
               SUM(CASE WHEN context_header IS NOT NULL
                         AND header_content_hash = content_hash THEN 1 ELSE 0 END),
               SUM(CASE WHEN context_header IS NOT NULL
                         AND (header_content_hash IS NULL
                              OR header_content_hash <> content_hash) THEN 1 ELSE 0 END)
          FROM minute_chunks
        """
    ).fetchone()
    if not total:
        return False, "chunk index is empty; run `pipeline chunk-index`"
    if not headed:
        return False, f"no chunk of {total} carries a context header; run `pipeline chunk-context`"
    trailer = (
        f", {stale} describing text that has since changed - stale; "
        "run `pipeline chunk-context`"
        if stale
        else ""
    )
    return True, f"{headed}/{total} chunks carry a current context header{trailer}"
