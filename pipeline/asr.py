"""Stage 2: audio -> speaker-labeled, word-timestamped transcript.

Three distinct jobs happen here, done by three different models:

  ASR         audio -> text            (what was said)
  alignment   audio + text -> times    (exactly when each word was said)
  diarization audio -> labeled spans   (who was speaking)

They are orthogonal. Diarization produces no words; ASR has no idea who is
talking. Merging them is what turns an anonymous wall of text into
"Ali: let's defer that to Q4", which is what makes action-item ownership
possible downstream.

The transcript produced here is Tier 1 material: retained for provenance and
recompilation, but never fed to the RAG index.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from pipeline.config import (
    GLOSSARY_FILE,
    IMPLAUSIBLE_SPEAKER_COUNT,
    INITIAL_PROMPT_TOKEN_BUDGET,
    REPLICATE_API_TOKEN,
    TARGET_SAMPLE_RATE,
    TRANSCRIPTS_DIR,
)

# ── Transcript model ──────────────────────────────────────────────────

@dataclass
class Word:
    start: float | None
    end: float | None
    text: str
    speaker: str | None = None


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    meeting_id: str
    model: str
    language: str
    duration_sec: float | None
    segments: list[Segment]

    @property
    def diarization_warning(self) -> str | None:
        """A warning when the speaker count looks like over-segmentation.

        A high count is usually the hosted diarizer splitting one noisy speaker rather than a
        real crowd, and it makes every downstream name unreliable. Surfaced rather
        than corrected: the fix is a bound or a better microphone, not a guess.
        """
        count = len(self.speaker_labels)
        if count > IMPLAUSIBLE_SPEAKER_COUNT:
            return (
                f"{count} speakers detected - likely over-segmentation. Speaker names "
                f"will be unreliable. Consider MMC_MAX_SPEAKERS or better mic placement."
            )
        if count == 0:
            return "no speakers detected - action items will have no owners"
        return None

    @property
    def speaker_labels(self) -> list[str]:
        """Distinct diarization labels, in first-appearance order."""
        seen: list[str] = []
        for seg in self.segments:
            if seg.speaker and seg.speaker not in seen:
                seen.append(seg.speaker)
        return seen

    def to_dict(self) -> dict:
        return {
            "meeting_id": self.meeting_id,
            "model": self.model,
            "language": self.language,
            "duration_sec": self.duration_sec,
            "segments": [asdict(s) for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Transcript:
        segments = [
            Segment(
                start=s["start"],
                end=s["end"],
                text=s["text"],
                speaker=s.get("speaker"),
                words=[Word(**w) for w in s.get("words", [])],
            )
            for s in data.get("segments", [])
        ]
        return cls(
            meeting_id=data["meeting_id"],
            model=data.get("model", "unknown"),
            language=data.get("language", "en"),
            duration_sec=data.get("duration_sec"),
            segments=segments,
        )


class Backend(Protocol):
    """Remote ASR implementation contract.

    The product always uses the configured Replicate provider.
    """

    name: str

    def transcribe(self, audio_path: Path, meeting_id: str, initial_prompt: str) -> Transcript:
        ...


# ── Audio normalization ───────────────────────────────────────────────

def normalize_audio(src: Path, dest: Path) -> Path:
    """Transcode to 16 kHz mono WAV.

    Every ASR model resamples to 16 kHz mono internally, so doing it up front
    costs nothing in accuracy and turns ~18 MB/hr of m4a into ~2-4 MB/hr.
    """
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-i", str(src),
            "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE),
            "-c:a", "pcm_s16le", str(dest),
        ],
        check=True, capture_output=True,
    )
    return dest


# ── Glossary -> initial_prompt ────────────────────────────────────────

def load_glossary_terms() -> list[str]:
    """Terms from glossary.md, in file order.

    File order IS priority order, because Whisper's initial_prompt is capped and
    the tail gets dropped. Put people's names and product names at the top.
    """
    if not GLOSSARY_FILE.exists():
        return []
    terms: list[str] = []
    for line in GLOSSARY_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        # A term is a list item, and only a list item. This used to accept any
        # non-heading line, which fed the file's own instructions to Whisper as
        # if they were vocabulary: of 19 "terms" extracted from the shipped
        # glossary, 4 were real and the rest were prose, markdown fragments and
        # HTML comments. Worse, the prose sits above the names, so it consumed
        # the ~224-token budget from the top and pushed the actual people the
        # file exists to protect toward the truncation point.
        if not re.match(r"^[-*+]\s+", stripped):
            continue
        stripped = re.sub(r"^[-*+]\s*", "", stripped)
        if stripped.startswith("<!--"):
            continue
        # Drop any explanatory text after a colon or dash; only the term biases ASR.
        stripped = re.split(r"\s+[-–—]\s+|:\s+", stripped, maxsplit=1)[0].strip()
        # Markdown emphasis around a term is formatting, not part of the word.
        stripped = stripped.strip("*_`").strip()
        if stripped:
            terms.append(stripped)
    return terms


def build_initial_prompt(terms: list[str] | None = None) -> str:
    """Vocabulary-biasing prompt, truncated to Whisper's token budget.

    A mangled product or person name does more damage than a mangled filler
    word: it fragments graph entities downstream, so the same feature ends up as
    three different nodes. Biasing the decoder is the cheapest fix available.
    """
    if terms is None:
        terms = load_glossary_terms()
    if not terms:
        return ""

    # ~4 chars per token for English is close enough, and erring small is safe:
    # over-running the cap silently drops the tail.
    char_budget = INITIAL_PROMPT_TOKEN_BUDGET * 4
    prefix = "Glossary: "
    kept: list[str] = []
    used = len(prefix)
    for term in terms:
        cost = len(term) + 2
        if used + cost > char_budget:
            break
        kept.append(term)
        used += cost
    return prefix + ", ".join(kept) + "." if kept else ""



def _segments_from_provider_output(result: dict) -> list[Segment]:
    """Convert remote-provider output into our model, tolerating missing fields.

    Alignment can leave individual words without timestamps, and un-aligned runs
    can leave segments without them too, so every numeric field is treated as
    optional rather than assumed present.
    """
    segments: list[Segment] = []
    for raw in result.get("segments", []):
        words = [
            Word(
                start=w.get("start"),
                end=w.get("end"),
                text=(w.get("word") or w.get("text") or "").strip(),
                speaker=w.get("speaker"),
            )
            for w in raw.get("words", [])
            if (w.get("word") or w.get("text"))
        ]
        text = (raw.get("text") or "").strip()
        if not text and words:
            text = " ".join(w.text for w in words)
        if not text:
            continue
        segments.append(
            Segment(
                start=float(raw.get("start") or 0.0),
                end=float(raw.get("end") or 0.0),
                text=text,
                speaker=raw.get("speaker"),
                words=words,
            )
        )
    return segments


# ── Rendering ─────────────────────────────────────────────────────────

def format_timestamp(seconds: float | None) -> str:
    """Seconds as H:MM:SS (or M:SS under an hour)."""
    if seconds is None:
        return "?:??"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def render_markdown(transcript: Transcript, speaker_names: dict[str, str] | None = None) -> str:
    """Readable transcript, consecutive same-speaker segments merged into turns.

    Turn-merged rather than segment-per-line because the minutes compiler reads
    this, and speech split every few seconds reads as fragmented noise.
    """
    names = speaker_names or {}
    lines = [
        f"# Transcript {transcript.meeting_id[:12]}",
        "",
        f"- Model: `{transcript.model}`",
        f"- Language: {transcript.language}",
        f"- Duration: {format_timestamp(transcript.duration_sec)}",
        "",
        "---",
        "",
    ]

    current_speaker: str | None = None
    buffer: list[str] = []
    turn_start: float | None = None

    def flush() -> None:
        if not buffer:
            return
        who = names.get(current_speaker or "", current_speaker or "UNKNOWN")
        lines.append(f"**[{format_timestamp(turn_start)}] {who}:** {' '.join(buffer)}")
        lines.append("")

    for seg in transcript.segments:
        if seg.speaker != current_speaker:
            flush()
            buffer = []
            current_speaker = seg.speaker
            turn_start = seg.start
        buffer.append(seg.text)
    flush()

    return "\n".join(lines)


# ── Stage entry point ─────────────────────────────────────────────────

def transcript_paths(meeting_id: str) -> tuple[Path, Path]:
    """(json, markdown) paths for a meeting's transcript."""
    return (
        TRANSCRIPTS_DIR / f"{meeting_id[:12]}.json",
        TRANSCRIPTS_DIR / f"{meeting_id[:12]}.md",
    )


def load_transcript(meeting_id: str) -> Transcript:
    """Read a retained transcript back off disk.

    This is what makes recompilation cheap: the minutes stage can be re-run over
    years of history from these files without touching ASR again.
    """
    import json

    json_path, _ = transcript_paths(meeting_id)
    return Transcript.from_dict(json.loads(json_path.read_text(encoding="utf-8")))


def save_transcript(transcript: Transcript, speaker_names: dict[str, str] | None = None) -> Path:
    """Write both the machine-readable JSON and the human-readable Markdown."""
    import json

    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path, md_path = transcript_paths(transcript.meeting_id)
    json_path.write_text(json.dumps(transcript.to_dict(), indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(transcript, speaker_names), encoding="utf-8")
    return json_path


def default_backend() -> Backend:
    """Return the required remote Replicate transcription backend."""
    if not REPLICATE_API_TOKEN:
        raise RuntimeError(
            "REPLICATE_API_TOKEN is required for transcription."
        )
    from pipeline.replicate_asr import ReplicateBackend

    return ReplicateBackend()
