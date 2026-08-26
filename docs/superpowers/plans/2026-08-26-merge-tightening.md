# Merge Tightening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a speaker merge final — it can be merged into a name you type, it leaves no trace of the old spelling anywhere visible, it cannot be undone by confirming a stale card, and it corrects already-compiled minutes without an LLM recompile.

**Architecture:** All merge paths converge on `db.merge_person` + `voices.merge_people`, called in that order by both the CLI and the dashboard. The work is: add a tombstone table and two column rewrites at that convergence point, add a pure text-substitution module for the minutes, lift one validation rule in `dashboard.merge_many_people`, and expose the one-time repairs behind a CLI flag. Ordering inside a merge is load-bearing and specified in the spec's section E.

**Tech Stack:** Python 3.12, stdlib `sqlite3` (no ORM), stdlib `re`, `pytest`. Frontend is vanilla JS with no build step and no JS test runner — browser-side logic is covered by `tests/js/check_controls.mjs` driven from `tests/test_dashboard_controls_js.py`.

**Spec:** `docs/superpowers/specs/2026-08-26-merge-tightening-design.md`

## Global Constraints

- The manifest is `db/manifest.db`, set by `config.DB_PATH`. Never open a bare `./manifest.db` — it silently reads an empty database and every count comes back zero.
- Transcripts are immutable. No task in this plan writes to `TRANSCRIPTS_DIR`.
- Do not bump `TEMPLATE_VERSION`. It marks every compiled minutes file stale and forces a ~7.8-min-per-meeting recompile, which the owner explicitly declined.
- Comments explain **why**, not what, and match the surrounding density. This codebase documents reasoning behind non-obvious choices deliberately.
- Run tests with `./.venv/Scripts/python.exe -m pytest`, not `uv run pytest` — `uv run` re-resolves the environment on Windows here and is slow.
- Lint with `uvx ruff check` (ruff is not a declared dependency). `pipeline/dashboard.py` has one **pre-existing** import-sort error at its import block; do not treat it as yours, and do not fix it in these commits.
- `tests/test_answer.py` has 4 **pre-existing** failures (`alert.send()` returns True with no alert configured). A task is done when it adds no new failures — not when the suite is fully green.
- Self-aliases (`person_aliases.alias == lower(canonical)`) are load-bearing: `db.add_person` registers every canonical as an alias of itself so lookup has one code path. Never delete one.

---

### Task 1: Minutes name rewriting

Pure text module, no database, no I/O beyond the named files. Built first because every later task can use it and it has the subtlest logic in the plan.

**Files:**
- Create: `pipeline/rename_minutes.py`
- Test: `tests/test_rename_minutes.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RewriteResult` (frozen dataclass, fields `rewritten: int`, `unchanged: int`, `missing: list[str]`), `name_pattern(old: str, new: str) -> re.Pattern[str]`, and `rewrite_files(paths: dict[str, Path], old: str, new: str) -> RewriteResult` where `paths` maps meeting id to its minutes file.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rename_minutes.py`:

```python
"""Correcting a merged person's name inside already-compiled minutes."""

from __future__ import annotations

from pipeline.rename_minutes import RewriteResult, name_pattern, rewrite_files


def _write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_replaces_the_bare_name():
    assert name_pattern("Faraz", "Faraz Mateen").sub(
        "Faraz Mateen", "Faraz opened the review."
    ) == "Faraz Mateen opened the review."


def test_rewrites_a_possessive():
    """`z`/`'` is a word boundary, so \\b already reaches this case."""
    assert name_pattern("Faraz", "Faraz Mateen").sub(
        "Faraz Mateen", "That was Faraz's call."
    ) == "That was Faraz Mateen's call."


def test_leaves_a_longer_word_alone():
    """Merging `Ru` must not touch `Ruth` - u/t is not a boundary."""
    assert name_pattern("Ru", "Ru Farrell").sub(
        "Ru Farrell", "Ruth and Ru spoke."
    ) == "Ruth and Ru Farrell spoke."


def test_does_not_double_an_already_correct_name():
    """The real hazard. Minutes compiled at different times hold both spellings,
    and \\bFaraz\\b matches the first half of the correct one."""
    result = name_pattern("Faraz", "Faraz Mateen").sub(
        "Faraz Mateen", "Faraz Mateen and Faraz agreed."
    )
    assert result == "Faraz Mateen and Faraz Mateen agreed."
    assert "Mateen Mateen" not in result


def test_unrelated_new_name_needs_no_lookahead():
    assert name_pattern("Bob", "Robert Smith").sub(
        "Robert Smith", "Bob spoke."
    ) == "Robert Smith spoke."


def test_rewrite_files_reports_each_outcome(tmp_path):
    changed = _write(tmp_path, "a.md", "# Notes\n\nFaraz decided.\n")
    untouched = _write(tmp_path, "b.md", "# Notes\n\nNobody relevant.\n")

    result = rewrite_files(
        {"m1": changed, "m2": untouched, "m3": tmp_path / "gone.md"},
        "Faraz",
        "Faraz Mateen",
    )

    assert result == RewriteResult(rewritten=1, unchanged=1, missing=["m3"])
    assert "Faraz Mateen decided." in changed.read_text(encoding="utf-8")
    assert untouched.read_text(encoding="utf-8") == "# Notes\n\nNobody relevant.\n"


