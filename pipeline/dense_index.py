"""Dense vector index over the chunks the BM25 index already serves.

`chunk_index` finds the passage that uses the caller's words. This finds the one
that means the same thing in different words, which is most of what a question
about a meeting actually needs - nobody asks "control inventory ownership
sampling" the way the minutes phrase it. LightRAG was meant to be the semantic
half and never worked: it reports 0 of 129 documents processed, 129 failed, so
until this module there is no semantic retrieval anywhere in the system.

It is deliberately plain, for the same reason `chunk_index` is. The corpus is
2,809 chunks at 384 float32 each - about 4 MB - and scoring a query against all
of them is one numpy matrix-vector product, well under 10 ms. An approximate
index at this size buys nothing and costs a native dependency and an
approximation. There is no vector extension, no service and no metered call:
`fastembed` runs the model on CPU through the onnxruntime that was already
installed.

The search cost that is worth knowing about is not the scoring, then, but the
one model pass that turns the query into a vector, and the one-off ONNX session
start behind it. Both are paid on the first search of a process and the session
is cached for the rest of it.

Three properties are load-bearing:

* **Nothing here can break BM25.** Importing this module loads no model and
  imports no fastembed; `search_dense` returns an empty list rather than raising
  when the model is unavailable, and `rerank` hands back the order it was given.
  BM25-only stays a fully working retrieval mode on a laptop with no network and
  an empty model cache. Only `embed_chunks` - the write path, run deliberately -
  reports a missing model as a failure, because a build that quietly wrote
  nothing would leave an operator believing the index exists.
* **`content_hash` is the invalidation, not the foreign key.**
  `reindex_meeting` deletes and re-inserts a changed meeting's chunks, so
  "m1:0003" after an edit is a different passage at the same id, and the ON
  DELETE CASCADE that would have cleaned up behind it only fires on a connection
  with `PRAGMA foreign_keys = ON` - which SQLite defaults to OFF. Every read and
  every write compares the hash the vector was built from against the chunk's
  current one.
* **One model at a time, and never two at once in a score.** Cosines from two
  models are not on the same scale. Every row carries the model that produced
  it and every search filters on it, the same namespacing rule `voice_samples`
  applies to voiceprints.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any, Protocol

from pipeline.chunk_index import MAX_CHUNKS_PER_MEETING, ChunkHit, fit_excerpt
from pipeline.config import EMBED_MODEL, RERANK_MODEL, now_iso

# numpy is imported inside the two functions that score rather than at module
# scope, matching voices.py. Today the only importer is `cli.cmd_dense_index`,
# which imports inside the command for the same reason; when the fusion step
# lands, this module will be on the import path of every retrieval and the
# deferral is what keeps a BM25-only search from paying for numpy.


class DenseIndexError(RuntimeError):
    """The embedding model could not be loaded, or would not answer.

    Raised by `embed_chunks` alone. The read paths degrade instead - a retrieval
    must not fail because a model download has not happened yet (GC5).
    """


class TextEmbedder(Protocol):
    """The one method this module needs from fastembed's `TextEmbedding`.

    Narrow on purpose, and the seam a test injects through. fastembed's
    `query_embed` is not here: for this model family it delegates straight to
    `embed` with no query-side instruction prefix, so a second method would be
    two code paths pretending to be different.
    """

    def embed(self, documents: Sequence[str], batch_size: int = ...) -> Iterable[Any]: ...


class CrossEncoder(Protocol):
    """The one method this module needs from fastembed's `TextCrossEncoder`."""

    def rerank(self, query: str, documents: Sequence[str]) -> Iterable[float]: ...


# ── Lazily loaded models ──────────────────────────────────────────────
#
# Cached at module level so a bulk embed pays the ONNX session start once. Both
# start as None and must still be None after an import: `pipeline chunk-index`
# and every BM25 search reach this module without wanting a model, and an import
# that blocked on a download would make the cheap half of retrieval depend on
# the expensive half being available.

