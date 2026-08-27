"""Voice enrollment: audio -> speaker embeddings, feeding `voices.py`.

`voices.py` is a complete consumer with no producer: three tables
(`speaker_matches`, `voice_samples`, `voice_clusters`) that nothing writes to,
and a `SNIPPETS_DIR` that stays empty. This module is the producer.

One job, done once per (meeting, diarization label):

    load audio      -> a 16 kHz mono waveform, decoded once per meeting
    embed a label    -> one vector, over that label's own speech regions
    snippet a label   -> three short audio clips, written to SNIPPETS_DIR
    record            -> a `speaker_matches` row, via `db.upsert_speaker_match`

Two different outcomes follow, depending on what the transcript pass already
knows about a label:

  * **Already human-confirmed** (`speakers.CONFIDENCE_CONFIRMED`, set via the
    dashboard or `voices.confirm()`) - bootstrap a `voice_samples` row
    directly, `source="bootstrap"`, and mark the label resolved. The owner
    already said who this is; asking again would be noise. This is also what
    lets a name confirmed once, months before voice enrollment existed at all,
    immediately clear `VOICE_MIN_ENROLL_MEETINGS` the first time this runs.
  * **Not yet confirmed** - write the embedding and snippets as a pending
    label, with the transcript pass's own guess (if any) attached as
    `llm_name`. `voices.band()` already knows what to do with that: it is a
    veto signal, never a source of truth by itself.

The batch ends by calling the two `voices.py` functions that otherwise have no
caller at all - `rematch_pending()` so newly-bootstrapped voiceprints resolve
yesterday's unknowns immediately, then `cluster_pending()` so whatever is left
becomes one review card per person, not per appearance.

Every step here is safe to re-run: labels already embedded are skipped unless
`force=True`, and bootstrap enrollment checks `voice_samples` for the exact
(canonical, meeting, label) triple before inserting.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pipeline import asr, db, voices
from pipeline.config import (
    HF_TOKEN,
    TARGET_SAMPLE_RATE,
    VOICE_MODEL,
)
from pipeline.speakers import CONFIDENCE_CONFIRMED

# Bounds how much of one label's audio is fed to the embedding model. A
# recurring colleague can easily speak for twenty minutes in a planning
# meeting; the embedding model needs a robust sample, not the whole thing, and
# CPU inference time should not scale with how talkative someone was. Taken in
# chronological order from that label's regions - see _select_embed_regions.
MAX_EMBED_SEC = float(os.environ.get("MMC_ENROLL_MAX_EMBED_SEC", "90"))

# Opus: the format the SNIPPETS_DIR budget in config.py ("~30 KB per speaker")
# is written against - small clips that still sound like speech.
SNIPPET_EXT = ".opus"
SNIPPET_BITRATE = "24k"


@dataclass
class LabelResult:
    label: str
    outcome: str  # "bootstrapped" | "embedded" | "skipped" | "empty" | "error"
    canonical: str | None = None
    detail: str | None = None


@dataclass
class MeetingEnrollResult:
    meeting_id: str
    labels: list[LabelResult] = field(default_factory=list)

    @property
    def bootstrapped(self) -> list[str]:
        return [r.canonical for r in self.labels if r.outcome == "bootstrapped" and r.canonical]

    @property
    def embedded(self) -> int:
        return sum(1 for r in self.labels if r.outcome in ("bootstrapped", "embedded"))

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.labels if r.outcome == "skipped")

    @property
    def errors(self) -> list[LabelResult]:
        return [r for r in self.labels if r.outcome == "error"]


@dataclass
class EnrollRunResult:
    meetings: list[MeetingEnrollResult] = field(default_factory=list)
    promoted: int = 0
    clusters: int = 0


# ΓöÇΓöÇ Eligibility ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

def eligible_meetings(conn) -> list[db.Meeting]:
    """Meetings that can be embedded right now: local audio, and a transcript.

    Diarization regions live in the retained transcript, not the manifest, so
    both files have to exist on disk. Most of the archive fails this today -
    audio is deleted after transcription - which is exactly the gap stage 05
    of the plan (re-fetching from Drive) exists to close later.
    """
    out = []
    rows = conn.execute("SELECT id FROM meetings ORDER BY meeting_date, meeting_time").fetchall()
    for row in rows:
        meeting = db.get_meeting(conn, row["id"])
        if not meeting or not meeting.audio_path or not meeting.transcript_path:
            continue
        if Path(meeting.audio_path).is_file() and Path(meeting.transcript_path).is_file():
            out.append(meeting)
    return out


def _label_confidence(conn, meeting_id: str) -> dict[str, tuple[str | None, str | None]]:
    """label -> (name, confidence) for one meeting, confidence included.

    `db.get_speakers()` deliberately drops confidence (its callers only ever
    want the name), so this reads `speakers` directly rather than adding a
    second db.py helper for one caller.
    """
    rows = conn.execute(
        "SELECT label, name, confidence FROM speakers WHERE meeting_id = ?", (meeting_id,)
    ).fetchall()
    return {r["label"]: (r["name"], r["confidence"]) for r in rows}


# ΓöÇΓöÇ Audio ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

def _load_waveform(audio_path: Path):
    """Decode one meeting's audio to a 16 kHz mono float32 array, once.

    Reuses whisperx's own loader (ffmpeg under the hood, any input format)
    rather than pipeline.asr.normalize_audio + a second decode - one ffmpeg
    invocation per meeting instead of two.
    """
    import whisperx  # type: ignore[import-not-found]

    return whisperx.load_audio(str(audio_path), sr=TARGET_SAMPLE_RATE)


def _label_regions(transcript: asr.Transcript, label: str) -> list[tuple[float, float]]:
    """A label's speech spans, from segment-level speaker assignment.

    Not from `Word.speaker`: alignment can leave individual words without a
    speaker tag even when the segment itself carries one (see
    `asr._segments_from_whisperx`), so segments are the reliable source.
    """
    return [(seg.start, seg.end) for seg in transcript.segments if seg.speaker == label]


def _label_words(transcript: asr.Transcript, label: str) -> list[tuple[float, float]]:
    """Aligned word spans inside one label's segments, for choose_snippets'
    near-silence check. Words without timestamps are dropped rather than
    guessed."""
    return [
        (w.start, w.end)
        for seg in transcript.segments
        if seg.speaker == label
        for w in seg.words
        if w.start is not None and w.end is not None
    ]


def _select_embed_regions(
    regions: list[tuple[float, float]], *, max_sec: float = MAX_EMBED_SEC
) -> list[tuple[float, float]]:
    """Truncate a label's regions to at most `max_sec` of audio, in order.

    A simple chronological cap, not an accuracy-motivated selection: the
    embedding model needs a robust sample, and VOICE_MIN_SPEECH_SEC (30s) is
    already the floor for that. This exists to bound CPU time on the rare
    person who talks for twenty minutes, not to pick the "best" seconds.
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


