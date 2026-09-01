"""Stage 3: turn SPEAKER_00 into a person's name.

Diarization gives anonymous labels. Minutes need real names, because an action
item without a correct owner is worse than one with no owner - it silently
assigns work to the wrong person and nobody notices.

The resolver is deliberately conservative: an unresolved label stays
`SPEAKER_01` rather than being guessed. A visible gap is fixable; a confident
wrong name is not. The same principle governs re-resolution: a label a human
has already confirmed, or one the LLM already named, must never be reset to
unknown just because a later pass over the same meeting failed to reconfirm
it - see `_merge_with_existing` below.

Resolution order, weakest evidence first so stronger evidence overwrites:
  1. LLM pass over the opening minutes - reads self-introductions and who is
     addressed by name, seeded with candidate names from the filename, the
     glossary's People section, and direct-address cues in the dialogue
  2. Manual overrides file - ground truth, always wins

The filename, glossary and direct-address cues supply *candidates*, never a
label mapping: knowing someone was probably present does not tell you which
diarization label is which.
"""

from __future__ import annotations

import re
from pathlib import Path

from pipeline import db
from pipeline.asr import Transcript, format_timestamp
from pipeline.config import GLOSSARY_FILE, SPEAKER_OVERRIDES_FILE
from pipeline.llm import LLMError, complete, extract_fenced_block

# How much of the opening to show the model. Introductions happen early, and
# sending the whole hour would be slow and add nothing.
INTRO_WINDOW_SEC = 240

CONFIDENCE_CONFIRMED = "confirmed"
CONFIDENCE_INFERRED = "inferred"
CONFIDENCE_UNKNOWN = "unknown"

# Capitalized tokens that turn up in filenames and dialogue but are never a
# person's name. Shared by every heuristic below that pulls candidates out of
# free text, so the exclusion list does not drift between them.
_NON_NAME_WORDS = frozenset({
    "aug", "standup", "recording", "meeting", "sync", "call", "review", "planning",
    "top", "pillar", "rehearsal", "architecture", "delivery", "roadmap", "decision", "team",
    "right", "yeah", "okay", "sure", "well", "thanks", "please", "so", "now", "then",
})


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
    """Likely attendee names from the filename, title hint, or owner.

    Only offered for calls with two or fewer diarized speakers. A filename
    hint is reliable for a 1:1 ("Ali Aug 10 at 11-12 a.m.") because it names
    the other person on the call - but for a group meeting it names at most
    the organizer, and handing the model a partial attendee list implies a
    completeness the hint never actually had. An incomplete candidate list is
    worse than none: it biases the model toward the two names it was given
    and away from whoever else was actually in the room.
    """
    if len(transcript.speaker_labels) > 2:
        return []

    names: list[str] = []
    if owner_name and owner_name.strip():
        names.append(owner_name.strip())

    hint = (title_hint or "").strip()
    # Extract common first names or capitalized tokens from title hint
    for word in re.findall(r"\b[A-Z][a-z]+\b", hint):
        if word.lower() not in _NON_NAME_WORDS and word not in names:
            names.append(word)

    return names


def glossary_people() -> list[str]:
    """Recurring attendees listed under glossary.md's "## People" section.

    A human already maintains this list to bias the ASR vocabulary (see
    asr.load_glossary_terms); treating it as a candidate pool for speaker
    resolution too costs nothing and recovers exactly the ground truth a
    title-hint regex can never see - people who are simply never named in a
    filename.
    """
    if not GLOSSARY_FILE.exists():
        return []

    names: list[str] = []
    in_people = False
    for line in GLOSSARY_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_people = stripped.lstrip("#").strip().lower() == "people"
            continue
        if not in_people:
            continue
        # Only actual bullets are terms - glossary.md's own format note says so.
        # Skipping this check would also pick up the section's descriptive prose
        # ("Recurring attendees. Use the spelling...") as a fake candidate name.
        match = re.match(r"[-*+]\s*(.+)", stripped)
        if not match:
            continue
        # Drop any explanatory text after a colon or dash, same as the ASR loader.
        term = re.split(r"\s+[-–—]\s+|:\s+", match.group(1), maxsplit=1)[0].strip()
        if term:
            names.append(term)
    return names


_ADDRESS_GREETING_RE = re.compile(r"\b(?:[Tt]hanks|[Tt]hank you|[Hh]ey|[Hh]i)[, ]+([A-Z][a-z]+)\b")
_ADDRESS_VOCATIVE_RE = re.compile(r",\s*([A-Z][a-z]+)[.?!]?$")


