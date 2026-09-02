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

from pathlib import Path

from pipeline import chunk_index, db

# Long enough that a section built from it clears the 200-char floor without
# any test needing to count characters by hand.
FILLER = (
    "The team reviewed the control inventory and agreed the evidence trail has to "
    "survive an audit without anyone reconstructing it from memory afterwards. "
    "Ownership stays with the first line while the second line reviews sampling. "
)


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

    hits = chunk_index.search_chunks(manifest, "USC control inventory")

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
    assert chunk_index.reindex_meeting(manifest, "absent") == 0
    assert chunk_index.reindex_meeting(manifest, "no-such-meeting") == 0


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


def test_missing_table_returns_empty_with_a_reason(tmp_path):
    """GC5: an unbuilt index answers empty, not with a stack trace."""
    import sqlite3

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
