---
name: regression-triage
description: Determine whether a failing test is a regression you caused or a pre-existing failure, by running the same tests against a clean checkout of HEAD in a temporary git worktree. Use whenever tests fail and you are not certain the working tree caused it — especially after multi-agent work, before reporting test results to anyone, or when an agent claims failures are "pre-existing" or "unrelated to my changes".
---

# Regression triage

**Never diagnose a failing test until you know whether you caused it.** Guessing
costs you either an hour fixing someone else's bug or a shipped regression, and
"that failure was already there" is the single easiest claim to be wrong about.

Answer it in about ninety seconds with a temporary worktree at `HEAD`.

## The procedure

```bash
cd claude-memory-compiler

# 1. A clean checkout of HEAD, alongside the working tree. Detached, so nothing
#    about your branch or index moves.
git worktree add "$TEMP/mmc-head" HEAD --detach -q

# 2. Config it needs. .env is gitignored, so the worktree has none, and without
#    it tests that read tokens or hosts fail for the wrong reason.
cp .env "$TEMP/mmc-head/.env" 2>/dev/null

# 3. The SAME selection that failed. Not the whole suite - you are comparing
#    like with like, and the whole suite takes ~2 minutes.
cd "$TEMP/mmc-head" && uv run pytest tests/test_thing.py -q

# 4. Always clean up. A stale worktree confuses the next `git worktree list`.
cd - && git worktree remove "$TEMP/mmc-head" --force
```

## Reading the result

| At HEAD | In your tree | Verdict |
|---|---|---|
| passes | fails | **You caused it.** Fix it before anything else. |
| fails identically | fails | **Pre-existing.** Say so, decide separately whether to fix. |
| fails differently | fails | Two problems. Separate them before diagnosing either. |
| passes | passes | Flake, ordering dependency, or shared state. Re-run before believing anything. |

## Worked example from this repo

18 tests failed after three agents landed work in parallel. The worktree gave an
unambiguous split in one run each:

- `tests/test_e2e.py` — **22 passed at HEAD**, 16 failing in the working tree.
  Ours. Root cause: an auth precheck applied to a test stub binary. Fixed.
- `tests/test_dashboard.py` — **the same 2 failures at HEAD.** Not ours. Handled
  separately, as stale assertions rather than a regression.

Without this, the honest options were "fix all 18 blind" or "assume they were
already broken". Both would have been wrong.

## When an agent tells you failures are pre-existing

Verify it yourself. In this project an agent reported its work green by running
`pytest -q -m "not e2e"` — which excluded the exact e2e tests its change had
broken. The exclusion was not malicious; it was a plausible-looking scope choice
that happened to hide the regression.

Two rules that follow:

- **Run the specific selection that failed**, not a filtered subset. If a report
  quotes a `-m`, `-k`, or path filter, re-run without it.
- **A "pre-existing" claim is a hypothesis until the worktree confirms it.**

## Gotchas

- **Deliberate behaviour changes look exactly like regressions.** If HEAD passes
  and your tree fails because you *intended* to change behaviour, the fix is to
  update the assertion **and say so explicitly in your report**. Two test
  expectations were deliberately changed in this repo — minutes `# H1` now beats
  the filename-derived title, and `at 11-12 a.m.` parses as `11:12` rather than
  flooring to `11:00`. Both are correct; both would read as regressions to anyone
  who was not told.
- **Shared external state breaks the comparison.** Both trees hit the same
  `db/manifest.db`, the same LightRAG on :9621, and the same Ollama. A test that
  mutates or reads real state gives the same wrong answer in both worktrees — that
  is itself a finding (see `silent-failure-auditor`). One real instance:
  `test_backup` walked the developer's actual `snippets/` directory because the
  fixture patched every path but that one.
- **`uv` may resolve the environment again** in the fresh worktree. Slower first
  run; not a failure.
