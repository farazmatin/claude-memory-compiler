"""Dense vector index over the chunks the BM25 index already serves.

LightRAG was meant to be the semantic half of retrieval and never worked - it
reports 0 of 129 documents processed - so this is the first semantic retrieval
in the system and nothing downstream has a fallback to compare it against.
These tests pin the four properties that decide whether it can be trusted:

* a vector survives SQLite unchanged, so a score is computed on the numbers the
  model actually produced;
* a vector is only ever compared against one built by the same model;
* a vector whose chunk text has changed underneath it is rebuilt, not scored;
* and none of it can take the BM25 path down when the model is missing.

No test may download a model. The embedder is injected through the same
`_load_embedder` seam production loads the real one through, and returns
hash-seeded vectors - deterministic, and explicit about which passages a test
considers related, which a real model's opinion would not be.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import numpy as np
import pytest

from pipeline import chunk_index, db, dense_index

REPO_ROOT = Path(__file__).resolve().parent.parent

# Long enough that a section built from it clears chunk_index's 200-char floor
# without any test needing to count characters by hand.
FILLER = (
    "The team reviewed the control inventory and agreed the evidence trail has to "
    "survive an audit without anyone reconstructing it from memory afterwards. "
    "Ownership stays with the first line while the second line reviews sampling. "
)

DIM = 8


# ── Fake embedder ─────────────────────────────────────────────────────

class FakeEmbedder:
    """Deterministic stand-in for fastembed's TextEmbedding.

    A document embeds to a hash-seeded random point unless it contains one of
    the `pinned` markers. Pinning is how a test states that two passages are
    related: with a real model that relationship would be the model's opinion,
    and a ranking assertion resting on it would be untestable.

    `documents` records every string handed to the model, in order, which is
    what lets a test see whether the context header was actually embedded.
    """

    def __init__(self, *, pinned: dict[str, list[float]] | None = None) -> None:
        self.pinned = pinned or {}
        self.documents: list[str] = []
        self.batches = 0

    def embed(self, documents, batch_size: int = 256, **kwargs):
        self.batches += 1
        for document in list(documents):
            self.documents.append(document)
            yield self._vector(document)

    def _vector(self, document: str):
        for marker, vector in self.pinned.items():
            if marker in document:
                assert len(vector) == DIM, "pinned vectors must match the fake dimension"
                return np.asarray(vector, dtype="float32")
        seed = int.from_bytes(hashlib.sha256(document.encode("utf-8")).digest()[:8], "big")
        return np.random.default_rng(seed).standard_normal(DIM).astype("float32")


def _install(monkeypatch, fake):
    """Put `fake` behind the production loader and clear the module cache.

    Setting the cached instance through monkeypatch rather than assigning it
    means the teardown restores None, so the "import loads no model" invariant
    is not quietly consumed by whichever test happened to run first.
    """
    monkeypatch.setattr(dense_index, "_load_embedder", lambda model: fake)
    monkeypatch.setattr(dense_index, "_EMBEDDER", None)
    monkeypatch.setattr(dense_index, "_EMBEDDER_MODEL", None)
    monkeypatch.setattr(dense_index, "_LOAD_FAILURE", None)
    return fake


@pytest.fixture()
def embedder(monkeypatch):
    return _install(monkeypatch, FakeEmbedder())


# ── Corpus helpers ────────────────────────────────────────────────────

def _add_meeting(conn, meeting_id: str, date: str, minutes: Path) -> None:
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


def _meeting(conn, tmp_path: Path, meeting_id: str, body: str, date: str = "2026-06-09") -> Path:
    path = tmp_path / f"{meeting_id}.md"
    path.write_text(body, encoding="utf-8")
    _add_meeting(conn, meeting_id, date, path)
    return path


def _indexed(conn, tmp_path: Path, bodies: dict[str, str], dates: dict[str, str] | None = None):
    """A chunked corpus, ready to embed."""
    dates = dates or {}
    for meeting_id, body in bodies.items():
        _meeting(conn, tmp_path, meeting_id, body, date=dates.get(meeting_id, "2026-06-09"))
    chunk_index.reindex_all(conn)


def _plain(title: str, repeats: int = 2, marker: str = "") -> str:
    """Minutes whose body carries `marker`.

    The marker goes in the body and never the heading: `chunk_index` lifts a
    heading line into its own column, so a marker placed there never reaches the
    text that gets embedded, and every fixture would come back as the same
    hash-seeded point.
    """
    lead = f"{marker} " if marker else ""
    return f"# {title}\n\n{lead}The control inventory was discussed. " + FILLER * repeats


# ── 1. Round trip ─────────────────────────────────────────────────────

def test_a_stored_vector_reads_back_bit_identical(manifest, tmp_path, monkeypatch):
    """The blob is the model's numbers, not an approximation of them.

    A float32 that survives the round trip as anything else turns every later
    cosine into a quiet lie, and a lie of that size ranks plausibly.
    """
    awkward = [0.1, -2.5, 3.14159265, 1e-7, -0.0, 65504.0, 1.0, 0.3333333333]
    _install(monkeypatch, FakeEmbedder(pinned={"AWKWARD": awkward}))
    _indexed(manifest, tmp_path, {"m1": _plain("Review", marker="AWKWARD")})

    stats = dense_index.embed_chunks(manifest)

    assert stats["embedded"] == 1
    row = manifest.execute(
        "SELECT vector, dim, model, content_hash FROM chunk_vectors WHERE chunk_id = 'm1:0000'"
    ).fetchone()
    assert row["dim"] == DIM
    assert row["model"] == dense_index.EMBED_MODEL
    # Byte comparison, not np.allclose: "close enough" is exactly the failure
    # this guards against.
    assert row["vector"] == np.asarray(awkward, dtype="<f4").tobytes()
    assert np.frombuffer(row["vector"], dtype="<f4").tolist() == (
        np.asarray(awkward, dtype="<f4").tolist()
    )
    stored_hash = manifest.execute(
        "SELECT content_hash FROM minute_chunks WHERE chunk_id = 'm1:0000'"
    ).fetchone()[0]
    assert row["content_hash"] == stored_hash


# ── 2. Models never mix ───────────────────────────────────────────────

def test_a_vector_from_another_model_is_never_searched(manifest, tmp_path, embedder):
    """Two models' vectors are not on the same scale; comparing them ranks noise."""
    _indexed(manifest, tmp_path, {"m1": _plain("First"), "m2": _plain("Second")})
    dense_index.embed_chunks(manifest)

    both = dense_index.search_dense(manifest, "control inventory", max_chars=100_000)
    assert {hit.meeting_id for hit in both} == {"m1", "m2"}

    manifest.execute("UPDATE chunk_vectors SET model = 'other/model' WHERE chunk_id LIKE 'm2:%'")

    hits = dense_index.search_dense(manifest, "control inventory", max_chars=100_000)
    assert hits, "the active model's vectors must still answer"
    assert {hit.meeting_id for hit in hits} == {"m1"}

    # And a chunk carrying only another model's vector is work to do, not a
    # chunk that has been embedded.
    again = dense_index.embed_chunks(manifest)
    assert again["embedded"] > 0
    assert {hit.meeting_id for hit in dense_index.search_dense(
        manifest, "control inventory", max_chars=100_000
    )} == {"m1", "m2"}