def test_rewriting_twice_changes_the_file_only_once(tmp_path):
    """What makes --repair-merges safe to re-run."""
    path = _write(tmp_path, "a.md", "Faraz decided.\n")

    first = rewrite_files({"m1": path}, "Faraz", "Faraz Mateen")
    after_first = path.read_text(encoding="utf-8")
    second = rewrite_files({"m1": path}, "Faraz", "Faraz Mateen")

    assert first.rewritten == 1
    assert second.rewritten == 0
    assert second.unchanged == 1
    assert path.read_text(encoding="utf-8") == after_first
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_rename_minutes.py -q`

Expected: collection error — `ModuleNotFoundError: No module named 'pipeline.rename_minutes'`.

- [ ] **Step 3: Write the minimal implementation**

Create `pipeline/rename_minutes.py`:

```python
"""Correct a person's name inside already-compiled minutes.

A merge that renames a speaker leaves every compiled minutes document still
saying the old spelling. Recompiling from the retained transcript is the
authoritative fix and costs ~7.8 minutes per meeting; substituting the name in
the finished markdown costs milliseconds and cannot rephrase content the owner
has already read and acted on. This module is the second option, chosen
deliberately - see the spec's "Decisions taken".

Transcripts are never touched here. They are the immutable source the repeatable
compile depends on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RewriteResult:
    """Three counts because they mean three different things to the caller.

    `missing` is the only one that leaves work outstanding: those meetings keep
    their old spelling until something recompiles them, and the caller has to be
    able to say so.
    """

    rewritten: int = 0
    unchanged: int = 0
    missing: list[str] = field(default_factory=list)


def name_pattern(old: str, new: str) -> re.Pattern[str]:
    r"""Match `old` only where it is a whole name that is not already correct.

    `\b` handles the two obvious cases on its own: "Faraz's" rewrites because
    z/' is a boundary, and "Ruth" survives merging "Ru" because u/t is not.

    The case it does not handle is doubling. When the new name begins with the
    old one - "Faraz" into "Faraz Mateen", the merge that prompted this - a
    document already saying "Faraz Mateen" contains a \bFaraz\b match at the
    start of the correct name, and substituting it yields
    "Faraz Mateen Mateen". Minutes for different meetings resolved at different
    times, so one document holding both spellings is the common case, not a
    corner. The negative lookahead skips the occurrences that are already right,
    which also makes the whole rewrite idempotent.
    """
    suffix = new[len(old) :] if new.lower().startswith(old.lower()) else ""
    tail = f"(?!{re.escape(suffix)})" if suffix else ""
    return re.compile(rf"\b{re.escape(old)}\b{tail}")


def rewrite_files(paths: dict[str, Path], old: str, new: str) -> RewriteResult:
    """Substitute `old` with `new` in each file, reporting what happened.

    Writes only when the content actually changed, so a no-op run leaves mtimes
    alone and a re-run is genuinely free.
    """
    old, new = old.strip(), new.strip()
    if not old or not new or old == new:
        return RewriteResult(unchanged=len(paths))

    pattern = name_pattern(old, new)
    rewritten = 0
    unchanged = 0
    missing: list[str] = []

    for meeting_id, path in paths.items():
        if not path or not Path(path).is_file():
            missing.append(meeting_id)
            continue
        path = Path(path)
        before = path.read_text(encoding="utf-8")
        after = pattern.sub(new, before)
        if after == before:
            unchanged += 1
            continue
        path.write_text(after, encoding="utf-8")
        rewritten += 1

    return RewriteResult(rewritten=rewritten, unchanged=unchanged, missing=sorted(missing))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_rename_minutes.py -q`

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/rename_minutes.py tests/test_rename_minutes.py
git commit -m "Add idempotent name rewriting for compiled minutes

Substitutes a merged person's name in already-compiled markdown instead of
recompiling from the transcript, which costs ~7.8 min per meeting.

The lookahead is the load-bearing part: minutes for different meetings
resolved at different times, so one document can hold both \"Faraz\" and
\"Faraz Mateen\", and a plain \\b match on the former hits the first half of
the latter and produces \"Faraz Mateen Mateen\"."
```

---

### Task 2: The tombstone table

**Files:**
- Modify: `pipeline/db.py` — add to `SCHEMA` (near the `person_aliases` block, around line 122), plus two new functions beside `merge_person` (around line 758)
- Test: `tests/test_people.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `db.record_merged_name(conn, old_spelling: str, canonical: str) -> None` and `db.resolve_merged_name(conn, name: str) -> str | None`, which returns the living person who absorbed `name`, following a chain of merges, or `None` when `name` is unknown or the chain ends nowhere.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_people.py`:

```python
def test_tombstone_maps_a_merged_spelling_to_its_absorber(manifest):
    db.add_person(manifest, "Faraz Mateen")
    db.record_merged_name(manifest, "Faraz", "Faraz Mateen")

    assert db.resolve_merged_name(manifest, "Faraz") == "Faraz Mateen"


def test_tombstone_follows_a_chain_of_merges(manifest):
    """A merged into B, then B merged into C, must resolve A to C."""
    db.add_person(manifest, "Ru Farrell")
    db.record_merged_name(manifest, "Roo", "Ru")
    db.record_merged_name(manifest, "Ru", "Ru Farrell")

    assert db.resolve_merged_name(manifest, "Roo") == "Ru Farrell"


def test_tombstone_refuses_rather_than_looping_on_a_cycle(manifest):
    """Two merges that point at each other must not hang the caller."""
    db.record_merged_name(manifest, "A", "B")
    db.record_merged_name(manifest, "B", "A")

    assert db.resolve_merged_name(manifest, "A") is None


def test_tombstone_returns_none_for_an_unknown_name(manifest):
    assert db.resolve_merged_name(manifest, "Nobody") is None


def test_tombstone_returns_none_when_the_chain_ends_nowhere(manifest):
    """The absorber was itself deleted without a tombstone of its own."""
    db.record_merged_name(manifest, "Roo", "Ru Farrell")

    assert db.resolve_merged_name(manifest, "Roo") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_people.py -k tombstone -q`

Expected: 5 failures, `AttributeError: module 'pipeline.db' has no attribute 'record_merged_name'`.

- [ ] **Step 3: Write the minimal implementation**

In `pipeline/db.py`, add to `SCHEMA` immediately after the `person_aliases` index (line 129):

```sql
-- Merged-away spellings. Deliberately NOT person_aliases: the owner's
-- requirement is that a folded-in spelling appears nowhere visible, and aliases
-- are rendered in the contacts list and consulted by speaker resolution. This
-- table is read by exactly two callers - the confirm guard in voices.py and
-- `people --repair-merges` - and rendered by none.
--
-- Without it, a review card cached before a merge can still be confirmed, and
-- voices.confirm() calls add_person(), which recreates the person the merge
-- just removed. That is the treadmill this table exists to stop.
CREATE TABLE IF NOT EXISTS merged_names (
    old_spelling TEXT PRIMARY KEY,   -- exactly as recorded, not lowercased
    canonical    TEXT NOT NULL,      -- who absorbed it, at merge time
    merged_at    TEXT NOT NULL,
    FOREIGN KEY (canonical) REFERENCES people(canonical) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_merged_names_canonical ON merged_names(canonical);
```

Then add beside `merge_person` (after line 823):

```python
def record_merged_name(conn: sqlite3.Connection, old_spelling: str, canonical: str) -> None:
    """Tombstone a spelling that has just been folded into `canonical`.

    Written before anything is destroyed, so a merge that fails part-way still
    leaves a record of what it intended.
    """
    old_spelling, canonical = old_spelling.strip(), canonical.strip()
    if not old_spelling or not canonical or old_spelling == canonical:
        return
    conn.execute(
        """
        INSERT INTO merged_names (old_spelling, canonical, merged_at)
        VALUES (?, ?, ?)
        ON CONFLICT(old_spelling) DO UPDATE SET canonical = ?, merged_at = ?
        """,
        (old_spelling, canonical, now_iso(), canonical, now_iso()),
    )


def resolve_merged_name(conn: sqlite3.Connection, name: str) -> str | None:
    """Return the living person who absorbed `name`, or None.

    Follows a chain, because a target can itself be merged later: Roo into Ru,
    then Ru into Ru Farrell, must answer Ru Farrell. The visited set is not
    defensive padding - the owner merges the same cluster of spellings
    repeatedly, and two tombstones pointing at each other would otherwise spin
    forever inside a request.
    """
    current = (name or "").strip()
    if not current:
        return None
    visited: set[str] = set()
    while current not in visited:
        visited.add(current)
        row = conn.execute(
            "SELECT canonical FROM merged_names WHERE old_spelling = ?", (current,)
        ).fetchone()
        if not row:
            # Only a name that is actually a person ends the walk successfully.
            exists = conn.execute(
                "SELECT 1 FROM people WHERE canonical = ?", (current,)
            ).fetchone()
            return current if exists and current != (name or "").strip() else None
        current = row["canonical"]
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_people.py -q`

Expected: all pass, including the 5 new ones.

- [ ] **Step 5: Commit**

```bash
git add pipeline/db.py tests/test_people.py
git commit -m "Add merged_names tombstone table

Records which spelling was folded into which person, so a review card cached
before a merge cannot be confirmed back into existence. Deliberately separate
from person_aliases: aliases are rendered in the contacts list and consulted by
speaker resolution, and the owner's requirement is that folded-in spellings
appear nowhere visible."
```

---

### Task 3: A merge leaves no ghost suggestions

**Files:**
- Modify: `pipeline/voices.py:536-552` (`merge_people`)
- Test: `tests/test_voices.py`

**Interfaces:**
- Consumes: `db.record_merged_name` from Task 2.
- Produces: `voices.merge_people` unchanged signature `(conn, source, target) -> int`, now additionally rewriting `best_canonical`/`next_canonical` in both `speaker_matches` and `voice_clusters` and writing the tombstone.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_voices.py`:

```python
def test_merge_rewrites_matcher_suggestions_in_both_tables(manifest):
    """78 rows in the live manifest suggested a name merged away hours earlier.

    A suggestion naming a person who no longer exists is not a weaker
    suggestion; it is the exact click that undoes the merge.
    """
    db.add_person(manifest, "Faraz Mateen")
    db.add_person(manifest, "Faraz")
    make_meeting(manifest, "m" * 64, "2026-08-12")
    db.upsert_speaker_match(
        manifest,
        meeting_id="m" * 64,
        label="SPEAKER_00",
        state="pending",
        speech_sec=10.0,
        best_canonical="Faraz",
        next_canonical="Faraz",
    )
    manifest.execute(
        """
        INSERT INTO voice_clusters
            (id, size, total_speech, best_canonical, best_score,
             next_canonical, next_score, band, created_at)
        VALUES ('c1', 1, 10.0, 'Faraz', 0.8, 'Faraz', 0.4, 'review', '2026-08-12T09:00:00')
        """
    )

    voices.merge_people(manifest, "Faraz", "Faraz Mateen")

    ghosts = manifest.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT best_canonical AS n FROM speaker_matches
            UNION ALL SELECT next_canonical FROM speaker_matches
            UNION ALL SELECT best_canonical FROM voice_clusters
            UNION ALL SELECT next_canonical FROM voice_clusters
        ) WHERE n = 'Faraz'
        """
    ).fetchone()[0]
    assert ghosts == 0


