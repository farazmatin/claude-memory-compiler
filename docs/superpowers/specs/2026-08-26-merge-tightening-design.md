# Tightening the speaker merge

**Date:** 2026-08-26
**Status:** approved after adversarial revision, not yet implemented

## Why

The owner's goal is to resolve every speaker once and keep the correction final.
The current merge flow works against that goal:

1. `voices.merge_people` rewrites `speaker_matches.resolved_as` but not matcher
   suggestions. The live manifest has 78 suggestion cells naming a person who no
   longer exists.
2. `voices.confirm()` calls `db.add_person()`, so a card cached before a merge can
   recreate the merged-away person.
3. A merge moves affected meetings to `speakers_resolved`. The live manifest has
   82 such meetings, all with minutes files present, and nothing routinely drains
   the queue.
4. `merge_many_people` cannot retain a spelling that is not already selected.
5. CLI, dashboard, database, voice, and filesystem merge behaviour is split
   across several call paths with different ordering.

Read-only inspection on 2026-08-26 also established the migration facts:

- all 78 ghost suggestion cells resolve through the current alias table;
- there are 22 non-self aliases;
- the new tombstone table does not exist yet; and
- all 82 stale meetings have a minutes file on disk.

Those mappings must be consumed and preserved before any alias is deleted.

## Decisions taken

### A hidden redirect makes a merge final

Folded-in spellings disappear from the contacts UI, but a private tombstone keeps
normalizing that spelling to the surviving person. `canonical_name()` consults
the tombstone after normal aliases, so a future transcript or stale review card
cannot recreate the duplicate.

A spelling cannot simultaneously be a living canonical person and a tombstone.
Typing a tombstoned spelling resolves to its living target. Deliberately reusing
that spelling for a different human requires a separate future operation; silently
reviving it during merge or confirmation is forbidden.

### Minutes use deterministic text rewriting, not an LLM recompile

The owner declined an approximately 10.7-hour recompile of the current 82
meetings. Existing minutes are rewritten deterministically, with transcripts left
byte-identical. Literal replacement is accepted only with these safeguards:

- every rewrite is planned before mutation and reports the affected files and
  exact matched spellings;
- all source spellings for one target are replaced in one pass, with the target
  alternative protected and longer sources considered first;
- name boundaries work for punctuation-ending names as well as word-ending names;
- replacement text is inserted through a callable, never interpreted as a regex
  replacement string;
- the before and after text and hashes are recorded in a durable rewrite job; and
- files are replaced atomically and can be resumed or restored after interruption.

The approved limitation remains: only literal occurrences are corrected. A
preview is required because a literal human name can also be an ordinary word.

### Existing aliases are migration evidence, not an undifferentiated delete set

The 22 current non-self aliases are candidates for old merge mappings, but the
schema records no provenance. The legacy repair must preview every alias-to-person
mapping and its effects. Only mappings included in an owner-approved preview may
be tombstoned and deleted. A blanket deletion of all non-self aliases is forbidden.

Self-aliases (`alias == lower(canonical)`) remain load-bearing and are never
deleted.

### One deep merge module owns the invariant

New module `pipeline/people_merge.py` is the seam for every merge and rename. Its
small interface hides target normalization, validation, role selection, affected
meeting discovery, tombstones, matcher suggestions, voice samples, structured
tables, aliases, rewrite jobs, status transitions, and recovery.

Dashboard and CLI are adapters at this seam. Neither may call `db.merge_person`
and `voices.merge_people` as an independent merge workflow.

## A. Module interface

```python
@dataclass(frozen=True)
class MergePreview:
    digest: str
    requested_target: str
    actual_target: str
    source_names: tuple[str, ...]
    speaker_rows: int
    affected_meetings: int
    files_changed: int
    literal_matches: int
    missing_files: tuple[str, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class MergeResult:
    target: str
    speaker_rows: int
    minutes_rewritten: int
    minutes_unchanged: int
    minutes_missing: int
    rewrite_conflicts: int
    pending_rewrites: int


def preview(names: Iterable[str], into: str) -> MergePreview: ...
def merge(names: Iterable[str], into: str, *, expected_digest: str) -> MergeResult: ...
def resume_pending_rewrites() -> MergeResult: ...
def preview_legacy_repair(*, excluded_aliases: Iterable[str] = ()) -> LegacyRepairPreview: ...
def apply_legacy_repair(path: Path, *, expected_digest: str) -> LegacyRepairResult: ...
```

`preview()` is read-only. `merge()` refuses when its digest does not match a fresh
preview, binding the mutation to the impact the owner confirmed. `merge()` accepts
these target shapes:

