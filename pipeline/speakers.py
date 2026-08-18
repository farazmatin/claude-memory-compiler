"""Stage 3: turn SPEAKER_00 into a person's name.

Diarization gives anonymous labels. Minutes need real names, because an action
item without a correct owner is worse than one with no owner - it silently
assigns work to the wrong person and nobody notices.

The resolver is deliberately conservative: an unresolved label stays
`SPEAKER_01` rather than being guessed. A visible gap is fixable; a confident
wrong name is not.

Resolution order, weakest evidence first so stronger evidence overwrites:
  1. LLM pass over the opening minutes - reads self-introductions and who is
     addressed by name, seeded with candidate names from the filename
  2. Manual overrides file - ground truth, always wins

The filename supplies *candidates*, never a label mapping: knowing two people were
present does not tell you which diarization label is which.
"""

from __future__ import annotations

from pathlib import Path

from pipeline import db
from pipeline.asr import Transcript, format_timestamp
from pipeline.config import SPEAKER_OVERRIDES_FILE
from pipeline.llm import LLMError, complete, extract_fenced_block

# How much of the opening to show the model. Introductions happen early, and
# sending the whole hour would be slow and add nothing.
INTRO_WINDOW_SEC = 240

CONFIDENCE_CONFIRMED = "confirmed"
CONFIDENCE_INFERRED = "inferred"
CONFIDENCE_UNKNOWN = "unknown"


# ── Manual overrides ──────────────────────────────────────────────────