def test_merge_does_not_leave_best_and_next_naming_the_same_person(manifest):
    """Both columns naming one person is not a runner-up, it is a duplicate,
    and it would render as "closest: Faraz Mateen" against itself."""
    db.add_person(manifest, "Faraz Mateen")
    db.add_person(manifest, "Faraz")
    manifest.execute(
        """
        INSERT INTO voice_clusters
            (id, size, total_speech, best_canonical, best_score,
             next_canonical, next_score, band, created_at)
        VALUES ('c1', 1, 10.0, 'Faraz Mateen', 0.8, 'Faraz', 0.4, 'review', '2026-08-12T09:00:00')
        """
    )

    voices.merge_people(manifest, "Faraz", "Faraz Mateen")

    row = manifest.execute(
        "SELECT best_canonical, next_canonical, next_score FROM voice_clusters WHERE id = 'c1'"
    ).fetchone()
    assert row["best_canonical"] == "Faraz Mateen"
    assert row["next_canonical"] is None
    assert row["next_score"] is None


def test_merge_records_the_tombstone(manifest):
    db.add_person(manifest, "Ru Farrell")
    db.add_person(manifest, "Roo")

    voices.merge_people(manifest, "Roo", "Ru Farrell")

    assert db.resolve_merged_name(manifest, "Roo") == "Ru Farrell"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_voices.py -k "matcher_suggestions or same_person or tombstone" -q`

Expected: 3 failures — the ghost count is 4 rather than 0, `next_canonical` is still `"Faraz Mateen"`, and `resolve_merged_name` returns `None`.

- [ ] **Step 3: Write the minimal implementation**

Replace the body of `voices.merge_people` (`pipeline/voices.py:536-552`):

```python
def merge_people(conn: sqlite3.Connection, source: str, target: str) -> int:
    """Fold one person's voice samples into another's.

    A routine correction rather than an edge case: the same person gets named
    "Mike" one week and "Michael" the next, and both accumulate voiceprints.
    """
    source, target = source.strip(), target.strip()
    if not source or not target or source == target:
        return 0

    # Before anything is destroyed, so a failure below still leaves a record of
    # what this merge intended.
    db.record_merged_name(conn, source, target)

    moved = db.reassign_voice_samples(conn, source, target)
    db.add_person(conn, target)
    conn.execute(
        "UPDATE speaker_matches SET resolved_as = ? WHERE resolved_as = ?", (target, source)
    )
    conn.execute("UPDATE speakers SET name = ? WHERE name = ?", (target, source))

    # The matcher's own suggestions. Missing this is what let a card keep
    # offering a name that had already been merged away - and confirming one
    # calls add_person(), which recreates the person the merge just removed.
    for table in ("speaker_matches", "voice_clusters"):
        conn.execute(
            f"UPDATE {table} SET best_canonical = ? WHERE best_canonical = ?",
            (target, source),
        )
        conn.execute(
            f"UPDATE {table} SET next_canonical = ? WHERE next_canonical = ?",
            (target, source),
        )
        # Rewriting both columns can leave them naming one person. That is not a
        # runner-up to compare against; it renders as "closest: X" against X.
        conn.execute(
            f"""
            UPDATE {table} SET next_canonical = NULL, next_score = NULL
            WHERE next_canonical IS NOT NULL AND next_canonical = best_canonical
            """
        )
    return moved
```

Note the `add_person(conn, target)` — the `aliases=[source]` argument is dropped here as part of Task 5; leaving it would create the alias Task 5 then deletes.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_voices.py tests/test_people.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/voices.py tests/test_voices.py
git commit -m "Rewrite matcher suggestions when people merge

voices.merge_people rewrote resolved_as but never best_canonical or
next_canonical, and never touched voice_clusters at all. 78 rows in the live
manifest still suggested a name merged away hours earlier, and confirming one
calls add_person() and recreates that person."
```

---

### Task 4: Confirming a stale card cannot resurrect a person

**Files:**
- Modify: `pipeline/voices.py:453-480` (`confirm`)
- Test: `tests/test_voices.py`

