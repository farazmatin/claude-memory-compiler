# Voiceprint Auto-Label: Adversarial Review and Implementation Blueprint

Date: 2026-08-28
**Spec:** `docs/superpowers/specs/2026-08-27-voiceprint-auto-label-design.md`
**Status:** review complete; the spec requires the Part A amendments before Part B
is executed. No implementation work exists yet.

**Goal:** restore prongs 7-8 (voiceprint match, cross-meeting clustering) with a
remote-only producer, without downgrading a single human-confirmed name, without
silently rewinding the indexed corpus, and without promising compounding that the
audio retention policy cannot deliver.

**Architecture:** a new shallow stage, `pipeline/voice_embed.py`, is the only
module that talks to the embedding provider. `pipeline/voices.py` stays the deep
module and gains one function (`apply_auto`) plus an explicit namespace accessor.
CLI, dashboard, and doctor are adapters. No schema change except one optional
uniqueness index.

**Tech stack:** Python 3.12, stdlib `sqlite3`/`json`/`subprocess`, `numpy`,
`replicate`, `pytest`. Local `ffmpeg` for snippet cutting only.

## Global constraints

- Use `config.DB_PATH`; never open a bare `db/manifest.db`.
- No `torch`, `whisperx`, `pyannote`, or `nemo` import anywhere under `pipeline/`.
- Transcripts are immutable. This plan never writes to `TRANSCRIPTS_DIR`.
- Nothing runs on a clock. The stage runs only on demand.
- Run tests with `./.venv/Scripts/python.exe -m pytest`; lint with `uvx ruff check`.
- The worktree carries unrelated uncommitted Drive-watcher changes
  (`pipeline/watcher.py`, `pipeline/graph_sync.py`, `pipeline/cli.py`,
  `scripts/install-drive-watcher.ps1`). Record them and use a clean worktree, or
  get an explicit decision on `cli.py`, which both changes touch. Never reset them.
- No task in B1-B9 spends money or mutates the live manifest. B10 is read-only
  against the live manifest. B11 requires new explicit approval and a stated budget.

---

# Part A — Adversarial review

Thirteen findings. Every number below was measured against the live
`db/manifest.db` on 2026-08-28, not inferred.

## Corpus baseline (measured)

| Fact | Value |
|---|---|
| meetings | 119 (106 `indexed`, 13 `failed`) |
| meetings with audio on disk | **14** (0 dangling `audio_path`) |
| transcripts on disk | 240 (orphans exceed DB rows) |
| `speaker_matches` | 268, all with an embedding |
| namespace of every stored vector | `pyannote/wespeaker-voxceleb-resnet34-LM` |
| `speaker_matches` pending | 166 — 92 `auto`, 43 `review`, 31 `new` |
| pending rows with playable snippets | 145 of 166 (84 at quality `low`) |
| `voice_samples` | 106 (86 `confirmed`, 20 `bootstrap`), 15 people |
| `voice_clusters` | **0** |
| `speakers` rows | 110 `confirmed`, 146 `inferred`, 216 `unknown` |
| distinct confirmed people | 23 |
| confirmed labels in audio-retaining meetings | **32** of 110 |
| labels per meeting | avg 3.9, max 10 |

## A1 — BLOCKER: the namespace default is wrong, and the review surface erases itself

`config.py:152` sets `VOICE_VECTOR_NAMESPACE = os.environ.get("MMC_VOICE_VECTOR_NAMESPACE", "historical")`.
`MMC_VOICE_VECTOR_NAMESPACE` is unset in `.env` and absent from `.env.example`.
Every stored vector is under `pyannote/wespeaker-voxceleb-resnet34-LM`.

Seven call sites take the namespace as a *default argument*
(`voices.py:175, 198, 273, 288, 321, 458`), and both dashboard callers omit it:

- `dashboard.py:438` — `voices.cluster_pending(conn)` runs on **every** load of
  `get_voice_clusters()`. It finds no pending rows in `historical`, so it calls
  `db.replace_clusters(conn, [])`, which deletes all clusters and nulls
  `cluster_id` on all 166 pending rows. That is why `voice_clusters` is empty
  while 166 pending matches exist.
