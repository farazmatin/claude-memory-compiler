"""BM25 chunk index over compiled minutes.

The corpus has never had a text search index. Retrieval today is substring
matching on graph entity labels plus a keyword scan of minutes files on disk,
so these tests pin the two properties that make a real index trustworthy:
chunk boundaries are deterministic (a reindex must not churn ids), and every
hit carries the provenance of the one meeting its text came from.

Fixtures are hand-written minutes, not the real corpus: the shapes that matter
here (a short trailing section, a section past the split threshold, a verbose
meeting) are easier to state exactly than to find.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pipeline import chunk_index, db

# Long enough that a section built from it clears the 200-char floor without
# any test needing to count characters by hand.
FILLER = (
    "The team reviewed the control inventory and agreed the evidence trail has to "
    "survive an audit without anyone reconstructing it from memory afterwards. "
    "Ownership stays with the first line while the second line reviews sampling. "
)

# sha256 of the first chunk `_sample_minutes` produces, recorded at a known-good
# state. Any change to chunk geometry moves it - which is the point: the hash is
# what reindex compares to decide a meeting is unchanged, so it cannot drift
# quietly. If a deliberate geometry change breaks this, re-derive it and say so
# in the commit; the whole corpus needs a rebuild either way.
GOLDEN_FIRST_CHUNK_HASH = "d0443224d5c3e48efa712e822d98ed3df02b835477933a9fbbdf83c30de9ec77"


def _write_minutes(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _add_meeting(conn, meeting_id: str, date: str, minutes: Path | str) -> None:
    """Insert a meeting already advanced to minutes_compiled."""
    db.insert_meeting(
        conn,
        meeting_id=meeting_id,
        source_path=f"/inbox/{meeting_id}.m4a",
        source_name=f"{meeting_id}.m4a",
        audio_path=f"/audio/{meeting_id}.m4a",
        meeting_date=date,
        meeting_time="09:00",
        title_hint=meeting_id,
        duration_sec=1800.0,
    )
    db.advance(
        conn,
        meeting_id,
        db.MINUTES_COMPILED,
        transcript_path=f"/transcripts/{meeting_id}.json",
        minutes_path=str(minutes),
    )


def _sample_minutes(tmp_path: Path, name: str = "sample.md") -> Path:
    return _write_minutes(
        tmp_path / name,
        "---\n"
        "date: 2026-06-09\n"
        "title: Unified System of Controls\n"
        "---\n\n"
        "# Unified System of Controls\n\n"
        f"{FILLER * 2}\n\n"
        "## Context\n\n"
        f"{FILLER * 3}\n\n"
        f"{FILLER * 3}\n\n"
        "## Decisions\n\n"
        f"- {FILLER}\n"
        f"- {FILLER}\n"
        f"- {FILLER}\n\n"
        "## Open Questions\n\n"
        "Who signs off?\n",
    )


# ── 1. Determinism ────────────────────────────────────────────────────

def test_chunker_is_deterministic(tmp_path):
    """Same bytes in, byte-identical ids and hashes out.

    Load-bearing beyond tidiness: reindex skips a meeting whose content hashes
    all match, so a chunker that drifts would rewrite the whole corpus on every
    run and invalidate every downstream embedding keyed on chunk_id.
    """
    path = _sample_minutes(tmp_path)

    first = chunk_index.chunk_minutes(path, meeting_id="m1")
    second = chunk_index.chunk_minutes(path, meeting_id="m1")

    assert first, "fixture produced no chunks"
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert [c.ordinal for c in first] == [c.ordinal for c in second]
    assert [c.content_hash for c in first] == [c.content_hash for c in second]
    assert [c.text for c in first] == [c.text for c in second]
    assert [c.ordinal for c in first] == list(range(len(first)))
    assert first[0].chunk_id == "m1:0000"

    # A literal, not a re-derivation. Two calls in one interpreter would agree
    # even if the chunker were nondeterministic across runs; only a value
    # written down at a known-good state makes this a cross-run assertion.
    assert first[0].content_hash == GOLDEN_FIRST_CHUNK_HASH


def test_a_crlf_checkout_hashes_the_same_as_an_lf_one(tmp_path):
    """Otherwise every chunk churns on the first reindex after a Windows clone.

    The guarantee comes from `read_text` applying universal newlines, not from
    any normalising this module does. It is pinned here so a future switch to
    `read_bytes().decode()` fails loudly instead of silently rewriting 2,809
    rows and invalidating everything keyed on their hashes.
    """
    # write_bytes, not write_text: on Windows text mode already translates \n to
    # \r\n on the way out, so a "LF fixture" written with write_text is not one.
    body = _sample_minutes(tmp_path, "seed.md").read_text(encoding="utf-8")
    lf = tmp_path / "lf.md"
    crlf = tmp_path / "crlf.md"
    lf.write_bytes(body.encode("utf-8"))
    crlf.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    assert b"\r\n" not in lf.read_bytes()
    assert b"\r\n" in crlf.read_bytes()

    assert [c.content_hash for c in chunk_index.chunk_minutes(crlf, meeting_id="m1")] == [
        c.content_hash for c in chunk_index.chunk_minutes(lf, meeting_id="m1")
    ]


# ── 2. No fragments ───────────────────────────────────────────────────

def test_no_chunk_below_the_floor_unless_it_is_the_only_one(tmp_path):
    path = _sample_minutes(tmp_path)
    chunks = chunk_index.chunk_minutes(path, meeting_id="m1")

    assert len(chunks) > 1
    assert all(len(c.text) >= chunk_index.MIN_CHUNK_CHARS for c in chunks)
    # "Who signs off?" is far under the floor; it must have been merged rather
    # than emitted as its own row.
    assert any("Who signs off?" in c.text for c in chunks)

    tiny = _write_minutes(tmp_path / "tiny.md", "# Standup\n\nNothing to report.\n")
    tiny_chunks = chunk_index.chunk_minutes(tiny, meeting_id="m2")
    assert len(tiny_chunks) == 1
    assert len(tiny_chunks[0].text) < chunk_index.MIN_CHUNK_CHARS


# ── 3. Idempotent reindex ─────────────────────────────────────────────

def test_reindex_is_idempotent(manifest, tmp_path):
    _add_meeting(manifest, "m1", "2026-06-09", _sample_minutes(tmp_path, "a.md"))
    _add_meeting(manifest, "m2", "2026-06-10", _sample_minutes(tmp_path, "b.md"))

    first = chunk_index.reindex_all(manifest)
    assert first["meetings"] == 2
    assert first["reindexed"] == 2
    assert first["chunks"] > 0

    second = chunk_index.reindex_all(manifest)
    assert second["chunks"] == 0
    assert second["reindexed"] == 0
    assert second["unchanged"] == 2

    rows = manifest.execute("SELECT COUNT(*) FROM minute_chunks").fetchone()[0]
    assert rows == first["chunks"]


# ── 4. An edit touches exactly one meeting ────────────────────────────

def test_editing_one_meeting_leaves_the_others_untouched(manifest, tmp_path):
    edited = _sample_minutes(tmp_path, "a.md")
    _add_meeting(manifest, "m1", "2026-06-09", edited)
    _add_meeting(manifest, "m2", "2026-06-10", _sample_minutes(tmp_path, "b.md"))
    chunk_index.reindex_all(manifest)

    def snapshot(meeting_id: str) -> list[tuple]:
        return [
            tuple(row)
            for row in manifest.execute(
                "SELECT chunk_id, ordinal, content_hash, indexed_at FROM minute_chunks "
                "WHERE meeting_id = ? ORDER BY ordinal",
                (meeting_id,),
            )
        ]

    before_other = snapshot("m2")
    before_edited = snapshot("m1")

    edited.write_text(
        edited.read_text(encoding="utf-8")
        + "\n## Risks\n\n"
        + FILLER * 3
        + "\n",
        encoding="utf-8",
    )
    stats = chunk_index.reindex_all(manifest)

    assert stats["reindexed"] == 1
    assert stats["unchanged"] == 1
    assert snapshot("m2") == before_other
    assert snapshot("m1") != before_edited


# ── 5. BM25 beats topical similarity ──────────────────────────────────

def test_bm25_ranks_the_exact_proper_noun_above_a_topical_match(manifest, tmp_path):
    """The rare term has to beat the common one, which is what IDF is for.

    The distractor meetings are not padding. On a two-document corpus FTS5
    clamps every IDF to its epsilon and bm25 collapses to a near-tie, so a
    ranking test without them would pass on an accident.
    """
    exact = _write_minutes(
        tmp_path / "exact.md",
        "# USC platform\n\n"
        "USC is the unified system of controls. The USC control inventory is the "
        "single register every USC control maps into, and the USC team owns it. "
        + FILLER,
    )
    topical = _write_minutes(
        tmp_path / "topical.md",
        "# Control inventory hygiene\n\n"
        "The control inventory needs owners recorded against every control before "
        "the next review cycle. " + FILLER * 2,
    )
    _add_meeting(manifest, "exact", "2026-06-09", exact)
    _add_meeting(manifest, "topical", "2026-06-10", topical)
    for n in range(8):
        _add_meeting(
            manifest,
            f"distractor{n}",
            "2026-05-01",
            _write_minutes(
                tmp_path / f"d{n}.md",
                f"# Weekly sync {n}\n\nControl inventory ownership again. " + FILLER * 2,
            ),
        )
    chunk_index.reindex_all(manifest)

    # max_chars is deliberately huge: with ten matching meetings the default
    # 4,000 admits only the first six or seven hits, and this test would then
    # fail as a ranking regression when the real cause was the budget.
    hits = chunk_index.search_chunks(manifest, "USC control inventory", max_chars=100_000)

    assert hits, "expected both meetings to match"
    assert hits[0].meeting_id == "exact"
    assert "topical" in {hit.meeting_id for hit in hits}
    by_meeting = {hit.meeting_id: hit.score for hit in hits}
    assert by_meeting["exact"] > by_meeting["topical"]
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)


# ── 6. as_of and exclude_meeting_ids ──────────────────────────────────

def test_as_of_excludes_later_meetings_and_exclude_ids_drops_the_source(manifest, tmp_path):
    dated = (("early", "2026-06-01"), ("mid", "2026-06-15"), ("late", "2026-07-01"))
    for meeting_id, date in dated:
        path = _write_minutes(
            tmp_path / f"{meeting_id}.md",
            f"# {meeting_id} review\n\nThe control inventory was discussed. " + FILLER * 2,
        )
        _add_meeting(manifest, meeting_id, date, path)
    chunk_index.reindex_all(manifest)

    everything = chunk_index.search_chunks(manifest, "control inventory")
    assert {hit.meeting_id for hit in everything} == {"early", "mid", "late"}

    bounded = chunk_index.search_chunks(manifest, "control inventory", as_of="2026-06-15")
    assert {hit.meeting_id for hit in bounded} == {"early", "mid"}

    without_source = chunk_index.search_chunks(
        manifest, "control inventory", exclude_meeting_ids=frozenset({"mid"})
    )
    assert {hit.meeting_id for hit in without_source} == {"early", "late"}


# ── 7. Per-meeting cap ────────────────────────────────────────────────

def test_one_verbose_meeting_cannot_fill_the_result_set(manifest, tmp_path):
    verbose = _write_minutes(
        tmp_path / "verbose.md",
        "".join(
            f"## Section {n}\n\nThe control inventory was discussed at length. {FILLER * 2}\n\n"
            for n in range(10)
        ),
    )
    brief = _write_minutes(
        tmp_path / "brief.md",
        "# Short meeting\n\nThe control inventory came up once. " + FILLER * 2,
    )
    _add_meeting(manifest, "verbose", "2026-06-09", verbose)
    _add_meeting(manifest, "brief", "2026-06-10", brief)
    chunk_index.reindex_all(manifest)

    stored = manifest.execute(
        "SELECT COUNT(*) FROM minute_chunks WHERE meeting_id = 'verbose'"
    ).fetchone()[0]
    assert stored >= 10, "fixture must give the verbose meeting many matching chunks"

    hits = chunk_index.search_chunks(manifest, "control inventory", max_chars=100_000)

    # The literal 2, not the constant: reading MAX_CHUNKS_PER_MEETING back would
    # make this assertion agree with whatever the cap happens to be.
    from_verbose = [hit for hit in hits if hit.meeting_id == "verbose"]
    assert len(from_verbose) <= 2
    assert chunk_index.MAX_CHUNKS_PER_MEETING == 2
    assert any(hit.meeting_id == "brief" for hit in hits)


# ── 8. max_chars ──────────────────────────────────────────────────────

def test_a_hit_that_does_not_fit_is_marked_or_dropped_never_silently_severed(
    manifest, tmp_path
):
    """A partial quote must announce itself, and a scrap must not be quoted at all.

    `_split_section` refuses to cut a chunk mid-line because a severed sentence
    reads as a misquote. The same holds at the budget boundary, and harder: this
    text goes out as background evidence with a citation attached to it.
    """
    for n in range(3):
        _add_meeting(
            manifest,
            f"m{n}",
            f"2026-06-0{n + 1}",
            _write_minutes(
                tmp_path / f"m{n}.md",
                f"# Meeting {n}\n\nThe control inventory was discussed. " + FILLER * 2,
            ),
        )
    chunk_index.reindex_all(manifest)

    whole = chunk_index.search_chunks(manifest, "control inventory", max_chars=100_000)
    assert len(whole) == 3
    top = len(whole[0].text)
    assert all(len(hit.text) > chunk_index.MIN_CHUNK_CHARS + 50 for hit in whole)

    # 300 characters left over: enough for an honest partial quote.
    marked = chunk_index.search_chunks(manifest, "control inventory", max_chars=top + 300)
    assert sum(len(hit.text) for hit in marked) <= top + 300
    assert marked[0].text == whole[0].text
    assert marked[-1].text.endswith("…")
    assert len(marked[-1].text) >= chunk_index.MIN_CHUNK_CHARS
    assert marked[-1].text[:-1] in whole[1].text

    # 50 characters left over: not evidence. Dropped, not severed.
    dropped = chunk_index.search_chunks(manifest, "control inventory", max_chars=top + 50)
    assert [hit.text for hit in dropped] == [whole[0].text]
    assert not any(hit.text.endswith("…") for hit in dropped)


def test_max_chars_is_honoured(manifest, tmp_path):
    for n in range(6):
        path = _write_minutes(
            tmp_path / f"m{n}.md",
            f"# Meeting {n}\n\nThe control inventory was discussed. " + FILLER * 4,
        )
        _add_meeting(manifest, f"m{n}", f"2026-06-0{n + 1}", path)
    chunk_index.reindex_all(manifest)

    hits = chunk_index.search_chunks(manifest, "control inventory", max_chars=1500)

    assert hits
    assert sum(len(hit.text) for hit in hits) <= 1500


# ── 9. FTS5 syntax is never user-visible ──────────────────────────────

def test_fts5_operators_in_a_user_query_never_raise(manifest, tmp_path):
    path = _write_minutes(
        tmp_path / "m.md",
        "# Controls\n\nThe control inventory needs owners. " + FILLER * 2,
    )
    _add_meeting(manifest, "m1", "2026-06-09", path)
    chunk_index.reindex_all(manifest)

    for hostile in ('"', "*", ":", "-", "NEAR", "AND", "OR", "NOT"):
        hits = chunk_index.search_chunks(manifest, f"control inventory {hostile}")
        assert isinstance(hits, list)
        assert hits, f"{hostile!r} suppressed an otherwise-matching query"

    # A query that is nothing but operators has no searchable term at all; empty
    # is the honest answer, and it still must not raise.
    assert chunk_index.search_chunks(manifest, '"" * : -') == []


# ── 10. Degrade explicitly ────────────────────────────────────────────

def test_a_missing_minutes_file_is_counted_not_raised(manifest, tmp_path):
    _add_meeting(manifest, "present", "2026-06-09", _sample_minutes(tmp_path, "a.md"))
    _add_meeting(manifest, "absent", "2026-06-10", tmp_path / "does-not-exist.md")

    stats = chunk_index.reindex_all(manifest)

    assert stats["meetings"] == 2
    assert stats["unreadable"] == 1
    assert stats["reindexed"] == 1
    assert stats["unchanged"] == 0
    # A meeting with no row and one with no minutes are genuinely nothing to do.
    # A meeting whose file will not read is not, and raises for the single-
    # meeting caller rather than being reported as "nothing to do".
    assert chunk_index.reindex_meeting(manifest, "no-such-meeting") == 0
    with pytest.raises(OSError):
        chunk_index.reindex_meeting(manifest, "absent")


def test_an_undecodable_file_is_unreadable_not_unchanged(manifest, tmp_path, capsys):
    """The file exists and opens; it just is not UTF-8.

    This is the case an `is_file()` pre-check waves through. It used to reach
    `reindex_meeting`, come back 0, and be tallied as "unchanged" - so the
    operator was told nothing was wrong while the meeting sat unsearchable.
    """
    _add_meeting(manifest, "present", "2026-06-09", _sample_minutes(tmp_path, "a.md"))
    broken = tmp_path / "broken.md"
    broken.write_bytes(b"# Meeting\n\nControl inventory \xff\xfe not utf-8 \x80\x81\n")
    _add_meeting(manifest, "broken", "2026-06-10", broken)

    stats = chunk_index.reindex_all(manifest)

    assert stats["unreadable"] == 1
    assert stats["unchanged"] == 0
    assert stats["reindexed"] == 1
    assert manifest.execute(
        "SELECT COUNT(*) FROM minute_chunks WHERE meeting_id = 'broken'"
    ).fetchone()[0] == 0
    with pytest.raises(UnicodeDecodeError):
        chunk_index.reindex_meeting(manifest, "broken")

    # The operator has to be able to see it from the exit code alone.
    manifest.commit()
    from pipeline import cli

    assert cli.main(["chunk-index"]) == 1
    assert "1 unreadable" in capsys.readouterr().out
    assert cli.main(["chunk-index", "--meeting", "broken"]) == 1
    assert "cannot read" in capsys.readouterr().err


# ── Consumer contract ─────────────────────────────────────────────────

def test_hits_carry_the_provenance_of_their_own_meeting(manifest, tmp_path):
    """GC3: a citation names the meeting the text actually came from."""
    paths = {}
    for meeting_id, date in (("m1", "2026-06-09"), ("m2", "2026-06-10")):
        paths[meeting_id] = _write_minutes(
            tmp_path / f"{meeting_id}.md",
            f"# {meeting_id}\n\nThe control inventory was discussed. " + FILLER * 2,
        )
        _add_meeting(manifest, meeting_id, date, paths[meeting_id])
    chunk_index.reindex_all(manifest)

    for hit in chunk_index.search_chunks(manifest, "control inventory"):
        assert hit.source_path == str(paths[hit.meeting_id])
        assert hit.meeting_date == {"m1": "2026-06-09", "m2": "2026-06-10"}[hit.meeting_id]
        assert hit.chunk_id.startswith(f"{hit.meeting_id}:")
        # Lines are never rewritten, only regrouped: the text came out of this
        # meeting's file and no other. (Whole-chunk equality is not asserted -
        # a merged fragment is rejoined with a blank line the source may not
        # have had. See _merge_fragments.)
        source = paths[hit.meeting_id].read_text(encoding="utf-8")
        assert all(line in source for line in hit.text.splitlines() if line.strip())


def test_an_oversized_paragraph_splits_on_line_boundaries(tmp_path):
    """A 2,775-char single paragraph is real in this corpus; it must not survive whole.

    The "## Entities" block is one paragraph of many lines with no blank line
    between them, so paragraph splitting alone leaves it intact and one chunk
    then eats most of a 4,000-character retrieval budget.
    """
    lines = [f"Entity {n} (team): owns the control inventory for domain {n}." for n in range(50)]
    body = "\n".join(lines)
    assert len(body) > 1_200 and "\n\n" not in body

    chunks = chunk_index.chunk_minutes(
        _write_minutes(tmp_path / "entities.md", f"# Meeting\n\n## Entities\n\n{body}\n"),
        meeting_id="m1",
    )

    assert len(chunks) > 1
    assert all(len(c.text) <= chunk_index.MAX_CHUNK_CHARS for c in chunks)
    # Split on line boundaries, never mid-line: every source line survives whole
    # in exactly one chunk, and none is cut in half.
    emitted = [line for chunk in chunks for line in chunk.text.splitlines()]
    assert emitted == lines


def test_a_pulled_forward_section_keeps_its_heading(tmp_path):
    """Chunk 0 has no predecessor, so it absorbs forward - heading line included.

    Without it the second section's heading survives in neither the merged text
    nor the `heading` column, and its content reads as part of the first
    section: a mis-attributed excerpt, and a term lost from the index.
    """
    chunks = chunk_index.chunk_minutes(
        _write_minutes(
            tmp_path / "short-open.md",
            "# Standup\n\nQuick sync.\n\n## Quagga Migration\n\n" + FILLER * 3,
        ),
        meeting_id="m1",
    )

    assert len(chunks) >= 1
    assert "Quick sync." in chunks[0].text
    assert "## Quagga Migration" in chunks[0].text


def test_a_pulled_forward_heading_is_still_searchable(manifest, tmp_path):
    _add_meeting(
        manifest,
        "m1",
        "2026-06-09",
        _write_minutes(
            tmp_path / "short-open.md",
            "# Standup\n\nQuick sync.\n\n## Quagga Migration\n\n" + FILLER * 3,
        ),
    )
    chunk_index.reindex_all(manifest)

    hits = chunk_index.search_chunks(manifest, "Quagga")

    assert [hit.meeting_id for hit in hits] == ["m1"]


# ── Atomicity ─────────────────────────────────────────────────────────

class _ExplodesOnWrite:
    """A connection that fails exactly where a replace is half-finished.

    sqlite3.Connection is a C type and its methods cannot be monkeypatched, so
    the failure is injected by proxy. `reindex_meeting` reaches this only after
    its DELETE has run.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")


