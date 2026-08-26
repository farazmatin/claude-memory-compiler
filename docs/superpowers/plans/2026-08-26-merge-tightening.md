# Merge Tightening Implementation Plan

**Goal:** Make every person merge final, preview-bound, consistent across CLI and
dashboard, recoverable across database/filesystem interruption, and capable of
repairing the existing manifest without deleting unreviewed alias evidence.

**Spec:** `docs/superpowers/specs/2026-08-26-merge-tightening-design.md`

**Architecture:** A new deep module, `pipeline/people_merge.py`, becomes the seam
for merge preview, merge application, rewrite recovery, and legacy repair. CLI and
dashboard become adapters. Database, voice, and text helpers remain internal
implementation details and cannot independently define merge ordering.

**Tech stack:** Python 3.12, stdlib `sqlite3`, `hashlib`, `json`, `os`, `re`,
`tempfile`, `pytest`, and the existing vanilla JavaScript test harness.

## Global constraints

- Use `config.DB_PATH`; never open a bare `db/manifest.db`.
- Transcripts are immutable. No code in this plan writes to `TRANSCRIPTS_DIR`.
- Do not bump `TEMPLATE_VERSION`.
- Do not contact LightRAG from unit tests.
- Run Python tests with `./.venv/Scripts/python.exe -m pytest`.
- Run lint with `uvx ruff check` and report unrelated baseline failures separately.
- Preserve `lightrag_doc_id` until the index has deleted the stale document.
- Preserve target self-aliases and legitimate aliases acquired by a source.
- Never delete all non-self aliases by predicate. Legacy deletion is bound to an
  approved repair-preview digest.
- A successful database commit may leave durable rewrite jobs pending, but never
  an untracked filesystem change.
- Tasks 1-11 are implementation and offline verification only. Task 12 is
  read-only against the live manifest. Task 13 requires new explicit approval.
- The worktree already contains unrelated/uncommitted dashboard changes. Before
  implementation, record them and either use a clean worktree or obtain an
  explicit decision on overlapping files. Never reset or discard them.

## Public interface to preserve across tasks

The interface lives in `pipeline/people_merge.py`:

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

`preview()` and `merge()` support one or more names. The merge-many adapters
require at least two selected names; the rename adapter requires exactly one
existing source and a new target. Tests and callers cross this interface instead
of reproducing its ordering.

---

### Task 0: Establish a safe implementation baseline

**Files:** none.

- [ ] Record `git status --short` and identify which current changes overlap
      `dashboard.py`, `app.js`, `index.html`, CSS, and dashboard tests.
- [ ] Do not modify, stage, or discard those changes merely to make the plan easy.
- [ ] Run the narrow existing baseline:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_people.py tests/test_voices.py tests/test_dashboard.py -q
```

- [ ] Record failures with their exact test names. Do not encode a stale assumed
      failure count into later acceptance criteria.
- [ ] Run a read-only aggregate script through `config.DB_PATH` and record only:
      database path, ghost count, resolvable ghost count, non-self alias count,
      `speakers_resolved` count, and present/missing minutes counts.
- [ ] If overlapping user changes cannot be isolated safely, stop before Task 1.

**Checkpoint:** no commit; this task changes nothing.

---

### Task 1: Build a pure, multi-name minutes rewrite planner

**Files:**

- Create: `pipeline/rename_minutes.py`
- Create: `tests/test_rename_minutes.py`

**Internal interface:**

```python
@dataclass(frozen=True)
class TextRewrite:
    before: str
    after: str
    matched_spellings: tuple[str, ...]
    match_count: int


def plan_text(text: str, mappings: Mapping[str, str]) -> TextRewrite: ...
def discover_spellings(text: str, normalized_alias: str) -> tuple[str, ...]: ...
```

No function in this module writes a file. Durable file application belongs to
Task 5.

- [ ] Write failing tests for:

  - bare names and possessives;
  - a longer word containing a source name;
  - punctuation-ending names such as `J.R.`;
  - an already-correct target occurrence;
  - two overlapping sources merged into one target, in both input orders;
  - multiple sources replaced in one pass rather than sequential passes;
  - a replacement containing backslashes, proving a callable replacement is used;
  - a common-word name such as `May`, which is reported exactly for preview;
  - exact matched spellings discovered case-insensitively from a lowercased
    historical alias; and
  - a second run producing identical text and zero replacements.

- [ ] Verify RED:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_rename_minutes.py -q
```

