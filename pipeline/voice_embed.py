"""Voice embedding: the remote producer that feeds `voices.py`.

`voices.py` is a complete consumer with no producer. It can match, band, cluster,
confirm and forget voiceprints - but since the local enroller was retired nothing
generates embeddings, so `speaker_matches` gets no new rows, `voice_clusters`
stays empty, and every meeting resolves speakers the slow way: an LLM guess plus
overrides, never compounding.

This module restores the producer without loading a single model weight locally.
The embedding model runs on Replicate, exactly like transcription, and the same
contract holds: this repository never loads ASR, alignment, diarization, or
speaker-embedding weights.

One job, once per (meeting, diarization label):

    regions   the label's own speech spans, from the retained transcript
    embed     one remote call per meeting -> one vector per label
    snippet   short clips cut locally with ffmpeg, into SNIPPETS_DIR
    record    a `speaker_matches` row, via `db.upsert_speaker_match`

Two outcomes, depending on what is already known about a label:

  * **Already human-confirmed** (`speakers.CONFIDENCE_CONFIRMED`, set from the
    dashboard or `voices.confirm()`) - enroll a `voice_samples` row directly,
    `source="bootstrap"`, and mark the label resolved. The owner already said who
    this is; asking again would be noise. This is also what lets every name
    confirmed since the beginning compound the moment the stage first runs.
  * **Not yet confirmed** - write the embedding and snippets as a pending label,
    with the transcript pass's own guess attached as `llm_name`.
    `voices.band()` already knows what to do with that: a veto signal, never a
    source of truth by itself.

The batch ends by calling the `voices.py` functions that otherwise have no
caller - `rematch_pending()` so newly-bootstrapped voiceprints resolve yesterday's
unknowns immediately, `apply_auto()` so an auto-band decision is actually
written, then `cluster_pending()` so whatever is left becomes one review card per
person rather than one per appearance.

Everything here is safe to re-run. Labels already carrying an embedding in the
active namespace are skipped unless `force=True`, so re-running never re-pays for
work already done, and bootstrap enrollment checks `voice_samples` for the exact
(canonical, meeting, label) triple before inserting.

Left unconfigured (`MMC_REMOTE_VOICE_MODEL` unset) the whole stage no-ops. A
second paid provider is added deliberately, never by upgrading.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pipeline import asr, db, voices
from pipeline.config import (
    REMOTE_VOICE_MODEL,
    SNIPPET_BITRATE,
    SNIPPET_EXT,
    TARGET_SAMPLE_RATE,
    VOICE_MAX_EMBED_SEC,
    VOICE_VECTOR_NAMESPACE,
)
from pipeline.speakers import CONFIDENCE_CONFIRMED

STAGE_NAME = "voice"

# Per-label outcomes, reported rather than summed into one number: "4 labels
# embedded" and "4 labels had no audio left" are very different states of the
# world and an operator has to be able to tell them apart.
OUTCOME_BOOTSTRAPPED = "bootstrapped"
OUTCOME_EMBEDDED = "embedded"
OUTCOME_SKIPPED = "skipped"
OUTCOME_NO_AUDIO = "no-audio"
OUTCOME_EMPTY = "empty"
OUTCOME_ERROR = "error"


class VoiceEmbeddingError(RuntimeError):
    """Raised when the remote embedding provider cannot be used.

    ``transient`` marks a network fault, worth another attempt later, apart from
    a rejected token or a malformed request, which repeats identically forever.
    """

    def __init__(self, *args: object, transient: bool = False) -> None:
        super().__init__(*args)
        self.transient = transient


# ── Provider seam ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Region:
    """One label's speech span, as sent to the provider."""

    label: str
    start: float
    end: float


@dataclass(frozen=True)
class EmbeddingBatch:
    """One provider response: a vector per label, in one vector space."""

    embeddings: dict[str, list[float]]
    dim: int
    encoder: str
    # What the provider says it actually embedded. Recorded for diagnosis only;
    # the `speech_sec` written to the row is computed here from the transcript,
    # because the request is capped at VOICE_MAX_EMBED_SEC and the row's value
    # gates auto-matching - it has to mean "how much this person spoke", not
    # "how much we paid to embed".
    speech_sec: dict[str, float] = field(default_factory=dict)