_EMBEDDER: TextEmbedder | None = None
_EMBEDDER_MODEL: str | None = None
_RERANKER: CrossEncoder | None = None
_RERANKER_MODEL: str | None = None
_RERANKER_WARNED = False

# model id -> why loading it failed. Two jobs, and the second is the important
# one.
#
# It is the reason channel: `search_dense` returns a bare list, so this is all a
# caller has to tell "there is no model" from "nothing matched" - the job
# `chunk_index.index_status` does for BM25.
#
# It is also a gate, checked BEFORE a load is attempted, so a process tries each
# model at most once. Caching only success is not enough: the reranker has never
# been downloaded on this machine and the disk is at 1%, so a failed load is the
# normal path, not an edge case, and a retrieval calls `rerank` every time. A
# per-call retry would pay a hub fetch and its timeout on every query -
# silently, because the one warning line was spent on the first one. Degradation
# that is slow and invisible is the failure mode explicit degradation exists to
# prevent.
_LOAD_FAILURES: dict[str, str] = {}

# Why the last dense call failed when it was not a load failure - a loaded model
# that refused one query. Reported, but deliberately not a gate: a single bad
# call must not disable a working model for the life of the process.
_LAST_ERROR: str | None = None


def _load_embedder(model: str) -> TextEmbedder:
    """Construct the fastembed embedding model. The only import of fastembed here.

    Split out from `_embedder` so a test replaces the model without replacing the
    caching, the batching or the write path around it.
    """
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model)


def _load_reranker(model: str) -> CrossEncoder:
    """Construct the fastembed cross-encoder. Separate seam, separate download."""
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=model)


def _record_load_failure(model: str, exc: BaseException) -> str:
    """Remember that `model` cannot be loaded in this process, and why.

    The `except` clauses feeding this are deliberately broad. An unknown model
    id arrives as ValueError, an uninstalled fastembed as ImportError, and an
    absent network as any of a dozen huggingface_hub, urllib3 and OSError types.
    Enumerating them would mean the next urllib3 release turns "no network" into
    a crash in the middle of an unattended run.
    """
    reason = f"{model} unavailable: {type(exc).__name__}: {exc}"
    _LOAD_FAILURES[model] = reason
    return reason


def _embedder(model: str) -> TextEmbedder:
    """The cached embedding model, built on first use. Raises DenseIndexError.

    A model that already failed in this process is not retried; see
    `_LOAD_FAILURES` for why, and `reset_models` for the way back.
    """
    global _EMBEDDER, _EMBEDDER_MODEL
    if _EMBEDDER is not None and _EMBEDDER_MODEL == model:
        return _EMBEDDER
    if model in _LOAD_FAILURES:
        raise DenseIndexError(_LOAD_FAILURES[model])
    try:
        embedder = _load_embedder(model)
    except Exception as exc:
        raise DenseIndexError(_record_load_failure(model, exc)) from exc
    _EMBEDDER, _EMBEDDER_MODEL = embedder, model
    return embedder


def _reranker(model: str) -> CrossEncoder:
    """The cached cross-encoder, built on first use. Raises DenseIndexError.

    Same one-attempt-per-process rule as `_embedder`, and it binds harder here:
    `rerank` runs on every retrieval, so this is the call that would otherwise
    re-attempt a download for every query anyone ever asks.
    """
    global _RERANKER, _RERANKER_MODEL
    if _RERANKER is not None and _RERANKER_MODEL == model:
        return _RERANKER
    if model in _LOAD_FAILURES:
        raise DenseIndexError(_LOAD_FAILURES[model])
    try:
        encoder = _load_reranker(model)
    except Exception as exc:
        raise DenseIndexError(_record_load_failure(model, exc)) from exc
    _RERANKER, _RERANKER_MODEL = encoder, model
    return encoder