- [ ] Implement one alternation that protects every target before considering
      source alternatives and orders sources longest-first.
- [ ] Use `(?<!\w)` and `(?!\w)` boundaries rather than `\b`, so names ending in
      punctuation remain matchable.
- [ ] Use `match.group(0)` and a callable replacement; never pass a human name as
      the raw `re.sub` replacement string.
- [ ] Keep routine rewrites case-sensitive. Historical discovery may scan
      case-insensitively, but it must return the exact observed spellings that the
      preview will bind.
- [ ] Verify GREEN:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_rename_minutes.py -q
```

**Checkpoint:** commit only `rename_minutes.py` and its tests.

---

### Task 2: Add flattened tombstones and durable rewrite jobs

**Files:**

- Modify: `pipeline/db.py`
- Create: `tests/test_people_merge_schema.py`

**Schema:** implement the `merged_names` and `minute_rewrite_jobs` tables exactly
as approved in the design spec. `merged_names.canonical` uses `ON DELETE RESTRICT`,
not `CASCADE`.

**Database helpers:**

```python
def person_key(name: str) -> str: ...
def resolve_merged_name(conn, name: str) -> str | None: ...
def flatten_and_record_merge(conn, old_spelling: str, canonical: str) -> None: ...
def affected_meeting_ids(conn, names: Iterable[str]) -> set[str]: ...
```

- [ ] Write failing schema/helper tests proving:

  - a tombstone target must exist;
  - `A -> B`, followed by `B -> C` and deletion of B, leaves A and B resolving to C;
  - normal writes cannot create a cycle;
  - a spelling cannot be both a living person and a tombstone;
  - tombstone lookup is case-insensitive through `person_key` while retaining the
    original display spelling;
  - the affected-meeting helper includes speaker, entity, relation-subject,
    relation-object, commitment, decision, and open-question-only references;
  - a rewrite job retains before/after text and hashes; and
  - a `NULL` minutes path is representable as a `missing` job.

- [ ] Verify RED, implement the schema and helpers, then verify GREEN:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_people_merge_schema.py tests/test_db.py -q
```

- [ ] Keep these helpers free of filesystem writes and commits. The deep merge
      module owns the transaction.

**Checkpoint:** commit the schema, helpers, and focused tests.

---

### Task 3: Implement read-only merge preview at the module seam

**Files:**

- Create: `pipeline/people_merge.py`
- Create: `tests/test_people_merge.py`

- [ ] Write failing tests through `people_merge.preview()` for:

  - a retained selected target;
  - a brand-new typed target;
  - an existing target outside the selection;
  - a tombstoned requested target resolving to its living person;
  - missing source, empty target, duplicate source, and invalid rename shapes;
  - preserving a target role and deterministic first-source role inheritance;
  - the full affected-meeting union, including a referenced non-attendee;
  - existing, missing, and `NULL` minutes paths;
  - exact literal match/file counts and common-word visibility;
  - preview digest stability for unchanged inputs;
  - digest change on any source row, alias, target, file hash, mapping, or database
    path change; and
  - byte-for-byte non-mutation of the manifest and named files.

