# Dashboard UI/UX Hardening

## Goal

Make the Meeting Memory dashboard a dependable, low-cognitive-load control room
without changing pipeline, storage, or privacy behavior.

## Approved decisions

- Show at most three speaker-review cards above the meeting library.
- Keep the complete speaker-review queue in People & Team.
- Use `Processing Idle` for an inactive pipeline.
- Report unresolved archive work separately from processing state.

## Scope

1. Normalize people aliases at the dashboard API boundary so contacts always
   render.
2. Separate pipeline activity from archive attention in the masthead.
3. Limit the library speaker queue to three cards and link to the full queue.
4. Remove narrow-screen horizontal overflow from archive filters and controls.
5. Implement the WAI-ARIA tab keyboard contract and durable form labels.
6. Repair voice-cluster regression tests and add UI contract coverage.

## Acceptance criteria

- Contacts render when aliases are missing, stored as JSON text, or returned as
  legacy scalar text.
- The idle badge says `Processing Idle`; archive work is visible in a separate,
  unambiguous status.
- The library renders no more than three voice cards and can open the full queue.
- No page-level horizontal overflow at a 390 px viewport.
- Tabs support Left, Right, Home, and End and expose `aria-controls`,
  `aria-labelledby`, and roving `tabindex`.
- Every editable field has an associated label.
- Dashboard tests, full pytest, and ruff pass.
- Live desktop and mobile inspection shows no dashboard console errors.

## Safety boundaries

- Do not run processing, export, query, confirmation, dismissal, or deletion
  actions during verification.
- Keep the server loopback-only.
- Do not modify meeting content, speaker assignments, or the manifest database.

## Verification

- Dashboard/auth/UI contract suite: 58 passed.
- Remaining repository suite outside the unrelated alert module: 429 passed.
- Ruff passes on every Python file changed by this task.
- JavaScript syntax and Git whitespace checks pass.
- Live desktop: three preview cards, full 15-card queue, 250 contacts rendered,
  working arrow-key tabs, focus handoff, and no dashboard console errors.
- Live narrow viewport: zero page overflow; three preview cards remain in one
  horizontally scrollable row.

## Existing repository failures outside this task

- Four alert-return-value tests in `tests/test_answer.py` fail.
- Repo-wide Ruff reports five existing findings in capture, CLI, minutes export,
  and doctor modules.