# ── 3. Cosine ranking ─────────────────────────────────────────────────

def test_the_semantically_nearest_vector_ranks_first(manifest, tmp_path, monkeypatch):
    near = [1.0, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    middle = [0.5, 0.866, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    far = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    _install(
        monkeypatch,
        FakeEmbedder(
            pinned={
                "who owns sampling": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "NEAR": near,
                "MIDDLE": middle,
                "FAR": far,
            }
        ),
    )
    _indexed(
        manifest,
        tmp_path,
        {
            "far": _plain("Archive cleanup", marker="FAR"),
            "middle": _plain("Sampling cadence", marker="MIDDLE"),
            "near": _plain("Sampling ownership", marker="NEAR"),
        },
    )
    dense_index.embed_chunks(manifest)

    hits = dense_index.search_dense(manifest, "who owns sampling", max_chars=100_000)

    assert [hit.meeting_id for hit in hits] == ["near", "middle", "far"]
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)
    # Orthogonal is zero, not "half related". A linear remap of [-1, 1] onto
    # [0, 1] would report this unrelated passage at 0.5.
    assert hits[-1].score == pytest.approx(0.0)
    assert hits[0].score > 0.99


# ── 4. as_of and exclude_meeting_ids ──────────────────────────────────

def test_as_of_excludes_later_meetings_and_exclude_ids_drops_the_source(
    manifest, tmp_path, embedder
):
    _indexed(
        manifest,
        tmp_path,
        {"early": _plain("Early"), "mid": _plain("Mid"), "late": _plain("Late")},
        dates={"early": "2026-06-01", "mid": "2026-06-15", "late": "2026-07-01"},
    )
    dense_index.embed_chunks(manifest)

    everything = dense_index.search_dense(manifest, "control inventory", max_chars=100_000)
    assert {hit.meeting_id for hit in everything} == {"early", "mid", "late"}

    bounded = dense_index.search_dense(
        manifest, "control inventory", as_of="2026-06-15", max_chars=100_000
    )
    assert {hit.meeting_id for hit in bounded} == {"early", "mid"}

    without_source = dense_index.search_dense(
        manifest,
        "control inventory",
        exclude_meeting_ids=frozenset({"mid"}),
        max_chars=100_000,
    )
    assert {hit.meeting_id for hit in without_source} == {"early", "late"}


def test_an_undated_meeting_is_excluded_by_as_of(manifest, tmp_path, embedder):
    """Same narrowing BM25 applies: undated cannot be shown to precede the cutoff."""
    _indexed(manifest, tmp_path, {"dated": _plain("Dated")}, dates={"dated": "2026-06-01"})
    path = tmp_path / "undated.md"
    path.write_text(_plain("Undated"), encoding="utf-8")
    _add_meeting(manifest, "undated", "2026-06-01", path)
    manifest.execute("UPDATE meetings SET meeting_date = NULL WHERE id = 'undated'")
    chunk_index.reindex_all(manifest)
    dense_index.embed_chunks(manifest)

    assert {
        hit.meeting_id
        for hit in dense_index.search_dense(manifest, "control inventory", max_chars=100_000)
    } == {"dated", "undated"}
    assert {
        hit.meeting_id
        for hit in dense_index.search_dense(
            manifest, "control inventory", as_of="2026-07-01", max_chars=100_000
        )
    } == {"dated"}


# ── 5. Per-meeting cap ────────────────────────────────────────────────

def test_one_verbose_meeting_cannot_fill_the_result_set(manifest, tmp_path, embedder):
    verbose = "".join(
        f"## Section {n}\n\nThe control inventory was discussed at length. {FILLER * 2}\n\n"
        for n in range(10)
    )
    _indexed(manifest, tmp_path, {"verbose": verbose, "brief": _plain("Short meeting")})
    dense_index.embed_chunks(manifest)

    stored = manifest.execute(
        "SELECT COUNT(*) FROM chunk_vectors v JOIN minute_chunks c ON c.chunk_id = v.chunk_id "
        "WHERE c.meeting_id = 'verbose'"
    ).fetchone()[0]
    assert stored >= 10, "fixture must give the verbose meeting many embedded chunks"

    hits = dense_index.search_dense(manifest, "control inventory", max_chars=100_000)

    # The literal 2, not the constant: reading MAX_CHUNKS_PER_MEETING back would
    # make this assertion agree with whatever the cap happens to be.
    assert len([hit for hit in hits if hit.meeting_id == "verbose"]) <= 2
    assert chunk_index.MAX_CHUNKS_PER_MEETING == 2
    assert any(hit.meeting_id == "brief" for hit in hits)


# ── 6. max_chars and limit ────────────────────────────────────────────

def test_a_hit_that_does_not_fit_is_marked_or_dropped_never_silently_severed(
    manifest, tmp_path, embedder
):
    """The trim rule is shared with BM25, not re-implemented beside it.

    Task 5 fuses the two lists, so a dense excerpt that got cut on a different
    boundary - or worse, cut without the ellipsis - would put an unmarked
    partial quote into the same list as a marked one.
    """
    _indexed(manifest, tmp_path, {f"m{n}": _plain(f"Meeting {n}") for n in range(3)})
    dense_index.embed_chunks(manifest)

    whole = dense_index.search_dense(manifest, "control inventory", max_chars=100_000)
    assert len(whole) == 3
    top = len(whole[0].text)
    assert all(len(hit.text) > chunk_index.MIN_CHUNK_CHARS + 50 for hit in whole)

    marked = dense_index.search_dense(manifest, "control inventory", max_chars=top + 300)
    assert sum(len(hit.text) for hit in marked) <= top + 300
    assert marked[0].text == whole[0].text
    assert marked[-1].text.endswith("…")
    assert len(marked[-1].text) >= chunk_index.MIN_CHUNK_CHARS
    assert marked[-1].text[:-1] in whole[1].text

    dropped = dense_index.search_dense(manifest, "control inventory", max_chars=top + 50)
    assert [hit.text for hit in dropped] == [whole[0].text]
    assert not any(hit.text.endswith("…") for hit in dropped)


def test_limit_and_max_chars_are_both_honoured(manifest, tmp_path, embedder):
    _indexed(manifest, tmp_path, {f"m{n}": _plain(f"Meeting {n}", repeats=4) for n in range(6)})
    dense_index.embed_chunks(manifest)

    budgeted = dense_index.search_dense(manifest, "control inventory", max_chars=1500)
    assert budgeted
    assert sum(len(hit.text) for hit in budgeted) <= 1500

    counted = dense_index.search_dense(manifest, "control inventory", limit=2, max_chars=100_000)
    assert len(counted) == 2


# ── 7. Idempotence ────────────────────────────────────────────────────

def test_a_second_embed_run_embeds_nothing(manifest, tmp_path, embedder):
    _indexed(manifest, tmp_path, {"m1": _plain("First"), "m2": _plain("Second")})

    first = dense_index.embed_chunks(manifest)
    assert first["chunks"] > 0
    assert first["embedded"] == first["chunks"]
    assert first["current"] == 0

    seen = len(embedder.documents)
    second = dense_index.embed_chunks(manifest)

    assert second["embedded"] == 0
    assert second["stale"] == 0
    assert second["current"] == second["chunks"] == first["chunks"]
    assert len(embedder.documents) == seen, "the model was asked to embed again"

    forced = dense_index.embed_chunks(manifest, force=True)
    assert forced["embedded"] == forced["chunks"]


def test_an_interrupted_build_keeps_the_batches_it_finished(manifest, tmp_path, monkeypatch):
    """A corpus build is tens of minutes, so a crash must not cost all of it.

    Measured on the real corpus this is about a chunk a second on a loaded
    laptop - 2,809 of them. Losing everything to an interrupt at chunk 2,700
    would mean starting from zero, and this is meant to run unattended.
    """

    class FailsPartway(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def embed(self, documents, batch_size: int = 256, **kwargs):
            self.attempts += 1
            if self.attempts > 2:
                raise RuntimeError("the model died")
            yield from super().embed(documents, batch_size=batch_size)

    _install(monkeypatch, FailsPartway())
    _indexed(manifest, tmp_path, {f"m{n}": _plain(f"Meeting {n}") for n in range(3)})
    # Committed first, so the connection is not mid-transaction when the embed
    # starts. That is the condition under which embed_chunks commits at all.
    manifest.commit()
    assert manifest.in_transaction is False

    with pytest.raises(dense_index.DenseIndexError):
        dense_index.embed_chunks(manifest, batch_size=1)

    # Read through a second connection: what this connection can still see says
    # nothing about what survived.
    other = sqlite3.connect(db.DB_PATH)
    try:
        assert other.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 2
    finally:
        other.close()


def test_a_caller_mid_transaction_is_not_committed_behind_its_back(
    manifest, tmp_path, monkeypatch
):
    """The other half of the same rule. `chunk_index` draws the line the same way."""

    class FailsPartway(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def embed(self, documents, batch_size: int = 256, **kwargs):
            self.attempts += 1
            if self.attempts > 2:
                raise RuntimeError("the model died")
            yield from super().embed(documents, batch_size=batch_size)

    _install(monkeypatch, FailsPartway())
    _indexed(manifest, tmp_path, {f"m{n}": _plain(f"Meeting {n}") for n in range(3)})
    assert manifest.in_transaction is True, "the chunk writes must still be open"

    with pytest.raises(dense_index.DenseIndexError):
        dense_index.embed_chunks(manifest, batch_size=1)

    other = sqlite3.connect(db.DB_PATH)
    try:
        assert other.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0
    finally:
        other.close()


# ── 8. content_hash is the invalidation ───────────────────────────────

def test_a_chunk_whose_text_changed_is_re_embedded_and_its_neighbours_are_not(
    manifest, tmp_path, embedder
):
    """The foreign key does not cover this and must not be trusted to.

    `reindex_meeting` deletes and re-inserts a changed meeting's chunks, so
    "m1:0001" after an edit is a different passage at the same id. ON DELETE
    CASCADE only fires on a connection with `PRAGMA foreign_keys = ON`, which
    SQLite defaults to OFF - so the vector row can survive, still resolving,
    now describing text nobody can read. This is that state.
    """
    _indexed(manifest, tmp_path, {"m1": _plain("First", repeats=2) + "\n\n## Risks\n\n" + FILLER * 3})
    dense_index.embed_chunks(manifest)
    assert manifest.execute("SELECT COUNT(*) FROM minute_chunks").fetchone()[0] >= 2

    def snapshot():
        return {
            row["chunk_id"]: (row["vector"], row["content_hash"], row["embedded_at"])
            for row in manifest.execute(
                "SELECT chunk_id, vector, content_hash, embedded_at FROM chunk_vectors"
            )
        }

    before = snapshot()
    replacement = "Entirely different text about the archive retention schedule. " + FILLER
    manifest.execute(
        "UPDATE minute_chunks SET text = ?, content_hash = ? WHERE chunk_id = 'm1:0001'",
        (replacement, hashlib.sha256(replacement.encode("utf-8")).hexdigest()),
    )

    stats = dense_index.embed_chunks(manifest)

    assert stats["stale"] == 1
    assert stats["embedded"] == 1
    after = snapshot()
    assert after["m1:0001"] != before["m1:0001"]
    assert after["m1:0001"][1] == hashlib.sha256(replacement.encode("utf-8")).hexdigest()
    untouched = {k: v for k, v in after.items() if k != "m1:0001"}
    assert untouched == {k: v for k, v in before.items() if k != "m1:0001"}

    # And until it is rebuilt, a stale vector is not scored at all: it would
    # rank the query against a passage the reader can no longer be shown.
    manifest.execute("UPDATE minute_chunks SET content_hash = 'drifted' WHERE chunk_id = 'm1:0000'")
    hits = dense_index.search_dense(manifest, "control inventory", max_chars=100_000)
    assert "m1:0000" not in {hit.chunk_id for hit in hits}
    searchable, reason = dense_index.dense_status(manifest)
    assert searchable
    assert "stale" in reason


# ── 9. The context header is embedded ─────────────────────────────────

def test_the_context_header_is_part_of_the_embedded_text(manifest, tmp_path, embedder):
    """Task 6 populates the column; the wiring has to exist before the writer.

    A chunk is a mid-document passage. "The second line reviews sampling" is
    unreachable by a query naming the programme unless the header rides along
    into the vector, which is the entire reason the column exists.
    """
    _indexed(manifest, tmp_path, {"m1": _plain("First")})
    header = "Unified System of Controls / 2026-06-09"
    manifest.execute(
        "UPDATE minute_chunks SET context_header = ? WHERE chunk_id = 'm1:0000'", (header,)
    )

    dense_index.embed_chunks(manifest)

    body = manifest.execute(
        "SELECT text FROM minute_chunks WHERE chunk_id = 'm1:0000'"
    ).fetchone()[0]
    assert embedder.documents == [f"{header}\n\n{body}"]

    # A header that is blank is absent, not a pair of leading newlines in front
    # of every vector in the corpus.
    manifest.execute("UPDATE minute_chunks SET context_header = '   ' WHERE chunk_id = 'm1:0000'")
    embedder.documents.clear()
    dense_index.embed_chunks(manifest, force=True)
    assert embedder.documents == [body]


# ── 10. Degrade explicitly ────────────────────────────────────────────

def test_a_missing_model_empties_the_dense_path_without_touching_bm25(
    manifest, tmp_path, monkeypatch
):
    """The load-bearing one. This runs on a laptop that may never download 67 MB.

    A dense half that raises when the model is absent would make the system
    worse than the BM25-only state it replaced.
    """
    _install(monkeypatch, FakeEmbedder())
    _indexed(manifest, tmp_path, {"m1": _plain("First"), "m2": _plain("Second")})
    dense_index.embed_chunks(manifest)
    assert dense_index.search_dense(manifest, "control inventory", max_chars=100_000)

    def unavailable(model):
        raise OSError("no network and no cached model")

    monkeypatch.setattr(dense_index, "_load_embedder", unavailable)
    monkeypatch.setattr(dense_index, "_EMBEDDER", None)
    monkeypatch.setattr(dense_index, "_EMBEDDER_MODEL", None)

    assert dense_index.search_dense(manifest, "control inventory", max_chars=100_000) == []
    searchable, reason = dense_index.dense_status(manifest)
    assert searchable is False
    assert "no network and no cached model" in reason

    # BM25 is untouched by any of it.
    assert chunk_index.search_chunks(manifest, "control inventory", max_chars=100_000)
    assert chunk_index.index_status(manifest)[0] is True

    # embed_chunks is the one entry point that reports rather than degrades -
    # silently writing no vectors would leave the operator believing the index
    # is built.
    manifest.execute("DELETE FROM chunk_vectors")
    with pytest.raises(dense_index.DenseIndexError):
        dense_index.embed_chunks(manifest)


def test_rerank_without_a_model_returns_the_input_order_truncated(monkeypatch, capsys):
    hits = [
        chunk_index.ChunkHit(
            chunk_id=f"m1:000{n}",
            meeting_id="m1",
            meeting_date="2026-06-09",
            source_path="/minutes/m1.md",
            ordinal=n,
            heading=None,
            text=f"passage {n}",
            score=1.0 - n / 10,
        )
        for n in range(5)
    ]

    def unavailable(model):
        raise OSError("reranker weights are not on this machine")

    monkeypatch.setattr(dense_index, "_load_reranker", unavailable)
    monkeypatch.setattr(dense_index, "_RERANKER", None)
    monkeypatch.setattr(dense_index, "_RERANKER_MODEL", None)
    monkeypatch.setattr(dense_index, "_RERANKER_WARNED", False)

    out = dense_index.rerank("who owns sampling", hits, top_n=3)
    again = dense_index.rerank("who owns sampling", hits, top_n=3)

    assert out == again == hits[:3]
    assert dense_index.rerank("who owns sampling", []) == []
    # Once per process, not once per retrieval - two calls above, one line.
    # On a machine with no reranker the degraded path is the normal path, and a
    # line per query is noise that trains the reader to ignore it.
    assert capsys.readouterr().out.count("reranker unavailable") == 1


def test_rerank_reorders_and_rescores_when_the_model_is_there(monkeypatch):
    hits = [
        chunk_index.ChunkHit(
            chunk_id=f"m1:000{n}",
            meeting_id="m1",
            meeting_date="2026-06-09",
            source_path="/minutes/m1.md",
            ordinal=n,
            heading=None,
            text=f"passage {n}",
            score=0.9,
        )
        for n in range(3)
    ]

    class FakeCrossEncoder:
        def rerank(self, query, documents, **kwargs):
            # "passage 2" is the answer; the dense order put it last.
            return [-4.0, 0.0, 6.0][: len(list(documents))]

    monkeypatch.setattr(dense_index, "_load_reranker", lambda model: FakeCrossEncoder())
    monkeypatch.setattr(dense_index, "_RERANKER", None)
    monkeypatch.setattr(dense_index, "_RERANKER_MODEL", None)

    out = dense_index.rerank("who owns sampling", hits, top_n=2)

    assert [hit.chunk_id for hit in out] == ["m1:0002", "m1:0001"]
    # The score has to follow the order it now claims: a list sorted by one
    # number and labelled with another contradicts itself.
    assert out[0].score > out[1].score
    assert all(0.0 <= hit.score <= 1.0 for hit in out)
    assert out[0].text == "passage 2"


# ── 11. Import loads nothing ──────────────────────────────────────────

def test_importing_the_module_loads_no_model():
    """In a subprocess, because in-process this can only ever be a weaker claim.

    Every BM25 search imports this module transitively through Task 5. If the
    import touched the model cache or the network, `pipeline chunk-index` on a
    fresh laptop would block on a 67 MB download it does not need.
    """
    probe = (
        "import sys\n"
        "from pipeline import dense_index\n"
        "assert dense_index._EMBEDDER is None, 'an embedder was constructed at import'\n"
        "assert dense_index._RERANKER is None, 'a reranker was constructed at import'\n"
        "assert 'fastembed' not in sys.modules, 'fastembed was imported at import time'\n"
        "print('clean')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


# ── 12. One hit shape for both halves ─────────────────────────────────

def test_a_dense_hit_is_the_type_bm25_returns(manifest, tmp_path, embedder):
    """Not a look-alike. Task 5 fuses the two lists without adapting either."""
    assert dense_index.ChunkHit is chunk_index.ChunkHit

    _indexed(manifest, tmp_path, {"m1": _plain("First"), "m2": _plain("Second")})
    dense_index.embed_chunks(manifest)

    dense = dense_index.search_dense(manifest, "control inventory", max_chars=100_000)
    bm25 = chunk_index.search_chunks(manifest, "control inventory", max_chars=100_000)
    assert dense and bm25

    by_id = {hit.chunk_id: hit for hit in bm25}
    shared = [hit for hit in dense if hit.chunk_id in by_id]
    assert shared, "the two halves must overlap on this corpus"
    for hit in shared:
        for field in fields(chunk_index.ChunkHit):
            if field.name == "score":
                continue
            assert getattr(hit, field.name) == getattr(by_id[hit.chunk_id], field.name)
    assert all(0.0 <= hit.score <= 1.0 for hit in dense)


# ── Status, the reason channel ────────────────────────────────────────

def test_dense_status_distinguishes_unbuilt_from_empty_from_ready(manifest, tmp_path, embedder):
    searchable, reason = dense_index.dense_status(manifest)
    assert searchable is False
    assert "dense-index" in reason

    _indexed(manifest, tmp_path, {"m1": _plain("First")})
    searchable, reason = dense_index.dense_status(manifest)
    assert searchable is False
    assert dense_index.EMBED_MODEL in reason

    dense_index.embed_chunks(manifest)
    searchable, reason = dense_index.dense_status(manifest)
    assert searchable is True
    assert dense_index.EMBED_MODEL in reason


def test_a_query_with_nothing_in_it_returns_empty_without_loading_a_model(
    manifest, tmp_path, embedder
):
    _indexed(manifest, tmp_path, {"m1": _plain("First")})
    dense_index.embed_chunks(manifest)
    embedder.documents.clear()

    assert dense_index.search_dense(manifest, "   ") == []
    assert embedder.documents == []


# ── Deleting a meeting ────────────────────────────────────────────────

def test_deleting_a_meeting_takes_its_vectors_with_it(manifest, tmp_path, embedder):
    """Without relying on the cascade, which is why the delete is explicit.

    `PRAGMA foreign_keys` is per-connection and defaults OFF, so a maintenance
    script opening the manifest with a bare sqlite3.connect() deletes a meeting
    and leaves its vectors behind - scoring queries against passages whose
    citation names a meeting the manifest can no longer produce.
    """
    _indexed(manifest, tmp_path, {"m1": _plain("First"), "m2": _plain("Second")})
    dense_index.embed_chunks(manifest)
    manifest.commit()
    manifest.execute("PRAGMA foreign_keys = OFF")
    assert manifest.execute("PRAGMA foreign_keys").fetchone()[0] == 0, "the pragma did not take"

    assert db.delete_meeting(manifest, "m1") is True

    remaining = {row[0] for row in manifest.execute("SELECT chunk_id FROM chunk_vectors")}
    assert remaining, "m2's vectors must survive"
    assert not [chunk_id for chunk_id in remaining if chunk_id.startswith("m1:")]


# ── The CLI surface ───────────────────────────────────────────────────

def test_the_cli_reports_what_it_built(manifest, tmp_path, embedder, capsys):
    from pipeline import cli

    _indexed(manifest, tmp_path, {"m1": _plain("First")})
    manifest.commit()

    assert cli.cmd_dense_index(argparse.Namespace(rebuild=False, model=None)) == 0
    out = capsys.readouterr().out

    assert "1 embedded" in out
    assert dense_index.EMBED_MODEL in out


def test_the_cli_reports_a_missing_model_and_says_bm25_still_works(
    manifest, tmp_path, monkeypatch, capsys
):
    """Non-zero and a sentence, not a traceback.

    An operator running this overnight on a laptop with no model cached needs to
    learn two things: it did not build, and the rest of retrieval is fine.
    """
    from pipeline import cli

    _install(monkeypatch, FakeEmbedder())
    _indexed(manifest, tmp_path, {"m1": _plain("First")})
    manifest.commit()

    def unavailable(model):
        raise OSError("no network and no cached model")

    monkeypatch.setattr(dense_index, "_load_embedder", unavailable)

    assert cli.cmd_dense_index(argparse.Namespace(rebuild=False, model=None)) == 1
    captured = capsys.readouterr()

    assert "no network and no cached model" in captured.err
    assert "BM25 retrieval is unaffected" in captured.err
    assert "Traceback" not in captured.err