**Interfaces:**
- Consumes: `db.resolve_merged_name` from Task 2.
- Produces: `voices.confirm` keeps its signature; its `canonical` argument is now resolved through the tombstone before use, and it raises `ValueError` when the name is a dead end.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_voices.py`:

```python
def test_confirming_a_merged_away_name_lands_on_the_absorber(manifest):
    """The treadmill, closed. A card cached before the merge must not undo it."""
    db.add_person(manifest, "Faraz Mateen")
    db.record_merged_name(manifest, "Faraz", "Faraz Mateen")
    make_meeting(manifest, "n" * 64, "2026-08-12")
    db.set_speaker(manifest, "n" * 64, "SPEAKER_00", None, "unknown")

    voices.confirm(manifest, meeting_id="n" * 64, label="SPEAKER_00", canonical="Faraz")

    assert manifest.execute(
        "SELECT 1 FROM people WHERE canonical = 'Faraz'"
    ).fetchone() is None
    assert manifest.execute(
        "SELECT name FROM speakers WHERE meeting_id = ? AND label = 'SPEAKER_00'",
        ("n" * 64,),
    ).fetchone()["name"] == "Faraz Mateen"


def test_confirming_a_genuinely_new_name_still_creates_the_person(manifest):
    """The guard must not break naming someone for the first time."""
    make_meeting(manifest, "p" * 64, "2026-08-12")
    db.set_speaker(manifest, "p" * 64, "SPEAKER_00", None, "unknown")

    voices.confirm(manifest, meeting_id="p" * 64, label="SPEAKER_00", canonical="Brand New")

    assert manifest.execute(
        "SELECT 1 FROM people WHERE canonical = 'Brand New'"
    ).fetchone() is not None


def test_confirming_a_name_whose_chain_ends_nowhere_is_refused(manifest):
    """Better a refusal the owner can see than a silently wrong attribution."""
    db.record_merged_name(manifest, "Roo", "Someone Deleted")
    make_meeting(manifest, "q" * 64, "2026-08-12")
    db.set_speaker(manifest, "q" * 64, "SPEAKER_00", None, "unknown")

    with pytest.raises(ValueError, match="merged"):
        voices.confirm(manifest, meeting_id="q" * 64, label="SPEAKER_00", canonical="Roo")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_voices.py -k "merged_away or genuinely_new or ends_nowhere" -q`

Expected: the first fails because `Faraz` is recreated; the third fails because no `ValueError` is raised.

- [ ] **Step 3: Write the minimal implementation**

At the top of `voices.confirm`, before the existing `db.add_person(conn, canonical)` call, insert:

```python
    # A card rendered before a merge still carries the old spelling, and the
    # add_person() below would recreate the person the merge just removed. That
    # round trip - merge, get re-offered the dead name, accept, back to two
    # people - is the loop this guard exists to break. A name that is already a
    # person passes straight through; only a tombstoned one is redirected.
    if not conn.execute(
        "SELECT 1 FROM people WHERE canonical = ?", (canonical,)
    ).fetchone():
        absorbed_by = db.resolve_merged_name(conn, canonical)
        if absorbed_by:
            canonical = absorbed_by
        elif conn.execute(
            "SELECT 1 FROM merged_names WHERE old_spelling = ?", (canonical,)
        ).fetchone():
            # Tombstoned, but the chain leads to nobody who still exists.
            # Refusing is better than attributing a voice to a guess.
            raise ValueError(
                f"'{canonical}' was merged into a contact that no longer exists. "
                "Refresh the speaker queue and choose again."
            )
```

Add `import pytest` to `tests/test_voices.py` if it is not already imported.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_voices.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/voices.py tests/test_voices.py
git commit -m "Stop a stale confirm from resurrecting a merged person

voices.confirm() calls add_person(), so confirming a card rendered before a
merge recreated the person that merge had just removed. It now resolves the
name through the tombstone first, and refuses when the chain ends nowhere."
```

---

### Task 5: Folded-in spellings are deleted

**Files:**
- Modify: `pipeline/db.py:783` (`merge_person`, the `add_person(conn, into, aliases=[from_name])` call)
- Test: `tests/test_people.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `db.merge_person` unchanged signature; it no longer creates an alias for the source and deletes the source's alias row instead.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_people.py`:

```python
def test_merge_deletes_the_folded_in_spelling(manifest):
    db.add_person(manifest, "Ru Farrell")
    db.add_person(manifest, "Roo")

    db.merge_person(manifest, from_name="Roo", into="Ru Farrell")

    assert manifest.execute(
        "SELECT 1 FROM person_aliases WHERE alias = 'roo'"
    ).fetchone() is None


def test_merge_keeps_the_targets_self_alias(manifest):
    """add_person registers every canonical as an alias of itself so lookup has
    one code path. Deleting that breaks name resolution for the person."""
    db.add_person(manifest, "Ru Farrell")
    db.add_person(manifest, "Roo")

    db.merge_person(manifest, from_name="Roo", into="Ru Farrell")

    assert manifest.execute(
        "SELECT 1 FROM person_aliases WHERE alias = 'ru farrell'"
    ).fetchone() is not None


def test_merge_moves_an_alias_the_source_had_acquired(manifest):
    """Aliases the source owned are not the source's own spelling and still have
    to follow it, or they point at a deleted person."""
    db.add_person(manifest, "Ru Farrell")
    db.add_person(manifest, "Roo", aliases=["rooo"])

    db.merge_person(manifest, from_name="Roo", into="Ru Farrell")

    row = manifest.execute(
        "SELECT canonical FROM person_aliases WHERE alias = 'rooo'"
    ).fetchone()
    assert row["canonical"] == "Ru Farrell"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_people.py -k "folded_in or self_alias or acquired" -q`

Expected: the first fails — `person_aliases` still holds a `roo` row, because `merge_person` creates it.

- [ ] **Step 3: Write the minimal implementation**

In `pipeline/db.py`, replace line 783:

```python
    add_person(conn, into, aliases=[from_name])
```

with:

```python
    # No `aliases=[from_name]` here. The merge used to create the very alias the
    # owner does not want to see again; the contacts list renders aliases, so a
    # merge left the folded-in spelling on screen. The source's own spelling is
    # deleted below and tombstoned in merged_names instead. The accepted
    # consequence, chosen by the owner: a later transcript saying "Roo" creates
    # a new person to merge again, because normalisation reads person_aliases.
    add_person(conn, into)
```

Then, immediately before the existing `DELETE FROM people` (line 821), replace:

```python
    conn.execute(
        "UPDATE person_aliases SET canonical = ? WHERE canonical = ?", (into, from_name)
    )
```

with:

```python
    # Aliases the source had acquired still have to follow it, or they point at
    # a row that is about to be deleted.
    conn.execute(
        "UPDATE person_aliases SET canonical = ? WHERE canonical = ?", (into, from_name)
    )
    # ...but the source's own spelling goes. Matched on the lowercased alias
    # because that is the column's storage form, and never against the target,
    # whose self-alias is load-bearing.
    conn.execute("DELETE FROM person_aliases WHERE alias = ?", (from_name.strip().lower(),))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_people.py tests/test_voices.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/db.py tests/test_people.py
git commit -m "Delete the folded-in spelling on merge

The merge created the alias the owner did not want to see again - the contacts
list renders aliases, so merging left the old spelling on screen. The source's
own spelling is now deleted; aliases it had acquired still follow it, and the
target's self-alias is never touched because lookup depends on it."
```

