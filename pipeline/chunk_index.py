"""BM25 text index over compiled minutes.

Until this module the archive had no text index at all. LightRAG runs with
vector ingestion disabled, so retrieval was substring matching on graph entity
labels plus a keyword scan of minutes files on disk - which means a meeting
whose minutes never named an extracted entity was effectively unreachable, and
the excerpts that did surface carried a fixed 0.8 score that says nothing about
relevance. This is the first real relevance signal in the system.

It is deliberately plain: SQLite's own FTS5 with `bm25()`, no service, no
network, no new dependency. The whole corpus is 117 meetings and 2,809 chunks,
and a full rebuild takes about 1.3 seconds - an index that lives inside the
manifest is worth more here than one that needs a container running.

Three properties are load-bearing for everything built on top:

* **Deterministic chunking.** The same bytes yield byte-identical chunk ids and
  hashes forever. Reindex skips a meeting whose hashes all match, so a chunker
  that drifted would rewrite the corpus on every run and invalidate every
  downstream artifact keyed on `chunk_id`.
* **Per-source provenance.** A chunk's `meeting_id`, `meeting_date` and
  `source_path` describe the meeting its text actually came from. Text is never
  merged across meetings under one citation.
* **Bounded reads.** `search_chunks` honours a result count, a character
  budget, and a per-meeting cap, so no caller can be handed an unbounded
  answer by accident.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import now_iso

# ── Chunk geometry ────────────────────────────────────────────────────
#
# Minutes are already structured prose: a heading per section, blank-line
# separated paragraphs inside it. The chunker follows that structure rather
# than sliding a fixed window over the text, because a chunk that starts and
# ends on a real boundary is a quotable excerpt and a window is not.

# A section longer than this is split further. Sized against the corpus: a
# typical "## Topics Discussed" bullet runs 700-1,200 characters, so this keeps
# one bullet whole while breaking up the eight-bullet section around it.
MAX_CHUNK_CHARS = 1_200

# Below this a chunk is a fragment - "Who signs off?" as its own row is a hit
# that costs a citation and tells the reader nothing. Merged into its
# predecessor instead. The merge can push a chunk past MAX_CHUNK_CHARS; a
# slightly long chunk beats a useless one.
MIN_CHUNK_CHARS = 200

# At most this many chunks from any one meeting in a result set. Without it a
# single verbose meeting - and this corpus has 18,000-character minutes - fills
# every slot and the answer is grounded in one conversation.
MAX_CHUNKS_PER_MEETING = 2

# Mean per-term BM25 magnitude that maps to a score of 0.5. See _normalise_score.
# Calibrated against the real corpus (2,808 chunks): a bare surname lands near
# 0.43, a three-word topical query near 0.47, a rare proper noun near 0.77.
BM25_HALF_SCORE = 2.0

# Column weights for bm25(): the body is the signal, heading and context header
# are supporting metadata that should nudge the ranking, not decide it. Both
# are short, and BM25 already rewards a match in a short field.
_BM25_WEIGHTS = (1.0, 0.5, 0.5)

# A hit smaller than this cannot be usefully truncated to fit the remaining
# character budget, so the budget is called spent instead.
_MIN_TRUNCATED_HIT = 200

# YAML frontmatter is machine metadata - template version, the local transcript
# path, a Drive URL for the source audio. Indexing it would make private links
# searchable text and let one surface as an "excerpt"; the meeting content it
# does carry (title, attendees, entities) is either repeated in the H1 below it
# or already served from the entities table.
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)

# Split before any ATX heading, keeping the heading line with the section it
# opens. Setext headings do not appear in this template.
_SECTION_BREAK = re.compile(r"(?m)^(?=#{1,6}[ \t])")
_HEADING_LINE = re.compile(r"\A#{1,6}[ \t]+(?P<title>.*?)[ \t]*#*[ \t]*$")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")

# Terms for the FTS5 MATCH expression. Word runs only: everything a user might
# type that FTS5 would read as syntax - quotes, `*`, `:`, `-`, `^`, `(` - is
# dropped here rather than escaped, and the surviving terms are quoted, which
# also demotes NEAR/AND/OR/NOT from operators to literal words.
_QUERY_TERM = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class Chunk:
    """One indexable passage of one meeting's minutes."""

    chunk_id: str
    meeting_id: str
    ordinal: int
    heading: str | None
    text: str
    content_hash: str

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class ChunkHit:
    """A ranked chunk, shaped for the ContextItem contract in context_provider."""

    chunk_id: str
    meeting_id: str
    meeting_date: str | None
    source_path: str
    ordinal: int
    heading: str | None
    text: str
    score: float


# ── Chunking ──────────────────────────────────────────────────────────

