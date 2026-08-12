"""Stage 4: compile a diarized transcript into structured minutes.

This is the compiler step, and the minutes it produces are the only thing that
ever reaches the RAG index. Transcripts are ~5% signal, have no headings to chunk
on, and are dense with pronouns that lose all meaning once split - indexing them
directly fills the graph with noise nodes. Minutes are the compiled artifact:
dense, structured, entity-preserving.

Minutes are a lossy compile, which is why transcripts are retained. Bump
TEMPLATE_VERSION and re-run this stage to rebuild years of history from those
transcripts with no ASR cost.
"""

from __future__ import annotations

import re
from pathlib import Path

from pipeline import db
from pipeline.asr import Transcript, format_timestamp
from pipeline.config import (
    MINUTES_DIR,
    MINUTES_TARGET_WORDS_MAX,
    MINUTES_TARGET_WORDS_MIN,
    MINUTES_TEMPLATE_FILE,
    PRIOR_CONTEXT_DOCS,
    ROOT_DIR,
    TEMPLATE_VERSION,
)
from pipeline.llm import LLMError, complete

# Prior minutes are context, not the subject. Cap them so a long history cannot
# crowd out the transcript actually being compiled.
PRIOR_EXCERPT_CHARS = 2500


def slugify(text: str) -> str:
    """Filename-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def minutes_path(meeting: db.Meeting, title: str | None = None) -> Path:
    """Where a meeting's minutes live: YYYY-MM-DD-slug.md."""
    date = meeting.meeting_date or "0000-00-00"
    stem = slugify(title or meeting.title_hint or "meeting") or "meeting"
    return MINUTES_DIR / f"{date}-{stem}-{meeting.id[:8]}.md"


def render_dialogue(transcript: Transcript, speaker_names: dict[str, str]) -> str:
    """Speaker-attributed, timestamped dialogue for the compiler prompt.

    Turns rather than segments: speech chopped every few seconds reads as noise
    and makes the model lose track of who is arguing what.
    """
    lines: list[str] = []
    current: str | None = None
    buffer: list[str] = []
    start: float | None = None

    def flush() -> None:
        if buffer:
            who = speaker_names.get(current or "", current or "UNKNOWN")
            lines.append(f"[{format_timestamp(start)}] {who}: {' '.join(buffer)}")

    for seg in transcript.segments:
        if seg.speaker != current:
            flush()
            buffer = []
            current = seg.speaker
            start = seg.start
        buffer.append(seg.text)
    flush()
    return "\n".join(lines)


def load_prior_context(conn, meeting: db.Meeting) -> str:
    """Excerpts from the most recent earlier minutes.

    Feeds the Changed-From-Previous-Position section. Only meetings that predate
    this one are included, which is why the pipeline processes in meeting-date
    order rather than discovery order - out-of-order compilation would compare a
    meeting against its own future.
    """
    priors = db.recent_indexed_before(
        conn,
        meeting.meeting_date,
        PRIOR_CONTEXT_DOCS,
        meeting_time=meeting.meeting_time,
        exclude_id=meeting.id,
    )
    if not priors:
        return "(no earlier minutes on record)"

    blocks: list[str] = []
    for prior in priors:
        if not prior.minutes_path:
            continue
        path = Path(prior.minutes_path)
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")[:PRIOR_EXCERPT_CHARS]
        blocks.append(f"### {prior.meeting_date} — {prior.title_hint or 'meeting'}\n\n{content}")

    return "\n\n".join(blocks) if blocks else "(no earlier minutes on record)"


def build_prompt(
    meeting: db.Meeting,
    transcript: Transcript,
    speaker_names: dict[str, str],
    prior_context: str,
) -> str:
    template = MINUTES_TEMPLATE_FILE.read_text(encoding="utf-8")
    attendees = sorted(set(speaker_names.values())) or ["(unresolved)"]
    unresolved = [
        label for label in transcript.speaker_labels if label not in speaker_names
    ]
    unresolved_note = (
        f"\nUnresolved speaker labels (list these as `Unknown speaker (LABEL)`, do not "
        f"guess names): {', '.join(unresolved)}"
        if unresolved
        else ""
    )
    audio_rel = _relative(meeting.audio_path)
    transcript_rel = _relative(meeting.transcript_path)

    return f"""You are a meeting minutes compiler. Convert the transcript below into
structured minutes that follow the specification exactly.

## Specification

{template}

## Meeting metadata

- Date: {meeting.meeting_date}
- Time: {meeting.meeting_time}
- Filename hint: {meeting.title_hint or "(none)"}
- Duration: {format_timestamp(transcript.duration_sec)}
- Resolved attendees: {", ".join(attendees)}{unresolved_note}
- template_version: "{TEMPLATE_VERSION}"
- source_audio: {audio_rel}
- source_transcript: {transcript_rel}

## Earlier minutes, for detecting reversals

Use these ONLY to populate "Changed From Previous Position". Do not summarize
them. Do not flag a change unless this meeting genuinely contradicts or
materially advances something recorded here.

{prior_context}

## Transcript

{render_dialogue(transcript, speaker_names)}

## Requirements

- Output ONLY the minutes document: YAML frontmatter, then the body. No preamble,
  no explanation, no code fences around the whole thing.
- Target {MINUTES_TARGET_WORDS_MIN}-{MINUTES_TARGET_WORDS_MAX} words. Do not
  compress into an executive summary - rationale must survive.
- Preserve feature names, people, customers, releases, and numbers verbatim.
- Every decision gets its rationale and who decided it.
- Cite timestamps in [H:MM:SS] form, taken from the transcript.
- Omit any section that has no genuine content. Never invent content to fill one.
"""


def _relative(path: str | None) -> str:
    """Path relative to the repo root when possible, for portable frontmatter."""
    if not path:
        return "(none)"
    try:
        return str(Path(path).resolve().relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def extract_title(document: str) -> str | None:
    """Pull `title:` out of the frontmatter, falling back to the first heading."""
    match = re.search(r"^title:\s*(.+)$", document, re.MULTILINE)
    if match:
        return match.group(1).strip().strip("\"'") or None
    match = re.search(r"^#\s+(.+)$", document, re.MULTILINE)
    return match.group(1).strip() if match else None


def strip_wrapping_fence(document: str) -> str:
    """Remove a fence wrapping the entire document.

    Models fence markdown output even when told not to, and a stray ``` before
    the frontmatter breaks every YAML parser downstream.
    """
    text = document.strip()
    if not text.startswith("```"):
        return text
    newline = text.find("\n")
    if newline == -1:
        return text
    body = text[newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[: -3]
    return body.strip()


def compile_meeting(
    conn,
    meeting: db.Meeting,
    transcript: Transcript,
    speaker_names: dict[str, str],
) -> tuple[Path, str]:
    """Compile one meeting's minutes and write them to disk.

    Returns (path, document). Raises LLMError if the model call fails, leaving the
    meeting at its current status so the batch can retry it later.
    """
    prompt = build_prompt(meeting, transcript, speaker_names, load_prior_context(conn, meeting))
    document = strip_wrapping_fence(complete(prompt))

    if not document.startswith("---"):
        raise LLMError("compiled minutes are missing YAML frontmatter")

    MINUTES_DIR.mkdir(parents=True, exist_ok=True)
    path = minutes_path(meeting, extract_title(document))
    path.write_text(document + "\n", encoding="utf-8")
    return path, document