def reset_models() -> None:
    """Forget the loaded models and every recorded failure. The way back.

    One attempt per process is right for a CLI run, and wrong on its own for a
    process that lives all day: the loopback context service would keep
    reporting a 03:00 failure at 17:00, long after the operator freed the disk
    or reconnected the network. This is the documented reset - call it once the
    cause is fixed and the next search loads the model again.

    Cheap and safe to call: it drops cached ONNX sessions, which are rebuilt on
    demand, and clears nothing persistent. It does not touch `chunk_vectors`.
    """
    global _EMBEDDER, _EMBEDDER_MODEL, _RERANKER, _RERANKER_MODEL
    global _RERANKER_WARNED, _LAST_ERROR
    _EMBEDDER = _EMBEDDER_MODEL = _RERANKER = _RERANKER_MODEL = None
    _RERANKER_WARNED = False
    _LAST_ERROR = None
    _LOAD_FAILURES.clear()


# ── Vector storage ────────────────────────────────────────────────────

def _pack(vector: Any) -> tuple[bytes, int]:
    """A model's output as a storable blob plus its dimension.

    float32 little-endian, so the blob round-trips through SQLite unchanged and
    stays readable by a different Python build. That is the same contract
    `voices.pack` applies to voiceprints, restated rather than shared: the two
    stores answer to different models and must be free to diverge.

    Stored exactly as the model produced it, not normalised. The row is then the
    model's answer rather than a derived form of it, and the normalisation stays
    where it is actually needed - at search time, where the query vector has to
    be normalised anyway.
    """
    import numpy as np

    array = np.ascontiguousarray(np.asarray(vector, dtype="<f4")).ravel()
    if array.size == 0:
        raise DenseIndexError("the model returned an empty vector")
    return array.tobytes(), int(array.size)


def _embedding_text(row: sqlite3.Row) -> str:
    """What actually goes to the model: the context header, then the chunk.

    A chunk is a mid-document passage. "The second line reviews sampling" means
    nothing without knowing which meeting and which programme it belongs to, and
    a query naming the programme would never reach it. `context_header` is the
    column that carries that; it is NULL until a later stage writes it, and
    prefixing it here is the entire point of populating it, so the wiring exists
    before the writer does.

    A header that is blank after stripping is treated as absent rather than
    prefixed, so an empty string cannot put two leading newlines in front of
    every vector in the corpus and shift the whole index off its axis.
    """
    header = (row["context_header"] or "").strip()
    return f"{header}\n\n{row['text']}" if header else str(row["text"])


# ── Embedding ─────────────────────────────────────────────────────────

def embed_chunks(
    conn: sqlite3.Connection,
    *,
    model: str | None = None,
    batch_size: int = 32,
    force: bool = False,
) -> dict[str, int]:
    """Embed every chunk with no current vector for the active model.

    Returns counts keyed `chunks` (rows considered), `embedded` (vectors
    written), `current` (skipped, already built from this exact text) and
    `stale` (carried a vector built from text that has since changed).

    `stale` is the count that matters and it is reported even under `--rebuild`,
    where it would otherwise vanish into `embedded`: a stale vector scores a
    query against a passage that no longer exists, and it is not a hypothetical
    - `reindex_meeting` produces exactly that state whenever it runs on a
    connection without `PRAGMA foreign_keys = ON`.

    Raises DenseIndexError when there is work to do and the model cannot be
    loaded. Nothing is written in that case, and BM25 is untouched.
    """
    model = (model or EMBED_MODEL).strip()
    rows = conn.execute(
        """
        SELECT c.chunk_id, c.text, c.context_header, c.content_hash,
               v.content_hash AS vector_hash
        FROM minute_chunks c
        -- LEFT JOIN, and the model belongs in the join rather than a WHERE
        -- clause: a chunk carrying some other model's vector has no vector for
        -- this one, so it is work to do, not a row to filter away.
        LEFT JOIN chunk_vectors v ON v.chunk_id = c.chunk_id AND v.model = ?
        ORDER BY c.chunk_id
        """,
        (model,),
    ).fetchall()

    stats = {"chunks": len(rows), "embedded": 0, "current": 0, "stale": 0}
    pending: list[sqlite3.Row] = []
    for row in rows:
        stale = row["vector_hash"] is not None and row["vector_hash"] != row["content_hash"]
        if stale:
            stats["stale"] += 1
        elif row["vector_hash"] is not None and not force:
            stats["current"] += 1
            continue
        pending.append(row)
    if not pending:
        return stats

    # Loaded here and not before: a run with nothing to do must not need a model
    # at all, which is what makes re-running this command free.
    embedder = _embedder(model)

    # Each batch is committed as it lands. Not premature caution: the full
    # corpus measures out at roughly a chunk a second on a loaded laptop, so a
    # build is tens of minutes, and losing all of it to an interrupt at chunk
    # 2,700 would mean starting from zero. Re-running picks up exactly where it
    # stopped, because what to embed is decided by content_hash and not by a
    # cursor.
    #
    # Not done when the caller already had a transaction open: committing inside
    # somebody else's unit of work is not this function's decision to make, and
    # `chunk_index` draws the same line with its savepoints.
    durable = not conn.in_transaction

    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        _write_vectors(conn, model, batch, embedder)
        if durable:
            conn.commit()
        stats["embedded"] += len(batch)
        # One line per ten batches. The full corpus is 88 batches and a line
        # each would bury the summary the CLI prints underneath it.
        if start // batch_size % 10 == 0 or stats["embedded"] == len(pending):
            print(f"    embedded {stats['embedded']}/{len(pending)} chunk(s)")
    return stats


