"""Answer questions by splitting retrieval from synthesis.

The other half of the subscription-ceiling mitigation. LightRAG's own /query path
runs keyword extraction through its configured LLM before it touches the graph at
all - on this deployment that is qwen3:4b on CPU at ~3.6 tok/s, measured returning
HTTP 500 after 242 SECONDS. That path is not "slow", it is unusable, and no amount
of caching or retrying fixes it: the bottleneck is architectural, not transient.

So retrieval here never calls it. Two sources instead, both LLM-free and both run
every time, combined when both return something:

    graph_sync.retrieve_context() -> label match + graph traversal   (~5s)
    fallback_local_context()      -> keyword scan of minutes/ on disk (~0.03s)

The graph gives the shape of the record - who owns what, what depends on what;
the minutes give the verbatim detail neither the graph nor a chunk preserves on
its own. Each is labelled distinctly in the prompt so the model can tell them
apart rather than blending citations across two different kinds of evidence.

Synthesis puts the actual reading, reasoning, and writing on the subscription
chain (`pipeline.llm`), timed separately from retrieval - a knowledge base you
wait three minutes for is one you stop using, and separate timings say whether a
slow answer is retrieval's fault or the model's.

`index.query()` (LightRAG's own generation) is kept only behind the explicit
`synthesize=False` / `pipeline query --local` flag, and even there it is bounded
to a few seconds rather than trusted - see `_bounded_raw_query`. It is not used
anywhere in the default path; a subscription model producing no answer is
strictly better than a 242-second wait for a 500.

Conversation history is accepted as a plain list of (question, answer) tuples
rather than anything storage-shaped - this module has no idea a session exists.
The dashboard owns persistence; it reads history before calling `ask` and writes
the result after. That keeps `pipeline query` on the CLI working exactly as it
always has, with no session at all.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass

from pipeline import graph_sync, index, llm
from pipeline.config import CHAT_HISTORY_TURNS
from pipeline.llm import LLMError, complete


@dataclass
class Answer:
    text: str
    retrieval_sec: float
    synthesis_sec: float
    provider: str | None
    context_chars: int
    synthesized: bool

    @property
    def total_sec(self) -> float:
        return self.retrieval_sec + self.synthesis_sec

    def timing_line(self) -> str:
        mode = self.provider if self.synthesized else "lightrag (local)"
        return (
            f"[retrieval {self.retrieval_sec:.1f}s | synthesis "
            f"{self.synthesis_sec:.1f}s via {mode} | context {self.context_chars} chars]"
        )


# Cap on one stored answer once it is quoted back into a prompt as history.
# Applied per-turn, before the token-budget walk in `_fit_history_to_budget`, so
# one long-winded reply early in a session cannot alone eat the whole budget.
HISTORY_ANSWER_CHAR_CAP = 1200

# Rough token budget for the history block, in the same "~4 chars/token" estimate
# used elsewhere in this codebase for prompt sizing (see MINUTES_PROMPT_TOKEN_BUDGET) -
# good enough for a budget whose job is avoiding runaway prompts, not precision.
HISTORY_TOKEN_BUDGET = 4000
_CHARS_PER_TOKEN_ESTIMATE = 4


def _clamp_history_answer(text: str) -> str:
    if len(text) <= HISTORY_ANSWER_CHAR_CAP:
        return text
    return text[:HISTORY_ANSWER_CHAR_CAP] + "... [truncated]"


def _fit_history_to_budget(
    history: list[tuple[str, str]], token_budget: int = HISTORY_TOKEN_BUDGET
) -> list[tuple[str, str]]:
    """Trim history to a character budget, keeping the most recent turns.

    Walks newest -> oldest so a long session loses its earliest turns first,
    then reverses back to chronological order for the prompt. The most recent
    turn is always kept even if it alone exceeds the budget - context from one
    turn ago is worth more than none. Retrieved context is the priority in this
    prompt, not history, so history is what gives when something has to.
    """
    char_budget = token_budget * _CHARS_PER_TOKEN_ESTIMATE
    kept: list[tuple[str, str]] = []
    used = 0
    for question, raw_answer in reversed(history):
        answer = _clamp_history_answer(raw_answer)
        cost = len(question) + len(answer)
        if kept and used + cost > char_budget:
            break
        kept.append((question, answer))
        used += cost
    return list(reversed(kept))


def _retrieval_query(question: str, history: list[tuple[str, str]]) -> str:
    """Fold the last couple of turns' questions into the retrieval query.

    A bare follow-up like "why?" carries no keywords of its own, so on its own
    it retrieves nothing; its keywords are whatever the prior turns were about.
    Only prior QUESTIONS are folded in, not answers - answers are prose and
    would dilute the keyword signal retrieval depends on.
    """
    if not history:
        return question
    return " ".join([q for q, _ in history[-2:]] + [question])


def build_synthesis_prompt(
    question: str, context: str, history: list[tuple[str, str]] | None = None
) -> str:
    history_block = ""
    if history:
        exchanges = "\n\n".join(
            f"Q: {q}\nA: {_clamp_history_answer(a)}" for q, a in history
        )
        history_block = f"""## Earlier in this conversation

