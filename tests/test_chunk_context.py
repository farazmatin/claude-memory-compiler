"""Contextual headers over the chunks both indexes serve.

A chunk lifted out of a meeting is close to meaningless - "he said push it to
Q3" - and the header is what makes it self-describing to BM25 and to the
embedder. That makes these tests about one thing above all others: **a header
must sit on the chunk it describes**. A header on the wrong chunk is worse than
no header, because it is served as evidence with a citation attached and nothing
downstream could ever detect it.

So the fake model here does not return canned strings. It reads the prompt and
answers passage N with a marker taken from passage N's own text, which means an
off-by-one shift shows up as a failed assertion rather than passing quietly. The
fake is injected through the same `complete` seam production hands `llm.complete`
to; no test may reach a real provider.
"""

from __future__ import annotations

import argparse
import re
import sqlite3

import pytest

from pipeline import chunk_context, chunk_index, cli, db, llm

# Long enough that a section built from it clears chunk_index's 200-char
# fragment floor without any test needing to count characters by hand.
FILLER = (
    "The team reviewed the control inventory and agreed the evidence trail has to "
    "survive an audit without anyone reconstructing it from memory afterwards. "
    "Ownership stays with the first line while the second line reviews sampling. "
)

# One rare token per section. The fake echoes whichever it finds in a passage,
# so a header that lands on the wrong chunk carries the wrong marker.
MARKERS = ["zolpidex", "quarrelsome", "vexillology", "kryptonite", "wombatry", "flumpish"]

_PASSAGE = re.compile(r"^--- passage (\d+) of (\d+)", re.MULTILINE)


# ── Fixture minutes ───────────────────────────────────────────────────

def _minutes_body(markers: list[str]) -> str:
    """Minutes whose every section carries exactly one marker word."""
    sections = [
        "---\ndate: 2026-06-09\ntitle: Unified System of Controls\n---\n",
        "\n# Unified System of Controls\n",
    ]
    for index, marker in enumerate(markers):
        sections.append(f"\n## Section {index} {marker}\n\n{FILLER * 2}{marker}.\n")
    return "".join(sections)


def _add_meeting(conn, meeting_id: str, date: str, tmp_path, markers: list[str]) -> None:
    """A meeting with minutes on disk, already chunked into the index."""
    path = tmp_path / f"{meeting_id}.md"
    path.write_text(_minutes_body(markers), encoding="utf-8")
    db.insert_meeting(
        conn,
        meeting_id=meeting_id,
        source_path=f"/inbox/{meeting_id}.m4a",
        source_name=f"{meeting_id}.m4a",
        audio_path=f"/audio/{meeting_id}.m4a",
        meeting_date=date,
        meeting_time="09:00",
        title_hint=meeting_id,
        duration_sec=1800.0,
    )
    db.advance(
        conn,
        meeting_id,
        db.MINUTES_COMPILED,
        transcript_path=f"/transcripts/{meeting_id}.json",
        minutes_path=str(path),
    )
    written = chunk_index.reindex_meeting(conn, meeting_id)
    assert written, f"fixture {meeting_id} produced no chunks"


# ── The fake model ────────────────────────────────────────────────────

class FakeModel:
    """Answers a header prompt by echoing each passage's own marker.

    `clause_for` receives the passage text and returns the clause to answer with,
    so a test states its intent in terms of what the model says about a specific
    passage. Defaults to the marker found in that passage.

    `drop` and `extra` perturb the number of answered lines, which is the one
    failure that must never be accepted. `fail_on` raises for a given meeting so
    a test can watch the run continue past it.
    """

    def __init__(
        self,
        *,
        clause_for=None,
        drop: int = 0,
        extra: int = 0,
        raw: str | None = None,
        fail_on: str | None = None,
        interrupt_on: str | None = None,
        provider: str = "fake-provider",
    ) -> None:
        self.clause_for = clause_for or self._marker
        self.drop = drop
        self.extra = extra
        self.raw = raw
        self.fail_on = fail_on
        self.interrupt_on = interrupt_on
        self.provider = provider
        self.prompts: list[str] = []

    @staticmethod
    def _marker(text: str) -> str:
        found = [m for m in MARKERS if m in text]
        return f"the section about {found[0] if found else 'nothing'}"

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.fail_on and self.fail_on in prompt:
            raise llm.LLMError("quota exhausted")
        if self.interrupt_on and self.interrupt_on in prompt:
            raise KeyboardInterrupt
        # Production reports which provider served a call by setting this;
        # the header's recorded model is read back from it.
        llm.last_provider = self.provider
        if self.raw is not None:
            return self.raw
        passages = _split_passages(prompt)
        lines = [f"{i}: {self.clause_for(text)}" for i, text in enumerate(passages, start=1)]
        if self.drop:
            lines = lines[: -self.drop]
        for n in range(self.extra):
            lines.append(f"{len(passages) + n + 1}: surplus clause")
        return "\n".join(lines)