_EMBEDDER_CACHE: dict[str, object] = {}


def _embedder(model_name: str = VOICE_MODEL):
    """Lazily load and cache the pyannote embedding model.

    Imported lazily: pyannote pulls in torch, and every stage besides voice
    enrollment must stay usable without it. Cached at module level because
    loading weights costs real time and a batch run shares one VOICE_MODEL
    across every meeting.
    """
    if model_name not in _EMBEDDER_CACHE:
        from pyannote.audio import Inference, Model

        model = Model.from_pretrained(model_name, use_auth_token=HF_TOKEN or None)
        _EMBEDDER_CACHE[model_name] = Inference(model, window="whole")
    return _EMBEDDER_CACHE[model_name]


def embed_label(waveform, regions: list[tuple[float, float]], *, model_name: str = VOICE_MODEL):
    """One embedding vector for a label, from its own speech only.

    Regions are concatenated into a single clip rather than embedded one at a
    time and averaged: a single forward pass over the label's actual speech is
    what the model in `window="whole"` mode expects, and it is what the
    smoke-tested feasibility pass in this project's Step 0 verified works on
    this machine. Returns None when the region list yields no usable audio -
    callers must not enroll from silence.

    Audio is passed as an in-memory `{"waveform", "sample_rate"}` dict rather
    than a file path: on this machine torchcodec (pyannote's default file
    decoder) cannot find its bundled ffmpeg DLLs, and pyannote's own warning at
    import time names this exact dict shape as the workaround.
    """
    import numpy as np
    import torch

    clips = [
        waveform[int(start * TARGET_SAMPLE_RATE) : int(end * TARGET_SAMPLE_RATE)]
        for start, end in _select_embed_regions(regions)
    ]
    clips = [c for c in clips if c.size > 0]
    if not clips:
        return None

    audio = np.concatenate(clips)
    tensor = torch.from_numpy(np.ascontiguousarray(audio)).unsqueeze(0)
    vector = _embedder(model_name)({"waveform": tensor, "sample_rate": TARGET_SAMPLE_RATE})
    return np.asarray(vector, dtype="float64").reshape(-1)


