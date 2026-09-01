"""Stage: produce the voice vectors that prongs 7-8 consume.

`voices.py` knows how to compare voiceprints, band a match and cluster the
leftovers. It has never known where a vector comes from. The producer that used
to fill that gap loaded pyannote and torch locally and was retired; since then
`speaker_matches` has taken no new rows, so voice review covers the historical
corpus and nothing else. This module is the replacement producer, and it is the
only thing here that talks to an embedding provider.

Three properties are load-bearing:

**One call per meeting, not per label.** The provider takes every region at once
and returns a vector per label. At an average of four labels a meeting the
per-label shape the retired module used - it had to be, holding the model in
memory - would be four times the cost and four times the cold-start exposure.

**Plan, then call, then apply.** Every decision is made before any I/O, so a
dry run is a pure function and a provider failure cannot leave half a meeting
enrolled.

**Audio is fetched, not required.** Transcription deletes the local copy of a
Drive-backed recording, but Drive still holds the original and
`capture.rehydrate_audio` restores it when unchanged. The earlier plan for this
stage assumed the audio was gone for good and scoped itself to the 14 meetings
that still had a local file; every meeting in the corpus has a Drive source, so
the real ceiling is the whole corpus.

No torch, no pyannote, no local weights. The stage no-ops entirely when
`REMOTE_VOICE_MODEL` is unset, because the provider call is the only thing here
that costs money.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pipeline import db, voices
from pipeline.config import (
    REMOTE_VOICE_MODEL,
    SNIPPET_BITRATE,
    SNIPPET_EXT,
    TARGET_SAMPLE_RATE,
    VOICE_MAX_EMBED_SEC,
)

ACTION_EMBED = "embed"
ACTION_BOOTSTRAP = "bootstrap"
ACTION_SKIP = "skip"


class VoiceEmbedError(RuntimeError):
    """The stage could not produce vectors for a meeting."""


# ── Provider interface ────────────────────────────────────────────────


@dataclass(frozen=True)
class LabelRegion:
    """One span of a single speaker's audio, in seconds from the recording start."""

    label: str
    start: float
    end: float


@dataclass(frozen=True)
class EmbedResponse:
    embeddings: dict[str, list[float]]
    dim: int
    encoder: str
    speech_sec: dict[str, float] = field(default_factory=dict)


class VoiceEmbeddingBackend(Protocol):
    """Everything this stage needs from an embedding provider.

    Narrow on purpose. A backend receives audio and regions and returns one
    vector per label; it makes no decisions about who anybody is.
    """

    def embed(
        self, audio_path: Path, regions: list[LabelRegion], *, encoder: str
    ) -> EmbedResponse: ...


# ── Planning (pure: no I/O, no provider) ──────────────────────────────


@dataclass(frozen=True)
class LabelPlan:
    label: str
    action: str
    canonical: str | None
    regions: tuple[tuple[float, float], ...]
    speech_sec: float
    reason: str
    reuse_blob: bytes | None = None
    dim: int | None = None

    @property
    def needs_provider(self) -> bool:
        return self.action in (ACTION_EMBED, ACTION_BOOTSTRAP) and self.reuse_blob is None


def label_regions(transcript, label: str) -> list[tuple[float, float]]:
    """A label's speech spans, from segment-level speaker assignment.

    Not from `Word.speaker`: alignment can leave individual words untagged even
    when their segment carries a speaker, so segments are the reliable source.
    """
    return [(s.start, s.end) for s in transcript.segments if s.speaker == label]


def label_words(transcript, label: str) -> list[tuple[float, float]]:
    """Aligned word spans inside a label's segments, for the near-silence check."""
    return [
        (w.start, w.end)
        for seg in transcript.segments
        if seg.speaker == label
        for w in seg.words
        if w.start is not None and w.end is not None
    ]


