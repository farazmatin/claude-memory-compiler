# Tightening the speaker merge

**Date:** 2026-08-26
**Status:** approved, not yet implemented

## Why

The owner's stated goal is to resolve every speaker. The merge flow actively
works against that today: it is possible to merge a name and then be offered the
merged-away spelling again, accept it, and silently undo the merge.

Three defects, all confirmed against the live manifest on 2026-08-26:

1. **Merging leaves the matcher suggesting names that no longer exist.**
   `voices.merge_people` rewrites `speaker_matches.resolved_as` but never
   `best_canonical` / `next_canonical`, and never touches `voice_clusters` at
   all. 78 rows still suggest `"Faraz"`, merged into `"Faraz Mateen"` earlier the
   same day.

2. **Accepting a stale suggestion resurrects the merged-away person.**
   `voices.confirm()` calls `db.add_person(conn, canonical)`
   (`pipeline/voices.py:478`), so confirming a card that reads "✓ Confirm as
   Faraz" recreates `Faraz` as a separate person. This is the treadmill: merge,
   get re-offered the dead spelling, accept, back to two people.

3. **Minutes go stale and stay stale.** Merges call
   `db.queue_minutes_refresh`, which moves affected meetings back to
   `speakers_resolved`. 82 meetings sit there now; the last `minutes` stage run
   was the previous evening. The queue works, nothing drains it.

A fourth problem is a missing capability rather than a defect:
`merge_many_people` rejects any target that is not one of the selected names, so
folding `"Ru"`, `"Roo"`, `"Roe"` into the correct `"Ru Farrell"` is impossible in
one step.

## Decisions taken

Two questions were put to the owner; both answers are binding on this design.

**Minutes are updated by textual rewrite, not by LLM recompile.** A full
recompile of 82 meetings costs roughly 7.8 minutes each — about 10.7 hours — and
burns subscription LLM quota at a moment when Antigravity is quota-locked and the
chain falls through to Codex. A word-boundary substitution in the already-compiled
markdown costs seconds and cannot rephrase content the owner has already read.
The accepted limitation: only literal occurrences are corrected.

**Folded-in spellings are deleted, not retained as aliases.** The owner was told
that deleting them means the next transcript saying "Roo" creates a brand-new
person to merge again, and chose deletion anyway. Self-aliases are exempt — see
section C.

## A. Merge into a typed name

`dashboard.merge_many_people(names, into)` currently raises when
`into not in selected`. That rule is removed and replaced with three accepted
shapes for `into`:

| `into` is | Behaviour |
|---|---|
| one of the selected names | unchanged from today |
| a spelling that does not exist yet | create the person, fold all selected into it |
| an existing person not among the selected | fold all selected into that person |

Validation that stays: at least two selected names, every selected name must
exist, `into` must be non-empty after stripping.

Role preservation generalises. Today the retained row's role is backfilled from a
source when the target had none; with a brand-new target there is no row to
backfill from, so the first non-empty role among the selected names — in the
order the caller supplied them, which is the order the owner ticked the
checkboxes — is carried onto the newly created person.

Return value stays "number of speaker rows rewritten" so the existing toast copy
remains correct.

### UI

The merge-suggestion panel gains a third action, **"Yes, but call them…"**,
which reveals a text input pre-filled with the longest of the suggested
spellings — the longest is the best guess at the fullest name, which is what the
owner wants in the `"Ru" / "Ru Farrell"` case. The existing two buttons keep
their current behaviour.

The people-manager merge modal's target control changes from a `<select>`
constrained to the selection into a select plus a free-text override, matching
the pattern already used by the voice-name modal (choose existing / add new).

## B. A merge leaves no ghost suggestions

`voices.merge_people(conn, source, target)` additionally rewrites, for both
`speaker_matches` and `voice_clusters`:

- `best_canonical`: `source` → `target`
- `next_canonical`: `source` → `target`

`voices.confirm()` gains a guard against resurrecting a dead name: when the
requested canonical is absent from `people` but resolves through
`person_aliases`, it confirms the alias's current canonical instead. This closes
the treadmill independently of section B's rewrite, so a stale card cached in a
browser tab cannot undo a merge either.

A one-time repair covers rows created before this change: for every
`best_canonical` / `next_canonical` in both tables that is not in `people`,
resolve it through `person_aliases` and rewrite it; where it does not resolve,
set it to `NULL`. `NULL` is correct rather than lossy — a suggestion naming a
person who no longer exists is not a weaker suggestion, it is not a suggestion.

The repair must run **before** any alias deletion from section C, or the mapping
it depends on is already gone. Ordering is specified in section E, and the
one-time invocation is specified under "One-time repairs".

## C. Folded-in spellings are deleted

`db.merge_person` and `voices.merge_people` both call
`add_person(conn, into, aliases=[from_name])` today — the merge itself creates
the alias the owner does not want. Both change to `add_person(conn, into)`
followed by deleting the source spelling's alias row.

**Self-aliases are exempt, and this is load-bearing.** `db.add_person` registers
every canonical as an alias of itself "so lookup has a single code path"
(`pipeline/db.py:699`). Of 331 alias rows in the live manifest, 309 are
self-aliases and only 22 are genuine folded-in spellings. Deleting a self-alias
would break name resolution for that person entirely. The delete therefore
targets exactly `lower(source)` and never `lower(target)`.