def chunk_minutes(path: Path, meeting_id: str | None = None) -> list[Chunk]:
    """Split one minutes file into ordered, hashed chunks.

    `meeting_id` defaults to the file stem so the chunker stays usable on a
    loose file, but the indexer always passes the manifest id - a chunk_id has
    to key back to a real meeting for its citation to mean anything.

    Raises OSError / UnicodeDecodeError if the file cannot be read; callers that
    walk the whole corpus catch that and count the meeting as skipped (GC5).
    """
    meeting_id = meeting_id or Path(path).stem
    # Line endings are normalised before anything is hashed: the same minutes
    # checked out on Windows and Linux must produce the same content_hash, or
    # every chunk churns on the first reindex after a clone.
    raw = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    body = _FRONTMATTER.sub("", raw)

    pieces: list[tuple[str | None, str | None, str]] = []  # heading, heading_line, text
    for section in _SECTION_BREAK.split(body):
        heading_line, heading, text = _split_heading(section)
        for piece in _split_section(text):
            pieces.append((heading, heading_line, piece))
            # The raw heading line rides along with the first piece only, for
            # _merge_fragments to put back if this piece turns out to be a
            # fragment. A later piece of the same section is already inside it.
            heading_line = None

    return _finalise(meeting_id, _merge_fragments(pieces))


def _split_heading(section: str) -> tuple[str | None, str | None, str]:
    """(raw heading line, heading text, body) for one section."""
    head, _, rest = section.partition("\n")
    match = _HEADING_LINE.match(head)
    if not match:
        return None, None, section.strip()
    return head.strip(), match.group("title").strip() or None, rest.strip()


def _split_section(text: str) -> list[str]:
    """A section as one chunk, or greedily packed paragraphs when it is too long.

    Paragraphs are the first fallback and lines the second. Nothing is ever cut
    mid-line: a chunk is quoted back to a reader as evidence, and a sentence
    severed at character 1,200 reads as a misquote. The corpus does contain
    single paragraphs past the threshold - a 2,775-character "## Entities"
    block, a 1,226-character topic bullet - so a slightly oversized chunk is
    the accepted cost.
    """
    if not text:
        return []
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]

    paragraphs = [block.strip() for block in _PARAGRAPH_BREAK.split(text) if block.strip()]
    chunks: list[str] = []
    for unit in _pack(paragraphs, "\n\n"):
        if len(unit) <= MAX_CHUNK_CHARS:
            chunks.append(unit)
            continue
        # _pack never joins past the limit, so an oversized unit is one
        # oversized paragraph. Split it on its own lines - and only here, so a
        # line-level piece can never be rejoined to a neighbour across a blank
        # line the source did not have.
        chunks.extend(_pack([line for line in unit.split("\n") if line.strip()], "\n"))
    return chunks


def _pack(units: list[str], separator: str) -> list[str]:
    """Greedily join units up to MAX_CHUNK_CHARS. A unit over the limit stands alone."""
    out: list[str] = []
    for unit in units:
        if out and len(out[-1]) + len(separator) + len(unit) <= MAX_CHUNK_CHARS:
            out[-1] = out[-1] + separator + unit
        else:
            out.append(unit)
    return out


def _merge_fragments(
    pieces: list[tuple[str | None, str | None, str]],
) -> list[tuple[str | None, str]]:
    """Fold anything under the floor into its neighbour.

    A fragment that opens a new section keeps its heading line, so the reader of
    the excerpt still sees "## Open Questions" above the question that merged
    in, rather than finding it filed silently under "Decisions".

    Merged text is a faithful regrouping of the source lines, not a byte-exact
    slice of the file: the join is always a blank line, where the source may
    have had a single newline. About 1% of the corpus differs this way.
    """
    merged: list[tuple[str | None, str]] = []
    for heading, heading_line, text in pieces:
        prefixed = f"{heading_line}\n\n{text}" if heading_line else text
        if merged and len(text) < MIN_CHUNK_CHARS:
            previous_heading, previous_text = merged[-1]
            merged[-1] = (previous_heading, f"{previous_text}\n\n{prefixed}")
        else:
            merged.append((heading, text))

    # The opening chunk has no predecessor to fold into. Everything after it is
    # at least MIN_CHUNK_CHARS by construction, so pulling one forward is enough.
    if len(merged) > 1 and len(merged[0][1]) < MIN_CHUNK_CHARS:
        heading, text = merged[0]
        merged[0] = (heading, f"{text}\n\n{merged[1][1]}")
        del merged[1]
    return merged