- `dashboard.py:570` — `voices.confirm(conn, ...)` writes the human's sample into
  the `historical` namespace.

The spec asserts the dashboard review surface "survived the retirement" and that
"writes never land in the quarantined historical namespace." Neither holds. It
also never says how seven default arguments become one runtime-resolved value.

**Amendment:** namespace resolution becomes a single accessor,
`voices.active_namespace(conn)`, reading manifest setting `voice.active_namespace`
and falling back to the configured encoder pin. Every default argument is removed
and the parameter made required, so a missed call site is a test failure rather
than a silent read of the wrong namespace.

## A2 — BLOCKER: `apply_auto` as specified downgrades human confirmations

Spec §3 writes `set_speaker(meeting, label, best_canonical, confidence="inferred")`.
`db.set_speaker` (`db.py:739`) is an unconditional upsert. `speakers.py:386`
documents this by name:

> "This is the fix for the corpus-wide data loss `db.set_speaker` otherwise
> causes … silently erasing names a human had already confirmed by ear."

`_merge_with_existing` (`speakers.py:380`) exists solely to stop it, with rule 1:
a `CONFIRMED` row is never downgraded by a weaker pass.

Measured blast radius: **10** of the 92 pending-auto rows sit on labels whose
`speakers.confidence` is already `confirmed`. `best_canonical` matches the stored
name in all 10, so no wrong name results today — but the confidence field is
downgraded, and the spec's own guarantee ("`confirmed` means a human ear decided …
un-degradable") is violated by its own implementation sketch.

**Amendment:** `apply_auto` routes through `speakers._merge_with_existing`
(promoted to a public `speakers.merge_label`) and never calls `db.set_speaker`
directly. A label already `confirmed` is skipped, and its match resolves to the
confirmed name rather than to `best_canonical`.

## A3 — BLOCKER: the first `apply_auto` rewinds 45 indexed meetings

92 rows are already banded `auto` across **46 meetings** — 45 `indexed`, 1
`failed`. `db.queue_minutes_refresh` (`db.py:692`) sets
`status = SPEAKERS_RESOLVED` for meetings in `MINUTES_COMPILED` or `INDEXED`.

So the stage's first invocation triggers 45 subscription-CLI minutes recompiles
and 45 index delete/insert cycles, and leaves 45 meetings out of `INDEXED` — the
graph and search index degraded — until `run` catches up. Spec §6 accounts only
for "one embedding call per new meeting … a rounding error."

Backlog composition (pending, band=`auto`): Yuliya 31, Faraz Matin 27, Ru Farrell
8, Tarun 6, Ruth 5, Zara 4, Lila 2, Dan 2, then 7 singletons. Minimum
`best_score` 0.703. **57 of the 92 have `llm_name IS NULL`**, so the LLM veto
never fired on them — they were banded auto on cosine alone.

**Amendment:** `apply_auto` is dry-run by default (`--apply` to commit), prints
the per-meeting refresh count before committing, and takes `--limit`. The first
production run is a separate, approved operation (task B11), not a side effect of
the first `pipeline voice`.

## A4 — BLOCKER: the compounding promise reaches 8 people, not the corpus

Spec §2: bootstrap "is what lets every name confirmed since the beginning compound
the moment the stage first runs." Bootstrap needs audio, because a new-namespace
vector cannot be derived from a pyannote-namespace blob (see A5).

Only 14 meetings retain audio. Confirmed labels inside them, by person:

| Person | meetings | Person | meetings |
|---|---|---|---|
| Yuliya | 6 | Ru Farrell | 2 |
| Tarun | 5 | Paul Wood | 2 |
| Zara | 4 | Neil | 2 |
| Faraz Matin | 4 | Tyler | 1 |
| Tejaswini | 3 | Lorraine | 1 |
| | | Liz | 1 |

