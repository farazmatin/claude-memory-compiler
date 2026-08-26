"""Voice identity: recognising the same speaker across meetings.

Diarization answers "how many voices are in this recording and when does each
one talk". It does not answer "who". Its labels are per-file, so `SPEAKER_00`
means nothing in tomorrow's meeting, and `speakers.py` has to re-derive identity
from the transcript text every single time - which fails silently whenever
nobody says a name early on.

This module gives the pipeline a memory of what people sound like:

    embed       label's speech  -> one vector
    voiceprint  a person's samples -> one vector (duration-weighted mean)
    match       vector x voiceprints -> best, runner-up, and a decision band
    cluster     pending vectors -> one card per person, not per appearance

The vectors come from the embedding model diarization already loads, so
enrollment and clustering share a vector space by construction rather than by
coincidence.

Two rules here exist because of the deployment profile - one phone on a table,
in person - and neither is optional at that distance:

  * the margin test: two people at the same table on the same microphone can
    both score highly, and absolute similarity alone picks one confidently and
    wrongly.
  * MIN_ENROLL_MEETINGS: a person enrolled from one meeting is enrolled from one
    seat. The same colleague across the table next week embeds differently
    enough to be mistaken for someone else.

The governing principle throughout is the one stated in speakers.py: an honest
gap is fixable, a confident wrong name is not. Every ambiguity resolves toward
asking rather than guessing.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from pipeline import db
from pipeline.config import (
    SNIPPET_COUNT,
    SNIPPET_MIN_SEPARATION_SEC,
    SNIPPET_MIN_WORD_COVERAGE,
    SNIPPET_SEC,
    SNIPPET_SKIP_OPENING_SEC,
    VOICE_AUTO,
    VOICE_CLUSTER_THRESHOLD,
    VOICE_MARGIN,
    VOICE_MIN_ENROLL_MEETINGS,
    VOICE_MIN_SPEECH_SEC,
    VOICE_MODEL,
    VOICE_REVIEW,
)

BAND_AUTO = "auto"
BAND_REVIEW = "review"
BAND_NEW = "new"

STATE_PENDING = "pending"
STATE_RESOLVED = "resolved"
STATE_DISMISSED = "dismissed"

QUALITY_OK = "ok"
QUALITY_LOW = "low"

# How the sensitivity control maps onto the three thresholds. One dial the owner
# understands, rather than three cosine values nobody should have to reason about.
SENSITIVITY_OFFSETS = {
    "cautious":  (+0.06, +0.05, +0.04),
    "balanced":  (0.0, 0.0, 0.0),
    "confident": (-0.05, -0.04, -0.03),
}


@dataclass(frozen=True)
class Thresholds:
    auto: float = VOICE_AUTO
    review: float = VOICE_REVIEW
    margin: float = VOICE_MARGIN
    min_speech_sec: float = VOICE_MIN_SPEECH_SEC
    min_enroll_meetings: int = VOICE_MIN_ENROLL_MEETINGS
    cluster: float = VOICE_CLUSTER_THRESHOLD

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> Thresholds:
        """Settings from the database, with the sensitivity dial applied.

        Environment defaults are the baseline; stored settings win; the dial
        shifts all three thresholds together so the owner never sees a cosine.
        """
        base = cls(
            auto=db.get_setting_float(conn, "voice.auto", VOICE_AUTO),
            review=db.get_setting_float(conn, "voice.review", VOICE_REVIEW),
            margin=db.get_setting_float(conn, "voice.margin", VOICE_MARGIN),
            min_speech_sec=db.get_setting_float(
                conn, "voice.min_speech_sec", VOICE_MIN_SPEECH_SEC
            ),
            min_enroll_meetings=int(
                db.get_setting_float(
                    conn, "voice.min_enroll_meetings", VOICE_MIN_ENROLL_MEETINGS
                )
            ),
            cluster=db.get_setting_float(conn, "voice.cluster_threshold", VOICE_CLUSTER_THRESHOLD),
        )
        dial = (db.get_setting(conn, "voice.sensitivity") or "balanced").strip().lower()
        d_auto, d_review, d_margin = SENSITIVITY_OFFSETS.get(dial, (0.0, 0.0, 0.0))
        return cls(
            auto=base.auto + d_auto,
            review=base.review + d_review,
            margin=base.margin + d_margin,
            min_speech_sec=base.min_speech_sec,
            min_enroll_meetings=base.min_enroll_meetings,
            cluster=base.cluster,
        )


@dataclass(frozen=True)
class MatchResult:
    best: str | None = None
    best_score: float = 0.0
    next: str | None = None
    next_score: float = 0.0

    @property
    def margin(self) -> float:
        return self.best_score - self.next_score


# ── Vector storage ────────────────────────────────────────────────────
#
# float32 little-endian, so a vector survives the round trip through SQLite
# unchanged and stays comparable to one written by a different Python build.

def pack(vector) -> tuple[bytes, int]:
    import numpy as np

    array = np.ascontiguousarray(np.asarray(vector, dtype="<f4"))
    return array.tobytes(), int(array.shape[0])


def unpack(blob: bytes, dim: int):
    import numpy as np

    array = np.frombuffer(blob, dtype="<f4")
    if array.shape[0] != dim:
        raise ValueError(f"embedding is {array.shape[0]} values, expected {dim}")
    return array.astype("float64")


def normalize(vector):
    """L2-normalise, so cosine similarity is a plain dot product.

    A zero vector is returned unchanged rather than producing NaN: a silent NaN
    propagates into every score and turns matching into nonsense that looks like
    a model problem.
    """
    import numpy as np

    array = np.asarray(vector, dtype="float64")
    norm = float(np.linalg.norm(array))
    return array if norm == 0.0 else array / norm


def cosine(a, b) -> float:
    import numpy as np

    return float(np.dot(normalize(a), normalize(b)))


# ── Voiceprints ───────────────────────────────────────────────────────

def voiceprint(conn: sqlite3.Connection, canonical: str, model: str = VOICE_MODEL):
    """A person's voiceprint: the duration-weighted mean of their samples.

    Weighted by speech duration because a thirty-second sample is better evidence
    of how someone sounds than a five-second one, and computed on read rather
    than cached so deleting a bad sample corrects the voiceprint immediately.
    """
    import numpy as np

    rows = db.person_samples(conn, canonical, model=model)
    if not rows:
        return None

    vectors, weights = [], []
    for row in rows:
        vectors.append(normalize(unpack(row["embedding"], row["dim"])))
        weights.append(max(float(row["speech_sec"] or 0.0), 0.1))

    stacked = np.vstack(vectors)
    weighted = np.average(stacked, axis=0, weights=np.asarray(weights))
    return normalize(weighted)


def enrolled(conn: sqlite3.Connection, model: str = VOICE_MODEL) -> dict[str, object]:
    """Every enrolled person's voiceprint, keyed by canonical name."""
    prints: dict[str, object] = {}
    for name in db.enrolled_names(conn, model):
        vector = voiceprint(conn, name, model=model)
        if vector is not None:
            prints[name] = vector
    return prints