# ΓöÇΓöÇ Snippets ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

def write_snippets(
    audio_path: Path, meeting_id: str, label: str, spans: list[tuple[float, float]]
) -> list[str]:
    """Cut each chosen span out of the source audio into its own opus clip.

    One ffmpeg invocation per clip, same pattern as
    `dashboard.extract_speaker_snippet` - but writing to SNIPPETS_DIR instead
    of piping to the response, since these have to outlive the source audio.
    Filenames are deterministic (`label-index.opus`), so re-running overwrites
    in place rather than accumulating duplicates.
    """
    # Lazy, not a top-of-file import: same reason as voices.py's own
    # _delete_snippet_files - reading config.SNIPPETS_DIR at call time is what
    # lets a test's monkeypatch.setattr(config, "SNIPPETS_DIR", ...) reach here.
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


# ΓöÇΓöÇ Per-meeting enrollment ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

def _already_sampled(conn, canonical: str, meeting_id: str, label: str, model: str) -> bool:
    """Whether this exact (person, meeting, label) is already a voice sample.

    Guards bootstrap enrollment against re-running: `add_voice_sample` has no
    unique constraint of its own (each confirmation, including a by-ear one
    from `voices.confirm()`, is meant to be able to add a fresh row), so this
    module owns not inserting the same evidence twice.
    """
    row = conn.execute(
        "SELECT 1 FROM voice_samples WHERE canonical = ? AND meeting_id = ? "
        "AND label = ? AND model = ? LIMIT 1",
        (canonical, meeting_id, label, model),
    ).fetchone()
    return row is not None


def enroll_meeting(
    conn, meeting: db.Meeting, *, model: str = VOICE_MODEL, force: bool = False
) -> MeetingEnrollResult:
    """Embed and snippet every diarization label in one meeting.

    Each label fails independently - one bad region or a codec hiccup on one
    clip must not lose the rest of the meeting's labels, which is why the loop
    body catches per label rather than letting the caller's per-meeting
    try/except (see cli.py's other cmd_* stages) be the only safety net.
    """
    transcript = asr.load_transcript(meeting.id)
    confidences = _label_confidence(conn, meeting.id)
    result = MeetingEnrollResult(meeting_id=meeting.id)

    # Decoded at most once, lazily: a meeting whose labels are all
    # bootstrap-eligible-and-already-embedded needs no audio at all. A plain
    # closure over a one-element list rather than a module-level cache - this
    # must not survive past one call, or two meetings sharing a test id would
    # see each other's decoded audio.
    _waveform: list = []

    def get_waveform():
        if not _waveform:
            _waveform.append(_load_waveform(Path(meeting.audio_path)))
        return _waveform[0]

    for label in transcript.speaker_labels:
        name, confidence = confidences.get(label, (None, None))
        existing = db.get_speaker_match(conn, meeting.id, label)
        already_embedded = bool(existing) and existing["embedding"] is not None

        try:
            if confidence == CONFIDENCE_CONFIRMED and name:
                canonical = db.canonical_name(conn, name) or name
                result.labels.append(
                    _bootstrap_label(
                        conn, meeting, label, transcript, canonical,
                        existing=existing, model=model, waveform_fn=get_waveform,
                    )
                )
                continue

            if already_embedded and not force:
                # Cheap refresh even on the skip path: speaker naming can
                # rerun and change the guess between enroll passes, and
                # llm_name is only ever a veto signal - safe to update freely.
                db.upsert_speaker_match(conn, meeting.id, label, llm_name=name)
                result.labels.append(LabelResult(label, "skipped"))
                continue

            regions = _label_regions(transcript, label)
            vector = embed_label(get_waveform(), regions, model_name=model)
            if vector is None:
                result.labels.append(LabelResult(label, "empty", detail="no usable audio"))
                continue

            _write_pending_label(conn, meeting, label, transcript, regions, vector, name, model)
            result.labels.append(LabelResult(label, "embedded"))
        except Exception as exc:  # isolate one label's failure from the rest of the meeting
            result.labels.append(LabelResult(label, "error", detail=f"{type(exc).__name__}: {exc}"))

    return result