With `MMC_VOICE_MIN_ENROLL_MEETINGS = 2`, exactly **8 of 23** confirmed people
can ever clear the far-field rule in a fresh namespace. The remaining 15 —
including Dan, Ruth, Paul McClean, Lila, Andy, Arif, Charles, Ian, Khatija — have
no retained audio and are permanently unmatchable there.

The dashboard already documents the cause (`dashboard.py:~447`): "Clips are cut
once, at enrollment, and transcription deletes the source audio in the same loop —
so this count is fixed for good."

**Amendment:** restate the compounding claim honestly as "8 people across 14
meetings at first run, growing one meeting at a time," and run A6's comparability
probe first, because it is the only path back to 23.

## A5 — HIGH: bootstrap vector reuse contaminates the new namespace

`docs/superpowers/reference/retired-enroll.py:368` `_bootstrap_label` reuses an
existing `speaker_matches` blob rather than re-embedding: "the embedding does not
change just because a human confirmed the name after the fact." True within one
encoder; false across encoders. Ported unchanged, it copies
`pyannote/wespeaker-voxceleb-resnet34-LM` vectors into `encoder@version`,
producing voiceprints that mix two vector spaces — and defeating the spec's own
test, "a new-model vector never matches a historical voiceprint."

**Amendment:** namespace-gate the reuse branch on
`existing["model"] == target_namespace`; otherwise re-embed.

## A6 — HIGH: the quarantine may be discarding a usable corpus for free

Spec out-of-scope: "Re-scoring or migrating the quarantined historical vectors."
But the stored namespace is not a placeholder called `historical` — it is
`pyannote/wespeaker-voxceleb-resnet34-LM`, and shortlist encoder #1 is
`wespeaker-resnet34-lm`, the same representation family. If the cog serves the
same weights, the 106 existing samples (23 people, 15 with samples) are directly
comparable and the corpus does not need to restart at 8 people.

**Amendment:** add a cheap comparability probe before any decision — embed ~10
labels from the 14 retained-audio meetings via the cog and cosine each against its
own stored pyannote vector. Same-label self-similarity near 1.0 means the
namespace can be aliased rather than quarantined. This is the highest-value paid
call in the plan and costs roughly one meeting's embedding.

## A7 — HIGH: the delete-audio guard deadlocks the backlog and cannot report refusal

Spec §4 refuses `POST /api/meetings/{id}/delete-audio` when the meeting has
unembedded unresolved labels. With `REMOTE_VOICE_MODEL` unset the stage no-ops, so
labels never become embedded and the refusal never lifts. Measured: **30**
unresolved labels across the 14 audio-retaining meetings — every one of those
meetings becomes undeletable, permanently, on the config the spec calls the default.

Two further gaps:

- `dashboard.py:1120 delete_meeting_audio` returns `bool`; `dashboard.py:1596`
  maps `False` to an error. There is no way to distinguish *refused, override
  available* from *no such meeting*. The signature must return a result object.
- `delete_entire_meeting` (`dashboard.py:1134`) unlinks the same audio with no
  guard. An unguarded escape hatch destroys exactly the evidence §4 protects.

**Amendment:** the guard is active only when the voice stage is configured
(`REMOTE_VOICE_MODEL` set); the handler returns
`{"refused": true, "reason": ..., "override": "force"}` with HTTP 409;
`delete_entire_meeting` warns with the same evidence summary.

## A8 — HIGH: the provider is called per label but costed and interfaced per meeting