def _finalise(meeting_id: str, merged: list[tuple[str | None, str]]) -> list[Chunk]:
    return [
        Chunk(
            chunk_id=f"{meeting_id}:{ordinal:04d}",
            meeting_id=meeting_id,
            ordinal=ordinal,
            heading=heading,
            text=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        for ordinal, (heading, text) in enumerate(merged)
    ]


# ── Indexing ──────────────────────────────────────────────────────────

@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str) -> Iterator[None]:
    """Nestable transaction around one meeting's rows.

    A SAVEPOINT rather than `with conn:` because callers hold the connection and
    may already be mid-transaction. Either the meeting's whole chunk set is
    replaced or none of it is; a half-indexed meeting would answer searches with
    part of its own minutes and no sign that the rest is missing.
    """
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")


def reindex_meeting(conn: sqlite3.Connection, meeting_id: str, *, force: bool = False) -> int:
    """Re-chunk one meeting. Returns the number of chunk rows written.

    Zero means nothing needed doing: no such meeting, no minutes yet, the file
    could not be read, or every chunk hashed the same as the stored one.
    """
    row = conn.execute(
        "SELECT id, meeting_date, minutes_path FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if row is None or not row["minutes_path"]:
        return 0

    try:
        chunks = chunk_minutes(Path(row["minutes_path"]), meeting_id=meeting_id)
    except (OSError, UnicodeDecodeError):
        return 0

    if not force and _unchanged(conn, meeting_id, chunks):
        return 0

    indexed_at = now_iso()
    with _savepoint(conn, "chunk_reindex"):
        conn.execute("DELETE FROM minute_chunks WHERE meeting_id = ?", (meeting_id,))
        conn.executemany(
            """
            INSERT INTO minute_chunks (
                chunk_id, meeting_id, meeting_date, source_path, ordinal, heading,
                text, context_header, char_count, content_hash, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            [
                (
                    chunk.chunk_id, meeting_id, row["meeting_date"], str(row["minutes_path"]),
                    chunk.ordinal, chunk.heading, chunk.text, chunk.char_count,
                    chunk.content_hash, indexed_at,
                )
                for chunk in chunks
            ],
        )
    return len(chunks)


def _unchanged(conn: sqlite3.Connection, meeting_id: str, chunks: list[Chunk]) -> bool:
    stored = [
        (row["ordinal"], row["content_hash"])
        for row in conn.execute(
            "SELECT ordinal, content_hash FROM minute_chunks WHERE meeting_id = ? ORDER BY ordinal",
            (meeting_id,),
        )
    ]
    return stored == [(chunk.ordinal, chunk.content_hash) for chunk in chunks]


def reindex_all(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, int]:
    """Reindex every meeting that has minutes.

    Returns counts keyed `meetings` (considered), `reindexed`, `unchanged`,
    `unreadable`, `chunks` (rows written). An unreadable minutes file is
    counted and stepped over, never raised: one deleted file must not stop the
    other 116 meetings from indexing (GC5).
    """
    stats = {"meetings": 0, "reindexed": 0, "unchanged": 0, "unreadable": 0, "chunks": 0}
    rows = conn.execute(
        "SELECT id, minutes_path FROM meetings WHERE minutes_path IS NOT NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        stats["meetings"] += 1
        if not Path(row["minutes_path"]).is_file():
            stats["unreadable"] += 1
            continue
        try:
            written = reindex_meeting(conn, str(row["id"]), force=force)
        except (OSError, UnicodeDecodeError):
            stats["unreadable"] += 1
            continue
        if written:
            stats["reindexed"] += 1
            stats["chunks"] += written
        else:
            stats["unchanged"] += 1
    return stats


# ── Search ────────────────────────────────────────────────────────────

def _tables_present(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE name IN ('minute_chunks', 'minute_chunks_fts')"
        ).fetchone()[0]
        == 2
    )


def index_status(conn: sqlite3.Connection) -> tuple[bool, str]:
    """(searchable, reason).

    `search_chunks` returns a bare list, so this is the only channel a caller
    has for *why* an answer was empty - "nobody has built the index" and "the
    query matched nothing" are very different problems (GC5).
    """
    if not _tables_present(conn):
        return False, "chunk index not built; run `pipeline chunk-index`"
    rows = conn.execute("SELECT COUNT(*) FROM minute_chunks").fetchone()[0]
    if not rows:
        return False, "chunk index is empty; run `pipeline chunk-index`"
    return True, f"{rows} chunks indexed"


def _match_expression(query: str) -> tuple[str, int]:
    """A user string as a safe FTS5 MATCH expression plus its term count.

    The expression is "" when the query has no searchable term at all.

    OR rather than AND: this feeds a ranked list, not a filter, and BM25's IDF
    already pushes the chunk that matched the rare term above the one that only
    matched "the". Requiring every term would silently return nothing for the
    long natural-language questions this index exists to answer.
    """
    terms = _QUERY_TERM.findall(query)
    return " OR ".join(f'"{term}"' for term in terms), len(terms)


def _normalise_score(raw: float, term_count: int) -> float:
    """FTS5's bm25() into the 0.0..1.0 the ContextItem contract requires.

        relevance = -raw / term_count                   (0 upward, better = larger)
        score     = relevance / (relevance + HALF)      (0.5 at relevance == HALF)

    Two decisions, both about comparability:

    Divided by the term count because bm25() sums a contribution per matched
    term, so an eight-word question scores four times a two-word one for the
    same quality of match. The mean per-term contribution is what actually
    means "how well does this passage answer the query".

    Saturating rather than dividing by the best score in the batch. Scaling to
    the batch maximum would hand a lone weak hit a 1.0 and make scores
    meaningless between queries - which is the defect this replaces, where
    every minute excerpt scored a fixed 0.8. This map is absolute, so a later
    fusion step can compare a BM25 score against a dense one directly.

    Not rounded: on a corpus small enough that FTS5 clamps IDF to its epsilon,
    every raw score sits within 1e-6 of zero and rounding would flatten the
    ranking into a tie.
    """
    relevance = max(0.0, -raw) / max(1, term_count)
    return relevance / (relevance + BM25_HALF_SCORE)


def _fit(text: str, limit: int) -> str:
    """`text` trimmed to `limit`, backing up to a word break when it has to cut."""
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    cut = clipped.rfind(" ")
    return (clipped[:cut] if cut > limit // 2 else clipped).rstrip()


def search_chunks(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    max_chars: int = 4000,
    as_of: str | None = None,
    exclude_meeting_ids: frozenset[str] = frozenset(),
) -> list[ChunkHit]:
    """Best-matching chunks, best first, bounded three ways.

    `as_of` keeps the answer to what was known on a date; `exclude_meeting_ids`
    drops the meeting that already grounds the artifact being written, so the
    result is prior context rather than the same minutes read back.

    `max_chars` is a budget over the returned text, not a per-hit cap: hits are
    taken in rank order until it is spent, and the one that straddles the
    boundary is trimmed at a word break rather than dropped. Call
    `index_status` when an empty list needs explaining.
    """
    match, term_count = _match_expression(query)
    if not match or not _tables_present(conn):
        return []

    where = ["minute_chunks_fts MATCH ?"]
    params: list[object] = [match]
    if as_of:
        where.append("c.meeting_date IS NOT NULL AND c.meeting_date <= ?")
        params.append(as_of)
    if exclude_meeting_ids:
        ids = sorted(exclude_meeting_ids)
        where.append(f"c.meeting_id NOT IN ({','.join('?' * len(ids))})")
        params.extend(ids)

    # The candidate pool is capped before ranking so a one-word query cannot
    # sort the whole corpus. It is deliberately much larger than `limit`: the
    # per-meeting cap is applied after ranking, so the pool has to hold enough
    # rows that discarding a verbose meeting's surplus still leaves others.
    params.append(min(1_000, max(limit * 25, 200)))

    sql = f"""
        WITH hits AS (
            SELECT c.chunk_id, c.meeting_id, c.meeting_date, c.source_path, c.ordinal,
                   c.heading, c.text,
                   bm25(minute_chunks_fts, {', '.join(str(w) for w in _BM25_WEIGHTS)}) AS raw_score
            FROM minute_chunks_fts
            JOIN minute_chunks c ON c.rowid = minute_chunks_fts.rowid
            WHERE {' AND '.join(where)}
            -- Ascending: FTS5's bm25() is negative and more negative is better.
            ORDER BY raw_score
            LIMIT ?
        ),
        ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY meeting_id ORDER BY raw_score, ordinal
            ) AS per_meeting
            FROM hits
        )
        SELECT * FROM ranked
        WHERE per_meeting <= ?
        ORDER BY raw_score, meeting_date, meeting_id, ordinal
    """
    params.append(MAX_CHUNKS_PER_MEETING)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # Only reachable if the FTS table went missing between the status check
        # and here. Empty beats a stack trace out of a read-only search (GC5).
        return []

    hits: list[ChunkHit] = []
    used = 0
    for row in rows:
        if len(hits) >= limit:
            break
        remaining = max_chars - used
        if remaining < _MIN_TRUNCATED_HIT:
            break
        text = _fit(row["text"], remaining)
        hits.append(
            ChunkHit(
                chunk_id=row["chunk_id"],
                meeting_id=row["meeting_id"],
                meeting_date=row["meeting_date"],
                source_path=row["source_path"],
                ordinal=row["ordinal"],
                heading=row["heading"],
                text=text,
                score=_normalise_score(row["raw_score"], term_count),
            )
        )
        used += len(text)
    return hits
