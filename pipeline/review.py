"""Human confirmation of what the compiler produced.

Everything upstream of this is inference. Diarization guesses how many people
spoke, a model guesses which label is whose name, and another model guesses which
of them owns each action item. This is where a person says yes or no, and it
exists because two of those guesses are unusually expensive to leave wrong:

**A wrong name fragments the graph.** Every spelling variant becomes its own node,
so one person recorded three ways is three disconnected entities that no query
finds together. The damage is not confined to the meeting that contains the
mistake - it degrades retrieval across the whole corpus, and it compounds with
every meeting that repeats the variant.

**An unowned action item is silently useless.** Diarization is skipped with only a
warning when `HF_TOKEN` is missing or its gated models were never accepted, and
the result is minutes whose action items have no owners at all. Nothing else in
the pipeline reports that; a queue of meetings with unresolved labels does.

## Correcting a name recompiles rather than patches

A speaker correction cannot be a find-and-replace over the minutes: the compiler
attributed decisions, wrote rationale and phrased action items around whoever it
believed was speaking, and patching the string leaves the reasoning wrong. So a
correction rewinds the meeting to `speakers_resolved` and the next `pipeline
minutes` run rebuilds it from the retained transcript.

That costs no ASR time, which is the entire reason transcripts are retained. It
is also why corrections are worth making early: the rebuild is cheap, but only
until the wrong name has propagated into months of graph.

## Editing minutes re-indexes, and refuses to half-succeed

Editing the document changes its content hash, which changes LightRAG's document
id. Inserting the new version without deleting the old one leaves both in the
graph, and retrieval starts returning two contradictory versions of the same
meeting. `index.replace_minutes` reports whether the delete succeeded; when it did
not, this module raises rather than advancing, because a caller cannot undo the
duplicate after the fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pipeline import db, entities, index
from pipeline.compile_minutes import extract_title
from pipeline.config import now_iso

# Meetings are reviewable once minutes exist. Earlier statuses have nothing to
# show; `failed` needs `pipeline retry`, not a human reading prose.
REVIEWABLE = (db.MINUTES_COMPILED, db.INDEXED)

# A diarization label that survived into the finished document - the compiler was
# told to leave them visible rather than invent a name, so their presence in the
# text means an attribution is genuinely unknown.
UNRESOLVED_IN_TEXT = re.compile(r"\bSPEAKER_\d+\b")


class ReviewError(RuntimeError):
    """A review action could not be completed safely."""


@dataclass
class ReviewItem:
    meeting: db.Meeting
    title: str
    speakers: list[dict[str, object]]
    unresolved_labels: list[str]
    unresolved_in_minutes: bool
    reviewed: bool

    @property
    def needs_attention(self) -> bool:
        return bool(self.unresolved_labels) or self.unresolved_in_minutes


def _read_minutes(meeting: db.Meeting) -> str | None:
    if not meeting.minutes_path:
        return None
    path = Path(meeting.minutes_path)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def item_for(conn, meeting: db.Meeting) -> ReviewItem:
    document = _read_minutes(meeting)
    rows = db.speaker_rows(conn, meeting.id)
    return ReviewItem(
        meeting=meeting,
        title=(document and extract_title(document)) or meeting.title_hint or meeting.source_name,
        speakers=rows,
        unresolved_labels=[str(r["label"]) for r in rows if not r["name"]],
        unresolved_in_minutes=bool(document and UNRESOLVED_IN_TEXT.search(document)),
        reviewed=bool(meeting.reviewed_at),
    )


def queue(conn, include_reviewed: bool = False) -> list[ReviewItem]:
    """Meetings awaiting confirmation, most recent first.

    Ones still carrying an unresolved label sort to the front: they are the only
    entries where doing nothing loses information rather than merely leaving a
    guess unconfirmed.
    """
    items = [item_for(conn, m) for m in db.list_meetings(conn, statuses=list(REVIEWABLE))]
    if not include_reviewed:
        items = [i for i in items if not i.reviewed]
    return sorted(items, key=lambda i: not i.needs_attention)


def save_speakers(conn, meeting: db.Meeting, names: dict[str, str]) -> bool:
    """Confirm or correct speaker names.

    Returns True when the meeting was rewound for recompilation. Names are
    normalized through the people registry before being stored and registered
    after, so the next meeting resolves against them rather than inventing a
    fourth spelling.
    """
    known = {str(row["label"]) for row in db.speaker_rows(conn, meeting.id)}
    unknown = set(names) - known
    if unknown:
        raise ReviewError(f"Not diarized labels for this meeting: {sorted(unknown)}")

    existing = db.get_speakers(conn, meeting.id)
    changed = False
    for label, raw in names.items():
        name = (raw or "").strip()
        canonical = (db.canonical_name(conn, name) or name) if name else None
        if canonical != existing.get(label):
            changed = True
        db.set_speaker(conn, meeting.id, label, canonical, "confirmed")
        if canonical:
            db.add_person(conn, canonical)

    if not changed:
        # Confirming what was already there is a real outcome - it is how a
        # meeting leaves the queue - but it must not trigger a pointless rebuild.
        return False

    # The compiler wrote decisions and rationale around the old names, so the
    # document has to be rebuilt rather than patched. Free of ASR cost because
    # the transcript was retained.
    db.advance(conn, meeting.id, db.SPEAKERS_RESOLVED)
    db.mark_reviewed(conn, meeting.id, None)
    return True


def save_minutes(conn, meeting: db.Meeting, markdown: str) -> Path:
    """Write an edited minutes document and re-derive its graph block.

    The entity and relation sections are re-parsed from the edited text, because
    an edit that renames a feature or reassigns an owner has to reach the graph;
    leaving the manifest's copy stale would re-index the old entities under the
    new document.
    """
    if not meeting.minutes_path:
        raise ReviewError("This meeting has no minutes to edit.")
    if not markdown.strip():
        raise ReviewError("Refusing to save empty minutes.")

    path = Path(meeting.minutes_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")

    found, relations = entities.extract(markdown)
    found, relations = entities.canonicalize(conn, found, relations)
    db.replace_entities(conn, meeting.id, found, relations)

    # Back to minutes_compiled: the indexed copy is now the stale one, and this
    # is the status the index stage claims, so a plain `pipeline run` picks it up
    # even if nobody presses approve.
    db.advance(conn, meeting.id, db.MINUTES_COMPILED, minutes_path=str(path))
    return path


def approve(conn, meeting: db.Meeting) -> str:
    """Re-index the meeting's minutes and mark it confirmed.

    Raises rather than advancing if the previously indexed copy could not be
    removed - see the module docstring.
    """
    document_path = Path(meeting.minutes_path) if meeting.minutes_path else None
    if document_path is None or not document_path.is_file():
        raise ReviewError("This meeting has no minutes file to index.")

    augment = entities.render_for_index(
        db.get_entities(conn, meeting.id),
        db.get_relations(conn, meeting.id),
    )
    try:
        doc_id, replaced = index.replace_minutes(
            document_path, meeting.lightrag_doc_id, augment=augment
        )
    except index.IndexError_ as exc:
        raise ReviewError(str(exc)) from exc

    if not replaced:
        raise ReviewError(
            f"The previously indexed copy ({meeting.lightrag_doc_id}) could not be "
            "deleted, so indexing this version would leave two contradictory copies "
            "of the meeting in the graph. Remove it in the LightRAG UI, then approve again."
        )

    db.advance(conn, meeting.id, db.INDEXED, lightrag_doc_id=doc_id)
    db.mark_reviewed(conn, meeting.id, now_iso())
    return doc_id