def direct_address_names(transcript: Transcript) -> list[str]:
    """Names surfaced by direct address in the dialogue.

    "Thanks, Ruth" and "...what do you think, Paul?" are strong evidence that
    someone by that name is on the call, and they are evidence the filename
    can never carry for a group meeting. Like every other function in this
    section, this returns candidates, never a label mapping: knowing "Ruth"
    was addressed does not say which SPEAKER_xx she is.
    """
    found: list[str] = []
    for seg in transcript.segments:
        text = seg.text.strip()
        for pattern in (_ADDRESS_GREETING_RE, _ADDRESS_VOCATIVE_RE):
            match = pattern.search(text)
            if not match:
                continue
            name = match.group(1)
            if name.lower() not in _NON_NAME_WORDS and name not in found:
                found.append(name)
    return found


# ── People registry normalization ──────────────────────────────────────

def fold_into_existing_person(conn, name: str) -> str:
    """Prefer an existing canonical name over minting a near-duplicate.

    `db.canonical_name` handles exact aliases. This covers the one further
    case that is safe to automate: a name that is exactly an existing
    canonical plus a trailing surname, in either direction - "Faraz" resolved
    in one meeting and "Faraz Matin" in another are the same identity caught
    with a shorter or fuller name, and every un-merged variant is a separate
    node in the graph. The pair is folded into whichever already has more
    meeting history, since that is the spelling every earlier meeting already
    uses.

    Spelling variants of the same given name ("Yuliya" / "Yulia") are
    deliberately NOT handled here. Measured against this project's own people
    registry, a similarity threshold loose enough to catch that pair (ratio
    0.91) is also loose enough to conflate "Tarun" and "Varun" (0.80) or
    "Catherine" and "Katherine" (0.89) - different people. That is exactly
    the failure this function exists to prevent, so cross-spelling duplicates
    stay a deliberate, preview-bound `people_merge.merge` call instead of an
    automatic one.

    Also declines to guess when a bare first name prefixes *several* unrelated
    full names already in the registry (three different "Paul"s, say) - which
    one is meant cannot be told from the name alone.
    """
    tokens = name.lower().split()
    if not tokens:
        return name

    matches: list[tuple[int, str]] = []
    for person in db.list_people(conn):
        canonical = str(person["canonical"])
        other = canonical.lower().split()
        shorter, longer = (tokens, other) if len(tokens) <= len(other) else (other, tokens)
        if shorter and longer[: len(shorter)] == shorter:
            matches.append((int(person["meetings"] or 0), canonical))

    if not matches:
        return name

    families = {tuple(c.lower().split()) for _, c in matches}
    for a in families:
        for b in families:
            if a == b:
                continue
            shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
            if longer[: len(shorter)] != shorter:
                return name  # ambiguous - more than one distinct identity matches

    return max(matches, key=lambda m: m[0])[1]


# ── LLM resolution ────────────────────────────────────────────────────

def dialogue_excerpt(transcript: Transcript, max_lines: int = 100) -> str:
    """Representative labeled dialogue across the meeting.

    The opening slice covers at least INTRO_WINDOW_SEC of wall-clock time, not
    just a fixed segment count: self-introductions happen in the first few
    minutes regardless of how choppy the turn-taking is, and a fast back-and-
    forth exchange can burn through 50 segments in under a minute - cutting
    the introductions off before they happen and starving the LLM pass of the
    one thing it most needs to see.
    """
    segs = transcript.segments
    if len(segs) <= max_lines:
        chosen = segs
    else:
        intro_count = sum(1 for s in segs if s.start <= INTRO_WINDOW_SEC)
        opening = segs[: max(intro_count, 50)]
        mid = len(segs) // 2
        chosen = opening + segs[max(len(opening), mid - 15) : mid + 15] + segs[-20:]
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
These are names known to be involved with this meeting somehow - which label is which is NOT known.
Treat them as leads to look for in the dialogue (someone introducing themselves,
being addressed, or being talked about), never as a default label-to-name mapping.

## Names seen in previous meetings
{known}
If one of these is who you are identifying, spell it exactly as shown here -
inconsistent spellings across meetings fragment the knowledge graph into
duplicate people.

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


# ── Persistence guard ───────────────────────────────────────────────────