def _write_vectors(
    conn: sqlite3.Connection,
    model: str,
    rows: list[sqlite3.Row],
    embedder: TextEmbedder,
) -> None:
    """Embed one batch and upsert it, or write none of it."""
    texts = [_embedding_text(row) for row in rows]
    try:
        vectors = list(embedder.embed(texts, batch_size=len(texts)))
    except Exception as exc:
        raise DenseIndexError(
            f"{model} failed on a batch of {len(texts)}: {type(exc).__name__}: {exc}"
        ) from exc
    if len(vectors) != len(rows):
        # A short batch would silently pair vectors with the wrong chunks, which
        # is worse than no index: every citation after the gap would be wrong.
        raise DenseIndexError(f"{model} returned {len(vectors)} vectors for {len(rows)} chunks")

    embedded_at = now_iso()
    params = []
    for row, vector in zip(rows, vectors, strict=True):
        blob, dim = _pack(vector)
        params.append((row["chunk_id"], model, dim, blob, row["content_hash"], embedded_at))
    conn.executemany(
        """
        INSERT INTO chunk_vectors (chunk_id, model, dim, vector, content_hash, embedded_at)
        VALUES (?, ?, ?, ?, ?, ?)
        -- chunk_id is the whole primary key, so re-embedding is an update in
        -- place and a plain INSERT would raise on the second run and on every
        -- model switch. DO UPDATE rather than INSERT OR REPLACE: it names the
        -- columns it changes, so a column added later is not silently reset to
        -- its default by a re-embed.
        ON CONFLICT(chunk_id) DO UPDATE SET
            model = excluded.model,
            dim = excluded.dim,
            vector = excluded.vector,
            content_hash = excluded.content_hash,
            embedded_at = excluded.embedded_at
        """,
        params,
    )


# ── Status, the reason channel ────────────────────────────────────────

def _tables_present(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('minute_chunks', 'chunk_vectors')"
        ).fetchone()[0]
        == 2
    )


def _vector_count(conn: sqlite3.Connection, model: str) -> int:
    if not _tables_present(conn):
        return 0
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM chunk_vectors WHERE model = ?", (model,)
        ).fetchone()[0]
    )


