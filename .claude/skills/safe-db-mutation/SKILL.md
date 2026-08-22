---
name: safe-db-mutation
description: Protocol for changing data in db/manifest.db — tagged backup, dry-run diff, apply, then verify cascades and row deltas. Use before any UPDATE, DELETE, merge, backfill, or bulk correction against the real manifest, and before running any pipeline command that mutates it (retry, speakers --all, minutes --recompile, people --merge, voices).
---

# Safe manifest mutation

`db/manifest.db` holds the only record of work that cost real money and hours of
CPU: transcripts that came from paid GPU time, minutes that came from paid LLM
calls, and human-confirmed speaker names that exist nowhere else. Audio is
frequently already deleted by the time you touch it.

**Four steps, in order, every time. No exceptions for "small" changes** — the
smallest change in this repo's history was a 33-row `UPDATE` that rewrote the date
of three quarters of the corpus.

## 1. Tagged backup

```bash
cd claude-memory-compiler
cp db/manifest.db "$TEMP/manifest.db.pre-<what-you-are-about-to-do>.bak"
```

Name it for the operation, not the date. `manifest.db.pre-merge.bak` tells you what
to restore and why; `manifest.db.bak.3` does not. Real ones from this repo:
`pre-datefix`, `pre-merge`, `pre-backfill`, `pre-junk-delete`, `pre-enroll`.

Restoring is `cp` in the other direction. Say the backup path out loud in your
report so the user can undo you without asking.

## 2. Dry run, and show the diff

Compute every change and print it **before** writing anything. The user should be
able to read the diff and veto it.

```python
# Pattern: same code path, one flag.
apply = "--apply" in sys.argv
changes = []
for row in conn.execute("SELECT ..."):
    new = compute(row)
    if new != current(row):
        changes.append((row["id"], current(row), new))

print(f"{len(changes)} rows to change\n")
for _id, old, new in changes:
    print(f"  {old}  ->  {new}")

if apply:
    for _id, _old, new in changes:
        conn.execute("UPDATE ... WHERE id = ?", (new, _id))
    conn.commit()
    print(f"\nAPPLIED {len(changes)} updates")
else:
    print("\n(dry run - pass --apply to write)")
```

The dry run is where you catch that your logic is wrong. A date backfill here
looked correct until the preview showed every recovered time landing exactly on the
hour — which exposed a second bug (a hyphen being read as a range instead of a
minute separator) that would otherwise have been written into 33 rows.

## 3. Sanity-check the *shape* of the result, not just the count

"33 rows updated" proves nothing. Ask what the data should look like if you are
right, then check that.

The date backfill's real proof was not the row count — it was that meetings then
fell on Aug 6, 7, 10, 11, 12, 13, 14, 16 **and nothing on the 8th, 9th or 15th,
which were weekends.** No plausible bug produces that pattern.

Find the equivalent for your change: a distribution, a ratio, an invariant, a
"this should now be impossible" query.

## 4. Verify cascades and orphans after any DELETE

`PRAGMA foreign_keys` is ON here and most child tables use `ON DELETE CASCADE`,
but confirm rather than assume — the pragma is per-connection.

```python
with db.connect() as c:
    print("foreign_keys:", c.execute("PRAGMA foreign_keys").fetchone()[0])
    for t in ("commitments", "decisions", "open_questions", "entities",
              "relations", "speakers", "stage_runs", "speaker_matches",
              "voice_samples", "seen_files", "drive_sources"):
        orphans = c.execute(
            f"SELECT COUNT(*) FROM {t} WHERE meeting_id NOT IN (SELECT id FROM meetings)"
        ).fetchone()[0]
        print(f"  {t:<16} orphaned={orphans}")
```

Any non-zero number is a bug in the delete path, not an acceptable remainder.

## Things that bite in this specific database

- **Use `config.DB_PATH`.** A bare `./manifest.db` silently opens an empty database
  in the repo root and every count comes back zero. That has already produced one
  confidently wrong conclusion in this project.
- **Merges must move more than the text tables.** `db.merge_person` rewrites
  speakers, entities, relations, commitments, decisions, open questions and
  aliases — and knows nothing about voiceprints. `voices.merge_people` moves those.
  Call both, or the folded-away identity comes back the next time that person
  speaks.
- **Aliases are stored lowercased.** An exact-case query against `person_aliases`
  returns zero and looks like a failed write. Use `db.canonical_name` to check.
- **A merge does not rewrite prose.** Historical `minutes/*.md` keep the old
  spelling; only a recompile changes those, and a recompile is ~7.8 min per meeting.
  Say this when reporting a merge, or the user will read the old name later and
  think the merge failed.
- **Deleting a meeting deletes artifacts too.** `dashboard.delete_entire_meeting`
  removes audio, transcript, minutes and the LightRAG document. Archive the files
  you care about *before* calling it, not just the database.
- **The graph is a second store.** Deleting meetings or merging people leaves stale
  nodes in LightRAG. Re-run `pipeline graph-sync`, and delete the stale entity nodes
  explicitly — they are keyed by name, so nothing removes them implicitly. Those
  DELETEs return HTTP 500 while still succeeding; verify with
  `graph_sync.graph_labels()` rather than trusting the status code.

## Commands that mutate without looking like it

Back up first for all of these:

| Command | What it changes |
|---|---|
| `pipeline retry` | Rewrites meeting statuses |
| `pipeline speakers --all` | Re-resolves every label in every meeting with any unresolved label |
| `pipeline minutes --recompile` | Rewrites minutes files; ~7.8 min per meeting |
| `pipeline people --merge` | Folds identities across many tables |
| `pipeline voices` | Writes voice samples, matches, clusters and snippet files |
| `pipeline graph-sync` | Writes to LightRAG, not SQLite — no backup covers it |

**`speakers --all` deserves specific caution.** It once overwrote human-confirmed
names with NULL whenever the LLM failed to name a label, so every rerun could only
lose ground. A guard now prevents downgrading a confirmed name — but that guard is
the only thing standing between a rerun and losing work a human did by ear.
Confirm it is still in `speakers.resolve` before running it across the corpus.