Spec §2's pseudocode issues `request the label's embedding` inside the per-label
loop. The cog's interface takes `regions: [{label, start, end}]` and returns
`{embeddings: {label: [...]}}` — one call, all labels — and §6 costs it as "one
embedding call per new meeting." At avg 3.9 labels (max 10) per meeting, the
pseudocode is roughly 4x the stated cost and 4x the L40S cold-start exposure.

**Amendment:** restructure into three phases — **plan** (per-label decisions, no
I/O), **one batch call**, **apply** (per-label persistence). This is the largest
structural departure from the archived reference, which was necessarily per-label
because it held the model in memory.

## A9 — MEDIUM: the over-segmentation veto does not exist

`band()` takes `over_segmented: bool = False` (`voices.py:234`) and honours it
(`:251`), but **nothing in the repository ever passes it True** except one test
(`test_voices.py:207`). `score_match` defaults it to `False` and `rematch_pending`
never supplies it. The spec lists it twice — as a surviving guard, and as part of
"the entire auto-apply policy." It is a dead parameter.

**Amendment:** the new stage owns the computation — it holds the transcript and
the label count — and threads it through `rematch_pending` → `score_match`. Define
the predicate explicitly (label count vs. expected participants, or labels whose
pairwise cosine exceeds the cluster threshold within one meeting). Without this,
the 57 llm-null auto rows have no second guard at all.

## A10 — MEDIUM: `replace_clusters` clobbers across namespaces

`db.py:1485`: `UPDATE speaker_matches SET cluster_id = NULL WHERE state = 'pending'`
— unfiltered by model. `cluster_pending(model=X)` therefore destroys cluster
assignments in namespace Y. The spec's benchmark phase guarantees two namespaces
coexist (existing pyannote rows plus `encoder@version`), and A6's probe adds a third.

**Amendment:** `replace_clusters` takes the namespace and filters both the delete
and the `cluster_id` reset by it.

## A11 — MEDIUM: BAND_AUTO rows are invisible until `apply_auto` exists

`cluster_pending` filters `row["band"] != BAND_AUTO` (`voices.py:344`), so an
auto-banded row never appears on a voice card. Nothing applies it either. That is
the 92-row limbo measured in A3 — rows in neither the review surface nor the
`speakers` table. If `apply_auto` ever fails or is skipped for a row, it silently
vanishes.

**Amendment:** `apply_auto` returns the rows it declined and why; any auto row it
declines is demoted to `review` so it lands on a card rather than disappearing.
Add a `doctor` check for auto-banded pending rows older than one run.

## A12 — LOW: "no migration needed" is very nearly true

Schema (`db.py:265-327`) supports every field the spec uses — `resolved_as`,
`snippet_paths`, `snippet_quality`, `source` including `bootstrap`. Correct.

One exception: the spec's test asserts bootstrap enrolls "exactly once per
(canonical, meeting, label)", but `voice_samples` has no such unique constraint by
design ("each confirmation … is meant to be able to add a fresh row" — reference
`_already_sampled` docstring). Enforcement is code-side only.

**Amendment:** either port `_already_sampled` verbatim and drop "exactly" from the
invariant, or add `UNIQUE(canonical, meeting_id, label, model)` — which is a
migration, so the spec line becomes "one additive index."

Also: `db.pending_matches` filters `embedding IS NOT NULL`, so a row written with
snippets but no embedding is invisible to rematch, cluster, *and* the queue. The
delete-audio guard in A7 must therefore query `speaker_matches` directly, not via
`pending_matches`.

## A13 — LOW: config, doctor, and CLI surfaces are emptier than the spec assumes

- `.env.example` documents **zero** `MMC_VOICE_*` or `MMC_SNIPPET_*` settings. The
  spec adds three more to an undocumented surface.
- `doctor.py` has **no** voice checks at all. The spec adds one; A1's namespace
  drift is the check that would actually have caught the live breakage.
- `pipeline/cli.py` contains **no** `voices.` call sites. There is no voice CLI
  entry point today, so `pipeline voice` is new surface, not a restored one.

---

# Part B — Implementation blueprint

## Public interface

`pipeline/voices.py` (deep module — additions):

```python
def active_namespace(conn: sqlite3.Connection) -> str: ...
    # manifest setting voice.active_namespace, else f"{ENCODER}@{VERSION}",
    # else raises. Never returns "historical".

@dataclass(frozen=True)
class AutoApplyResult:
    applied:  tuple[tuple[str, str, str], ...]   # (meeting_id, label, canonical)
    skipped:  tuple[tuple[str, str, str], ...]   # (meeting_id, label, reason)
    demoted:  tuple[tuple[str, str], ...]        # auto -> review
    meetings_requeued: int