def _stale_count(conn: sqlite3.Connection, model: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM chunk_vectors v
            JOIN minute_chunks c ON c.chunk_id = v.chunk_id
            WHERE v.model = ? AND v.content_hash <> c.content_hash
            """,
            (model,),
        ).fetchone()[0]
    )


def dense_status(conn: sqlite3.Connection, *, model: str | None = None) -> tuple[bool, str]:
    """(searchable, reason).

    `search_dense` returns a bare list, so this is the only place a caller can
    learn why an answer was empty - "nobody has built the index", "the model is
    not on this machine" and "nothing was close enough" are very different
    problems and only the first two are actionable (GC5).
    """
    model = (model or EMBED_MODEL).strip()
    if not _tables_present(conn):
        return False, "dense index not built; run `pipeline dense-index`"
    total = _vector_count(conn, model)
    if not total:
        return False, f"no vectors for {model}{_orphans(conn, model)}; run `pipeline dense-index`"
    failure = _LOAD_FAILURES.get(model) or _LAST_ERROR
    if failure:
        # Vectors exist but nothing can embed a query against them, so the index
        # is unusable however complete it looks. The count rides along because
        # this line is also what the CLI prints when a build stops partway, and
        # "how much did I keep" is the question then.
        return False, f"{failure} ({total} vectors already stored)"
    stale = _stale_count(conn, model)
    if stale:
        return True, (
            f"{total} vectors for {model}, {stale} stale and not being searched; "
            "run `pipeline dense-index`"
        )
    return True, f"{total} vectors indexed for {model}{_orphans(conn, model)}"


def _orphans(conn: sqlite3.Connection, model: str) -> str:
    """A clause naming vectors stored under a model no search will read, if any.

    `search_dense` reads `EMBED_MODEL` and nothing else, so an index built under
    a one-off `--model` is unreachable. Reported here rather than only in the
    run that created it, so the condition resurfaces on every later
    `pipeline dense-index` instead of scrolling out of a build log.
    """
    rows = conn.execute(
        "SELECT model, COUNT(*) FROM chunk_vectors WHERE model <> ? GROUP BY model "
        "ORDER BY COUNT(*) DESC",
        (EMBED_MODEL,),
    ).fetchall()
    if not rows:
        return ""
    listed = ", ".join(f"{count} under {other}" for other, count in rows)
    return f" ({listed}, which no search will read)"


# ── Search ────────────────────────────────────────────────────────────

def _normalise_similarity(cosine: float) -> float:
    """Cosine similarity into the 0.0..1.0 the ContextItem contract requires.

    Clamped, not rescaled. Mapping [-1, 1] linearly onto [0, 1] would report a
    completely unrelated passage at 0.5 and an opposed one at 0.0, and anything
    reading the number as a confidence would be misled by every result.

    What this is NOT is comparable to `chunk_index`'s BM25 score. Real English
    embeddings sit in a narrow band - two unrelated meeting passages land around
    0.6 - so a fusion step has to fuse on rank, not by putting these numbers and
    BM25's on the same axis.
    """
    return min(1.0, max(0.0, cosine))


def _embed_query(query: str, model: str):
    """The query as a float64 vector. Raises DenseIndexError."""
    import numpy as np

    global _LAST_ERROR
    embedder = _embedder(model)
    try:
        vectors = list(embedder.embed([query], batch_size=1))
    except Exception as exc:
        # Recorded on _LAST_ERROR, not in _LOAD_FAILURES: the model loaded, so
        # one refused query must not disable it for the life of the process.
        _LAST_ERROR = f"{model} failed to embed the query: {type(exc).__name__}: {exc}"
        raise DenseIndexError(_LAST_ERROR) from exc
    if not vectors:
        _LAST_ERROR = f"{model} returned no vector for the query"
        raise DenseIndexError(_LAST_ERROR)
    _LAST_ERROR = None
    return np.asarray(vectors[0], dtype="float64").ravel()


def _candidates(
    conn: sqlite3.Connection,
    model: str,
    dim: int,
    as_of: str | None,
    exclude_meeting_ids: frozenset[str],
) -> list[sqlite3.Row]:
    """Every scoreable vector, already filtered. No pool cap, unlike BM25's.

    BM25 caps its candidate pool because FTS5 could otherwise sort the corpus
    for a one-word query. Nothing to cap here: every vector has to be scored to
    know which are nearest, and the whole corpus is 2,809 rows - 4 MB of vectors
    and under 3 MB of text. Ranking first and fetching text second would be two
    queries to save a few milliseconds.

    `v.content_hash = c.content_hash` is a filter, not an optimisation. A vector
    built from text that has since changed would be scored against the query and
    then quoted as the *new* text: a mis-scored hit under a citation that reads
    perfectly. Missing beats wrong; `dense_status` reports the count.
    """
    where = ["v.model = ?", "v.dim = ?", "v.content_hash = c.content_hash"]
    params: list[object] = [model, dim]
    if as_of:
        # Also drops meetings with no date at all, exactly as BM25 does: an
        # undated meeting cannot be shown to precede the cutoff, and including
        # it would let a later conversation answer a question asked earlier.
        where.append("c.meeting_date IS NOT NULL AND c.meeting_date <= ?")
        params.append(as_of)
    if exclude_meeting_ids:
        ids = sorted(exclude_meeting_ids)
        where.append(f"c.meeting_id NOT IN ({','.join('?' * len(ids))})")
        params.extend(ids)

    sql = f"""
        SELECT c.chunk_id, c.meeting_id, c.meeting_date, c.source_path, c.ordinal,
               c.heading, c.text, v.vector
        FROM chunk_vectors v
        JOIN minute_chunks c ON c.chunk_id = v.chunk_id
        WHERE {' AND '.join(where)}
        ORDER BY c.chunk_id
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # Only reachable if a table went missing between the status check and
        # here. Empty beats a stack trace out of a read-only search (GC5).
        return []


def search_dense(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    max_chars: int = 4000,
    as_of: str | None = None,
    exclude_meeting_ids: frozenset[str] = frozenset(),
) -> list[ChunkHit]:
    """Semantically nearest chunks, best first, bounded the same three ways as BM25.

    Returns `chunk_index.ChunkHit` - the identical type, not a look-alike - so
    the two result lists fuse without either side being adapted. `as_of`,
    `exclude_meeting_ids`, the per-meeting cap and the `max_chars` budget all
    behave exactly as in `chunk_index.search_chunks`, down to the ellipsis on a
    trimmed excerpt and the outright drop of one too short to quote honestly.

    Never raises. An unbuilt index, an empty index, a blank query and a model
    that will not load all come back as an empty list; `dense_status` says which
    it was. Degrading here rather than raising is what keeps BM25-only a fully
    working retrieval mode (GC5).
    """
    if not query.strip():
        return []
    # Counted before the model is touched, so a search against an index nobody
    # has built yet costs nothing rather than a download.
    if not _vector_count(conn, EMBED_MODEL):
        return []
    try:
        vector = _embed_query(query, EMBED_MODEL)
    except DenseIndexError:
        # The reason is on _LOAD_FAILURE for dense_status to report. A retrieval
        # that cannot reach a model returns nothing; it does not take its caller
        # down with it.
        return []

    rows = _candidates(conn, EMBED_MODEL, int(vector.size), as_of, exclude_meeting_ids)
    if not rows:
        return []

    import numpy as np

    # One contiguous read of every candidate blob, reshaped into a matrix. The
    # dim filter in _candidates is what makes the reshape safe.
    matrix = np.frombuffer(b"".join(row["vector"] for row in rows), dtype="<f4")
    matrix = matrix.reshape(len(rows), int(vector.size)).astype("float64")
    norms = np.linalg.norm(matrix, axis=1) * float(np.linalg.norm(vector))
    with np.errstate(invalid="ignore", divide="ignore"):
        cosines = (matrix @ vector) / norms
    # A zero vector divides to NaN. Silently propagating it would turn every
    # comparison against it into False and scramble the sort; zero is the honest
    # score for a vector with no direction.
    scores = [_normalise_similarity(float(value)) for value in np.nan_to_num(cosines, nan=0.0)]

    ranked = sorted(
        range(len(rows)),
        key=lambda i: (
            -scores[i],
            rows[i]["meeting_date"] or "",
            rows[i]["meeting_id"],
            rows[i]["ordinal"],
        ),
    )

    # The per-meeting cap is applied to the ranked list before the budget, the
    # same order BM25 applies them in. Capping afterwards would let a verbose
    # meeting's third chunk into a result set only because its first two were
    # too long to fit.
    seen: dict[str, int] = {}
    capped: list[int] = []
    for i in ranked:
        meeting_id = rows[i]["meeting_id"]
        if seen.get(meeting_id, 0) >= MAX_CHUNKS_PER_MEETING:
            continue
        seen[meeting_id] = seen.get(meeting_id, 0) + 1
        capped.append(i)

    hits: list[ChunkHit] = []
    used = 0
    for i in capped:
        if len(hits) >= limit or used >= max_chars:
            break
        row = rows[i]
        text = fit_excerpt(row["text"], max_chars - used)
        if text is None:
            # Too little budget left to quote this chunk honestly. Keep going: a
            # smaller lower-ranked chunk may still fit whole, and a whole chunk
            # is better evidence than a severed one.
            continue
        hits.append(
            ChunkHit(
                chunk_id=row["chunk_id"],
                meeting_id=row["meeting_id"],
                meeting_date=row["meeting_date"],
                source_path=row["source_path"],
                ordinal=row["ordinal"],
                heading=row["heading"],
                text=text,
                score=scores[i],
            )
        )
        used += len(text)
    return hits


# ── Reranking ─────────────────────────────────────────────────────────

def _sigmoid(logit: float) -> float:
    """A cross-encoder logit into the 0.0..1.0 the ContextItem contract requires.

    Clamped before the exponential so a confident model cannot overflow the one
    number a caller is allowed to trust.
    """
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, logit))))