- [ ] Verify RED:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_people_merge.py -k preview -q
```

- [ ] Implement preview as a pure read over one database connection plus file
      reads. Serialize a canonical JSON payload with sorted object keys and stable
      list ordering, then SHA-256 that payload.
- [ ] Include the resolved `config.DB_PATH` in the digest payload.
- [ ] Return `actual_target` separately from the spelling the caller requested.
- [ ] Do not create the target, tombstone, rewrite job, temporary file, or preview
      artifact from this function.
- [ ] Verify GREEN and prove manifest/file hashes are unchanged.

**Checkpoint:** commit the module preview and interface-level tests.

---

### Task 4: Put every database and voice mutation behind `merge()`

**Files:**

- Modify: `pipeline/people_merge.py`
- Modify: `pipeline/db.py`
- Modify: `pipeline/voices.py`
- Modify: `tests/test_people_merge.py`
- Update direct low-level tests in `tests/test_people.py`, `tests/test_voices.py`,
  `tests/test_commitments.py`, and `tests/test_e2e.py`

**Refactor rule:** `db` and `voices` may expose internal row-rewrite helpers, but
they may not add merge aliases, delete people, queue minutes, commit, or define
workflow ordering. Existing public merge workflows are removed or made private
after all callers move in Task 6.

- [ ] Write failing interface-level tests proving one digest-bound merge:

  - rejects a missing or stale digest before any mutation;
  - creates/retains the actual target and preserves the correct role;
  - flattens incoming tombstones before deleting an intermediate target;
  - rewrites `resolved_as`, best/next suggestions in both matcher tables, voice
    samples, speakers, entities, both relation ends, commitments, decisions, and
    open questions;
  - clears a duplicated runner-up and score;
  - carries a source's legitimate acquired aliases to the target;
  - deletes only each source self-alias;
  - leaves no source person row;
  - records one rewrite job per affected meeting; and
  - leaves transcripts byte-identical.

- [ ] Verify RED:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_people_merge.py -k "merge and not rewrite_resume" -q
```

- [ ] Implement the transaction in the design-spec order. Recompute the preview
      inside the transaction and compare digests before the first write.
- [ ] Insert durable rewrite jobs with the exact before/after text and hashes.
- [ ] Move completed affected meetings to `speakers_resolved` while jobs are
      pending so the index cannot read stale text.
- [ ] Do not call `queue_minutes_refresh()` from this merge path.
- [ ] Verify GREEN, then run the low-level regression tests listed above.

**Checkpoint:** commit the deep transaction and removal of duplicated workflow
logic together; do not leave an intermediate commit with two authoritative paths.

---

### Task 5: Drain and resume rewrite jobs atomically

**Files:**

- Modify: `pipeline/people_merge.py`
- Modify: `tests/test_people_merge.py`

- [ ] Write failing tests for the durable outbox:

  - unchanged text sets the job `unchanged` and the meeting `minutes_compiled`;
  - a normal rewrite uses a same-directory temporary file and `os.replace`;
  - missing and `NULL` paths are reported and remain `speakers_resolved`;
  - a current hash matching neither before nor after becomes `conflict` and is
    never overwritten;
  - a crash before replacement leaves a resumable pending job;
  - a crash after replacement but before job/status update is recognized by the
    after hash and completes without a second rewrite;
  - a crash after job completion is a no-op on resume;
  - successful jobs move meetings from `indexed`, `minutes_compiled`, or legacy
    `speakers_resolved` to `minutes_compiled`;
  - `lightrag_doc_id` remains unchanged; and
  - before text remains available for explicit restoration.

- [ ] Verify RED, implement `resume_pending_rewrites()`, then verify GREEN:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_people_merge.py -k "rewrite or resume or missing or conflict" -q
```

- [ ] Flush and `os.fsync()` the temporary file before `os.replace()`.
- [ ] Attempt to fsync the containing directory where supported; tolerate the
      Windows limitation explicitly and test the portable behaviour.
- [ ] Make `merge()` drain its own jobs synchronously after commit and return an
      honest non-zero pending/conflict count when it cannot finish.

**Checkpoint:** commit the outbox executor and crash-recovery tests.

---

### Task 6: Make canonicalization and every caller use the module seam

**Files:**

- Modify: `pipeline/db.py` (`canonical_name`)
- Modify: `pipeline/voices.py` (`confirm`)
- Modify: `pipeline/dashboard.py`
- Modify: `pipeline/cli.py`
- Modify: `tests/test_people.py`, `tests/test_voices.py`, `tests/test_dashboard.py`,
  `tests/test_cli.py`

- [ ] Add failing tests proving:

  - `canonical_name()` resolves a hidden tombstone after normal aliases;
  - future speaker/entity/commitment normalization cannot recreate a dead spelling;
  - stale `voices.confirm()` lands on the living person;
  - an unknown spelling still creates a genuinely new person;
  - dashboard pair merge, merge-many, and rename call the deep module and return
    its actual target/result;
  - CLI merge preview is read-only;
  - CLI apply requires `--expected-digest`; and
  - CLI rewrite resume drains only pending/conflicting work it can safely apply; and
  - CLI and dashboard produce equivalent observable outcomes for the same merge.

- [ ] Add a dashboard route:

```text
POST /api/people/merge-preview
```

It accepts `{names, into}` and returns the serialized `MergePreview`.

- [ ] Require `expected_digest` on the existing merge and merge-many mutations.
      Return HTTP 409 for preview drift, not a generic 500.
- [ ] Update the CLI shape:

```powershell
uv run pipeline people --merge FROM INTO
uv run pipeline people --merge FROM INTO --apply --expected-digest SHA256
uv run pipeline people --resume-merge-rewrites
```

The first command previews only. The second applies the exact preview.
- [ ] Remove all CLI/dashboard direct workflow calls to `db.merge_person` and
      `voices.merge_people`.
- [ ] Verify:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_people.py tests/test_voices.py tests/test_dashboard.py tests/test_cli.py -q
```