def _existing_speakers(conn, meeting_id: str) -> dict[str, tuple[str | None, str]]:
    """Label -> (name, confidence) as currently stored, unresolved included.

    `db.get_speakers` deliberately omits unresolved labels (it answers "who
    said what", not "what do we know"), so this reads the speakers table
    directly - the same pattern dashboard.py and voices.py already use for
    queries db.py has no dedicated accessor for.
    """
    rows = conn.execute(
        "SELECT label, name, confidence FROM speakers WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchall()
    return {r["label"]: (r["name"], r["confidence"]) for r in rows}


def merge_label(
    conn,
    meeting_id: str,
    label: str,
    name: str | None,
    confidence: str,
) -> tuple[str | None, str]:
    """Persist one label's name through the merge rules. Returns what was stored.

    The single-label read-decide-write wrapper around `_merge_with_existing`,
    for callers holding one label rather than a whole meeting - `voices.apply_auto`
    above all. It exists so no caller outside this module has to reach for
    `db.set_speaker`, which is an unconditional upsert and will happily overwrite
    a name a human confirmed by ear.

    `resolve()` keeps calling `_merge_with_existing` directly against its one
    bulk `_existing_speakers` read: it loops over every label in the meeting, and
    a per-label read there would be one query per speaker for no benefit.
    """
    row = conn.execute(
        "SELECT name, confidence FROM speakers WHERE meeting_id = ? AND label = ?",
        (meeting_id, label),
    ).fetchone()
    existing = (row["name"], row["confidence"] or CONFIDENCE_UNKNOWN) if row else None
    merged = _merge_with_existing(existing, name, confidence)
    # An identical rewrite changes nothing but makes a refusal look like a write.
    # The `existing is not None` half is load-bearing: a brand-new label with no
    # name still needs its NULL row, or it never reaches the dashboard's
    # unresolved queue, which selects on `s.name IS NULL`.
    if existing is not None and merged == existing:
        return merged
    db.set_speaker(conn, meeting_id, label, merged[0], merged[1])
    return merged


def _merge_with_existing(
    existing: tuple[str | None, str] | None,
    new_name: str | None,
    new_confidence: str,
) -> tuple[str | None, str]:
    """Decide what to persist for one label, given what is already stored.

    This is the fix for the corpus-wide data loss `db.set_speaker` otherwise
    causes: it is an unconditional upsert, and `resolve()` used to call it for
    every label on every pass, including labels the LLM failed to name this
    time. Re-running `pipeline speakers --all` therefore reset every label the
    LLM did not reconfirm back to NULL/unknown - silently erasing names a
    human had already confirmed by ear, along with every prior inference.

    Two rules, in order:
      1. A CONFIRMED row is never downgraded by a weaker pass. Only another
         CONFIRMED value (a human editing speaker-overrides.yaml) may replace
         it.
      2. A null/empty name from this pass never overwrites an existing name.
         The gap this pass leaves is invisible loss; keeping the old value is
         at worst a stale name, which is what the next pass or an override is
         for.
    """
    existing_name, existing_confidence = existing or (None, CONFIDENCE_UNKNOWN)

    if existing_confidence == CONFIDENCE_CONFIRMED and new_confidence != CONFIDENCE_CONFIRMED:
        return existing_name, existing_confidence
    if not new_name and existing_name:
        return existing_name, existing_confidence
    return new_name, new_confidence


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
    overrides beat every inference. Persistence itself never runs backwards:
    see `_merge_with_existing` for why a label already resolved can only be
    strengthened, never blanked, by a later pass.
    """
    labels = transcript.speaker_labels
    if not labels:
        return {}

    resolved: dict[str, str] = {}
    confidence: dict[str, str] = {}

    # Candidates only - none of these say which label is which, only that
    # someone was probably present.
    candidates = candidates_from_filename(transcript, meeting.title_hint, owner_name)
    for name in glossary_people():
        if name not in candidates:
            candidates.append(name)
    for name in direct_address_names(transcript):
        if name not in candidates:
            candidates.append(name)

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
    for label, original in list(resolved.items()):
        canonical = db.canonical_name(conn, original) or original
        if canonical == original:
            canonical = fold_into_existing_person(conn, original)
        resolved[label] = canonical
        if canonical != original:
            # Register the surface form as an alias so the next lookup is an
            # exact hit instead of walking the registry again.
            db.add_person(conn, canonical, aliases=[original])

    # Hand the voice matcher this pass's conclusion, so its LLM veto has
    # something to disagree with. `band()` treats a missing llm_name as "no
    # disagreement", so the second guard on auto-applying a name is silently
    # absent wherever this is unset - which was every row, because the only
    # thing that ever wrote it was the retired local enrollment stage.
    #
    # This is independent evidence by construction: `resolved` comes from the
    # transcript, the overrides and the glossary, never from a voiceprint. Do
    # NOT source it from the `speakers` table instead - `voices.apply_auto`
    # writes the matcher's own answer there, so that would be the matcher
    # confirming itself.
    for label in labels:
        db.set_match_llm_name(conn, meeting.id, label, resolved.get(label))

    existing = _existing_speakers(conn, meeting.id)
    for label in labels:
        name, conf = _merge_with_existing(
            existing.get(label), resolved.get(label), confidence.get(label, CONFIDENCE_UNKNOWN)
        )
        db.set_speaker(conn, meeting.id, label, name, conf)
        # Report what actually ended up persisted, not just what this pass
        # inferred, so callers logging `resolved` see the true final state.
        if name:
            resolved[label] = name
        elif label in resolved:
            del resolved[label]

    # Register anyone new so the next meeting normalizes against them.
    for name in resolved.values():
        db.add_person(conn, name)

    return resolved