def _split_passages(prompt: str) -> list[str]:
    """The passage bodies out of a prompt, in the order it numbered them.

    Deliberately coupled to the prompt's own format: the fake stands in for a
    model reading that prompt, and a format change that broke the numbering
    should break these tests rather than pass with headers nobody aligned.
    """
    bounds = [m.start() for m in _PASSAGE.finditer(prompt)]
    assert bounds, "prompt contains no numbered passages"
    bounds.append(len(prompt))
    return [prompt[bounds[i] : bounds[i + 1]] for i in range(len(bounds) - 1)]


def _headers(conn, meeting_id: str) -> list[tuple[int, str | None, str | None, str | None]]:
    return [
        (row["ordinal"], row["text"], row["context_header"], row["header_content_hash"])
        for row in conn.execute(
            "SELECT ordinal, text, context_header, header_content_hash "
            "FROM minute_chunks WHERE meeting_id = ? ORDER BY ordinal",
            (meeting_id,),
        )
    ]


# ── 1. Alignment: N chunks in, N headers out, matched by ordinal ──────

def test_every_header_lands_on_the_chunk_it_describes(manifest, tmp_path):
    """The property the whole feature rests on.

    Each section of the fixture carries one rare marker and the fake answers
    passage N with passage N's marker, so a header shifted by one position
    carries a marker its chunk's text does not contain.
    """
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS)

    stats = chunk_context.generate_all(manifest, complete=FakeModel())

    rows = _headers(manifest, "m1")
    assert stats["headed"] == 1
    assert stats["chunks"] == len(rows)
    assert stats["failed"] == 0
    for ordinal, text, header, header_hash in rows:
        assert header, f"chunk {ordinal} has no header"
        assert header_hash, f"chunk {ordinal} has no header provenance"
        present = [m for m in MARKERS if m in text]
        assert present, f"fixture chunk {ordinal} carries no marker"
        assert any(m in header for m in present), (
            f"chunk {ordinal} header {header!r} describes another chunk; "
            f"its own text is about {present}"
        )


def test_the_header_carries_its_own_meeting_date_and_title(manifest, tmp_path):
    """Provenance is per-source and comes from the manifest, not the model.

    The fake's clause never mentions a date, so a date in the stored header can
    only have come from the meeting row - which is what stops a model inventing
    one (GC2), and what keeps two meetings' headers apart (GC3).
    """
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:3])
    _add_meeting(manifest, "m2", "2026-07-21", tmp_path, MARKERS[3:])

    chunk_context.generate_all(manifest, complete=FakeModel())

    for meeting_id, date in (("m1", "2026-06-09"), ("m2", "2026-07-21")):
        for ordinal, _text, header, _hash in _headers(manifest, meeting_id):
            assert header.startswith(date), f"{meeting_id}:{ordinal} -> {header!r}"
            assert "Unified System of Controls" in header
    other = {h for _o, _t, h, _x in _headers(manifest, "m2")}
    assert not any("2026-06-09" in h for h in other), "m2 headers carry m1's date"


# ── 2. A count mismatch must fail the meeting, never misalign ─────────