**Checkpoint:** commit adapters, routes, canonicalization, and tests together.

---

### Task 7: Generate a non-mutating legacy-repair preview

**Files:**

- Modify: `pipeline/people_merge.py`
- Modify: `pipeline/cli.py`
- Create: `tests/test_merge_repair.py`

**CLI:**

```powershell
uv run pipeline people --repair-merges --preview-to PATH [--exclude-alias ALIAS ...]
```

- [ ] Write failing tests proving preview:

  - reads candidate mappings from `person_aliases` before tombstones exist;
  - maps resolvable ghost suggestions through that alias snapshot;
  - proposes but does not automatically include unresolvable clears;
  - discovers exact observed casing for lowercased historical aliases;
  - lists every file, match count, source/target, before/after hash, missing path,
    and common-word/ambiguous warning;
  - excludes only explicitly requested alias keys;
  - never includes self-aliases for deletion;
  - stores the resolved `config.DB_PATH` and a canonical digest;
  - writes only the requested private preview artifact; and
  - leaves the manifest and every minutes file byte-identical.

- [ ] Verify RED, implement, and verify GREEN:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_merge_repair.py -k preview -q
```

- [ ] The preview artifact must contain no credentials and must default under
      `config.DB_DIR / "merge-control"` when PATH is omitted by a future caller.
- [ ] Print an exact summary and the digest, then state that nothing was changed.

**Checkpoint:** commit the preview generator and non-mutation tests.

---

### Task 8: Apply only an exact approved legacy-repair preview

**Files:**

- Modify: `pipeline/people_merge.py`
- Modify: `pipeline/cli.py`
- Modify: `tests/test_merge_repair.py`

**CLI:**

```powershell
uv run pipeline people --repair-merges --apply PATH --expected-digest SHA256
```

- [ ] Write failing tests proving apply:

  - rejects the wrong database path, wrong digest, changed alias/suggestion row,
    changed file hash, or changed target before any mutation;
  - seeds tombstones from only approved alias mappings;
  - rewrites resolvable ghosts through the captured alias map before alias deletion;
  - promotes a valid runner-up when best is cleared or merged into it;
  - enqueues and drains only approved minutes rewrites;
  - deletes only approved alias keys, never every non-self alias;
  - preserves unrelated legitimate aliases and every self-alias;
  - handles legacy `speakers_resolved` rows with present files by moving them to
    `minutes_compiled` after rewrite;
  - counts missing and `NULL` paths accurately;
  - reports pending/conflicting jobs without claiming completion; and
  - applying the same plan twice is a no-op.

- [ ] Verify RED, implement through the same tombstone/outbox implementation used
      by ordinary merge, then verify GREEN:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_merge_repair.py -q
```

- [ ] Do not create a special direct-write repair path. The deep module earns its
      locality by using one implementation for routine and historical rewrites.

**Checkpoint:** commit digest-bound repair application and idempotency tests.

---

### Task 9: Complete both typed-target UIs and behavioural tests