def apply_auto(
    conn, *, namespace: str, dry_run: bool = True, limit: int | None = None
) -> AutoApplyResult: ...

def over_segmented(transcript, *, expected: int | None = None) -> bool: ...
```

`pipeline/voice_embed.py` (new, shallow — the only provider caller):

```python
class VoiceEmbeddingBackend(Protocol):
    def embed(
        self, audio_uri: str, regions: list[LabelRegion], *, encoder: str
    ) -> EmbedResponse: ...
        # EmbedResponse: embeddings {label: list[float]}, dim, encoder, speech_sec

@dataclass(frozen=True)
class LabelPlan:
    label: str
    action: str                   # bootstrap | embed | skip
    canonical: str | None
    regions: tuple[tuple[float, float], ...]
    speech_sec: float
    reuse_blob: bytes | None      # only when existing["model"] == namespace

def plan_meeting(conn, meeting, transcript, *, namespace, force) -> list[LabelPlan]: ...
def embed_meeting(conn, meeting, *, backend, namespace, force) -> MeetingEmbedResult: ...
def run(owner, *, meeting_id=None, force=False, backend=None) -> RunResult: ...
```

`pipeline/speakers.py`: promote `_merge_with_existing` to public
`merge_label(conn, meeting_id, label, name, confidence) -> tuple[str | None, str]`,
which reads the existing row and applies both rules before `db.set_speaker`.

## Revised order of operations

Two changes from spec §2: one batch call, and namespace-gated reuse.

```text
per meeting (transcribed or later, audio on disk):
 0. namespace = voices.active_namespace(conn)          # never "historical"
 1. load transcript; per-label regions, speech_sec, label confidences
 2. PLAN every label, no I/O:
      confirmed by human + already sampled in `namespace`   -> skip
      confirmed by human + reusable blob in `namespace`     -> bootstrap (free)
      confirmed by human, no vector in `namespace`          -> bootstrap (embed)
      unresolved, embedded in `namespace`, not --force      -> skip (refresh llm_name)
      otherwise                                             -> embed
 3. cut snippets (ffmpeg) for every planned `embed` label
 4. ONE provider call with every region needing a vector    # bootstrap + embed
 5. APPLY per label: voice_samples (bootstrap) / speaker_matches (embed)
 6. compute over_segmented(transcript) once; store it on the meeting
 7. rematch_pending(namespace) -> apply_auto(dry_run) -> cluster_pending(namespace)