def load_overrides(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Read speaker-overrides.yaml.

    Shape, where the key is either a meeting id prefix or `default`:

        default:
          SPEAKER_00: Faraz
        a1b2c3d4e5f6:
          SPEAKER_01: Ali
    """
    target = path or SPEAKER_OVERRIDES_FILE
    if not target.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        print(f"    override file unreadable ({exc}); ignoring")
        return {}

    if not isinstance(data, dict):
        return {}
    return {
        str(key): {str(k): str(v) for k, v in value.items()}
        for key, value in data.items()
        if isinstance(value, dict)
    }


def overrides_for(meeting_id: str, overrides: dict[str, dict[str, str]]) -> dict[str, str]:
    """Merge the `default` block with any meeting-specific block."""
    merged = dict(overrides.get("default", {}))
    for key, mapping in overrides.items():
        if key != "default" and meeting_id.startswith(key):
            merged.update(mapping)
    return merged


# ── Heuristics ────────────────────────────────────────────────────────

def speaking_time(transcript: Transcript) -> dict[str, float]:
    """Total seconds per speaker label."""
    totals: dict[str, float] = {}
    for seg in transcript.segments:
        if seg.speaker:
            totals[seg.speaker] = totals.get(seg.speaker, 0.0) + max(0.0, seg.end - seg.start)
    return totals


def candidates_from_filename(
    transcript: Transcript, title_hint: str | None, owner_name: str | None
) -> list[str]:
    """Likely attendee names from the filename, title hint, or owner."""
    names: list[str] = []
    if owner_name and owner_name.strip():
        names.append(owner_name.strip())

    import re
    hint = (title_hint or "").strip()
    # Extract common first names or capitalized tokens from title hint
    for word in re.findall(r"\b[A-Z][a-z]+\b", hint):
        if word.lower() not in {
            "aug", "standup", "recording", "meeting", "sync", "call", "review", "planning",
            "top", "pillar", "rehearsal", "architecture", "delivery", "roadmap", "decision", "team"
        }:
            if word not in names:
                names.append(word)

    return names


# ── LLM resolution ────────────────────────────────────────────────────

def dialogue_excerpt(transcript: Transcript, max_lines: int = 100) -> str:
    """Representative labeled dialogue across the meeting."""
    segs = transcript.segments
    if len(segs) <= max_lines:
        chosen = segs
    else:
        # Sample opening, middle, and closing turns
        mid = len(segs) // 2
        chosen = segs[:50] + segs[max(0, mid - 15) : min(len(segs), mid + 15)] + segs[-20:]
    lines: list[str] = []
    for seg in chosen:
        who = seg.speaker or "UNKNOWN"
        lines.append(f"[{format_timestamp(seg.start)}] {who}: {seg.text}")
    return "\n".join(lines)


def build_resolution_prompt(
    transcript: Transcript,
    known_names: list[str],
    title_hint: str | None,
    candidates: list[str] | None = None,
) -> str:
    labels = transcript.speaker_labels
    known = ", ".join(known_names) if known_names else "(none recorded yet)"
    hint = title_hint or "(none)"
    likely = ", ".join(candidates) if candidates else "(none)"
    return f"""You are identifying speakers in a meeting transcript.

The diarization system assigned anonymous labels ({", ".join(labels)}).
Your job is to map each label to a real person's name, using evidence in the transcript.

## Labels to resolve
{", ".join(labels)}

## Filename hint
{hint}

## Likely attendees / candidates
{likely}

## Names seen in previous meetings
{known}

## Representative Dialogue
{dialogue_excerpt(transcript)}

## Rules
- Use a name only when the transcript supports it: someone introduces themselves, is addressed by name ("Hi Paul", "Christine, what do you think?"), or is clearly identifiable from context.
- If you cannot determine a label's name with confidence, output `null` for it. An honest null is far better than a guess.
- Do not invent names.

## Output
Return ONLY a valid JSON object mapping every label to a name string or null. No other text.
Example:
{{"SPEAKER_00": "Faraz", "SPEAKER_01": "Paul", "SPEAKER_02": null}}"""


def resolve_with_llm(
    transcript: Transcript,
    known_names: list[str],
    title_hint: str | None,
    candidates: list[str] | None = None,
) -> dict[str, str]:
    """Ask the model to name the speakers. Returns only confident mappings."""
    import json

    if not transcript.speaker_labels:
        return {}

    prompt = build_resolution_prompt(transcript, known_names, title_hint, candidates)
    try:
        raw = complete(prompt)
    except LLMError as exc:
        print(f"    speaker LLM pass failed ({exc}); leaving labels unresolved")
        return {}

    try:
        block = extract_fenced_block(raw, "json") or extract_fenced_block(raw) or raw.strip()
        s = block.find("{")
        e = block.rfind("}")
        if s != -1 and e != -1 and e > s:
            block = block[s : e + 1]
        parsed = json.loads(block)
    except (json.JSONDecodeError, ValueError):
        print("    speaker LLM returned unparseable output; leaving labels unresolved")
        return {}

    if not isinstance(parsed, dict):
        return {}

    valid = set(transcript.speaker_labels)
    return {
        str(label): str(name).strip()
        for label, name in parsed.items()
        if label in valid and isinstance(name, str) and name.strip() and name.lower() not in {"null", "unknown", "none"}
    }


# ── Stage entry point ─────────────────────────────────────────────────

def resolve(
    conn,
    meeting: db.Meeting,
    transcript: Transcript,
    owner_name: str | None = None,
    use_llm: bool = True,
) -> dict[str, str]:
    """Resolve every speaker label for one meeting and persist the result.

    Later sources win, so the cascade runs cheapest-first and lets manual
    overrides beat every inference.
    """
    labels = transcript.speaker_labels
    if not labels:
        return {}

    resolved: dict[str, str] = {}
    confidence: dict[str, str] = {}

    # Candidates only - the filename says who was probably present, never which
    # label is which.
    candidates = candidates_from_filename(transcript, meeting.title_hint, owner_name)

    if use_llm:
        for label, name in resolve_with_llm(
            transcript, db.known_speaker_names(conn), meeting.title_hint, candidates
        ).items():
            resolved[label] = name
            confidence[label] = CONFIDENCE_INFERRED

    # Manual overrides are ground truth and win over every inference.
    for label, name in overrides_for(meeting.id, load_overrides()).items():
        if label in labels:
            resolved[label] = name
            confidence[label] = CONFIDENCE_CONFIRMED

    # Normalize through the people registry before persisting. A model asked to
    # spell a name the same way it did four months ago is not a reliable strategy,
    # and every variant it invents becomes a separate graph node.
    for label, name in list(resolved.items()):
        resolved[label] = db.canonical_name(conn, name) or name

    for label in labels:
        db.set_speaker(
            conn,
            meeting.id,
            label,
            resolved.get(label),
            confidence.get(label, CONFIDENCE_UNKNOWN),
        )

    # Register anyone new so the next meeting normalizes against them.
    for name in resolved.values():
        db.add_person(conn, name)

    return resolved