**Files:**

- Modify: `pipeline/static/index.html`
- Modify: `pipeline/static/app.js`
- Modify: `pipeline/static/style.css`
- Modify: `tests/test_dashboard_ui_contract.py`
- Modify: `tests/js/check_controls.mjs`
- Modify: `tests/test_dashboard_controls_js.py`

- [ ] Add **Yes, but call them...** to the duplicate-suggestion card with an
      accessible labelled input.
- [ ] Add a free-text override to the general people-manager merge modal; do not
      leave it constrained to a `<select>`.
- [ ] Preserve selection order in `state.selectedPeople` if role inheritance is
      described as click order. Otherwise explicitly use and display alphabetical
      order in both preview and tests.
- [ ] Request a merge preview before enabling the final mutation. Display:

  - requested and actual retained spelling;
  - affected meetings and files;
  - literal match count;
  - missing/conflicting files; and
  - the fact that folded spellings become hidden redirects, not visible aliases.

- [ ] Submit the preview digest with the merge request. On HTTP 409, keep the
      suggestion/modal open and require a refreshed preview.
- [ ] Disable yes/no/rename/confirm/input controls as one busy set. Restore every
      control on failure and prevent duplicate submission.
- [ ] Update success copy from “minutes are ready to refresh” to the precise
      result: rewritten/unchanged/missing/pending and ready to re-index when true.
- [ ] Extend the Node harness to execute real shipped functions and assert:

  - reveal and prefill behaviour;
  - typed target and digest request payload;
  - general modal typed override;
  - stale-preview recovery;
  - success removes one suggestion;
  - failure keeps it; and
  - duplicate calls cannot be issued while busy.

- [ ] Keep HTML contract tests for accessible control presence, but do not use
      source-string assertions as the only behavioural evidence.
- [ ] Verify:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_dashboard_ui_contract.py tests/test_dashboard_controls_js.py -q
node --check pipeline/static/app.js
```

**Checkpoint:** commit both UI entry points, corrected copy, and behaviour tests.

---

### Task 10: Update operational documentation

**Files:**

- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `SPEAKER_GUIDE.md` if merge instructions are present

- [ ] Document the `people_merge` module interface and the invariant that CLI and
      dashboard must not reproduce merge ordering.
- [ ] Document preview-bound ordinary merge, `resume_pending_rewrites`, and the
      legacy preview/apply commands.
- [ ] State that tombstones are hidden redirects, not visible aliases.
- [ ] Document repair artifact privacy under `config.DB_DIR / "merge-control"`.
- [ ] Document that implementation/offline verification does not authorize the
      live repair.
- [ ] Verify every documented CLI form against `pipeline --help` output.

**Checkpoint:** commit documentation after commands are executable.

---

### Task 11: Offline verification and independent self-review

- [ ] Run focused suites first:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_rename_minutes.py tests/test_people_merge_schema.py tests/test_people_merge.py tests/test_merge_repair.py -q
./.venv/Scripts/python.exe -m pytest tests/test_people.py tests/test_voices.py tests/test_commitments.py tests/test_dashboard.py tests/test_cli.py -q
./.venv/Scripts/python.exe -m pytest tests/test_dashboard_ui_contract.py tests/test_dashboard_controls_js.py -q
```

- [ ] Run the complete offline suite:

```powershell
./.venv/Scripts/python.exe -m pytest -q
```

- [ ] Run lint on touched Python files and syntax-check shipped JavaScript:

```powershell
uvx ruff check pipeline/people_merge.py pipeline/rename_minutes.py pipeline/db.py pipeline/voices.py pipeline/dashboard.py pipeline/cli.py tests/test_people_merge.py tests/test_people_merge_schema.py tests/test_merge_repair.py tests/test_rename_minutes.py
node --check pipeline/static/app.js
```

- [ ] Inspect tests for the prohibited shortcuts:

  - no blanket non-self alias delete;
  - no bare `db/manifest.db` connection;
  - no file write inside the merge database transaction;
  - no direct CLI/dashboard merge workflow outside `people_merge`;
  - no status filter that excludes legacy `speakers_resolved` rewrites;
  - no source-string-only JavaScript behaviour test; and
  - no live LightRAG call from unit tests.