# ── Matching ──────────────────────────────────────────────────────────

def match(vector, prints: dict[str, object]) -> MatchResult:
    """Score one embedding against every voiceprint; keep the top two."""
    if not prints:
        return MatchResult()

    scored = sorted(
        ((name, cosine(vector, print_)) for name, print_ in prints.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )
    best, best_score = scored[0]
    if len(scored) == 1:
        return MatchResult(best=best, best_score=best_score)
    runner_up, runner_score = scored[1]
    return MatchResult(best=best, best_score=best_score, next=runner_up, next_score=runner_score)


def band(
    result: MatchResult,
    speech_sec: float,
    *,
    thresholds: Thresholds,
    enroll_meetings: int = 0,
    llm_name: str | None = None,
    over_segmented: bool = False,
) -> str:
    """Decide whether to apply silently, ask, or treat as an unknown voice.

    Every guard here resolves an ambiguity toward asking. The cost of asking is
    six seconds of the owner's attention; the cost of a wrong auto-match is a
    real person silently credited with someone else's commitments.
    """
    if result.best is None or result.best_score < thresholds.review:
        return BAND_NEW

    disqualified = (
        speech_sec < thresholds.min_speech_sec
        # One meeting means one seat. See the module docstring.
        or enroll_meetings < thresholds.min_enroll_meetings
        # Over-segmentation splits one person across several labels, so every
        # embedding from such a meeting is a fragment of someone.
        or over_segmented
        or result.best_score < thresholds.auto
        or result.margin < thresholds.margin
        # Two independent signals disagreeing is information. Preferring one
        # silently would throw it away.
        or (llm_name is not None and llm_name != result.best)
    )
    return BAND_REVIEW if disqualified else BAND_AUTO


def score_match(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    prints: dict[str, object],
    thresholds: Thresholds,
    *,
    over_segmented: bool = False,
) -> tuple[MatchResult, str]:
    """Score one stored `speaker_matches` row and band it."""
    vector = unpack(row["embedding"], row["dim"])
    result = match(vector, prints)
    enroll_meetings = (
        db.sample_meeting_count(conn, result.best, row["model"] or VOICE_MODEL)
        if result.best
        else 0
    )
    decided = band(
        result,
        float(row["speech_sec"] or 0.0),
        thresholds=thresholds,
        enroll_meetings=enroll_meetings,
        llm_name=row["llm_name"],
        over_segmented=over_segmented,
    )
    return result, decided


def rematch_pending(conn: sqlite3.Connection, model: str = VOICE_MODEL) -> int:
    """Re-score every pending label against the current voiceprints.

    Run nightly. This is the job that makes effort compound downward: each label
    the owner supplies improves the voiceprints, which resolves adjacent unknowns
    without ever being asked, which shrinks tomorrow's queue.

    Returns the number promoted to `auto`.
    """
    thresholds = Thresholds.load(conn)
    prints = enrolled(conn, model)
    promoted = 0

    for row in db.pending_matches(conn, model=model):
        result, decided = score_match(conn, row, prints, thresholds)
        db.upsert_speaker_match(
            conn,
            row["meeting_id"],
            row["label"],
            best_canonical=result.best,
            best_score=result.best_score,
            next_canonical=result.next,
            next_score=result.next_score,
            band=decided,
        )
        if decided == BAND_AUTO and row["band"] != BAND_AUTO:
            promoted += 1

    return promoted


# ── Clustering ────────────────────────────────────────────────────────

def cluster_pending(conn: sqlite3.Connection, model: str = VOICE_MODEL) -> int:
    """Group pending labels believed to be the same person.

    The unit of work has to be a voice, not a meeting. Grouping by meeting asks
    the same question once per appearance: a colleague in twelve meetings would
    produce twelve identical decisions, and the twelfth is no more informative
    than the first.

    Agglomerative with average linkage at a threshold deliberately tighter than
    the auto-match threshold - a contaminated cluster enrolls a poisoned
    voiceprint from a single confirmation, so the bias is toward too many small
    clusters rather than one wrong big one.

    Rebuilds from scratch and is safe to re-run. Returns the cluster count.
    """
    thresholds = Thresholds.load(conn)
    rows = [r for r in db.pending_matches(conn, model=model) if r["band"] != BAND_AUTO]
    if not rows:
        db.replace_clusters(conn, [])
        return 0

    vectors = [normalize(unpack(row["embedding"], row["dim"])) for row in rows]
    groups = _agglomerate(vectors, thresholds.cluster)

    clusters: list[dict[str, object]] = []
    for members in groups:
        rows_in = [rows[i] for i in members]
        leader = max(rows_in, key=lambda r: float(r["speech_sec"] or 0.0))
        clusters.append(
            {
                "id": uuid.uuid4().hex,
                "size": len(rows_in),
                "total_speech": sum(float(r["speech_sec"] or 0.0) for r in rows_in),
                "best_canonical": leader["best_canonical"],
                "best_score": leader["best_score"],
                "next_canonical": leader["next_canonical"],
                "next_score": leader["next_score"],
                # A group is "review" if anything in it looks like a known
                # person; otherwise it is a genuinely new voice.
                "band": BAND_REVIEW if leader["best_canonical"] else BAND_NEW,
                "members": [(r["meeting_id"], r["label"]) for r in rows_in],
            }
        )

    db.replace_clusters(conn, clusters)
    return len(clusters)


def _agglomerate(vectors: list, threshold: float) -> list[list[int]]:
    """Average-linkage agglomerative clustering over cosine similarity.

    Written out rather than pulled from scipy: the pipeline already carries a
    heavy dependency tree, and at a few hundred pending labels the naive O(n^3)
    merge loop costs milliseconds.
    """
    groups = [[i] for i in range(len(vectors))]

    while len(groups) > 1:
        best_pair, best_score = None, threshold
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                score = _average_linkage(vectors, groups[a], groups[b])
                if score >= best_score:
                    best_pair, best_score = (a, b), score
        if best_pair is None:
            break
        a, b = best_pair
        groups[a] = groups[a] + groups[b]
        groups.pop(b)

    return groups


def _average_linkage(vectors: list, left: list[int], right: list[int]) -> float:
    total = sum(cosine(vectors[i], vectors[j]) for i in left for j in right)
    return total / (len(left) * len(right))


def split_cluster(conn: sqlite3.Connection, cluster_id: str) -> list[str]:
    """Dissolve a cluster into one single-label cluster per member.

    Clustering will occasionally group two people, and confirming such a group
    would enroll a poisoned voiceprint. The owner can see the appearance count,
    so they are the last line of defence against it.
    """
    members = db.cluster_labels(conn, cluster_id)
    if not members:
        return []

    clusters = [
        {
            "id": uuid.uuid4().hex,
            "size": 1,
            "total_speech": float(row["speech_sec"] or 0.0),
            "best_canonical": row["best_canonical"],
            "best_score": row["best_score"],
            "next_canonical": row["next_canonical"],
            "next_score": row["next_score"],
            "band": BAND_REVIEW if row["best_canonical"] else BAND_NEW,
            "members": [(row["meeting_id"], row["label"])],
        }
        for row in members
    ]

    # Everything not in this cluster has to be carried across, because
    # replace_clusters rebuilds the table wholesale.
    for existing in db.pending_clusters(conn, limit=10_000):
        if existing["id"] == cluster_id:
            continue
        rows = db.cluster_labels(conn, existing["id"])
        if not rows:
            continue
        clusters.append(
            {
                "id": existing["id"],
                "size": existing["size"],
                "total_speech": existing["total_speech"],
                "best_canonical": existing["best_canonical"],
                "best_score": existing["best_score"],
                "next_canonical": existing["next_canonical"],
                "next_score": existing["next_score"],
                "band": existing["band"],
                "members": [(r["meeting_id"], r["label"]) for r in rows],
            }
        )

    db.replace_clusters(conn, clusters)
    return [c["id"] for c in clusters[: len(members)]]  # type: ignore[misc]


# ── Resolution ────────────────────────────────────────────────────────

def confirm(
    conn: sqlite3.Connection,
    cluster_id: str,
    canonical: str,
    *,
    model: str = VOICE_MODEL,
) -> int:
    """Name every label in a cluster, enrolling each as a voice sample.

    One answer covers every appearance - that is the whole point of clustering.
    Returns the number of labels resolved.
    """
    members = db.cluster_labels(conn, cluster_id)
    if not members:
        return 0

    canonical = canonical.strip()
    if not canonical:
        raise ValueError("cannot confirm a cluster to an empty name")

    # Resolve the alias BEFORE registering, so "Mike" and "Michael" do not become
    # two voiceprints of one person. Order matters: add_person() writes the name
    # as an alias of itself, which would overwrite an existing mike -> Michael
    # mapping and defeat the very normalisation this is here for.
    canonical = db.canonical_name(conn, canonical) or canonical
    db.add_person(conn, canonical)

    for row in members:
        if row["embedding"]:
            db.add_voice_sample(
                conn,
                canonical=canonical,
                meeting_id=row["meeting_id"],
                label=row["label"],
                embedding=row["embedding"],
                dim=row["dim"],
                model=row["model"] or model,
                speech_sec=float(row["speech_sec"] or 0.0),
                source="confirmed",
            )
        db.upsert_speaker_match(
            conn,
            row["meeting_id"],
            row["label"],
            state=STATE_RESOLVED,
            resolved_as=canonical,
        )
        # The owner listened to this voice and named it. That is evidence, not
        # inference, so it outranks anything the transcript pass concluded.
        db.set_speaker(conn, row["meeting_id"], row["label"], canonical, "confirmed")

    db.queue_minutes_refresh(conn, [row["meeting_id"] for row in members])
    conn.execute("DELETE FROM voice_clusters WHERE id = ?", (cluster_id,))
    return len(members)


def dismiss(conn: sqlite3.Connection, cluster_id: str) -> int:
    """Mark a cluster as not a real speaker - crosstalk, noise, a passing voice.

    Reversible, and the embeddings are kept. Over-segmentation means a dismissed
    fragment is sometimes a real person who barely spoke, and a dismissal that
    destroyed the evidence would be unrecoverable.
    """
    members = db.cluster_labels(conn, cluster_id)
    for row in members:
        db.upsert_speaker_match(conn, row["meeting_id"], row["label"], state=STATE_DISMISSED)
    conn.execute("DELETE FROM voice_clusters WHERE id = ?", (cluster_id,))
    return len(members)


def unsure(conn: sqlite3.Connection, cluster_id: str) -> int:
    """Return a cluster to the queue unanswered.

    A first-class answer, not a failure. People are unreliable at identifying
    voices they know only slightly, and a UI that pressures a decision
    manufactures confident wrong labels. Left pending, the cluster often resolves
    itself once a neighbouring voice is named.
    """
    members = db.cluster_labels(conn, cluster_id)
    conn.execute("DELETE FROM voice_clusters WHERE id = ?", (cluster_id,))
    return len(members)


def forget(conn: sqlite3.Connection, canonical: str) -> tuple[int, int]:
    """Delete every voice sample and snippet for one person.

    Every speaker who appears is enrolled, which is the owner's stated choice -
    so this is the remedy that choice depends on, and it has to remove files as
    well as rows. Returns (samples deleted, snippet files deleted).

    Not retroactive to backups. Any UI calling this must say so.
    """
    rows = db.person_samples(conn, canonical)
    snippets_removed = 0
    for row in rows:
        if not row["meeting_id"] or not row["label"]:
            continue
        match_row = db.get_speaker_match(conn, row["meeting_id"], row["label"])
        if match_row:
            snippets_removed += _delete_snippet_files(match_row["snippet_paths"])

    deleted = db.delete_person_voice_data(conn, canonical)
    return deleted, snippets_removed


def _delete_snippet_files(snippet_paths: str | None) -> int:
    from pipeline.config import SNIPPETS_DIR

    if not snippet_paths:
        return 0
    try:
        paths = json.loads(snippet_paths)
    except (json.JSONDecodeError, TypeError):
        return 0

    removed = 0
    for relative in paths:
        target = (SNIPPETS_DIR / str(relative)).resolve()
        # Same guard as capture._remove_handoff_file: never unlink outside the
        # directory this module owns.
        if not target.is_relative_to(SNIPPETS_DIR.resolve()):
            continue
        if target.exists():
            target.unlink()
            removed += 1
    return removed


# ── Snippet selection ─────────────────────────────────────────────────

def choose_snippets(
    regions: list[tuple[float, float]],
    words: list[tuple[float, float]] | None = None,
    *,
    count: int = SNIPPET_COUNT,
    clip_sec: float = SNIPPET_SEC,
    skip_opening_sec: float = SNIPPET_SKIP_OPENING_SEC,
    min_separation_sec: float = SNIPPET_MIN_SEPARATION_SEC,
) -> tuple[list[tuple[float, float]], str]:
    """Pick the clips the owner will actually hear.

    Naive selection ruins the review surface: the first six seconds of a label is
    usually "yeah - sorry, can you hear me?" over crosstalk, and asking someone
    to identify a voice from that is asking them to guess.

    `regions` are that label's (start, end) speech spans; `words` are aligned
    word spans used to reject near-silence. Returns the chosen spans and a
    quality verdict, so the UI can warn instead of silently offering six seconds
    of noise for a decision.
    """
    candidates: list[tuple[float, float]] = []
    for start, end in sorted(regions):
        # A clip must sit entirely inside one region - never spanning a speaker
        # change, or the owner hears two people and cannot answer.
        if end - start < clip_sec:
            continue
        # Generate across the whole region and filter by the opening window
        # below, rather than starting at the boundary: a meeting that ends
        # before skip_opening_sec would otherwise produce no candidates at all
        # and leave the fallback with nothing to fall back to.
        clip_start = start
        while clip_start + clip_sec <= end:
            candidates.append((clip_start, clip_start + clip_sec))
            clip_start += clip_sec

    eligible = [span for span in candidates if span[0] >= skip_opening_sec]
    quality = QUALITY_OK
    if not eligible:
        # Nothing after the opening window. Better a poor clip than no clip: an
        # unlabellable meeting is worse than a hard one.
        eligible = candidates
        quality = QUALITY_LOW

    if words:
        dense = [span for span in eligible if _word_coverage(span, words) >= SNIPPET_MIN_WORD_COVERAGE]
        if dense:
            eligible = dense
        else:
            quality = QUALITY_LOW

    chosen: list[tuple[float, float]] = []
    for span in eligible:
        if len(chosen) >= count:
            break
        # Spread the clips out, so three clips are not three slices of one
        # sentence and a mumbled moment does not sink the whole card.
        if all(abs(span[0] - picked[0]) >= min_separation_sec for picked in chosen):
            chosen.append(span)

    if not chosen and eligible:
        chosen = eligible[:count]
        quality = QUALITY_LOW
    if len(chosen) < count:
        quality = QUALITY_LOW

    return chosen, quality


def _word_coverage(span: tuple[float, float], words: list[tuple[float, float]]) -> float:
    """Fraction of a clip actually covered by spoken words."""
    start, end = span
    duration = end - start
    if duration <= 0:
        return 0.0
    covered = sum(
        max(0.0, min(end, word_end) - max(start, word_start)) for word_start, word_end in words
    )
    return min(covered / duration, 1.0)
