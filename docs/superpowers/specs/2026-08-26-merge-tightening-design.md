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
requested canonical is absent from `people` but is recorded as a merged-away
spelling, it confirms the person who absorbed it instead. This closes the
treadmill independently of section B's rewrite, so a stale card cached in a
browser tab cannot undo a merge either.

**The guard needs a tombstone, not an alias.** An earlier draft resolved the
name through `person_aliases`, which section C deletes — the two sections
cancelled out. A new table carries the mapping instead:

```sql
CREATE TABLE IF NOT EXISTS merged_names (
    old_spelling TEXT PRIMARY KEY,   -- exactly as it was, not lowercased
    canonical    TEXT NOT NULL,      -- who absorbed it, at merge time
    merged_at    TEXT NOT NULL,
    FOREIGN KEY (canonical) REFERENCES people(canonical) ON DELETE CASCADE
);
```

It is written on every merge and read only by this guard and by
`--repair-merges`. Nothing renders it, so premerged spellings still appear
nowhere in the UI — the owner's requirement is about what is visible, and a
tombstone is not. It also answers "what did I fold into this person?" without
putting the old spellings back on screen.

A merge whose target is itself later merged leaves a tombstone pointing at a
name that no longer exists. The guard therefore follows the chain, with a visited
set so a cycle cannot loop, and falls through to refusing the confirm if the
chain ends nowhere.

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

**Matching.** `\b` plus `re.escape`, case-sensitive, which already handles both
of the obvious cases: `Faraz's` becomes `Faraz Mateen's` because `z`/`'` is a
boundary, and `Ruth` survives merging `Ru` because `u`/`t` is not. An earlier
draft of this spec claimed `\b` was insufficient and named the `Ruth` case as the
hazard; that was wrong, and the reasoning is corrected here because the real
hazard is different and worse.

**The real hazard is doubling.** When the new name begins with the old one —
exactly the `Faraz` → `Faraz Mateen` case that prompted this work — a document
that already says "Faraz Mateen" contains a `\bFaraz\b` match at the start of the
correct name, and substituting it yields **"Faraz Mateen Mateen"**. Because
minutes for different meetings resolved at different times, the same document can
hold both spellings, so this is the common case rather than a corner.

The guard is a negative lookahead for the remainder of the new name, applied only
when `new` starts with `old`:

```python
suffix = new[len(old):] if new.lower().startswith(old.lower()) else ""
tail = f"(?!{re.escape(suffix)})" if suffix else ""
pattern = re.compile(rf"\b{re.escape(old)}\b{tail}")
```

For `old="Faraz"`, `new="Faraz Mateen"` this is `\bFaraz\b(?! Mateen)`: it skips
the already-correct occurrences and rewrites only the bare ones. It also makes
the rewrite idempotent, which is what lets `--repair-merges` be safe to re-run.

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
2. record the tombstone               (B)  before the alias goes
3. rewrite matcher suggestions        (B)
4. move voice samples, speakers, resolved_as   (exists)
5. rewrite entities, relations, commitments,
   decisions, open_questions          (exists)
6. rewrite minutes markdown           (D)
7. delete the source's alias row      (C)  last
8. delete the source person           (exists)
9. leave the meeting at minutes_compiled for re-index, not recompile
```

The tombstone is written before anything is destroyed, so a failure anywhere
after it leaves a recoverable record of what was intended.

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
- a merge writes a `merged_names` row for the source spelling
- `confirm()` on a merged-away name confirms the absorbing person and does **not**
  recreate the old one
- `confirm()` follows a two-hop chain (A merged into B, B merged into C) to C
- `confirm()` refuses rather than looping when tombstones form a cycle
- `confirm()` still creates a genuinely new person when the name is unknown to
  both `people` and `merged_names`
- the repair maps a resolvable ghost through the tombstone and NULLs an
  unresolvable one

**C — aliases deleted**
- a merge leaves no `person_aliases` row for the source spelling
- a merge does **not** delete the target's self-alias
- the cleanup removes folded-in rows and leaves all 309 self-aliases intact

**D — minutes rewritten**
- the old name is replaced in the minutes file
- a possessive is rewritten correctly (`Faraz's` → `Faraz Mateen's`)
- **a longer word containing the old name is left alone** (`Ruth` survives
  merging `Ru`)
- **an already-correct occurrence is not doubled** (a document holding both
  "Faraz" and "Faraz Mateen" ends with two "Faraz Mateen", never a
  "Faraz Mateen Mateen")
- rewriting the same file twice changes it only the first time
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