def capped_regions(
    regions: list[tuple[float, float]], *, max_sec: float = VOICE_MAX_EMBED_SEC
) -> list[tuple[float, float]]:
    """Truncate a label's regions to at most `max_sec`, chronologically.

    A cost bound, not a quality selection - `VOICE_MIN_SPEECH_SEC` is already
    the floor for a robust sample.
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


def plan_meeting(
    conn, meeting, transcript, *, namespace: str, force: bool = False
) -> list[LabelPlan]:
    """Decide what to do with every label. Pure: no I/O beyond reading the manifest.

    The decision tree, in order:

      confirmed by a human, already enrolled here   -> skip
      confirmed by a human, reusable vector here    -> bootstrap, free
      confirmed by a human, no vector here          -> bootstrap, needs the provider
      unresolved, already embedded here, no --force -> skip
      otherwise                                     -> embed

    Bootstrap reuse is namespace-gated. The retired module reused a stored blob
    on the grounds that "the embedding does not change just because a human
    confirmed the name after the fact" - true within one encoder, false across
    two. Copying a vector between namespaces would mix vector spaces and quietly
    defeat the guarantee that a new-model vector never matches an old voiceprint.
    """
    stored = {
        r["label"]: (r["name"], r["confidence"])
        for r in conn.execute(
            "SELECT label, name, confidence FROM speakers WHERE meeting_id = ?",
            (meeting.id,),
        )
    }

    plans: list[LabelPlan] = []
    for label in transcript.speaker_labels:
        regions = label_regions(transcript, label)
        speech = sum(end - start for start, end in regions)
        capped = tuple(capped_regions(regions))
        name, confidence = stored.get(label, (None, None))
        existing = db.get_speaker_match(conn, meeting.id, label)

        if name and confidence == "confirmed":
            if db.voice_sample_exists(conn, name, meeting.id, label, namespace):
                plans.append(
                    LabelPlan(label, ACTION_SKIP, name, capped, speech, "already enrolled")
                )
                continue
            reuse = None
            if existing and existing["embedding"] and existing["model"] == namespace:
                reuse = existing["embedding"]
            plans.append(
                LabelPlan(
                    label, ACTION_BOOTSTRAP, name, capped, speech,
                    "confirmed by a human" + (" (vector reused)" if reuse else ""),
                    reuse_blob=reuse,
                    dim=existing["dim"] if reuse and existing else None,
                )
            )
            continue

        if (
            not force
            and existing
            and existing["embedding"]
            and existing["model"] == namespace
        ):
            plans.append(
                LabelPlan(label, ACTION_SKIP, None, capped, speech, "already embedded")
            )
            continue

        plans.append(LabelPlan(label, ACTION_EMBED, None, capped, speech, "unresolved"))

    return plans


# ── Snippets ──────────────────────────────────────────────────────────


def write_snippets(
    audio_path: Path, meeting_id: str, label: str, spans: list[tuple[float, float]]
) -> list[str]:
    """Cut each chosen span into its own opus clip under SNIPPETS_DIR.

    These have to outlive the source audio: the local recording is deleted once
    transcription commits, and a review card with no clip is a card asking
    someone to name a voice they cannot hear. Filenames are deterministic, so
    re-running overwrites in place rather than accumulating duplicates.
    """
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


# ── Per-meeting execution ─────────────────────────────────────────────


@dataclass
class MeetingEmbedResult:
    meeting_id: str
    embedded: int = 0
    bootstrapped: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)


def audio_for(meeting) -> Path:
    """The meeting's audio, restored from Drive if the local copy is gone.

    Transcription deletes the local archive of a Drive-backed recording to save
    space; Drive still holds the byte-identical original, and `rehydrate_audio`
    refuses to return one whose checksum moved. This is why the stage is not
    limited to meetings that happen to still have a local file.
    """
    if meeting.audio_path and Path(meeting.audio_path).is_file():
        return Path(meeting.audio_path)

    from pipeline import capture

    try:
        return capture.rehydrate_audio(meeting)
    except Exception as exc:
        raise VoiceEmbedError(f"no audio available for {meeting.id[:12]}: {exc}") from exc


def embed_meeting(
    conn, meeting, transcript, *, backend: VoiceEmbeddingBackend, namespace: str,
    force: bool = False,
) -> MeetingEmbedResult:
    """Plan, make one provider call, then persist. In that order, always.

    A provider failure leaves the manifest exactly as it was: nothing is written
    before the call returns, so a meeting is either fully embedded or untouched.
    """
    result = MeetingEmbedResult(meeting.id)
    plans = plan_meeting(conn, meeting, transcript, namespace=namespace, force=force)
    result.skipped = sum(1 for p in plans if p.action == ACTION_SKIP)

    wanted = [p for p in plans if p.needs_provider]
    response: EmbedResponse | None = None
    audio: Path | None = None

    if wanted:
        audio = audio_for(meeting)
        regions = [
            LabelRegion(p.label, start, end) for p in wanted for start, end in p.regions
        ]
        if not regions:
            for plan in wanted:
                result.failed.append(f"{plan.label}: no speech regions")
            return result
        response = backend.embed(audio, regions, encoder=namespace)

    for plan in plans:
        if plan.action == ACTION_SKIP:
            continue
        blob, dim = plan.reuse_blob, plan.dim
        if blob is None:
            vector = (response.embeddings if response else {}).get(plan.label)
            if not vector:
                result.failed.append(f"{plan.label}: provider returned no vector")
                continue
            blob, dim = voices.pack(vector)

        if plan.action == ACTION_BOOTSTRAP and plan.canonical:
            db.add_voice_sample(
                conn,
                canonical=plan.canonical,
                meeting_id=meeting.id,
                label=plan.label,
                embedding=blob,
                dim=dim or 0,
                model=namespace,
                speech_sec=plan.speech_sec,
                source="bootstrap",
            )
            result.bootstrapped += 1
            continue

        snippet_paths: list[str] = []
        quality = voices.QUALITY_LOW
        if audio is not None:
            spans, quality = voices.choose_snippets(
                label_regions(transcript, plan.label), label_words(transcript, plan.label)
            )
            if spans:
                try:
                    snippet_paths = write_snippets(audio, meeting.id, plan.label, spans)
                except (subprocess.SubprocessError, OSError) as exc:
                    # A missing clip degrades the review card; it does not
                    # invalidate the vector, which is the expensive part.
                    result.failed.append(f"{plan.label}: snippets failed ({exc})")

        db.upsert_speaker_match(
            conn,
            meeting.id,
            plan.label,
            embedding=blob,
            dim=dim or 0,
            model=namespace,
            speech_sec=plan.speech_sec,
            snippet_paths=json.dumps(snippet_paths),
            snippet_quality=quality,
        )
        result.embedded += 1

    return result


@dataclass
class RunResult:
    meetings: int = 0
    embedded: int = 0
    bootstrapped: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)


def configured() -> bool:
    """Whether a provider is configured. The stage no-ops when it is not."""
    return bool(REMOTE_VOICE_MODEL)


def eligible_meetings(conn, limit: int | None = None) -> list:
    """Meetings past transcription, oldest first, that have a transcript on disk."""
    rows = conn.execute(
        """
        SELECT id FROM meetings
        WHERE transcript_path IS NOT NULL AND status != ?
        ORDER BY meeting_date IS NULL, meeting_date, meeting_time, created_at
        """,
        (db.FAILED,),
    ).fetchall()
    meetings = [db.get_meeting(conn, r["id"]) for r in rows]
    meetings = [m for m in meetings if m and m.transcript_path]
    return meetings[:limit] if limit else meetings


def run(
    *,
    backend: VoiceEmbeddingBackend,
    namespace: str,
    meeting_id: str | None = None,
    limit: int | None = None,
    force: bool = False,
    verbose: bool = True,
) -> RunResult:
    """Embed every eligible meeting. Killable between meetings, idempotent."""
    from pipeline import asr

    result = RunResult()
    with db.connect() as conn:
        queue = (
            [db.get_meeting(conn, meeting_id)] if meeting_id else eligible_meetings(conn, limit)
        )
        queue = [m for m in queue if m]

    for meeting in queue:
        try:
            transcript = asr.load_transcript(meeting.id)
        except Exception as exc:
            result.failures.append(f"{meeting.id[:12]}: transcript unreadable ({exc})")
            continue
        try:
            with db.connect() as conn:
                one = embed_meeting(
                    conn, meeting, transcript,
                    backend=backend, namespace=namespace, force=force,
                )
        except VoiceEmbedError as exc:
            result.failures.append(str(exc))
            continue
        except Exception as exc:
            result.failures.append(f"{meeting.id[:12]}: {type(exc).__name__}: {exc}")
            continue

        result.meetings += 1
        result.embedded += one.embedded
        result.bootstrapped += one.bootstrapped
        result.skipped += one.skipped
        result.failures.extend(f"{meeting.id[:12]}: {f}" for f in one.failed)
        if verbose and (one.embedded or one.bootstrapped):
            print(
                f"  {meeting.id[:12]}  {one.embedded} embedded, "
                f"{one.bootstrapped} bootstrapped, {one.skipped} skipped"
            )

    return result