def test_a_failed_replace_leaves_the_old_chunks_intact(manifest, tmp_path):
    """Half a meeting in the index is worse than a stale one.

    A stale meeting answers with last week's text under an honest citation. A
    half-replaced one answers with part of its own minutes and nothing says the
    rest is missing.
    """
    _add_meeting(manifest, "m1", "2026-06-09", _sample_minutes(tmp_path, "a.md"))
    chunk_index.reindex_all(manifest)

    def snapshot() -> list[tuple]:
        return [
            tuple(row)
            for row in manifest.execute(
                "SELECT chunk_id, ordinal, content_hash FROM minute_chunks ORDER BY ordinal"
            )
        ]

    before = snapshot()
    assert before

    with pytest.raises(sqlite3.OperationalError):
        chunk_index.reindex_meeting(_ExplodesOnWrite(manifest), "m1", force=True)

    assert snapshot() == before
    manifest.execute("INSERT INTO minute_chunks_fts(minute_chunks_fts) VALUES('integrity-check')")


# ── Cascade ───────────────────────────────────────────────────────────

def test_deleting_a_meeting_removes_its_chunks_and_its_fts_rows(manifest, tmp_path):
    """The FK cascade and the FTS delete trigger must both fire.

    A stale FTS index is the dangerous half: it keeps matching a deleted
    meeting's terms, and because the table is external-content the rowids it
    hands back can then resolve against whatever now occupies them - another
    meeting's text under the deleted meeting's citation.
    """
    _add_meeting(
        manifest,
        "gone",
        "2026-06-09",
        _write_minutes(
            tmp_path / "gone.md",
            "# Gone\n\nThe quagga control inventory was discussed. " + FILLER * 2,
        ),
    )
    _add_meeting(manifest, "kept", "2026-06-10", _sample_minutes(tmp_path, "kept.md"))
    chunk_index.reindex_all(manifest)

    def fts_hits(term: str) -> int:
        return manifest.execute(
            "SELECT COUNT(*) FROM minute_chunks_fts WHERE minute_chunks_fts MATCH ?", (term,)
        ).fetchone()[0]

    assert fts_hits('"quagga"') == 1
    kept_before = manifest.execute(
        "SELECT COUNT(*) FROM minute_chunks WHERE meeting_id = 'kept'"
    ).fetchone()[0]

    assert db.delete_meeting(manifest, "gone") is True

    assert manifest.execute(
        "SELECT COUNT(*) FROM minute_chunks WHERE meeting_id = 'gone'"
    ).fetchone()[0] == 0
    assert fts_hits('"quagga"') == 0
    assert chunk_index.search_chunks(manifest, "quagga") == []
    assert manifest.execute(
        "SELECT COUNT(*) FROM minute_chunks WHERE meeting_id = 'kept'"
    ).fetchone()[0] == kept_before
    # FTS5's own reconciliation of the index against the content table. This is
    # what catches a delete trigger that silently did not fire.
    manifest.execute("INSERT INTO minute_chunks_fts(minute_chunks_fts) VALUES('integrity-check')")