@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"drop": 1}, "one clause short"),
        ({"extra": 1}, "one clause too many"),
        ({"raw": "I cannot help with that."}, "no numbered lines at all"),
        ({"raw": "1: fine\n3: skipped two\n4: and so on"}, "an ordinal missing from the middle"),
        # Six lines for six passages, so a count check alone would wave this
        # through: only the ordinals say which passage each clause belongs to.
        (
            {"raw": "1: a\n1: b\n2: c\n3: d\n4: e\n5: f"},
            "the right number of lines, numbered wrongly",
        ),
    ],
)
def test_a_count_mismatch_leaves_the_headers_null_and_counts_a_failure(
    manifest, tmp_path, kwargs, why
):
    """Never guess, never pad, never truncate to fit.

    Accepting a short or long answer would attach headers to the wrong chunks
    from the mismatch onward, and every citation after it would point at the
    wrong passage with nothing to surface the error.
    """
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS)

    stats = chunk_context.generate_all(manifest, complete=FakeModel(**kwargs))

    assert stats["failed"] == 1, why
    assert stats["headed"] == 0
    assert stats["chunks"] == 0
    assert all(header is None for _o, _t, header, _h in _headers(manifest, "m1")), why


# ── 3. The 200-character contract ─────────────────────────────────────

def test_an_over_long_clause_is_clipped_to_the_contract(manifest, tmp_path):
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:2])
    long_clause = "the section about " + "sampling and evidence retention " * 20

    stats = chunk_context.generate_all(
        manifest, complete=FakeModel(clause_for=lambda _text: long_clause)
    )

    rows = _headers(manifest, "m1")
    assert stats["clipped"] == len(rows)
    for ordinal, _text, header, _hash in rows:
        assert len(header) <= chunk_context.HEADER_MAX_CHARS, f"{ordinal}: {len(header)} chars"
        assert header.endswith("…"), f"{ordinal}: a clipped header must say so"
        assert header.startswith("2026-06-09"), f"{ordinal}: clipping ate the provenance"


def test_a_header_that_fits_is_stored_untouched(manifest, tmp_path):
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:2])

    stats = chunk_context.generate_all(manifest, complete=FakeModel())

    assert stats["clipped"] == 0
    assert not any(h.endswith("…") for _o, _t, h, _x in _headers(manifest, "m1"))


# ── 4/5. Resumability: skip what is done, --force to redo it ──────────

def test_a_rerun_skips_meetings_already_headed(manifest, tmp_path):
    """The resume rule. A 117-meeting run that was killed must not start over."""
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:3])
    chunk_context.generate_all(manifest, complete=FakeModel())
    before = _headers(manifest, "m1")

    second = FakeModel(clause_for=lambda _text: "a different clause entirely")
    stats = chunk_context.generate_all(manifest, complete=second)

    assert second.prompts == [], "a completed meeting must cost no model call"
    assert stats["skipped"] == 1
    assert stats["headed"] == 0
    assert _headers(manifest, "m1") == before


def test_force_regenerates_headers_that_are_already_current(manifest, tmp_path):
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:3])
    chunk_context.generate_all(manifest, complete=FakeModel())

    second = FakeModel(clause_for=lambda _text: "a different clause entirely")
    stats = chunk_context.generate_all(manifest, complete=second, force=True)

    assert len(second.prompts) == 1
    assert stats["headed"] == 1
    assert stats["skipped"] == 0
    assert all(
        "a different clause entirely" in h for _o, _t, h, _x in _headers(manifest, "m1")
    )


# ── 6. One meeting's failure must not stop the run ────────────────────

def test_a_provider_failure_on_one_meeting_does_not_abort_the_others(manifest, tmp_path):
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:2])
    _add_meeting(manifest, "m2", "2026-06-10", tmp_path, MARKERS[2:4])
    _add_meeting(manifest, "m3", "2026-06-11", tmp_path, MARKERS[4:])

    stats = chunk_context.generate_all(manifest, complete=FakeModel(fail_on="2026-06-10"))

    assert stats["failed"] == 1
    assert stats["headed"] == 2
    assert all(h is None for _o, _t, h, _x in _headers(manifest, "m2"))
    for survivor in ("m1", "m3"):
        assert all(h for _o, _t, h, _x in _headers(manifest, survivor)), survivor