def _bootstrap_label(
    conn, meeting, label, transcript, canonical, *, existing, model, waveform_fn
) -> LabelResult:
    """Enroll a label the transcript pass already resolved by name.

    Reuses a previously-embedded vector from `speaker_matches` when one
    exists, rather than re-running pyannote: the embedding does not change
    just because a human confirmed the name after the fact.
    """
    if _already_sampled(conn, canonical, meeting.id, label, model):
        return LabelResult(label, "skipped", canonical=canonical, detail="already sampled")

    regions = _label_regions(transcript, label)
    speech_sec = sum(end - start for start, end in regions)

    if existing and existing["embedding"] is not None and existing["dim"] is not None:
        blob, dim = existing["embedding"], existing["dim"]
    else:
        vector = embed_label(waveform_fn(), regions, model_name=model)
        if vector is None:
            return LabelResult(label, "empty", detail="no usable audio")
        blob, dim = voices.pack(vector)

    db.add_person(conn, canonical)
    db.add_voice_sample(
        conn, canonical=canonical, meeting_id=meeting.id, label=label,
        embedding=blob, dim=dim, model=model, speech_sec=speech_sec, source="bootstrap",
    )
    # Mirrors voices.confirm(): the label is answered, so it leaves the
    # pending queue rather than surfacing as a review card for a name the
    # owner already gave through the transcript, not through voice review.
    db.upsert_speaker_match(
        conn, meeting.id, label,
        embedding=blob, dim=dim, model=model, speech_sec=speech_sec,
        state=voices.STATE_RESOLVED, resolved_as=canonical,
    )
    return LabelResult(label, "bootstrapped", canonical=canonical)


def _write_pending_label(conn, meeting, label, transcript, regions, vector, llm_name, model) -> None:
    """Persist one not-yet-confirmed label: embedding, snippets, llm_name.

    Left in `state="pending"` (upsert_speaker_match's default on first
    insert) for rematch_pending / cluster_pending to pick up.
    """
    blob, dim = voices.pack(vector)
    speech_sec = sum(end - start for start, end in regions)
    words = _label_words(transcript, label)
    spans, quality = voices.choose_snippets(regions, words)
    relative_paths = write_snippets(Path(meeting.audio_path), meeting.id, label, spans)

    db.upsert_speaker_match(
        conn, meeting.id, label,
        embedding=blob, dim=dim, model=model, speech_sec=speech_sec,
        snippet_paths=json.dumps(relative_paths), snippet_quality=quality,
        llm_name=llm_name,
    )


# ΓöÇΓöÇ Batch entry point ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

def finalize(conn, *, model: str = VOICE_MODEL) -> tuple[int, int]:
    """The two `voices.py` functions this producer exists to feed.

    Run once after every meeting in the batch, not per meeting: clustering is
    inherently a pass over ALL pending labels at once, and rematching a label
    before every meeting's bootstrap enrollment has landed would miss
    voiceprints this same run just created.
    """
    promoted = voices.rematch_pending(conn, model=model)
    clusters = voices.cluster_pending(conn, model=model)
    return promoted, clusters