def test_the_cascade_holds_without_the_explicit_delete(manifest, tmp_path):
    """`delete_meeting`'s explicit DELETE is belt-and-braces, not the mechanism."""
    _add_meeting(manifest, "m1", "2026-06-09", _sample_minutes(tmp_path, "a.md"))
    chunk_index.reindex_all(manifest)
    assert manifest.execute("SELECT COUNT(*) FROM minute_chunks").fetchone()[0] > 0

    manifest.execute("DELETE FROM meetings WHERE id = 'm1'")

    assert manifest.execute("SELECT COUNT(*) FROM minute_chunks").fetchone()[0] == 0
    manifest.execute("INSERT INTO minute_chunks_fts(minute_chunks_fts) VALUES('integrity-check')")


def test_a_chunk_cannot_name_a_meeting_that_does_not_exist(manifest):
    """GC3 at the schema level: no citation without a meeting behind it."""
    with pytest.raises(sqlite3.IntegrityError):
        manifest.execute(
            """
            INSERT INTO minute_chunks (
                chunk_id, meeting_id, meeting_date, source_path, ordinal, heading,
                text, context_header, char_count, content_hash, indexed_at
            ) VALUES ('ghost:0000', 'ghost', '2026-06-09', '/x.md', 0, NULL,
                      'orphan', NULL, 6, 'deadbeef', '2026-06-09T00:00:00')
            """
        )