{exchanges}

"""

    return f"""Answer the question using ONLY the retrieved meeting records below.

These are compiled minutes from the user's own meetings. Treat them as the record
of what happened.

## Rules

- Answer from the records. If they do not contain the answer, say so plainly
  rather than filling the gap from general knowledge.
- Cite the meetings you used - dates and titles appear in the records.
- When records conflict, say so and prefer the more recent one, noting that the
  position changed. A reversed decision is usually the most useful thing to
  surface.
- A section marked "parsed, authoritative" is a register row extracted from the
  minutes at compile time, with its owner, rationale and timestamp intact. Prefer
  it over the prose excerpts below it, which contain the same facts less
  precisely. Carry its owner and due date through into the answer verbatim.
- Preserve rationale. "We chose X" is much less useful than "we chose X because Y".
- Be direct. No preamble.
- The conversation below is context for interpreting the question - pronouns,
  "that meeting", a bare "why?" - not evidence. Every fact in the answer must
  still come from the retrieved records.

{history_block}## Retrieved records

{context}

## Question

{question}"""


# `index.query()` is LightRAG's own retrieve-and-generate endpoint. It is
# confirmed broken on this deployment (HTTP 500 after 242s - see the module
# docstring) and must never sit on a request's critical path unbounded. This is
# its only remaining caller (the explicit `synthesize=False` / `--local` path);
# even there, it gets a few seconds and no more.
RAW_QUERY_CAP_SEC = 5.0


def _bounded_raw_query(question: str, mode: str | None, top_k: int | None) -> str | None:
    """Try LightRAG's own generation, but never wait more than a few seconds.

    Returns None on timeout or failure - the caller decides what to say. A
    thread rather than a client-side timeout on the request itself: `index.py`
    is not this module's to change, and Python cannot interrupt a blocking call
    once it is in flight regardless. The abandoned call is left running in a
    daemon thread and its eventual result (or 500) is simply discarded.
    """
    result: dict[str, str] = {}

    def run() -> None:
        with contextlib.suppress(index.IndexError_):
            result["text"] = index.query(question, mode=mode, top_k=top_k)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=RAW_QUERY_CAP_SEC)
    return None if thread.is_alive() else result.get("text")


def _retrieve_context(question: str) -> str:
    """Retrieval, in priority order, LLM-free throughout.

    1. graph_sync.retrieve_context() - label match + graph traversal (~5s)
    2. fallback_local_context()      - keyword scan of minutes/ on disk (~0.03s)

    Both run every time and both are kept when both return something - see the
    module docstring for why one is not a substitute for the other. Sections
    are labelled distinctly so the synthesis prompt can tell them apart.
    """
    sections: list[str] = []

    # The registers go first, deliberately. They are parsed rows carrying owner,
    # rationale, due date and a timestamp; everything below them is prose the
    # model has to interpret. Order is the cheapest instruction available.
    register_context = structured_context(question)
    if register_context.strip():
        sections.append(register_context)

    graph_context = graph_sync.retrieve_context(question)
    if graph_context.strip():
        sections.append(graph_context)

    minutes_context = fallback_local_context(question)
    if minutes_context.strip():
        sections.append(f"## Minutes excerpts\n\n{minutes_context}")

    return "\n\n---\n\n".join(sections)


# Words that carry no retrieval signal. Shared by the register scorer and the
# minutes scan so the two cannot drift apart and disagree about what a question
# is even asking.
_STOPWORDS = frozenset({
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
    "about", "did", "does", "was", "were", "the", "and", "for", "with", "from",
    "that", "this", "there", "their", "have", "has", "had", "any", "all",
    "recent", "meetings", "meeting", "discuss", "discussed", "decide", "decided",
    "decision", "decisions", "tell", "show", "summary", "our", "out", "are",
})


def question_keywords(question: str) -> list[str]:
    """Content words from a question, lowercased."""
    import re

    return [
        w.lower()
        for w in re.findall(r"\b[a-zA-Z0-9_-]{3,}\b", question)
        if w.lower() not in _STOPWORDS
    ]


def _match(text: str, keywords: list[str]) -> tuple[int, int]:
    """(distinct keywords present, total occurrences).

    Distinct count is what decides a match. Ranking on total occurrences alone
    meant one common word carried a row: "access" appears in most of a security
    team's decisions, so every question returned ~7k characters and all three
    registers. Distinct-keyword matching is what makes this retrieval rather
    than a dump of the table.
    """
    lowered = (text or "").lower()
    present = [k for k in keywords if k in lowered]
    return len(present), sum(lowered.count(k) for k in present)


def _min_distinct(keywords: list[str]) -> int:
    """How many distinct keywords a row must contain to count as a match.

    A single-keyword question ("NCID?") has nothing else to corroborate with, so
    one hit has to do. Anything richer must agree on at least two, which is what
    separates "CRUD access" from every row that merely says "access".
    """
    return 1 if len(keywords) < 2 else 2


def _cite(row: dict) -> str:
    """Where a register row came from, in a form the model can quote back."""
    from pipeline.titles import clean_meeting_title

    where = clean_meeting_title(
        row.get("source_name"), row.get("title_hint"), row.get("minutes_path")
    )
    stamp = (row.get("timestamp_cite") or "").strip()
    return f"{row.get('meeting_date') or 'undated'} - {where}" + (f" {stamp}" if stamp else "")


def structured_context(question: str, per_register: int = 6) -> str:
    """The decision, commitment and open-question registers as retrieval.

    These three tables are parsed out of the minutes at compile time and carry
    exactly what prose retrieval has to guess at: who decided, why, who owes it,
    when it is due, and a timestamp to cite. 642 rows of it, and no part of the
    answer path read any of them - "what did we decide about X" was answered by
    keyword-scoring whole minutes files, so the rationale that minutes are kept
    long in order to preserve never reached the model at all.

    Two retrieval routes, because questions arrive in two shapes:

    * keyword overlap, for "what did we decide about CRUD access"
    * owner match, for "what does Ali owe" - which shares no keyword with the
      commitment's own text, but owner is a column

    Returns "" when nothing matches. An empty section under a confident heading
    is worse than no section at all: it reads as "the register is empty" and
    invites the model to fill the silence.
    """
    from pipeline import db

    keywords = question_keywords(question)
    if not keywords:
        return ""
    floor = _min_distinct(keywords)

    db.init_db()
    with db.connect() as conn:
        decisions = db.list_decisions(conn)
        commitments = db.list_commitments(conn)
        questions = db.list_open_questions(conn)

    # A question naming a known owner is about that person whatever else it says.
    # Match on whole name parts so "ali" cannot fire on "Alignment".
    owners = {str(row["owner"]).strip() for row in commitments + questions if row.get("owner")}
    named = {
        owner
        for owner in owners
        if any(part.lower() in keywords for part in owner.split() if len(part) > 2)
    }

    def ranked(rows: list[dict], field: str) -> list[dict]:
        scored = []
        for row in rows:
            distinct, total = _match(row.get(field) or "", keywords)
            owned = bool(row.get("owner")) and str(row["owner"]).strip() in named
            if distinct >= floor or owned:
                # An owner match is a direct hit; sort it with the strongest.
                scored.append((len(keywords) if owned else distinct, total, row))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [row for _, _, row in scored[:per_register]]

    sections: list[str] = []

    # Rationale is scored too: "why did we deprioritise X" often matches only there.
    decision_rows = ranked(
        [dict(d, _scored=f"{d['text']} {d.get('rationale') or ''}") for d in decisions], "_scored"
    )
    if decision_rows:
        lines = []
        for d in decision_rows:
            line = f"- {d['text']}"
            if d.get("decided_by"):
                line += f"\n  Decided by: {d['decided_by']}"
            if d.get("rationale"):
                line += f"\n  Rationale: {d['rationale']}"
            line += f"\n  Source: {_cite(d)}"
            lines.append(line)
        sections.append("## Decision register (parsed, authoritative)\n\n" + "\n".join(lines))

    commitment_rows = ranked(commitments, "text")
    if commitment_rows:
        lines = [
            f"- {c['text']}\n  Owner: {c.get('owner') or 'unassigned'}"
            f" - Due: {c.get('due_date') or 'unspecified'}"
            f" - Status: {c.get('state') or 'open'}\n  Source: {_cite(c)}"
            for c in commitment_rows
        ]
        sections.append("## Commitment register (parsed, authoritative)\n\n" + "\n".join(lines))

    question_rows = ranked(questions, "text")
    if question_rows:
        lines = [
            f"- {q['text']}\n  Waiting on: {q.get('owner') or 'unassigned'}\n  Source: {_cite(q)}"
            for q in question_rows
        ]
        sections.append("## Open questions (parsed, unresolved)\n\n" + "\n".join(lines))

    return "\n\n".join(sections)


def fallback_local_context(question: str, top_n: int = 5) -> str:
    """Keyword scan of the minutes files directly on disk - no LLM, no LightRAG."""
    import re

    from pipeline.config import MINUTES_DIR
    keywords = [
        w.lower()
        for w in re.findall(r"\b[a-zA-Z0-9_-]{3,}\b", question)
        if w.lower() not in {
            "what", "when", "where", "which", "about", "did", "were", "the", "and", "for", "with",
            "recent", "meetings", "meeting", "discuss", "discussed", "tell", "show", "summary"
        }
    ]

    scored_files: list[tuple[int, str, str]] = []
    for md_file in MINUTES_DIR.glob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            lower_content = content.lower()
            score = sum(lower_content.count(k) for k in keywords) if keywords else 1
            if score > 0:
                scored_files.append((score, md_file.name, content))
        except OSError:
            continue

    scored_files.sort(key=lambda x: x[0], reverse=True)
    if not scored_files:
        newest = sorted(MINUTES_DIR.glob("*.md"), reverse=True)[:3]
        return "\n\n---\n\n".join(f"## {p.name}\n{p.read_text(encoding='utf-8')[:3000]}" for p in newest)

    return "\n\n---\n\n".join(f"## {name}\n{text[:3500]}" for _, name, text in scored_files[:top_n])


def ask(
    question: str,
    mode: str | None = None,
    top_k: int | None = None,
    synthesize: bool = True,
    history: list[tuple[str, str]] | None = None,
) -> Answer:
    """Retrieve, then synthesize.

    `synthesize=False` (`pipeline query --local`) asks LightRAG's own /query
    endpoint instead of the subscription chain - see `_bounded_raw_query` for
    why that is bounded to a few seconds rather than trusted outright. It is
    not the fallback when a provider is unreachable; see the `except LLMError`
    branch below for that case, which never calls it.

    `history` is prior (question, answer) turns of the same conversation, oldest
    first, from a caller that tracks sessions (the dashboard does; the CLI does
    not and passes None). It is trimmed to `CHAT_HISTORY_TURNS` and then to
    `HISTORY_TOKEN_BUDGET` before it touches retrieval or the prompt - see
    `_fit_history_to_budget`.
    """
    history = _fit_history_to_budget(list(history or [])[-CHAT_HISTORY_TURNS:])
    retrieval_query = _retrieval_query(question, history)

    if not synthesize:
        started = time.monotonic()
        text = _bounded_raw_query(retrieval_query, mode, top_k)
        if text is None:
            text = (
                "LightRAG's own generation is not usable on this deployment - it "
                "runs keyword extraction through a local model that returns HTTP "
                "500 after several minutes, so this gave up early instead of "
                "waiting on it. Drop `--local` to use the subscription chain."
            )
        return Answer(
            text=text,
            retrieval_sec=time.monotonic() - started,
            synthesis_sec=0.0,
            provider=None,
            context_chars=0,
            synthesized=False,
        )

    started = time.monotonic()
    context = _retrieve_context(retrieval_query)
    retrieval_sec = time.monotonic() - started

    if not context.strip():
        return Answer(
            text=(
                "No relevant records were retrieved. The corpus may be empty, or "
                "the knowledge graph and minutes archive may be unreachable - "
                "check `pipeline doctor`."
            ),
            retrieval_sec=retrieval_sec,
            synthesis_sec=0.0,
            provider=None,
            context_chars=0,
            synthesized=False,
        )

    started = time.monotonic()
    try:
        text = complete(build_synthesis_prompt(question, context, history))
        synthesis_sec = time.monotonic() - started
        return Answer(
            text=text,
            retrieval_sec=retrieval_sec,
            synthesis_sec=synthesis_sec,
            provider=llm.last_provider,
            context_chars=len(context),
            synthesized=True,
        )
    except LLMError as exc:
        # No LightRAG-generation fallback here on purpose - see the module
        # docstring. The retrieved context is still real and useful, so hand
        # it back directly rather than spend a guaranteed-doomed 242s finding
        # that out again.
        print(f"    synthesis unavailable ({exc}); returning retrieved context unsynthesized")
        return Answer(
            text=(
                f"Synthesis is unavailable right now ({exc}). Here is the "
                f"retrieved context directly - re-ask once a provider is "
                f"available for a written answer:\n\n{context}"
            ),
            retrieval_sec=retrieval_sec,
            synthesis_sec=0.0,
            provider=None,
            context_chars=len(context),
            synthesized=False,
        )