def run(*, limit: int | None = None, force: bool = False, model: str = VOICE_MODEL) -> EnrollRunResult:
    """Embed every eligible meeting, then rematch and cluster once.

    Opens one connection per meeting, like `cmd_speakers` / `cmd_transcribe` in
    cli.py, so one meeting's work is committed before the next begins and a
    crash mid-batch loses at most one meeting's progress.
    """
    db.init_db()
    with db.connect() as conn:
        queue = eligible_meetings(conn)
    if limit:
        queue = queue[:limit]

    result = EnrollRunResult()
    for meeting in queue:
        with db.connect() as conn:
            result.meetings.append(enroll_meeting(conn, meeting, model=model, force=force))

    with db.connect() as conn:
        result.promoted, result.clusters = finalize(conn, model=model)
    return result


# ΓöÇΓöÇ Accuracy evaluation ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
#
# The plan's own stage-02 gate: far-field phone recordings of group meetings
# are the hard case for speaker embeddings, and the right response to that is
# a measurement, not an assumption. This is deliberately decoupled from how an
# embedding was produced - `samples` is plain (name, meeting_id, label,
# vector, speech_sec) tuples - so the exact same leave-one-out mechanics run
# under a fast synthetic-vector unit test and under the real pyannote pass
# used to produce this project's reported accuracy number.

@dataclass
class AccuracyResult:
    correct: int
    total: int
    misses: list[tuple[str, str, str | None]] = field(default_factory=list)

    @property
    def top1(self) -> float:
        return self.correct / self.total if self.total else 0.0


def leave_one_out_accuracy(
    conn, samples: list[tuple[str, str, str, object, float]], *, model: str = VOICE_MODEL
) -> AccuracyResult:
    """Cross-meeting top-1 accuracy: can a person be recognised in a meeting
    their voiceprint was never built from?

    Held out at the (name, meeting) grain, not per label: hiding one label but
    leaving a second label from the SAME person in the SAME meeting enrolled
    would let the voiceprint recognise its own recording session rather than
    the person, which is not the question this measures. Only names appearing
    in at least two distinct meetings are testable at all - a person enrolled
    from one meeting has nothing to hold out, which is a property of the
    corpus, not a bug here (see VOICE_MIN_ENROLL_MEETINGS for the same limit
    applied to real auto-matching).

    Writes into `conn` and restores it to the state it was in on return; pass
    a throwaway connection, never the real manifest.
    """
    meetings_by_name: dict[str, set[str]] = {}
    for name, meeting_id, _label, _vec, _sec in samples:
        meetings_by_name.setdefault(name, set()).add(meeting_id)
    testable = {name for name, meetings in meetings_by_name.items() if len(meetings) >= 2}

    sample_ids: dict[tuple[str, str, str], int] = {}
    for name, meeting_id, label, vector, speech_sec in samples:
        db.add_person(conn, name)
        blob, dim = voices.pack(vector)
        sample_ids[(name, meeting_id, label)] = db.add_voice_sample(
            conn, canonical=name, meeting_id=meeting_id, label=label,
            embedding=blob, dim=dim, model=model, speech_sec=speech_sec, source="bootstrap",
        )

    pairs = sorted({(name, meeting_id) for name, meeting_id, *_ in samples if name in testable})
    correct = 0
    total = 0
    misses: list[tuple[str, str, str | None]] = []

    for name, meeting_id in pairs:
        held_out = [s for s in samples if s[0] == name and s[1] == meeting_id]
        for _n, _m, label, _v, _s in held_out:
            db.delete_voice_sample(conn, sample_ids[(name, meeting_id, label)])

        prints = voices.enrolled(conn, model)
        for _n, _m, _label, vector, _s in held_out:
            result = voices.match(vector, prints)
            total += 1
            if result.best == name:
                correct += 1
            else:
                misses.append((name, meeting_id, result.best))

        for _n, _m, label, vector, speech_sec in held_out:
            blob, dim = voices.pack(vector)
            sample_ids[(name, meeting_id, label)] = db.add_voice_sample(
                conn, canonical=name, meeting_id=meeting_id, label=label,
                embedding=blob, dim=dim, model=model, speech_sec=speech_sec, source="bootstrap",
            )

    return AccuracyResult(correct=correct, total=total, misses=misses)