class VoiceEmbeddingBackend(Protocol):
    """Remote speaker-embedding contract.

    Deliberately per-meeting rather than per-label: the provider embeds the
    labels' own regions server-side and returns one duration-weighted centroid
    each, so there is no label-mapping problem to get wrong and one paid call per
    meeting rather than one per speaker.
    """

    name: str

    def namespace(self) -> str:
        ...

    def embed(self, audio_path: Path, regions: list[Region]) -> EmbeddingBatch:
        ...


def default_backend() -> VoiceEmbeddingBackend | None:
    """The configured provider, or None when the stage is switched off.

    None is a normal state, not an error: until the benchmark picks an encoder
    the model id stays unset and the pipeline is exactly the pipeline without
    this stage.
    """
    if not REMOTE_VOICE_MODEL:
        return None
    from pipeline.replicate_voice import ReplicateVoiceBackend

    return ReplicateVoiceBackend()


# ── Results ───────────────────────────────────────────────────────────

@dataclass
class LabelResult:
    label: str
    outcome: str
    canonical: str | None = None
    detail: str | None = None


@dataclass
class MeetingResult:
    meeting_id: str
    labels: list[LabelResult] = field(default_factory=list)
    failed: str | None = None

    @property
    def embedded(self) -> int:
        return sum(
            1 for r in self.labels if r.outcome in (OUTCOME_BOOTSTRAPPED, OUTCOME_EMBEDDED)
        )

    @property
    def bootstrapped(self) -> list[str]:
        return [
            r.canonical for r in self.labels
            if r.outcome == OUTCOME_BOOTSTRAPPED and r.canonical
        ]

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.labels if r.outcome == OUTCOME_SKIPPED)

    @property
    def errors(self) -> list[LabelResult]:
        return [r for r in self.labels if r.outcome == OUTCOME_ERROR]


@dataclass
class RunResult:
    namespace: str = ""
    meetings: list[MeetingResult] = field(default_factory=list)
    promoted: int = 0
    backfilled: int = 0
    clusters: int = 0
    disabled: bool = False

    @property
    def failures(self) -> list[MeetingResult]:
        return [m for m in self.meetings if m.failed or m.errors]


# ── Namespace ─────────────────────────────────────────────────────────

def resolve_namespace(conn, backend: VoiceEmbeddingBackend) -> str:
    """The namespace this run reads and writes, recorded in the manifest.

    Resolved from the provider (encoder plus the version actually served) rather
    than assumed, and stored so the dashboard queue and every later run read the
    same space the producer wrote. Re-pinning to a different encoder therefore
    starts a clean namespace instead of scoring new vectors against old ones,
    which is the failure that produces confident wrong names.
    """
    namespace = backend.namespace().strip()
    if not namespace:
        raise VoiceEmbeddingError("embedding provider returned an empty namespace")
    if namespace == VOICE_VECTOR_NAMESPACE:
        # The quarantine for the retired local enroller's vectors. Writing new
        # work into it would mix two incomparable vector spaces silently.
        raise VoiceEmbeddingError(
            f"refusing to write new embeddings into the quarantined "
            f"'{VOICE_VECTOR_NAMESPACE}' namespace"
        )
    if db.get_setting(conn, "voice.active_namespace") != namespace:
        db.set_setting(conn, "voice.active_namespace", namespace)
    return namespace


# ── Transcript geometry ───────────────────────────────────────────────

def label_regions(transcript: asr.Transcript, label: str) -> list[tuple[float, float]]:
    """A label's speech spans, from segment-level speaker assignment.

    Not from `Word.speaker`: alignment can leave individual words untagged even
    when their segment carries a speaker, so segments are the reliable source.
    """
    return [(seg.start, seg.end) for seg in transcript.segments if seg.speaker == label]


