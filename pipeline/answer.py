"""Answer questions by splitting retrieval from synthesis.

The other half of the subscription-ceiling mitigation. LightRAG normally retrieves
*and* generates in one call, which means the answer is written by whatever model
serves its HTTP endpoint - a small local model on CPU. That caps answer quality and
makes latency depend on local generation speed.

Splitting the two puts each job on the right model:

    retrieval  -> LightRAG + local model   (graph traversal, embeddings)
    synthesis  -> subscription chain       (reading, reasoning, writing)

A knowledge base you wait three minutes for is one you stop using, so both phases
are timed separately. That also makes latency at scale measurable rather than
theoretical: when queries get slow, the timing says whether the graph traversal or
the generation is responsible.

Falls back to LightRAG's own generation when synthesis is unavailable, because an
answer from a small model beats no answer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pipeline import index
from pipeline.llm import LLMError, complete, last_provider


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


def build_synthesis_prompt(question: str, context: str) -> str:
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
- Preserve rationale. "We chose X" is much less useful than "we chose X because Y".
- Be direct. No preamble.

## Retrieved records

{context}

## Question

{question}"""


def ask(
    question: str,
    mode: str | None = None,
    top_k: int | None = None,
    synthesize: bool = True,
) -> Answer:
    """Retrieve, then synthesize.

    `synthesize=False` uses LightRAG's own generation instead - useful for
    comparing the two, and the automatic fallback when no provider is reachable.
    """
    if not synthesize:
        started = time.monotonic()
        text = index.query(question, mode=mode, top_k=top_k)
        return Answer(
            text=text,
            retrieval_sec=time.monotonic() - started,
            synthesis_sec=0.0,
            provider=None,
            context_chars=0,
            synthesized=False,
        )

    started = time.monotonic()
    context = index.query_context(question, mode=mode, top_k=top_k)
    retrieval_sec = time.monotonic() - started

    if not context.strip():
        # No context could mean an empty corpus or an unreachable server. Either
        # way, asking a model to answer from nothing invites invention.
        return Answer(
            text=(
                "No relevant records were retrieved. The corpus may be empty, or "
                "LightRAG may be unreachable - check `pipeline doctor`."
            ),
            retrieval_sec=retrieval_sec,
            synthesis_sec=0.0,
            provider=None,
            context_chars=0,
            synthesized=False,
        )

    started = time.monotonic()
    try:
        text = complete(build_synthesis_prompt(question, context))
        synthesis_sec = time.monotonic() - started
        return Answer(
            text=text,
            retrieval_sec=retrieval_sec,
            synthesis_sec=synthesis_sec,
            provider=last_provider,
            context_chars=len(context),
            synthesized=True,
        )
    except LLMError as exc:
        # An answer from the small local model beats no answer.
        print(f"    synthesis unavailable ({exc}); falling back to LightRAG generation")
        started = time.monotonic()
        text = index.query(question, mode=mode, top_k=top_k)
        return Answer(
            text=text,
            retrieval_sec=retrieval_sec,
            synthesis_sec=time.monotonic() - started,
            provider=None,
            context_chars=len(context),
            synthesized=False,
        )