def test_an_interrupt_keeps_the_meetings_already_committed(manifest, tmp_path, monkeypatch):
    """Ctrl-C mid-run loses at most the meeting in flight.

    Read back on a second connection, so what is asserted is what was committed
    and not what is merely visible inside this test's own transaction.
    """
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:2])
    _add_meeting(manifest, "m2", "2026-06-10", tmp_path, MARKERS[2:4])
    manifest.commit()

    stats = chunk_context.generate_all(
        manifest, complete=FakeModel(interrupt_on="2026-06-10")
    )

    assert stats["interrupted"] == 1
    assert stats["headed"] == 1
    other = sqlite3.connect(str(db.DB_PATH))
    other.row_factory = sqlite3.Row
    try:
        committed = other.execute(
            "SELECT meeting_id, COUNT(*) AS n FROM minute_chunks "
            "WHERE context_header IS NOT NULL GROUP BY meeting_id"
        ).fetchall()
    finally:
        other.close()
    assert [(row["meeting_id"], row["n"]) for row in committed] == [
        ("m1", len(_headers(manifest, "m1")))
    ]


# ── 7. content_hash is the invalidation ───────────────────────────────

def test_an_edited_chunk_invalidates_its_header(manifest, tmp_path):
    """A header describes one exact passage, and the hash is what pins that.

    The edit here is applied to the row directly, which is the state
    `reindex_meeting` produces on a connection without foreign keys on: the text
    moved on and the header now describes something nobody can be shown.
    """
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:3])
    _add_meeting(manifest, "m2", "2026-06-10", tmp_path, MARKERS[3:])
    chunk_context.generate_all(manifest, complete=FakeModel())

    edited = f"Rewritten passage about {MARKERS[0]}. {FILLER}"
    manifest.execute(
        "UPDATE minute_chunks SET text = ?, content_hash = ? WHERE chunk_id = ?",
        (edited, "0" * 64, "m1:0000"),
    )
    assert chunk_context.context_status(manifest)[1].count("stale") == 1

    second = FakeModel(clause_for=lambda _text: "the rewritten section")
    stats = chunk_context.generate_all(manifest, complete=second)

    assert len(second.prompts) == 1, "only the edited meeting needs a call"
    assert stats["headed"] == 1
    assert stats["skipped"] == 1, "the untouched meeting must still be skipped"
    rows = dict(
        manifest.execute(
            "SELECT chunk_id, header_content_hash FROM minute_chunks WHERE meeting_id = 'm1'"
        ).fetchall()
    )
    assert rows["m1:0000"] == "0" * 64, "the header must be pinned to the text it describes"


def test_rechunking_a_meeting_clears_its_headers(manifest, tmp_path):
    """The production invalidation path: new minutes, so re-chunk, so no header."""
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:3])
    chunk_context.generate_all(manifest, complete=FakeModel())

    (tmp_path / "m1.md").write_text(_minutes_body(MARKERS[3:]), encoding="utf-8")
    chunk_index.reindex_meeting(manifest, "m1")

    assert all(h is None for _o, _t, h, _x in _headers(manifest, "m1"))
    assert chunk_context.generate_all(manifest, complete=FakeModel())["headed"] == 1


# ── Selection, batching, provenance, and the FTS payoff ───────────────

def test_limit_and_meeting_select_what_runs(manifest, tmp_path):
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:2])
    _add_meeting(manifest, "m2", "2026-06-10", tmp_path, MARKERS[2:4])
    _add_meeting(manifest, "m3", "2026-06-11", tmp_path, MARKERS[4:])

    limited = FakeModel()
    assert chunk_context.generate_all(manifest, complete=limited, limit=2)["headed"] == 2
    assert len(limited.prompts) == 2

    one = FakeModel()
    stats = chunk_context.generate_all(manifest, complete=one, meeting="m3")
    assert stats["meetings"] == 1, "--meeting must narrow what is considered, not just run"
    assert stats["headed"] == 1
    assert len(one.prompts) == 1
    assert all(h for _o, _t, h, _x in _headers(manifest, "m3"))