def test_a_pre_foreign_key_chunk_table_is_migrated_not_kept(tmp_path):
    """An index built before the FK existed is dropped and rebuilt, not inherited.

    CREATE TABLE IF NOT EXISTS would skip the new definition silently, leaving
    an installed manifest permanently on the old shape while every test passed
    against a table created from scratch.
    """
    db_path = tmp_path / "legacy.db"
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        conn.executescript(
            """
            DROP TRIGGER minute_chunks_ai;
            DROP TRIGGER minute_chunks_ad;
            DROP TRIGGER minute_chunks_au;
            DROP TABLE minute_chunks_fts;
            DROP TABLE minute_chunks;
            CREATE TABLE minute_chunks (
                chunk_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, meeting_date TEXT,
                source_path TEXT NOT NULL, ordinal INTEGER NOT NULL, heading TEXT,
                text TEXT NOT NULL, context_header TEXT, char_count INTEGER NOT NULL,
                content_hash TEXT NOT NULL, indexed_at TEXT NOT NULL
            );
            """
        )
        assert not conn.execute("PRAGMA foreign_key_list(minute_chunks)").fetchall()

    db.init_db(db_path)

    with db.connect(db_path) as conn:
        keys = conn.execute("PRAGMA foreign_key_list(minute_chunks)").fetchall()
        assert [(k["table"], k["to"], k["on_delete"]) for k in keys] == [
            ("meetings", "id", "CASCADE")
        ]
        # The FTS table and all three triggers came back with it.
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'minute_chunks%'"
            )
        }
        assert {"minute_chunks_fts", "minute_chunks_ai", "minute_chunks_ad",
                "minute_chunks_au"} <= names
        _add_meeting(conn, "m1", "2026-06-09", _sample_minutes(tmp_path, "a.md"))
        assert chunk_index.reindex_meeting(conn, "m1") > 0
        conn.execute("INSERT INTO minute_chunks_fts(minute_chunks_fts) VALUES('integrity-check')")