| `into` is | Behaviour |
|---|---|
| one selected name | retain it |
| a new spelling | create it, unless it is tombstoned |
| an existing unselected person | merge all selected people into it |
| a tombstoned spelling | resolve to the living target and report `actual_target` |

Every supplied source must exist. `preview()` and `merge()` support one or more
names: merge-many adapters require at least two selected names, while the rename
adapter requires exactly one existing source and a new target. The rename still
uses the same preview, tombstone, rewrite, and recovery behaviour.

Role inheritance is deterministic: preserve the living target's role; otherwise
use the first non-empty role in the ordered request. The UI must either preserve
selection order or describe and send the deterministic order it actually uses.

## B. Tombstones and canonicalization

```sql
CREATE TABLE IF NOT EXISTS merged_names (
    old_key      TEXT PRIMARY KEY,   -- lower(trim(old_spelling))
    old_spelling TEXT NOT NULL,      -- display spelling captured at merge time
    canonical    TEXT NOT NULL,
    merged_at    TEXT NOT NULL,
    FOREIGN KEY (canonical) REFERENCES people(canonical) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_merged_names_canonical
    ON merged_names(canonical);
```

Before deleting source B during `B -> C`, the merge module:

1. verifies C is a living person;
2. rewrites every existing tombstone targeting B to target C;
3. upserts B's normalized key and display spelling to target C; and
4. only then deletes B.

This flattening preserves `A -> C` when B had previously absorbed A and keeps the
foreign key meaningful. `resolve_merged_name()` retains a visited-set guard for
corrupt or manually edited databases, but normal writes never create chains or
cycles.

`canonical_name()` checks, in order:

1. the normal alias table;
2. the hidden tombstone table; and
3. the stripped unknown spelling unchanged.

`voices.confirm()` uses `canonical_name()` rather than implementing a second
tombstone algorithm.

## C. Affected meetings and database rewrites

Affected meeting IDs are collected before mutation from the union of:

- `speakers.name`;
- `entities.name`;
- `relations.subject` and `relations.object`;
- `commitments.owner`;
- `decisions.decided_by`; and
- `open_questions.owner`.

The merge transaction then rewrites:

- `speaker_matches.resolved_as`, `best_canonical`, and `next_canonical`;
- `voice_clusters.best_canonical` and `next_canonical`;
- voice samples and confirmed speaker rows;
- entities, both ends of relations, commitments, decisions, and open questions;
- incoming tombstones and the source tombstone; and
- only the source self-alias or an explicitly approved legacy alias.

If rewriting makes `best_canonical == next_canonical`, the runner-up and its score
are cleared. A legacy repair that clears an invalid best match promotes a valid
runner-up rather than leaving it stranded in `next_canonical`.

## D. Durable minutes rewriting

The manifest gains a durable outbox:

```sql
CREATE TABLE IF NOT EXISTS minute_rewrite_jobs (
    id             TEXT PRIMARY KEY,
    operation_id   TEXT NOT NULL,
    meeting_id     TEXT NOT NULL,
    minutes_path   TEXT,
    mappings_json  TEXT NOT NULL,
    before_sha256  TEXT,
    after_sha256   TEXT,
    before_text    TEXT,
    after_text     TEXT,
    state          TEXT NOT NULL,  -- pending|applied|unchanged|missing|conflict
    error          TEXT,
    created_at     TEXT NOT NULL,
    finished_at    TEXT,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
```

Within the database transaction, the merge module records every rewrite job and
moves affected completed meetings to `speakers_resolved`. It commits before
touching files. It then drains the outbox synchronously:

1. `missing` path/file: retain `speakers_resolved` and report the meeting;
2. current hash equals `after_sha256`: treat as an interrupted-but-completed write;
3. current hash differs from both hashes: mark `conflict`, do not overwrite, and
   retain `speakers_resolved`;
4. current hash equals `before_sha256`: write the recorded after text to a
   same-directory temporary file, flush and fsync it, then `os.replace()`; and
5. applied or unchanged file: set the meeting to `minutes_compiled`, preserving
   `lightrag_doc_id` so indexing deletes the stale document first.

A crash can leave work pending, but never untracked. `resume_pending_rewrites()`
is idempotent and handles crashes before replacement, after replacement, and
before the final database status update. Before text remains available for an
explicit restore until the corrected index is verified.

The merge path does not call `queue_minutes_refresh()` after a successful rewrite.
That helper remains appropriate for speaker changes that genuinely require an LLM
recompile.

## E. Legacy repair

The existing damage is repaired through a preview/apply workflow:

```bash
uv run pipeline people --repair-merges --preview-to <private-json-path>
uv run pipeline people --repair-merges --apply <private-json-path> \
  --expected-digest <sha256>
uv run pipeline people --resume-merge-rewrites
```

The preview is read-only and uses `config.DB_PATH`. It contains:

- the resolved absolute database path;
- candidate non-self aliases and their canonical targets;
- exact ghost suggestion updates and proposed clears;
- exact observed source spellings in affected minutes;
- every affected file, match count, and before/after hash;
- missing files and ambiguous/common-word matches requiring review; and
- a canonical digest over the proposed operation.

Private preview files live under `config.DB_DIR / "merge-control"`, which is
already excluded from version control with the database directory.

Apply refuses if the database path, source hashes, rows, or digest differ. It:

1. seeds/updates tombstones from only the approved alias mappings;
2. rewrites resolvable ghost suggestions before deleting aliases;
3. promotes a still-valid runner-up when necessary;
4. enqueues and drains approved minutes rewrites;
5. deletes only approved folded-in alias rows; and
6. reports rewritten, unchanged, missing, conflicting, and pending work.

Running apply twice with the same preview is a no-op. Unresolvable suggestions are
cleared only when the preview explicitly includes that action.

## F. UI

Both merge entry points support typed targets:

1. The duplicate-suggestion card gains **Yes, but call them...**.
2. The general people-manager merge modal gains a free-text override beside the
   selected-name control.

Before enabling the final merge action, the UI requests `/api/people/merge-preview`
and displays the actual retained spelling, affected meetings, files, literal
matches, missing files, and conflicts. The merge request submits the preview
digest. Stale previews receive HTTP 409 and must be refreshed.

Copy must say that folded-in spellings are hidden and redirected; it must not say
they become visible aliases. Controls are disabled together during the request,
and failure restores all controls without removing the suggestion.

The UI preserves selection order if role inheritance claims to use click order.
Otherwise both UI and documentation must state the actual deterministic order.

## G. Order of operations

```text
read-only preview
  1. normalize target, including hidden redirects
  2. validate all sources and collect the full affected-meeting union
  3. calculate database changes and exact minutes rewrites
  4. hash the complete proposal

digest-bound merge transaction
  5. revalidate preview inputs and create/retain the target
  6. flatten incoming tombstones and record each source tombstone
  7. rewrite suggestions, voices, speakers, and structured records
  8. enqueue durable minutes rewrite jobs and block stale indexing
  9. delete only source/approved aliases, then source people
 10. commit

recoverable filesystem phase
 11. drain rewrite jobs with hash checks and atomic replacement
 12. move successful/no-op meetings to minutes_compiled
 13. report missing, conflicting, or pending jobs without claiming completion
```

## H. Testing

Tests cross the `people_merge` module interface wherever possible. Required cases:

- typed new target, existing unselected target, selected target, and tombstoned target;
- target role preservation and deterministic source-role inheritance;
- full affected-meeting union, including a person referenced but not attending;
- merge chains remain flattened after the intermediate person is deleted;
- stale confirm and future canonicalization cannot resurrect a tombstoned spelling;
- matcher best/next rewrites, deduplication, and runner-up promotion;
- source alias deletion without deleting unrelated legitimate aliases;
- punctuation-ending names, possessives, longer words, replacement backslashes,
  already-correct targets, common-word previews, and overlapping source names;
- transcript bytes remain unchanged;
- missing and `NULL` minutes paths are both reported;
- rewritten/unchanged meetings reach `minutes_compiled` from `indexed`,
  `minutes_compiled`, or pre-existing `speakers_resolved`;
- `lightrag_doc_id` remains intact;
- crashes before replacement, after replacement, and before status update resume
  idempotently;
- CLI and dashboard adapters produce the same observable result;
- repair preview is non-mutating and digest-bound apply rejects drift;
- applying the same repair twice is a no-op;
- both UI entry points submit the actual target and digest; and
- busy/error UI behaviour prevents duplicate submissions.

## One-time live repair authorization

Implementation and offline tests do not authorize live mutation. The live sequence
is split into two separately reported steps:

1. generate the read-only preview, verify the configured database path, and show
   the exact mappings/counts to the owner;
2. only after explicit approval, create and verify a backup at an explicit path,
   apply the exact preview digest, resume pending jobs, re-index, and verify both
   the manifest and retrieval.

## Out of scope

- Resolving the remaining speaker decisions for the owner.
- Changing the minutes template or bumping `TEMPLATE_VERSION`.
- Automatically re-indexing during an ordinary merge; successful rewrites are
  left at `minutes_compiled` for the existing index stage.
- Reusing a tombstoned spelling for a genuinely different human without an
  explicit future "revive spelling" operation.