def test_a_meeting_too_large_for_one_prompt_falls_back_to_batches(
    manifest, tmp_path, monkeypatch
):
    """Per-meeting is the default; the fallback is per-batch for that meeting only."""
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS)
    monkeypatch.setattr(chunk_context, "PROMPT_CHAR_BUDGET", 2_000)

    model = FakeModel()
    stats = chunk_context.generate_all(manifest, complete=model)

    assert len(model.prompts) > 1, "an oversized meeting must be split"
    assert stats["batched"] == 1
    assert stats["headed"] == 1
    rows = _headers(manifest, "m1")
    assert stats["chunks"] == len(rows)
    for ordinal, text, header, _hash in rows:
        present = [m for m in MARKERS if m in text]
        assert any(m in header for m in present), f"batch boundary misaligned chunk {ordinal}"


def test_a_batch_failure_leaves_the_whole_meeting_null(manifest, tmp_path, monkeypatch):
    """Partial headers across a meeting would be an invisible hole in the index."""
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS)
    monkeypatch.setattr(chunk_context, "PROMPT_CHAR_BUDGET", 2_000)

    stats = chunk_context.generate_all(manifest, complete=FakeModel(fail_on="part 2 of"))

    assert stats["failed"] == 1
    assert stats["headed"] == 0
    assert all(h is None for _o, _t, h, _x in _headers(manifest, "m1"))


def test_the_provider_that_served_the_call_is_recorded(manifest, tmp_path):
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:2])

    chunk_context.generate_all(manifest, complete=FakeModel(provider="codex"))

    models = {
        row[0]
        for row in manifest.execute(
            "SELECT header_model FROM minute_chunks WHERE meeting_id = 'm1'"
        )
    }
    assert models == {"codex"}
    assert all(
        row[0]
        for row in manifest.execute(
            "SELECT headers_generated_at FROM minute_chunks WHERE meeting_id = 'm1'"
        )
    )


def test_a_header_makes_its_chunk_reachable_by_bm25(manifest, tmp_path):
    """The point of the feature: a word only in the header still finds the chunk.

    The FTS index is external-content and maintained by triggers, so this also
    pins that the header UPDATE keeps the index in step.
    """
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:2])
    assert chunk_index.search_chunks(manifest, "peregrination") == []

    chunk_context.generate_all(
        manifest, complete=FakeModel(clause_for=lambda _t: "a peregrination through controls")
    )

    hits = chunk_index.search_chunks(manifest, "peregrination")
    assert hits, "a header word must be searchable"
    assert {hit.meeting_id for hit in hits} == {"m1"}


def test_status_reports_absence_and_progress(manifest, tmp_path):
    searchable, reason = chunk_context.context_status(manifest)
    assert not searchable
    assert "chunk-index" in reason

    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:2])
    searchable, reason = chunk_context.context_status(manifest)
    assert not searchable
    assert "chunk-context" in reason

    chunk_context.generate_all(manifest, complete=FakeModel())
    searchable, reason = chunk_context.context_status(manifest)
    assert searchable
    assert "stale" not in reason


# ── CLI surface ───────────────────────────────────────────────────────

def _args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**{"meeting": None, "limit": None, "force": False, **kwargs})


def test_cli_runs_the_chain_and_reports_counts(manifest, tmp_path, monkeypatch, capsys):
    """The CLI must reach the real `llm.complete` seam and nothing else."""
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:2])
    manifest.commit()
    monkeypatch.setattr(llm, "complete", FakeModel())

    assert cli.cmd_chunk_context(_args()) == 0

    out = capsys.readouterr().out
    assert "1 headed" in out
    assert "header(s)" in out


def test_cli_reports_a_failed_meeting_as_a_nonzero_exit(manifest, tmp_path, monkeypatch):
    _add_meeting(manifest, "m1", "2026-06-09", tmp_path, MARKERS[:2])
    manifest.commit()
    monkeypatch.setattr(llm, "complete", FakeModel(drop=1))

    assert cli.cmd_chunk_context(_args()) == 1


def test_cli_rejects_an_unknown_meeting_id(manifest, tmp_path, monkeypatch, capsys):
    """A typo and an already-current meeting both do no work; only one is an error."""
    monkeypatch.setattr(llm, "complete", FakeModel())
    assert cli.cmd_chunk_context(_args(meeting="nope")) == 1
    assert "nope" in capsys.readouterr().err