A one-time cleanup removes the 22 existing folded-in rows, identified as
`alias != lower(canonical)`. It runs under the flag described in "One-time
repairs" below, after section B's ghost repair has consumed the mapping.

## D. Minutes rewritten as text

New module `pipeline/rename_minutes.py`, one public function:

```python
@dataclass(frozen=True)
class RewriteResult:
    rewritten: int      # minutes files actually changed on disk
    unchanged: int      # files present but containing no occurrence
    missing: list[str]  # meeting ids whose minutes file is gone from disk


def rewrite_name(meeting_ids: Iterable[str], old: str, new: str) -> RewriteResult
```

It rewrites each affected meeting's `minutes_path` file in place. The three
counts are separate because they mean different things to the caller: `missing`
is the only one that leaves work outstanding, and a bare integer could not say
so — see the status-handling note below.

**Matching.** Word-boundary substitution that also handles possessives, so
`Faraz's` becomes `Faraz Mateen's`. `\b` alone is insufficient: the pattern must
not fire inside a longer word, which is the failure that would turn **Ruth**
into *Ru Farrellth* when merging `Ru`. The guard is a lookaround on word
characters and apostrophes rather than `\b`, for the same reason `ingest.py`
avoids `\b` — a bare `\b` does not fire beside every character that matters here.

**Scope.** Only files named by `meetings.minutes_path`. Transcripts are the
immutable source and are never touched; the whole repeatable-compile property
depends on that.

**Status handling.** This replaces `queue_minutes_refresh`'s effect for the
merge path. After a successful rewrite the meeting is left at
`minutes_compiled`, not moved back to `speakers_resolved`, so the `index` stage
re-embeds it without a 7.8-minute recompile. `lightrag_doc_id` is preserved so
`index` deletes the stale search document before inserting the corrected one —
the same reasoning `queue_minutes_refresh` already documents.

A meeting whose minutes file is missing from disk is left at
`speakers_resolved` — queued for a real recompile — rather than silently
skipped, and is named in `RewriteResult.missing` so the caller can say how many
still need one.

## One-time repairs

Sections B, C and D each leave existing damage that new code alone does not
clean up: 78 ghost suggestions, 22 folded-in alias rows, and 82 meetings stuck
at `speakers_resolved`. All three are exposed together behind a single new flag
on the existing command:

```bash
uv run pipeline people --repair-merges
```

It runs the three repairs in section E's order, prints what each one changed,
and is idempotent — running it twice is a no-op the second time. It is a
deliberate, owner-invoked action rather than an automatic migration inside
`db.init_db()`: it rewrites minutes files on disk, and that must never happen as
a side effect of opening the database.

## E. Order of operations inside one merge

Load-bearing sequence. The alias delete is last because step 2 needs the mapping
it removes.

```
1. collect affected meeting ids
2. rewrite matcher suggestions        (B)  needs person_aliases intact
3. move voice samples, speakers, resolved_as   (exists)
4. rewrite entities, relations, commitments,
   decisions, open_questions          (exists)
5. rewrite minutes markdown           (D)
6. delete the source's alias row      (C)  last
7. delete the source person           (exists)
8. leave the meeting at minutes_compiled for re-index, not recompile
```

All of it runs inside the single existing transaction per merge, so a failure
part-way leaves neither half-merged rows nor a rewritten file paired with an
un-merged database. Because step 5 touches the filesystem and cannot be rolled
back by SQLite, it is ordered after every step that can still raise on
validation and before only the two deletes, which cannot fail on valid input.

## Testing

TDD throughout. Each item below is a failing test before it is code.

**A — typed target**
- merging two existing names into a brand-new spelling creates it and folds both
- merging into an existing person not in the selection folds into that person
- merging into one of the selected names still behaves as it does today
- an empty or whitespace-only target is rejected
- a new target inherits the first non-empty role among the selected names

**B — no ghosts**
- after a merge, no `speaker_matches` or `voice_clusters` row names the source in
  `best_canonical` or `next_canonical`
- `confirm()` on a merged-away name that resolves through an alias confirms the
  current canonical and does not recreate the old person
- the repair maps a resolvable ghost through aliases and NULLs an unresolvable one

**C — aliases deleted**
- a merge leaves no `person_aliases` row for the source spelling
- a merge does **not** delete the target's self-alias
- the cleanup removes folded-in rows and leaves all 309 self-aliases intact

**D — minutes rewritten**
- the old name is replaced in the minutes file
- a possessive is rewritten correctly
- **a longer word containing the old name is left alone** (`Ruth` survives
  merging `Ru`)
- the transcript file is byte-identical afterwards
- status ends at `minutes_compiled` with `lightrag_doc_id` intact
- a meeting whose minutes file is absent stays queued for recompile and is counted

**E — ordering and repairs**
- a merge that fails validation leaves the database and the minutes file
  untouched
- `--repair-merges` run twice changes nothing the second time

## Out of scope

- Resolving the 155 pending speaker decisions. This makes each merge clean; it
  does not make the decisions for the owner.
- Any change to how the minutes template phrases names, which would require a
  `TEMPLATE_VERSION` bump and a full recompile the owner has explicitly declined.
- Re-running `index` automatically. The merge leaves meetings ready for it; when
  it runs stays the owner's call.