def test_missing_table_returns_empty_with_a_reason(tmp_path):
    """GC5: an unbuilt index answers empty, not with a stack trace."""
    conn = sqlite3.connect(tmp_path / "bare.db")
    conn.row_factory = sqlite3.Row
    try:
        assert chunk_index.search_chunks(conn, "control inventory") == []
        searchable, reason = chunk_index.index_status(conn)
        assert not searchable
        assert "not built" in reason
    finally:
        conn.close()


def test_an_empty_index_is_reported_as_empty_not_as_missing(manifest):
    """An unbuilt index and an empty one are different problems."""
    searchable, reason = chunk_index.index_status(manifest)
    assert not searchable
    assert "empty" in reason


def test_cli_chunk_index_reports_counts(manifest, tmp_path, capsys):
    """The CLI adapter reports what happened; the module decides what happens."""
    from pipeline import cli

    _add_meeting(manifest, "m1", "2026-06-09", _sample_minutes(tmp_path, "a.md"))
    manifest.commit()

    assert cli.main(["chunk-index"]) == 0
    out = capsys.readouterr().out
    assert "1 meeting(s) with minutes: 1 reindexed" in out
    assert "chunk(s)" in out

    # Second run is a no-op, and says so rather than claiming work.
    assert cli.main(["chunk-index"]) == 0
    assert "0 reindexed, 1 unchanged" in capsys.readouterr().out

    assert cli.main(["chunk-index", "--meeting", "m1", "--rebuild"]) == 0
    assert "indexed" in capsys.readouterr().out

    # A mistyped id must not read as "nothing to do".
    assert cli.main(["chunk-index", "--meeting", "typo"]) == 1
    assert "No meeting typo" in capsys.readouterr().err