```

Steps 2-7 remain individually idempotent and killable. Step 4 is the only paid
call and the only network I/O.

## Tasks

Offline and free unless marked. Each task ends green before the next starts.

**B1 — Fix the namespace; unbreak the live surface.** Add
`voices.active_namespace`; make `model` a required parameter on `voiceprint`,
`enrolled`, `rematch_pending`, `cluster_pending`, `confirm`; filter
`db.replace_clusters` by namespace (A10); update both dashboard call sites; add a
`doctor` check that the configured namespace matches the namespace the stored rows
actually use (A1, A13). Test: a namespace with no rows must not delete another
namespace's clusters. *This task alone restores today's dead review surface — do it
first, independent of everything else.*

**B2 — `speakers.merge_label`.** Promote `_merge_with_existing`, add the
existing-row read, keep every current test green. Test: `inferred` never
overwrites `confirmed`; empty never overwrites non-empty (A2).

**B3 — `voices.over_segmented`.** Define the predicate; thread it through
`score_match` and `rematch_pending`. Test: a synthetic over-segmented transcript
never yields `BAND_AUTO` (A9).

**B4 — `voices.apply_auto`.** Dry-run default, `--limit`, routes through
`merge_label`, demotes declined rows to `review`, returns `AutoApplyResult`. Test:
an auto row applies as `inferred`; an already-`confirmed` label is skipped, not
downgraded; LLM disagreement declines; the refresh count is reported before commit
(A2, A3, A11).

**B5 — `VoiceEmbeddingBackend` + scripted fake.** Protocol, `EmbedResponse`, and a
fake mirroring the ASR test fakes. No network. No provider yet.

**B6 — `plan_meeting`.** Pure function, no I/O, ported from the archived
reference's decision tree with the namespace gate on reuse. Test: reuse only on
namespace match; `--force` re-embeds; an already-sampled bootstrap is a skip (A5).

**B7 — Snippet cutting.** Port `write_snippets` verbatim from
`docs/superpowers/reference/retired-enroll.py:250`; drop `_load_waveform`,
`_embedder`, `embed_label`. Test: no `torch`/`whisperx`/`pyannote`/`nemo` import
under `pipeline/` (extends the existing invariant).

**B8 — `embed_meeting` + `run`.** One batch call; per-label failure isolation;
provider failure leaves prior embeddings intact and does not advance the meeting.
Test with the B5 fake over the real CLI. This is where the plan→call→apply shape
lands (A8).

**B9 — Adapters.** `pipeline voice --owner [--force] [--meeting ID]`; `--no-voice`
on `run`/`watch`; the `voice.stage_in_run` manifest gate; the delete-audio guard as
a result object with HTTP 409 + `force`, active only when `REMOTE_VOICE_MODEL` is
set; the same evidence summary on `delete_entire_meeting`; `.env.example` gains
every `MMC_VOICE_*` and `MMC_SNIPPET_*` setting; `doctor` gains the
model-resolves check (A7, A13). E2E: fake provider, audio in, minutes out with the
auto-applied name, second meeting auto-labelled with no owner action.

**B10 — Read-only reality check** (live manifest, no writes, no money). Run
`plan_meeting` across all 119 meetings and print: labels to embed, labels to
bootstrap, meetings skipped for missing audio, and `apply_auto --dry-run`'s refresh
count. Expect ~14 embeddable meetings and ~45 refreshes. Confirm Part A's numbers
still hold before spending anything.

**B11 — Paid; requires new explicit approval; in this order:**
1. **Comparability probe** (A6) — ~10 labels from retained-audio meetings via the
   cog, cosined against their own stored pyannote vectors. Decides whether the
   corpus is 23 people or 8. Cost: about one meeting.
2. **Encoder benchmark** — 5-10 meetings scored against human-confirmed labels;
   winner pinned by version hash.
3. **Backfill** — the 14 audio-retaining meetings, then `apply_auto --apply
   --limit` in batches, watching the 45 requeued meetings recompile.

## Doc updates (with the code, per spec)

`docs/ARCHITECTURE.md` (provider policy + flow line), `docs/TESTING.md`,
`SPEAKER_GUIDE.md` (auto-apply semantics, delete-audio guard), `AGENTS.md`
(sequence line), the `config.py` comment block, and `.env.example` — which today
documents no voice settings at all.

## Spec amendments this plan assumes

| # | Spec statement | Correction |
|---|---|---|
| A1 | "Dashboard review surface survived" | It is namespace-broken and self-erasing; B1 restores it |
| A2 | `set_speaker(..., "inferred")` | Route through `merge_label` |
| A3 | "a rounding error" | Plus 45 minutes recompiles and reindexes on first apply |
| A4 | "every name confirmed since the beginning compounds" | 8 of 23 people, 14 of 119 meetings |
| A5 | bootstrap reuses the stored blob | Only when the namespace matches |
| A6 | historical vectors out of scope | Probe comparability first; same encoder family |
| A7 | delete-audio guard | Gate on `REMOTE_VOICE_MODEL`; result object; cover `delete_entire_meeting` |
| A8 | per-label provider call | One batch call per meeting |
| A9 | over-segmentation veto "survived" | Dead parameter; the new stage must compute it |
| A10 | namespaced tables | `replace_clusters` is not namespaced |
| A12 | "No migration needed" | True, unless the uniqueness invariant is enforced in SQL |

## Out of scope (unchanged from the spec)

The 2026-08-16 day-workflow UX; threshold or sensitivity changes; the
merge/confirm machinery; reviving tombstoned spellings.