def label_words(transcript: asr.Transcript, label: str) -> list[tuple[float, float]]:
    """Aligned word spans inside one label's segments.

    Feeds `choose_snippets`' near-silence check. Words without timestamps are
    dropped rather than guessed at.
    """
    return [
        (w.start, w.end)
        for seg in transcript.segments
        if seg.speaker == label
        for w in seg.words
        if w.start is not None and w.end is not None
    ]


def select_embed_regions(
    regions: list[tuple[float, float]], *, max_sec: float = VOICE_MAX_EMBED_SEC
) -> list[tuple[float, float]]:
    """Truncate a label's regions to at most `max_sec`, in chronological order.

    A bound on what is sent, not an attempt to pick the "best" seconds: the model
    needs a robust sample, `VOICE_MIN_SPEECH_SEC` is already the floor for that,
    and neither cost nor latency should scale with how talkative someone was.
    """
    total = 0.0
    chosen: list[tuple[float, float]] = []
    for start, end in sorted(regions):
        if total >= max_sec:
            break
        span_end = min(end, start + (max_sec - total))
        if span_end > start:
            chosen.append((start, span_end))
            total += span_end - start
    return chosen


def speech_seconds(regions: list[tuple[float, float]]) -> float:
    return sum(max(0.0, end - start) for start, end in regions)


# ── Snippets ──────────────────────────────────────────────────────────

def write_snippets(
    audio_path: Path, meeting_id: str, label: str, spans: list[tuple[float, float]]
) -> list[str]:
    """Cut each chosen span out of the source audio into its own opus clip.

    One ffmpeg invocation per clip, written to SNIPPETS_DIR rather than streamed,
    because these have to outlive the source audio - a voice card months from now
    is the only way a label whose audio was deleted can still be named by ear.
    Filenames are deterministic, so re-running overwrites in place instead of
    accumulating duplicates.

    ffmpeg is local audio handling, not model inference: decode and cut, no
    weights. The provider policy bounds the second, not the first.
    """
    # Lazy, not a top-of-file import: reading config.SNIPPETS_DIR at call time is
    # what lets a test's monkeypatch reach here - the same reason
    # voices._delete_snippet_files does it.
    from pipeline.config import SNIPPETS_DIR

    out_dir = SNIPPETS_DIR / meeting_id
    out_dir.mkdir(parents=True, exist_ok=True)
    relative: list[str] = []
    for index, (start, end) in enumerate(spans):
        dest = out_dir / f"{label}-{index}{SNIPPET_EXT}"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y",
                "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
                "-i", str(audio_path),
                "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE),
                "-c:a", "libopus", "-b:a", SNIPPET_BITRATE,
                str(dest),
            ],
            check=True, capture_output=True,
        )
        relative.append(f"{meeting_id}/{dest.name}")
    return relative


# ── Eligibility ───────────────────────────────────────────────────────

def eligible_meetings(
    conn, *, limit: int | None = None, meeting_id: str | None = None
) -> list[db.Meeting]:
    """Meetings this stage can work on: transcribed or later, transcript on disk.

    Diarization regions live in the retained transcript, not the manifest, so the
    transcript file is the hard requirement. Audio is checked per label rather
    than here: a meeting whose labels are all embedded already needs no audio at
    all, and refusing it up front would report a gap that does not exist.
    """
    if meeting_id:
        meeting = db.get_meeting(conn, meeting_id)
        candidates = [meeting] if meeting else []
    else:
        rows = conn.execute(
            "SELECT id FROM meetings WHERE status IN (?, ?, ?) "
            "ORDER BY meeting_date, meeting_time",
            (db.TRANSCRIBED, db.SPEAKERS_RESOLVED, db.MINUTES_COMPILED),
        ).fetchall()
        candidates = [db.get_meeting(conn, row["id"]) for row in rows]

    out: list[db.Meeting] = []
    for meeting in candidates:
        if not meeting:
            continue
        transcript_json, _ = asr.transcript_paths(meeting.id)
        if transcript_json.is_file():
            out.append(meeting)
    return out[:limit] if limit else out


