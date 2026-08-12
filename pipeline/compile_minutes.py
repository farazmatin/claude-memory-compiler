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

from pipeline import db, entities
from pipeline.asr import Transcript, format_timestamp
from pipeline.config import (
    MINUTES_DIR,
    MINUTES_MAP_WINDOW_TOKENS,
    MINUTES_PROMPT_TOKEN_BUDGET,
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

# Retrieved topical material is already condensed by LightRAG, but cap it so a
# broad match cannot crowd out the transcript being compiled.
TOPICAL_EXCERPT_CHARS = 4000

# ~4 characters per token for English. Deliberately rough and deliberately
# conservative: the cost of over-estimating is an unnecessary map-reduce pass,
# while under-estimating means a blown context window or a burned quota.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough token count. See CHARS_PER_TOKEN for why an estimate is enough."""
    return len(text) // CHARS_PER_TOKEN


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


def split_dialogue(dialogue: str, window_tokens: int) -> list[str]:
    """Split rendered dialogue into windows on speaker-turn boundaries.

    Splitting mid-turn would cut a decision away from its rationale, so windows
    only break between turns even if that makes them uneven.
    """
    budget = window_tokens * CHARS_PER_TOKEN
    windows: list[str] = []
    current: list[str] = []
    size = 0

    for line in dialogue.split("\n"):
        if current and size + len(line) > budget:
            windows.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1

    if current:
        windows.append("\n".join(current))
    return windows


def build_map_prompt(window: str, index: int, total: int) -> str:
    """Extract durable content from one window of a long meeting."""
    return f"""You are extracting notes from part {index} of {total} of a long meeting
transcript.

Do NOT write minutes yet. Extract only what is durable, preserving detail:

- Topics discussed
- Decisions, each with its rationale and who decided
- Open questions
- Action items with owners and due dates
- Customer or user statements, close to verbatim
- Risks, blockers, dependencies
- Product entities named (features, epics, releases, customers)

Rules:
- Preserve names, product names, numbers and dates EXACTLY as they appear.
- Keep the [H:MM:SS] timestamps attached to what they refer to.
- Omit filler, small talk and repetition.
- Do not summarize away rationale. "Chose X because Y" must keep the Y.
- If this part contains nothing durable, reply exactly: NOTHING

## Transcript part {index}/{total}

{window}"""


def map_reduce_dialogue(dialogue: str) -> str:
    """Condense an over-budget transcript into per-window extracts.

    Used only when a meeting exceeds the prompt budget - a three-hour recording
    would otherwise either overflow the context window or burn a disproportionate
    slice of subscription quota in one call. Extraction keeps rationale and
    entities, so the reduce step still has real material to compile from.
    """
    windows = split_dialogue(dialogue, MINUTES_MAP_WINDOW_TOKENS)
    print(f"    long meeting: {len(windows)} windows -> map/reduce")

    extracts: list[str] = []
    for position, window in enumerate(windows, 1):
        try:
            extract = complete(build_map_prompt(window, position, len(windows))).strip()
        except LLMError as exc:
            # One failed window should not lose the rest of the meeting; note the
            # gap explicitly so the reduce step does not invent continuity.
            print(f"    window {position}/{len(windows)} failed: {exc}")
            extracts.append(f"### Part {position}/{len(windows)}\n(extraction failed)")
            continue
        if extract and extract != "NOTHING":
            extracts.append(f"### Part {position}/{len(windows)}\n{extract}")

    if not extracts:
        raise LLMError("map pass produced no usable extracts")
    return "\n\n".join(extracts)


# Kept as a split string rather than a literal list: it is edited by hand and
# reads far better as prose. SIM905 suggests otherwise; readability wins here.
STOPWORDS = frozenset(
    (
        "a an and are as at be been but by for from had has have he her him his i if in "
        "is it its me my not of on or our she that the their them then there these they "
        "this to was we were what when which who will with would you your yeah okay just "
        "like really think know going get got right sure well actually basically"
    ).split(" ")
)


def salient_terms(dialogue: str, limit: int = 12) -> list[str]:
    """Frequent, distinctive terms from the dialogue.

    Crude on purpose: this only has to produce a good enough retrieval query. It
    favours capitalised and hyphenated tokens, which is where product names,
    people and feature names live.
    """
    counts: dict[str, int] = {}
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9'\-]{2,}", dialogue):
        token = raw.strip("'-")
        lowered = token.lower()
        if lowered in STOPWORDS or len(token) < 3:
            continue
        # Capitalised mid-sentence or hyphenated tokens are likely proper nouns.
        weight = 3 if (token[0].isupper() or "-" in token) else 1
        counts[token] = counts.get(token, 0) + weight

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [term for term, _ in ranked[:limit]]


def build_topic_query(meeting: db.Meeting, dialogue: str) -> str:
    """A retrieval query describing what this meeting is about."""
    terms = salient_terms(dialogue)
    subject = ", ".join(filter(None, [meeting.title_hint, *terms]))
    return (
        "Prior decisions, commitments and stated positions relating to: "
        f"{subject}. Include what was decided and the reasoning given."
    )


def load_prior_context(conn, meeting: db.Meeting, dialogue: str | None = None) -> str:
    """Earlier minutes that this meeting might contradict.

    Two sources, combined:

    1. **Chronological** - the last few meetings. Cheap, always available, and
       catches same-day and next-day reversals.
    2. **Topical** - a LightRAG retrieval for the subjects this meeting actually
       discusses. This is the important one: a decision being reversed is usually
       months old, so recency alone systematically misses exactly the long-horizon
       reversals worth flagging.

    The topical lookup is safe by construction: minutes are compiled in stage 4 and
    indexed in stage 5, so this meeting is not yet in the index and every hit
    necessarily comes from an earlier one. No date filtering is needed, which is
    fortunate because LightRAG offers none.

    Degrades to chronological-only when LightRAG is unreachable - prior context is
    a quality improvement, not a prerequisite.
    """
    blocks: list[str] = []

    priors = db.recent_indexed_before(
        conn,
        meeting.meeting_date,
        PRIOR_CONTEXT_DOCS,
        meeting_time=meeting.meeting_time,
        exclude_id=meeting.id,
    )
    for prior in priors:
        if not prior.minutes_path:
            continue
        path = Path(prior.minutes_path)
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")[:PRIOR_EXCERPT_CHARS]
        blocks.append(f"### {prior.meeting_date} — {prior.title_hint or 'meeting'}\n\n{content}")

    if dialogue:
        from pipeline import index

        retrieved = index.query_context(build_topic_query(meeting, dialogue)).strip()
        if retrieved:
            blocks.append(
                "### Topically related earlier material\n\n"
                f"{retrieved[:TOPICAL_EXCERPT_CHARS]}"
            )

    return "\n\n".join(blocks) if blocks else "(no earlier minutes on record)"


def build_prompt(
    meeting: db.Meeting,
    transcript: Transcript,
    speaker_names: dict[str, str],
    prior_context: str,
    dialogue: str | None = None,
    condensed: bool = False,
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
    body = dialogue if dialogue is not None else render_dialogue(transcript, speaker_names)

    if condensed:
        source_heading = (
            "## Extracted notes\n\n"
            "This meeting was too long to pass verbatim, so it was extracted in\n"
            "parts first. Compile the minutes from these notes. Treat them as the\n"
            "record of what happened, and do not invent continuity across a part\n"
            "marked as failed."
        )
    else:
        source_heading = "## Transcript"

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

{source_heading}

{body}

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
    full_dialogue = render_dialogue(transcript, speaker_names)
    dialogue = full_dialogue
    condensed = False

    if estimate_tokens(full_dialogue) > MINUTES_PROMPT_TOKEN_BUDGET:
        dialogue = map_reduce_dialogue(full_dialogue)
        condensed = True

    prompt = build_prompt(
        meeting,
        transcript,
        speaker_names,
        # Topical retrieval uses the full dialogue, not the condensed form, so
        # term extraction sees what was actually said.
        load_prior_context(conn, meeting, dialogue=full_dialogue),
        dialogue=dialogue,
        condensed=condensed,
    )
    document = strip_wrapping_fence(complete(prompt))

    if not document.startswith("---"):
        raise LLMError("compiled minutes are missing YAML frontmatter")

    # Entities and relations were emitted by a frontier model; normalize the person
    # names through the registry and persist them independently of LightRAG.
    parsed_entities, parsed_relations = entities.extract(document)
    parsed_entities, parsed_relations = entities.canonicalize(
        conn, parsed_entities, parsed_relations
    )
    db.replace_entities(conn, meeting.id, parsed_entities, parsed_relations)
    for entity in parsed_entities:
        if entity.get("kind") == "person":
            db.add_person(conn, entity["name"])

    MINUTES_DIR.mkdir(parents=True, exist_ok=True)
    path = minutes_path(meeting, extract_title(document))
    path.write_text(document + "\n", encoding="utf-8")
    return path, document