- [ ] Re-read the complete design spec and map every requirement to a passing test
      or an explicitly deferred live verification.
- [ ] Report unrelated/pre-existing failures separately. Do not call the plan
      complete while a new or required check is failing.

**Checkpoint:** one verification commit only if the review itself requires fixes.

---

### Task 12: Generate the live repair preview, then stop

This task is read-only with respect to the manifest and minutes. It does not
authorize Task 13.

- [ ] Resolve and print `config.DB_PATH`; verify it is the intended live manifest.
- [ ] Hash the manifest and all candidate minutes files before preview.
- [ ] Generate the private preview:

```powershell
$previewPath = ./.venv/Scripts/python.exe -c "from pipeline import config; print((config.DB_DIR / 'merge-control' / 'merge-repair-preview.json').resolve())"
uv run pipeline people --repair-merges --preview-to $previewPath
```

- [ ] Hash the manifest and files again; require exact equality.
- [ ] Report without exposing meeting content:

  - resolved database path;
  - candidate and excluded alias counts;
  - suggestion rewrites and proposed clears;
  - minutes files/matches/missing/conflicts;
  - common-word or ambiguous matches;
  - preview path; and
  - preview digest.

- [ ] Show the owner the exact alias-to-target mappings in the private review
      surface or a privacy-preserving local report.
- [ ] Stop and request explicit approval of that exact digest. Do not back up,
      apply, re-index, or clean anything yet.

**Checkpoint:** no commit; private artifacts remain ignored.

---

### Task 13: Apply the approved live repair and verify retrieval

**Authorization:** run only after the owner explicitly approves the Task 12 digest.

- [ ] Re-resolve the database path and verify the approved preview still names it.
- [ ] Create a backup at an explicit path; `--to` is required:

```powershell
$backupPath = Read-Host 'Absolute private backup directory (outside the repository)'
$backupPath = [System.IO.Path]::GetFullPath($backupPath)
uv run pipeline backup --to $backupPath
```

- [ ] Verify the backup command exits zero, its manifest passes SQLite integrity
      checking, and the minutes tree is present. Record its resolved path.
- [ ] Apply the exact approved preview:

```powershell
uv run pipeline people --repair-merges --apply $previewPath --expected-digest APPROVED_SHA256
```

- [ ] If pending jobs remain, run the resume command and stop on any unresolved
      conflict rather than indexing stale text:

```powershell
uv run pipeline people --resume-merge-rewrites
```
- [ ] Verify through `config.DB_PATH`, not a hardcoded SQLite path:

  - approved ghost suggestions are rewritten/cleared;
  - approved aliases are gone and all other aliases remain;
  - tombstones resolve to living people;
  - rewritten meetings are `minutes_compiled`;
  - missing/conflicting meetings remain `speakers_resolved`;
  - all `lightrag_doc_id` values are preserved before indexing; and
  - transcript hashes remain unchanged.

- [ ] Re-index corrected minutes:

```powershell
uv run pipeline index
```

- [ ] Verify no corrected meeting remains unintentionally pending, old index IDs
      were replaced rather than duplicated, `/health` is healthy, and one
      representative person query returns only the surviving canonical spelling.
- [ ] Report before/after counts, backup path, applied digest, index result,
      unresolved work, and rollback instructions.
- [ ] Do not delete the backup, preview, rewrite-job history, or retained before
      text without a separate cleanup request.

**Checkpoint:** no source commit; this task changes private operational state only.

---

## Completion criteria

The implementation is complete only when:

1. every merge/rename adapter crosses `people_merge`;
2. hidden tombstones flatten correctly and prevent resurrection everywhere;
3. all affected name-bearing records and minutes are included;
4. filesystem interruption is resumable and hash-checked;
5. routine and legacy mutations are bound to read-only previews;
6. unrelated aliases cannot be deleted by a broad predicate;
7. both typed-target UIs work under behavioural tests;
8. all required offline checks pass or pre-existing failures are isolated; and
9. live application remains unperformed until its exact digest is approved.