def _label_confidence(conn, meeting_id: str) -> dict[str, tuple[str | None, str | None]]:
    """label -> (name, confidence) for one meeting.

    `db.get_speakers()` deliberately drops confidence (its callers only want the
    name), so this reads `speakers` directly rather than adding a second db.py
    helper for one caller.
    """
    rows = conn.execute(
        "SELECT label, name, confidence FROM speakers WHERE meeting_id = ?", (meeting_id,)
    ).fetchall()
    return {r["label"]: (r["name"], r["confidence"]) for r in rows}


def _already_sampled(conn, canonical: str, meeting_id: str, label: str, model: str) -> bool:
    """Whether this exact (person, meeting, label) is already a voice sample.

    `add_voice_sample` has no unique constraint of its own - each by-ear
    confirmation is meant to be able to add a fresh row - so this module owns not
    inserting the same evidence twice on a re-run.
    """
    row = conn.execute(
        "SELECT 1 FROM voice_samples WHERE canonical = ? AND meeting_id = ? "
        "AND label = ? AND model = ? LIMIT 1",
        (canonical, meeting_id, label, model),
    ).fetchone()
    return row is not None


# ── Per-meeting embedding ─────────────────────────────────────────────

@dataclass
class _Plan:
    """What one label needs before anything is paid for."""

    label: str
    regions: list[tuple[float, float]]
    speech_sec: float
    canonical: str | None          # set when the label is human-confirmed
    llm_name: str | None
    reuse: tuple[bytes, int] | None  # an embedding already held in this namespace


def _plan_labels(
    conn, meeting: db.Meeting, transcript: asr.Transcript, namespace: str, force: bool
) -> tuple[list[_Plan], list[LabelResult]]:
    """Decide per label what to do, before any provider call is made.

    Planning first is what keeps the paid call to one per meeting and keeps a
    re-run free: everything already embedded or already sampled drops out here.
    """
    confidences = _label_confidence(conn, meeting.id)
    plans: list[_Plan] = []
    settled: list[LabelResult] = []

    for label in transcript.speaker_labels:
        name, confidence = confidences.get(label, (None, None))
        regions = label_regions(transcript, label)
        existing = db.get_speaker_match(conn, meeting.id, label)
        # An embedding only counts as present if it is in THIS namespace. A row
        # left by another encoder is not a cheaper version of this one; it is a
        # vector from a different space that must never be matched against.
        reuse: tuple[bytes, int] | None = None
        if (
            existing
            and existing["embedding"] is not None
            and existing["dim"] is not None
            and existing["model"] == namespace
        ):
            reuse = (existing["embedding"], existing["dim"])

        canonical: str | None = None
        if confidence == CONFIDENCE_CONFIRMED and name:
            canonical = db.canonical_name(conn, name) or name
            if _already_sampled(conn, canonical, meeting.id, label, namespace):
                settled.append(
                    LabelResult(label, OUTCOME_SKIPPED, canonical, "already enrolled")
                )
                continue
        elif reuse and not force:
            # Cheap refresh even on the skip path: the transcript pass can rerun
            # and change its guess between voice passes, and `llm_name` is only
            # ever a veto signal - safe to update freely.
            db.upsert_speaker_match(conn, meeting.id, label, llm_name=name)
            settled.append(LabelResult(label, OUTCOME_SKIPPED, detail="already embedded"))
            continue

        if not regions:
            settled.append(LabelResult(label, OUTCOME_EMPTY, canonical, "no speech regions"))
            continue

        plans.append(
            _Plan(
                label=label,
                regions=regions,
                speech_sec=speech_seconds(regions),
                canonical=canonical,
                llm_name=name,
                # A confirmed label re-uses its vector even under --force: the
                # embedding does not change because a human named it afterwards.
                reuse=reuse if (canonical or not force) else None,
            )
        )

    return plans, settled