---

### Task 6: Merge into a name you type

**Files:**
- Modify: `pipeline/dashboard.py:251-299` (`merge_many_people`)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `dashboard.merge_many_people(names: list[str], into: str) -> int` with the `into in selected` requirement removed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard.py`:

```python
def test_merge_into_a_brand_new_spelling(manifest):
    """The case that prompted this: Ru, Roo and Roe are all Ru Farrell, and
    none of the three is the name worth keeping."""
    for name in ("Ru", "Roo", "Roe"):
        db.add_person(manifest, name)
    manifest.commit()

    dashboard.merge_many_people(["Ru", "Roo", "Roe"], "Ru Farrell")

    survivors = [
        row["canonical"]
        for row in manifest.execute(
            "SELECT canonical FROM people WHERE canonical IN ('Ru','Roo','Roe','Ru Farrell')"
        )
    ]
    assert survivors == ["Ru Farrell"]


def test_merge_into_an_existing_person_outside_the_selection(manifest):
    db.add_person(manifest, "Ru")
    db.add_person(manifest, "Roo")
    db.add_person(manifest, "Ru Farrell")
    manifest.commit()

    dashboard.merge_many_people(["Ru", "Roo"], "Ru Farrell")

    assert manifest.execute(
        "SELECT COUNT(*) FROM people WHERE canonical IN ('Ru','Roo')"
    ).fetchone()[0] == 0


def test_merge_into_one_of_the_selected_names_still_works(manifest):
    """The path that already existed must not regress."""
    db.add_person(manifest, "Mike")
    db.add_person(manifest, "Michael")
    manifest.commit()

    dashboard.merge_many_people(["Mike", "Michael"], "Michael")

    assert manifest.execute(
        "SELECT 1 FROM people WHERE canonical = 'Michael'"
    ).fetchone() is not None


def test_a_new_target_inherits_the_first_role_among_the_selected(manifest):
    db.add_person(manifest, "Ru")
    db.add_person(manifest, "Roo", role="PM")
    manifest.commit()

    dashboard.merge_many_people(["Ru", "Roo"], "Ru Farrell")

    assert manifest.execute(
        "SELECT role FROM people WHERE canonical = 'Ru Farrell'"
    ).fetchone()["role"] == "PM"


def test_an_empty_target_is_rejected(manifest):
    db.add_person(manifest, "Ru")
    db.add_person(manifest, "Roo")
    manifest.commit()

    with pytest.raises(ValueError, match="name to keep"):
        dashboard.merge_many_people(["Ru", "Roo"], "   ")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dashboard.py -k "brand_new or outside_the_selection or first_role" -q`

Expected: `ValueError: The spelling to keep must be one of the selected names.`

- [ ] **Step 3: Write the minimal implementation**

In `pipeline/dashboard.py`, replace the validation block:

```python
    if len(selected) < 2:
        raise ValueError("Select at least two contact names to merge.")
    if target not in selected:
        raise ValueError("The spelling to keep must be one of the selected names.")
```

with:

```python
    if len(selected) < 2:
        raise ValueError("Select at least two contact names to merge.")
    if not target:
        raise ValueError("Enter the name to keep.")
```

Then replace the role-preservation block inside the loop:

```python
            # Keep useful role information when the chosen spelling had none.
            target_role = rows[target]["role"]
            if not target_role and rows[source]["role"]:
                db.add_person(conn, target, role=rows[source]["role"])
                rows[target] = {"canonical": target, "role": rows[source]["role"]}
```

with a single pass before the loop, and create the target when it is new:

```python
        # The target no longer has to be one of the selected names: folding "Ru",
        # "Roo" and "Roe" into the correct "Ru Farrell" is one answer, and
        # requiring the kept spelling to already exist made it three.
        if target not in rows:
            # First non-empty role among the selected, in the order the owner
            # ticked the checkboxes - there is no target row to backfill from.
            inherited = next(
                (rows[name]["role"] for name in selected if rows[name]["role"]), None
            )
            db.add_person(conn, target, role=inherited)
        else:
            inherited = rows[target]["role"] or next(
                (rows[name]["role"] for name in selected if rows[name]["role"]), None
            )
            if inherited and not rows[target]["role"]:
                db.add_person(conn, target, role=inherited)
```

and delete the old in-loop role block.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dashboard.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/dashboard.py tests/test_dashboard.py
git commit -m "Allow merging into a name that is not one of the selected

Folding \"Ru\", \"Roo\" and \"Roe\" into the correct \"Ru Farrell\" was
impossible in one step: the kept spelling had to already be one of the
selected names. A new target is created and inherits the first role among
the selected names."
```

---

### Task 7: The merge corrects the minutes

