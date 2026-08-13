"""Read-only HTTP surface over the compiled corpus.

The pipeline is operated through the CLI; this exists so the corpus can be
*used* without one. It calls the same modules in process - `answer.ask` for
retrieval and synthesis, `db` for the manifest - so there is no second
implementation of query semantics to keep in sync.

Two things shape the design:

**Citations come from retrieval, not from prose.** The synthesis prompt asks the
model to cite meetings, but a model summarizing its own reading can name a
meeting it did not read. `answer.Answer.sources` carries the filenames LightRAG
actually returned; this layer resolves those to manifest rows, so every citation
the reader sees is one the retriever produced and can be opened.

**Asking is slow and synchronous.** Retrieval plus synthesis is tens of seconds
on CPU. The ask route is a plain `def`, which Starlette runs in a worker thread -
an `async def` would block the event loop for the whole query and stall every
other request behind it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline import answer, db, index, review
from pipeline.compile_minutes import extract_title
from pipeline.config import MINUTES_DIR

STATIC_DIR = Path(__file__).resolve().parent / "static"

QUERY_MODES = ("hybrid", "global", "local", "naive", "mix")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    mode: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)
    # Use LightRAG's own local generation instead of the subscription chain.
    # Exposed for the same reason the CLI exposes it: comparing the two is how
    # you tell a retrieval problem from a synthesis problem.
    local: bool = False


class SpeakersRequest(BaseModel):
    # label -> name. An empty string clears a name back to unresolved, which is
    # the honest state when nobody can tell who spoke.
    names: dict[str, str]


class MinutesRequest(BaseModel):
    markdown: str = Field(min_length=1)


class Citation(BaseModel):
    source: str
    meeting_id: str | None = None
    date: str | None = None
    title: str | None = None
    status: str | None = None


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation]
    synthesized: bool
    provider: str | None
    retrieval_sec: float
    synthesis_sec: float
    context_chars: int


def _minutes_file(meeting: db.Meeting) -> Path:
    """The meeting's minutes, refused if they sit outside the minutes directory.

    The path comes from our own manifest, so this is a containment check rather
    than input validation: a corrupted or hand-edited row must not turn a read
    endpoint into arbitrary file disclosure.
    """
    if not meeting.minutes_path:
        raise HTTPException(status_code=404, detail="This meeting has no compiled minutes yet.")
    path = Path(meeting.minutes_path).resolve()
    if not path.is_relative_to(MINUTES_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Minutes path is outside the minutes directory.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Minutes file is missing from disk.")
    return path


def _resolve_citations(sources: list[str]) -> list[Citation]:
    """Attach manifest metadata to each retrieved filename.

    A source with no matching row still becomes a citation, just an unlinked
    one - the retrieval genuinely happened, and hiding it would misrepresent
    what the answer was grounded in.
    """
    if not sources:
        return []
    with db.connect() as conn:
        found = db.meetings_by_minutes_names(conn, sources)

    citations = []
    for name in sources:
        meeting = found.get(name)
        if meeting is None:
            citations.append(Citation(source=name))
            continue
        citations.append(
            Citation(
                source=name,
                meeting_id=meeting.id,
                date=meeting.meeting_date,
                title=_title_for(meeting),
                status=meeting.status,
            )
        )
    return citations


def _title_for(meeting: db.Meeting) -> str:
    """The compiler's title if the minutes are readable, else the filename guess."""
    if meeting.minutes_path:
        path = Path(meeting.minutes_path)
        if path.is_file():
            title = extract_title(path.read_text(encoding="utf-8"))
            if title:
                return title
    return meeting.title_hint or meeting.source_name