def embed_meeting(
    conn,
    meeting: db.Meeting,
    backend: VoiceEmbeddingBackend,
    *,
    namespace: str,
    force: bool = False,
) -> MeetingResult:
    """Embed and snippet every diarization label in one meeting.

    One provider call covers the meeting. A provider failure fails this meeting
    and nothing else: whatever embeddings it already had are left untouched, the
    meeting's status is not changed, and the run continues to the next meeting.
    """
    result = MeetingResult(meeting_id=meeting.id)
    transcript = asr.load_transcript(meeting.id)
    plans, settled = _plan_labels(conn, meeting, transcript, namespace, force)
    result.labels.extend(settled)
    if not plans:
        return result

    audio_path = Path(meeting.audio_path) if meeting.audio_path else None
    has_audio = bool(audio_path and audio_path.is_file())

    needs_provider = [plan for plan in plans if plan.reuse is None]
    batch: EmbeddingBatch | None = None
    if needs_provider:
        if not has_audio:
            # The audio was deleted before the voice stage ever saw it. Say so
            # per label rather than failing the meeting: the labels that can
            # still be enrolled from a retained vector should be.
            for plan in needs_provider:
                result.labels.append(
                    LabelResult(plan.label, OUTCOME_NO_AUDIO, plan.canonical, "audio deleted")
                )
            plans = [plan for plan in plans if plan.reuse is not None]
            if not plans:
                return result
        else:
            regions = [
                Region(plan.label, start, end)
                for plan in needs_provider
                for start, end in select_embed_regions(plan.regions)
            ]
            try:
                batch = backend.embed(audio_path, regions)  # type: ignore[arg-type]
            except Exception as exc:
                result.failed = f"{type(exc).__name__}: {exc}"
                return result

    for plan in plans:
        try:
            blob, dim = _vector_for(plan, batch)
        except LookupError as exc:
            result.labels.append(
                LabelResult(plan.label, OUTCOME_EMPTY, plan.canonical, str(exc))
            )
            continue
        except Exception as exc:  # one label's failure must not lose the rest
            result.labels.append(
                LabelResult(
                    plan.label, OUTCOME_ERROR, plan.canonical, f"{type(exc).__name__}: {exc}"
                )
            )
            continue

        try:
            if plan.canonical:
                _enroll_confirmed(conn, meeting, plan, blob, dim, namespace)
                result.labels.append(
                    LabelResult(plan.label, OUTCOME_BOOTSTRAPPED, plan.canonical)
                )
            else:
                _write_pending(
                    conn, meeting, plan, blob, dim, namespace, transcript,
                    audio_path if has_audio else None,
                )
                result.labels.append(LabelResult(plan.label, OUTCOME_EMBEDDED))
        except Exception as exc:
            result.labels.append(
                LabelResult(
                    plan.label, OUTCOME_ERROR, plan.canonical, f"{type(exc).__name__}: {exc}"
                )
            )

    return result


def _vector_for(plan: _Plan, batch: EmbeddingBatch | None) -> tuple[bytes, int]:
    if plan.reuse is not None:
        return plan.reuse
    if batch is None or plan.label not in batch.embeddings:
        raise LookupError("provider returned no embedding for this label")
    vector = batch.embeddings[plan.label]
    if not vector:
        raise LookupError("provider returned an empty embedding")
    return voices.pack(vector)


def _enroll_confirmed(
    conn, meeting: db.Meeting, plan: _Plan, blob: bytes, dim: int, namespace: str
) -> None:
    """Enroll a label a human already named, and take it out of the queue.

    Mirrors `voices.confirm()`: the label is answered, so it must not surface as
    a review card asking for a name the owner already gave.
    """
    canonical = plan.canonical or ""
    db.add_person(conn, canonical)
    db.add_voice_sample(
        conn,
        canonical=canonical,
        meeting_id=meeting.id,
        label=plan.label,
        embedding=blob,
        dim=dim,
        model=namespace,
        speech_sec=plan.speech_sec,
        source="bootstrap",
    )
    db.upsert_speaker_match(
        conn, meeting.id, plan.label,
        embedding=blob, dim=dim, model=namespace, speech_sec=plan.speech_sec,
        state=voices.STATE_RESOLVED, resolved_as=canonical,
    )