**Files:**
- Modify: `pipeline/dashboard.py` — `merge_people` (line 224), `merge_many_people` (line 251), `rename_person` (line 304)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `rename_minutes.rewrite_files` and `RewriteResult` from Task 1.
- Produces: `dashboard._apply_name_to_minutes(conn, meeting_ids: set[str], old: str, new: str) -> RewriteResult`, used by all three merge/rename entry points.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard.py`:

```python
def test_merge_rewrites_the_minutes_and_leaves_them_ready_to_reindex(
    manifest, tmp_path, monkeypatch
):
    """The owner's complaint: 82 meetings sat at speakers_resolved waiting for a
    7.8-min-per-meeting recompile that never ran, so the minutes still said the
    old name."""
    minutes = tmp_path / "brief.md"
    minutes.write_text("# Brief\n\nFaraz owns the rollout.\n", encoding="utf-8")
    meeting_id = "r" * 64
    make_meeting(
        manifest, meeting_id, "2026-08-12", status=db.INDEXED, minutes_path=str(minutes)
    )
    manifest.execute(
        "UPDATE meetings SET lightrag_doc_id = 'doc-1' WHERE id = ?", (meeting_id,)
    )
    manifest.execute(
        "INSERT INTO speakers (meeting_id, label, name, confidence) VALUES (?, 'SPEAKER_00', 'Faraz', 'confirmed')",
        (meeting_id,),
    )
    db.add_person(manifest, "Faraz")
    db.add_person(manifest, "Faraz Mateen")
    manifest.commit()

    dashboard.merge_many_people(["Faraz", "Faraz Mateen"], "Faraz Mateen")

    assert "Faraz Mateen owns the rollout." in minutes.read_text(encoding="utf-8")
    row = manifest.execute(
        "SELECT status, lightrag_doc_id FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    # minutes_compiled, not speakers_resolved: index re-embeds, nothing recompiles.
    assert row["status"] == db.MINUTES_COMPILED
    # The old id has to survive so index deletes the stale document first.
    assert row["lightrag_doc_id"] == "doc-1"


def test_a_meeting_whose_minutes_file_is_gone_stays_queued_for_recompile(
    manifest, tmp_path
):
    meeting_id = "s" * 64
    make_meeting(
        manifest,
        meeting_id,
        "2026-08-12",
        status=db.INDEXED,
        minutes_path=str(tmp_path / "never-written.md"),
    )
    manifest.execute(
        "INSERT INTO speakers (meeting_id, label, name, confidence) VALUES (?, 'SPEAKER_00', 'Faraz', 'confirmed')",
        (meeting_id,),
    )
    db.add_person(manifest, "Faraz")
    db.add_person(manifest, "Faraz Mateen")
    manifest.commit()

    dashboard.merge_many_people(["Faraz", "Faraz Mateen"], "Faraz Mateen")

    assert manifest.execute(
        "SELECT status FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()["status"] == db.SPEAKERS_RESOLVED


def test_the_transcript_is_never_touched(manifest, tmp_path):
    minutes = tmp_path / "brief.md"
    minutes.write_text("Faraz spoke.\n", encoding="utf-8")
    transcript = tmp_path / "verbatim.md"
    transcript.write_text("Faraz spoke.\n", encoding="utf-8")
    before = transcript.read_bytes()
    meeting_id = "t" * 64
    make_meeting(
        manifest,
        meeting_id,
        "2026-08-12",
        status=db.INDEXED,
        minutes_path=str(minutes),
        transcript_path=str(transcript),
    )
    manifest.execute(
        "INSERT INTO speakers (meeting_id, label, name, confidence) VALUES (?, 'SPEAKER_00', 'Faraz', 'confirmed')",
        (meeting_id,),
    )
    db.add_person(manifest, "Faraz")
    db.add_person(manifest, "Faraz Mateen")
    manifest.commit()

    dashboard.merge_many_people(["Faraz", "Faraz Mateen"], "Faraz Mateen")

    assert transcript.read_bytes() == before
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dashboard.py -k "rewrites_the_minutes or gone_stays_queued or never_touched" -q`

Expected: the first two fail — the minutes file still says "Faraz" and the status is `speakers_resolved`.

- [ ] **Step 3: Write the minimal implementation**

Add `from pipeline import rename_minutes` to `pipeline/dashboard.py`'s imports, then add above `merge_people` (line 224):

```python
def _apply_name_to_minutes(
    conn: sqlite3.Connection, meeting_ids: set[str], old: str, new: str
) -> rename_minutes.RewriteResult:
    """Correct a renamed person inside the already-compiled minutes.

    Called instead of leaving the meeting queued for a full recompile. A
    recompile is authoritative and costs ~7.8 minutes per meeting; 82 meetings
    sat waiting for one that never ran, so the minutes said the old name for as
    long as it took the owner to notice. Substitution is exact and instant.

    A meeting whose minutes file is missing keeps its refresh queued: there is
    nothing to substitute into, and silently skipping it would report success
    for a document still carrying the old spelling.
    """
    if not meeting_ids:
        return rename_minutes.RewriteResult()

    placeholders = ",".join("?" for _ in meeting_ids)
    paths = {
        row["id"]: Path(row["minutes_path"])
        for row in conn.execute(
            f"SELECT id, minutes_path FROM meetings WHERE id IN ({placeholders})",
            tuple(meeting_ids),
        )
        if row["minutes_path"]
    }
    result = rename_minutes.rewrite_files(paths, old, new)

    # Everything that was rewritten only needs re-embedding, so it goes back to
    # minutes_compiled rather than speakers_resolved. lightrag_doc_id is left
    # intact deliberately: the index stage needs the old id to delete the stale
    # search document before inserting the corrected one.
    corrected = sorted(set(paths) - set(result.missing))
    if corrected:
        marks = ",".join("?" for _ in corrected)
        conn.execute(
            f"""
            UPDATE meetings SET status = ?, error = NULL, updated_at = ?
            WHERE id IN ({marks}) AND status IN (?, ?)
            """,
            (
                db.MINUTES_COMPILED,
                config.now_iso(),
                *corrected,
                db.MINUTES_COMPILED,
                db.INDEXED,
            ),
        )
    return result
```

In each of `merge_people`, `merge_many_people` and `rename_person`, call it after the existing rewrite work and before the existing `db.queue_minutes_refresh(conn, affected_meetings)`. In `merge_many_people` the call goes inside the per-source loop, using that source's name:

```python
            _apply_name_to_minutes(conn, affected_meetings, source, target)
```

`queue_minutes_refresh` stays where it is — it moves everything to `speakers_resolved` first, and `_apply_name_to_minutes` moves back only what it actually corrected, leaving genuinely missing files queued.

Reorder so `queue_minutes_refresh` runs *before* `_apply_name_to_minutes`. Confirm `Path`, `sqlite3` and `config` are imported in `dashboard.py`; add whichever are missing.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dashboard.py tests/test_people.py tests/test_voices.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/dashboard.py tests/test_dashboard.py
git commit -m "Correct compiled minutes when a person is merged

Merges queued a minutes refresh that nothing drained - 82 meetings sat at
speakers_resolved with the old spelling still in their minutes. The name is now
substituted in the compiled markdown and the meeting returns to
minutes_compiled so index re-embeds it, keeping lightrag_doc_id so the stale
search document is deleted first. Meetings whose minutes file is gone stay
queued for a real recompile."
```

---

### Task 8: `pipeline people --repair-merges`

**Files:**
- Modify: `pipeline/dashboard.py` (add `repair_merges`), `pipeline/cli.py:769-808` (`cmd_people`) and `pipeline/cli.py:1025-1034` (parser)
- Test: `tests/test_people.py`

**Interfaces:**
- Consumes: `rename_minutes.rewrite_files`, `db.resolve_merged_name`, `_apply_name_to_minutes`.
- Produces: `dashboard.repair_merges() -> dict[str, int]` with keys `suggestions_rewritten`, `suggestions_cleared`, `aliases_removed`, `minutes_rewritten`, `still_queued`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_people.py`:

```python
def test_repair_rewrites_a_resolvable_ghost_suggestion(manifest):
    db.add_person(manifest, "Faraz Mateen")
    db.record_merged_name(manifest, "Faraz", "Faraz Mateen")
    manifest.execute(
        """
        INSERT INTO voice_clusters
            (id, size, total_speech, best_canonical, best_score,
             next_canonical, next_score, band, created_at)
        VALUES ('c1', 1, 10.0, 'Faraz', 0.8, NULL, NULL, 'review', '2026-08-12T09:00:00')
        """
    )
    manifest.commit()

    report = dashboard.repair_merges()

    assert report["suggestions_rewritten"] >= 1
    assert manifest.execute(
        "SELECT best_canonical FROM voice_clusters WHERE id = 'c1'"
    ).fetchone()["best_canonical"] == "Faraz Mateen"


def test_repair_clears_an_unresolvable_ghost_suggestion(manifest):
    """A suggestion naming nobody who exists is not a weaker suggestion."""
    manifest.execute(
        """
        INSERT INTO voice_clusters
            (id, size, total_speech, best_canonical, best_score,
             next_canonical, next_score, band, created_at)
        VALUES ('c2', 1, 10.0, 'Vanished', 0.8, NULL, NULL, 'review', '2026-08-12T09:00:00')
        """
    )
    manifest.commit()

    report = dashboard.repair_merges()

    assert report["suggestions_cleared"] >= 1
    assert manifest.execute(
        "SELECT best_canonical FROM voice_clusters WHERE id = 'c2'"
    ).fetchone()["best_canonical"] is None


def test_repair_removes_folded_in_aliases_but_keeps_self_aliases(manifest):
    db.add_person(manifest, "Ru Farrell", aliases=["roo"])
    manifest.commit()

    report = dashboard.repair_merges()

    assert report["aliases_removed"] == 1
    assert manifest.execute(
        "SELECT 1 FROM person_aliases WHERE alias = 'roo'"
    ).fetchone() is None
    assert manifest.execute(
        "SELECT 1 FROM person_aliases WHERE alias = 'ru farrell'"
    ).fetchone() is not None


def test_repair_is_idempotent(manifest):
    db.add_person(manifest, "Ru Farrell", aliases=["roo"])
    manifest.commit()

    dashboard.repair_merges()
    second = dashboard.repair_merges()

    assert second == {
        "suggestions_rewritten": 0,
        "suggestions_cleared": 0,
        "aliases_removed": 0,
        "minutes_rewritten": 0,
        "still_queued": 0,
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_people.py -k repair -q`

Expected: `AttributeError: module 'pipeline.dashboard' has no attribute 'repair_merges'`.

- [ ] **Step 3: Write the minimal implementation**

Add to `pipeline/dashboard.py`:

```python
def repair_merges() -> dict[str, int]:
    """Clean up damage left by merges that predate the current merge path.

    Deliberately owner-invoked rather than an automatic migration inside
    init_db(): it rewrites minutes files on disk, and that must never happen as
    a side effect of opening the database.

    Ordered like a live merge, and for the same reason - the ghost repair reads
    the mapping the alias cleanup removes, so it has to run first.
    """
    db.init_db()
    report = {
        "suggestions_rewritten": 0,
        "suggestions_cleared": 0,
        "aliases_removed": 0,
        "minutes_rewritten": 0,
        "still_queued": 0,
    }
    with db.connect() as conn:
        # 1. Ghost suggestions: names the matcher offers that are not people.
        for table in ("speaker_matches", "voice_clusters"):
            for column in ("best_canonical", "next_canonical"):
                ghosts = [
                    row[0]
                    for row in conn.execute(
                        f"""
                        SELECT DISTINCT {column} FROM {table}
                        WHERE {column} IS NOT NULL
                          AND {column} NOT IN (SELECT canonical FROM people)
                        """
                    )
                ]
                for ghost in ghosts:
                    absorbed_by = db.resolve_merged_name(conn, ghost)
                    if absorbed_by:
                        cursor = conn.execute(
                            f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                            (absorbed_by, ghost),
                        )
                        report["suggestions_rewritten"] += cursor.rowcount or 0
                    else:
                        score = "best_score" if column == "best_canonical" else "next_score"
                        cursor = conn.execute(
                            f"UPDATE {table} SET {column} = NULL, {score} = NULL WHERE {column} = ?",
                            (ghost,),
                        )
                        report["suggestions_cleared"] += cursor.rowcount or 0

        # 2. Minutes still carrying an old spelling, for every tombstone.
        for row in conn.execute("SELECT old_spelling, canonical FROM merged_names"):
            affected = {
                m["meeting_id"]
                for m in conn.execute(
                    "SELECT DISTINCT meeting_id FROM speakers WHERE name = ?",
                    (row["canonical"],),
                )
            }
            result = _apply_name_to_minutes(
                conn, affected, row["old_spelling"], row["canonical"]
            )
            report["minutes_rewritten"] += result.rewritten
            report["still_queued"] += len(result.missing)

        # 3. Folded-in aliases, last, now that step 1 no longer needs them.
        cursor = conn.execute(
            "DELETE FROM person_aliases WHERE alias != LOWER(canonical)"
        )
        report["aliases_removed"] += cursor.rowcount or 0
    return report
```

In `pipeline/cli.py`, add to `cmd_people` before the `if args.merge:` block:

```python
        if args.repair_merges:
            report = dashboard.repair_merges()
            print("Repaired merges left behind by earlier versions:")
            print(f"  matcher suggestions rewritten : {report['suggestions_rewritten']}")
            print(f"  matcher suggestions cleared   : {report['suggestions_cleared']}")
            print(f"  folded-in aliases removed     : {report['aliases_removed']}")
            print(f"  minutes files corrected       : {report['minutes_rewritten']}")
            if report["still_queued"]:
                print(
                    f"  {report['still_queued']} meeting(s) have no minutes file on disk "
                    "and stay queued for a full recompile."
                )
            return 0
```

Add `from pipeline import dashboard` to `cli.py` if absent, and register the flag beside the others (line 1030):

```python
    p_people.add_argument(
        "--repair-merges", action="store_true",
        help="clean up ghost suggestions, folded-in aliases and stale minutes "
             "left by merges made before these were handled",
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_people.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/dashboard.py pipeline/cli.py tests/test_people.py
git commit -m "Add people --repair-merges for damage from earlier merges

Rewrites or clears matcher suggestions naming people who no longer exist,
corrects minutes still carrying an old spelling, and removes folded-in
aliases - in that order, because the suggestion repair reads the mapping the
alias cleanup deletes. Owner-invoked rather than an init_db migration: it
writes files on disk."
```

---

### Task 9: The UI offers a name to type

**Files:**
- Modify: `pipeline/static/index.html:237-248` (suggestion panel), `pipeline/static/app.js` (`renderPeopleSuggestion`, `acceptPeopleSuggestion`, `setupEventListeners`), `pipeline/static/style.css`
- Test: `tests/test_dashboard_ui_contract.py`

**Interfaces:**
- Consumes: `POST /api/people/merge-many` with `{names, into}`, which Task 6 made accept an arbitrary `into`.
- Produces: control ids `people-suggestion-rename`, `people-suggestion-name`, `people-suggestion-rename-confirm`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dashboard_ui_contract.py`:

```python
def test_the_merge_suggestion_offers_a_corrected_spelling():
    """"Ru" and "Ru Farrell" are the same person and neither shown spelling is
    the one worth keeping. Answering yes to the wrong name is the whole problem."""
    page = _index()
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    for control in ("people-suggestion-rename", "people-suggestion-name",
                    "people-suggestion-rename-confirm"):
        assert control in page.by_id

    assert "people-suggestion-name" in page.labels_for
    assert "function openSuggestionRename()" in script
    assert "function acceptPeopleSuggestionAs(" in script
    # Pre-filled with the longest spelling: the fullest name is the best guess
    # at what the owner actually wants to keep.
    assert "longestName" in script
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dashboard_ui_contract.py -k corrected_spelling -q`

Expected: `AssertionError` on the first missing control id.

- [ ] **Step 3: Write the minimal implementation**

In `index.html`, replace the actions block (lines 243-246):

```html
            <div class="people-suggestion-actions">
              <button type="button" class="btn-action btn-sm" id="people-suggestion-yes">Yes, merge</button>
              <button type="button" class="btn-outline btn-sm" id="people-suggestion-rename">Yes, but call them…</button>
              <button type="button" class="btn-outline btn-sm" id="people-suggestion-no">No, different people</button>
            </div>
            <div class="people-suggestion-rename" id="people-suggestion-rename-row" hidden>
              <label for="people-suggestion-name">Keep this spelling</label>
              <input id="people-suggestion-name" type="text" autocomplete="off" />
              <button type="button" class="btn-action btn-sm" id="people-suggestion-rename-confirm">Merge into this name</button>
            </div>
```

In `app.js`, add beside `acceptPeopleSuggestion`:

```javascript
function openSuggestionRename() {
  const panel = $("people-suggestions");
  const names = JSON.parse(panel.dataset.names || "[]");
  // The longest spelling first: the fullest name is the best guess at the one
  // worth keeping, which is the "Ru" vs "Ru Farrell" case this exists for.
  const longestName = names.slice().sort((a, b) => b.length - a.length)[0] || "";
  $("people-suggestion-name").value = longestName;
  $("people-suggestion-rename-row").hidden = false;
  $("people-suggestion-name").focus();
}

async function acceptPeopleSuggestionAs(target) {
  const panel = $("people-suggestions");
  const names = JSON.parse(panel.dataset.names || "[]");
  const chosen = (target || "").trim();
  if (!chosen) {
    showToast("Enter the name to keep.", "error");
    return;
  }
  $("people-suggestion-yes").disabled = true;
  $("people-suggestion-no").disabled = true;
  const merged = await mergeManyPersonProfiles(names, chosen);
  if (merged) {
    state.peopleSuggestions.shift();
    $("people-suggestion-rename-row").hidden = true;
    renderPeopleSuggestion();
  } else {
    $("people-suggestion-yes").disabled = false;
    $("people-suggestion-no").disabled = false;
  }
}
```

Register both in `setupEventListeners` beside line 284:

```javascript
  $("people-suggestion-rename").addEventListener("click", openSuggestionRename);
  $("people-suggestion-rename-confirm").addEventListener("click", () =>
    acceptPeopleSuggestionAs($("people-suggestion-name").value),
  );
```

In `renderPeopleSuggestion`, hide the row whenever a new suggestion is drawn, next to the two `disabled = false` lines:

```javascript
  $("people-suggestion-rename-row").hidden = true;
```

In `style.css`, beside the existing `.people-suggestion-actions` rule:

```css
/* Revealed only after "Yes, but call them…" - a text input shown next to two
   one-click answers invites typing when clicking is what was wanted. */
.people-suggestion-rename {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
}

.people-suggestion-rename label {
  font-size: 0.68rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted, #6b7280);
}

.people-suggestion-rename input {
  border: 1px solid var(--rule);
  background: transparent;
  color: var(--ink);
  padding: 6px 10px;
  min-width: 200px;
  flex: 1 1 200px;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dashboard_ui_contract.py tests/test_dashboard_controls_js.py -q`

Expected: all pass. `node --check pipeline/static/app.js` must also exit 0 — `test_app_js_parses` covers it.

- [ ] **Step 5: Commit**

```bash
git add pipeline/static/index.html pipeline/static/app.js pipeline/static/style.css tests/test_dashboard_ui_contract.py
git commit -m "Offer a corrected spelling on the merge suggestion

\"Do these 2 names describe the same person? Ru, Ru Farrell\" had two answers
and neither kept a name the owner had chosen. A third reveals a text input
pre-filled with the longest spelling."
```

---

### Task 10: Run the repair on the live manifest

Not a code change. The one action that turns the previous nine tasks into a fixed dashboard, kept as its own task because it writes to real data and wants its own before-and-after.

- [ ] **Step 1: Back up the manifest**

```bash
uv run pipeline backup
```

- [ ] **Step 2: Record the before state**

```bash
./.venv/Scripts/python.exe -c "
import sqlite3
c=sqlite3.connect('db/manifest.db')
q=lambda s: c.execute(s).fetchone()[0]
print('ghost suggestions:', sum(
    q(f\"SELECT COUNT(*) FROM {t} WHERE {col} IS NOT NULL AND {col} NOT IN (SELECT canonical FROM people)\")
    for t in ('speaker_matches','voice_clusters') for col in ('best_canonical','next_canonical')))
print('folded-in aliases:', q('SELECT COUNT(*) FROM person_aliases WHERE alias != LOWER(canonical)'))
print('stale at speakers_resolved:', q(\"SELECT COUNT(*) FROM meetings WHERE status='speakers_resolved'\"))
"
```

Expected, from the 2026-08-26 measurement: 78 ghost suggestions, 22 folded-in aliases, 82 stale meetings.

- [ ] **Step 3: Run the repair**

```bash
uv run pipeline people --repair-merges
```

- [ ] **Step 4: Confirm all three counts reached zero**

Re-run Step 2's command. Expected: `ghost suggestions: 0`, `folded-in aliases: 0`, and `stale at speakers_resolved` reduced to only meetings whose minutes file is genuinely missing — the number the repair printed as "stay queued for a full recompile".

- [ ] **Step 5: Re-index the corrected minutes**

```bash
uv run pipeline index
```

- [ ] **Step 6: Commit nothing, report the numbers**

No code changed. Report the before/after counts and how many meetings still need a real recompile.

---

## Self-Review

**Spec coverage.** Section A → Task 6 (backend) and Task 9 (UI). Section B → Tasks 2, 3, 4. Section C → Task 5. Section D → Tasks 1, 7. Section E ordering → enforced by Task 3's tombstone-first body and Task 7's placement of `_apply_name_to_minutes`. "One-time repairs" → Tasks 8 and 10. Every spec test bullet maps to a named test above.

**Two spec bullets deliberately not given their own task:** "a merge that fails validation leaves the database and the minutes file untouched" is covered by Task 6's `test_an_empty_target_is_rejected` combined with `_apply_name_to_minutes` running after validation; and `confirm()` creating a genuinely new name is Task 4's `test_confirming_a_genuinely_new_name_still_creates_the_person`.

**Type consistency check.** `RewriteResult` is defined once (Task 1) with `rewritten`/`unchanged`/`missing` and used with those names in Tasks 7 and 8. `db.record_merged_name` / `db.resolve_merged_name` are defined in Task 2 and called with the same signatures in Tasks 3, 4 and 8. `_apply_name_to_minutes(conn, meeting_ids, old, new)` is defined in Task 7 and called with that argument order in Task 8. `repair_merges()` returns the same five keys in its definition, its CLI print block, and Task 8's idempotency assertion.

**Known risk not eliminated.** Task 7 writes files inside a SQL transaction, so a crash between the file write and the commit leaves corrected minutes with an un-merged database. The repair in Task 8 is idempotent and re-runnable, which is the mitigation rather than a fix; a true fix needs the write staged outside the transaction and is out of scope here.