def _warn_reranker_unavailable(exc: BaseException) -> None:
    """Say it once per process, not once per query.

    On a machine that has never fetched the cross-encoder the degraded path is
    the normal path, and a line per retrieval would be noise that trains the
    reader to ignore it.
    """
    global _RERANKER_WARNED
    if _RERANKER_WARNED:
        return
    _RERANKER_WARNED = True
    print(f"    reranker unavailable ({type(exc).__name__}: {exc}); keeping the retrieval order")


def rerank(query: str, hits: list[ChunkHit], *, top_n: int = 8) -> list[ChunkHit]:
    """Re-order a shortlist with a cross-encoder and keep the best `top_n`.

    A cross-encoder reads the query and the passage together instead of
    comparing two independently-built vectors, which is why it can separate "who
    owns sampling" from "sampling was reviewed by the second line". It costs a
    model pass per hit, so it runs over a shortlist and never over the corpus.

    This is the one component allowed to be absent. A reranker that will not
    load leaves ranking quality where it already was; it must never fail a
    retrieval, so the input order comes straight back, truncated (GC5).

    The score is rewritten from the cross-encoder's own judgement rather than
    carried over: a list ordered by one number and labelled with another is a
    list whose scores contradict its order.
    """
    if not hits:
        return []
    try:
        scores = list(_reranker(RERANK_MODEL).rerank(query, [hit.text for hit in hits]))
    except Exception as exc:
        # As broad as the embedder's, and for the same reason - except that here
        # even a genuine bug in the reranker must not cost the caller its hits.
        _warn_reranker_unavailable(exc)
        return hits[:top_n]
    if len(scores) != len(hits):
        _warn_reranker_unavailable(ValueError(f"{len(scores)} scores for {len(hits)} hits"))
        return hits[:top_n]

    # Ties break on the incoming order, so a reranker with nothing to say leaves
    # the retrieval ranking exactly as it found it.
    order = sorted(range(len(hits)), key=lambda i: (-scores[i], i))
    return [replace(hits[i], score=_sigmoid(float(scores[i]))) for i in order[:top_n]]