def _write_pending(
    conn,
    meeting: db.Meeting,
    plan: _Plan,
    blob: bytes,
    dim: int,
    namespace: str,
    transcript: asr.Transcript,
    audio_path: Path | None,
) -> None:
    """Persist one not-yet-named label: embedding, snippets, and the LLM's guess.

    Left `state="pending"` for `rematch_pending` / `apply_auto` /
    `cluster_pending` to pick up.
    """
    fields: dict[str, object] = {
        "embedding": blob,
        "dim": dim,
        "model": namespace,
        "speech_sec": plan.speech_sec,
        "llm_name": plan.llm_name,
    }
    if audio_path is not None:
        spans, quality = voices.choose_snippets(plan.regions, label_words(transcript, plan.label))
        fields["snippet_paths"] = json.dumps(
            write_snippets(audio_path, meeting.id, plan.label, spans)
        )
        fields["snippet_quality"] = quality
    db.upsert_speaker_match(conn, meeting.id, plan.label, **fields)


# ── Batch entry point ─────────────────────────────────────────────────

def finalize(conn, *, namespace: str) -> tuple[int, int, int]:
    """The `voices.py` calls this producer exists to feed.

    Run once after the whole batch, never per meeting: clustering is inherently a
    pass over every pending label at once, and rematching before the batch's
    bootstrap enrollments have landed would miss voiceprints this same run just
    created.

    Returns (promoted, backfilled, clusters). `backfilled` is a second
    `apply_auto` pass, which picks up anything banded `auto` by an earlier run
    and never applied - the state every corpus is in before this stage existed.
    """
    promoted = voices.rematch_pending(conn, model=namespace)
    backfilled = len(voices.apply_auto(conn, namespace))
    clusters = voices.cluster_pending(conn, model=namespace)
    return promoted, backfilled, clusters


def _summarize(result: MeetingResult) -> str:
    counts: dict[str, int] = {}
    for label in result.labels:
        counts[label.outcome] = counts.get(label.outcome, 0) + 1
    return ", ".join(f"{outcome}={count}" for outcome, count in sorted(counts.items())) or "no labels"


def run(
    *,
    limit: int | None = None,
    force: bool = False,
    meeting_id: str | None = None,
    backend: VoiceEmbeddingBackend | None = None,
) -> RunResult:
    """Embed every eligible meeting, then close the loop on what that resolved.

    A missing provider is not a failure: `disabled` comes back True, nothing is
    written, and the caller reports success. That is what makes the stage safe to
    put in `run`/`watch` before the encoder has been chosen.
    """
    provider = backend or default_backend()
    if provider is None:
        return RunResult(disabled=True)

    with db.connect() as conn:
        namespace = resolve_namespace(conn, provider)
        queue = eligible_meetings(conn, limit=limit, meeting_id=meeting_id)

    result = RunResult(namespace=namespace)
    for meeting in queue:
        with db.connect() as conn:
            run_id = db.start_stage(conn, meeting.id, STAGE_NAME)
        with db.connect() as conn:
            meeting_result = embed_meeting(
                conn, meeting, provider, namespace=namespace, force=force
            )
        result.meetings.append(meeting_result)
        with db.connect() as conn:
            # The stage never touches the meeting's status - it sits beside the
            # pipeline rather than in it, so a provider outage costs voice
            # labelling and nothing else. `mark_failed` would park the meeting
            # and stop its minutes from ever compiling.
            db.finish_stage(
                conn,
                run_id,
                meeting_result.failed is None,
                meeting_result.failed or _summarize(meeting_result),
            )

    with db.connect() as conn:
        result.promoted, result.backfilled, result.clusters = finalize(
            conn, namespace=namespace
        )
    return result