def create_app() -> FastAPI:
    app = FastAPI(title="Meeting Memory", docs_url=None, redoc_url=None)

    @app.get("/api/health")
    def health() -> dict:
        try:
            lightrag = {"reachable": True, "detail": index.health()}
        except index.IndexError_ as exc:
            lightrag = {"reachable": False, "detail": str(exc)}
        try:
            with db.connect() as conn:
                counts = db.status_counts(conn)
        except sqlite3.Error as exc:
            # Reported, not raised: a broken manifest is exactly what the health
            # panel exists to show, and a 500 here would render it blank instead.
            return {"lightrag": lightrag, "manifest": {"error": str(exc)}}
        return {"lightrag": lightrag, "manifest": counts}

    @app.post("/api/ask", response_model=AskResponse)
    def ask(request: AskRequest) -> AskResponse:
        if request.mode is not None and request.mode not in QUERY_MODES:
            raise HTTPException(
                status_code=422, detail=f"mode must be one of {', '.join(QUERY_MODES)}"
            )
        try:
            result = answer.ask(
                request.question,
                mode=request.mode,
                top_k=request.top_k,
                synthesize=not request.local,
            )
        except index.IndexError_ as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return AskResponse(
            answer=result.text,
            citations=_resolve_citations(result.sources),
            synthesized=result.synthesized,
            provider=result.provider,
            retrieval_sec=round(result.retrieval_sec, 2),
            synthesis_sec=round(result.synthesis_sec, 2),
            context_chars=result.context_chars,
        )

    @app.get("/api/meetings/{meeting_id}/minutes")
    def minutes(meeting_id: str) -> dict:
        with db.connect() as conn:
            meeting = db.get_meeting(conn, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="No such meeting.")
        path = _minutes_file(meeting)
        return {
            "meeting_id": meeting.id,
            "date": meeting.meeting_date,
            "time": meeting.meeting_time,
            "title": _title_for(meeting),
            "status": meeting.status,
            "duration_sec": meeting.duration_sec,
            "markdown": path.read_text(encoding="utf-8"),
        }

    # ── review queue ──────────────────────────────────────────────────

    def _load(conn, meeting_id: str) -> db.Meeting:
        meeting = db.get_meeting(conn, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="No such meeting.")
        return meeting

    @app.get("/api/review")
    def review_queue(include_reviewed: bool = False) -> dict:
        with db.connect() as conn:
            items = review.queue(conn, include_reviewed=include_reviewed)
        return {
            "items": [
                {
                    "meeting_id": item.meeting.id,
                    "date": item.meeting.meeting_date,
                    "time": item.meeting.meeting_time,
                    "title": item.title,
                    "status": item.meeting.status,
                    "speakers": item.speakers,
                    "unresolved_labels": item.unresolved_labels,
                    "unresolved_in_minutes": item.unresolved_in_minutes,
                    "needs_attention": item.needs_attention,
                    "reviewed": item.reviewed,
                }
                for item in items
            ]
        }

    @app.post("/api/meetings/{meeting_id}/speakers")
    def save_speakers(meeting_id: str, request: SpeakersRequest) -> dict:
        with db.connect() as conn:
            meeting = _load(conn, meeting_id)
            try:
                recompiling = review.save_speakers(conn, meeting, request.names)
            except review.ReviewError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "recompiling": recompiling,
            "detail": (
                "Names changed - the meeting was rewound so `pipeline minutes` "
                "rebuilds it from the retained transcript, at no ASR cost."
                if recompiling
                else "Speakers confirmed as they were."
            ),
        }

    @app.put("/api/meetings/{meeting_id}/minutes")
    def save_minutes(meeting_id: str, request: MinutesRequest) -> dict:
        with db.connect() as conn:
            meeting = _load(conn, meeting_id)
            _minutes_file(meeting)  # containment check before writing
            try:
                path = review.save_minutes(conn, meeting, request.markdown)
            except review.ReviewError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"saved": path.name, "status": db.MINUTES_COMPILED}

    @app.post("/api/meetings/{meeting_id}/approve")
    def approve(meeting_id: str) -> dict:
        with db.connect() as conn:
            meeting = _load(conn, meeting_id)
            try:
                doc_id = review.approve(conn, meeting)
            except review.ReviewError as exc:
                # 409, not 500: the corpus is intact and the operator has a
                # concrete next step, which a server-error page would hide.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"indexed": True, "lightrag_doc_id": doc_id}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
