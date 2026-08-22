# Session State — corpus recompile + speaker review (2026-08-19)

Resume notes. Everything below is verified, not assumed.
**Test suite: 468 passed, 2 skipped, 0 failures. `ruff`: clean.**

## The corpus was recompiled. This is the big one.

`TEMPLATE_VERSION` went **"1" → "2"** and all 41 then-existing meetings were
rebuilt from retained transcripts. The previous session left this as open
decision #1, declined at an estimated ~6 hours; the Antigravity fix below made it
**29.1 minutes, 42.6s per meeting, 41/41 successful, zero provider fallbacks**.
No Replicate cost — recompiling does not re-transcribe.

Measured before → after across the 41:

| | old | new |
|---|---|---|
| mean | 1,473w | **3,079w** |
| median | 1,346w | 3,017w |
| shortest | 992w | **2,169w** |
| under the 1,200 floor | 5 | **0** |
| corpus total | 60,399w | **126,243w** (2.09x) |

Every one grew; none shrank. Compression now tracks meeting length, which was the
entire point of removing the ceiling:

| meeting size | old kept | new kept |
|---|---|---|
| shortest 25% (3.3k–14k words) | 16.0% | 30.5% |
| middle 50% (14k–27k) | 8.0% | 17.2% |
| longest 25% (27k–81k) | 4.3% | 9.5% |

**The gradient narrowed but did not disappear, and the cause moved.** It is no
longer the word rule — it is `MINUTES_PROMPT_TOKEN_BUDGET` (60k). Four meetings
exceed it, switch to map-reduce, and the model writes from extracted notes rather
than verbatim dialogue. The 81,178-word meeting went 1.8% → 3.2% and is still the
hardest-compressed thing in the archive. Raising the budget is the lever if those
specific meetings matter.

Backups before the run: `%TEMP%\mmc-pre-tmplv2\manifest.db.pre-tmplv2.bak` and
`%TEMP%\mmc-pre-tmplv2\minutes\` (all 41 pre-recompile files).

Recompile also corrected filename dates (`2026-08-17-…` → `2026-08-06-…`) and
deleted the superseded files. Derived tables were re-parsed at compile time:
**278 commitments, 216 decisions, 148 open questions, 1,548 entities, 1,162
relations.**

## Three silent-failure bugs, all found by disbelieving a success report

A theme worth carrying forward: every one of these reported success while doing
nothing. None would have been caught by exit codes alone.

**1. Antigravity sent the wrong envelope key.** The provider sent Claude Code's
`{"type": "user", …}`; `agy` wants `{"event": "user", …}`. Same `message`
sub-object — one key. `--input-format stream-json` was **added in agy 1.1.15**
(it did not exist in 1.1.14, contrary to the previous session's note), and the
binary self-updated on 2026-08-19 01:02, which is when this broke.

It hid because **agy writes the complaint to stdout and leaves stderr empty**, so
it surfaced as a bare "exited 1", and `detail = stdout[:500]` spent all 500
characters on the init event's 57 tool names. Both fixed: the key, and a new
`AntigravityProvider._error_detail()` that pulls agy's own `error` field out of
the `result` event and falls back to the stream **tail**, never the head.
Verified live: 8.20s, no fallback.

**2. `index.delete_document()` reported success on a no-op.** LightRAG declines
deletes while its ingestion pipeline is busy — with **HTTP 200 and
`{"status": "busy"}`**, not a 4xx. The client trusted the status code, printed
`deleted 14/14`, deleted none, and the re-insert behind it then 409'd on records
it believed were gone. Now treats `status` of `busy`/`not_allowed` as failure.

**3. `pipeline graph-sync` exited 0 having written nothing.** `return 0 if after
else 1` asked only whether the graph has any entities, so a run where all 1,604
writes were refused with 409 still reported success — while the graph quietly
kept serving the *previous* corpus. Now exits 1 and says so when
`entities_written + relations_written == 0` and there were errors.

Graph after a clean re-run: **307 → 824 entities.** (Entity creates still return
HTTP 500 while the node lands — LightRAG errors on the embedding step afterwards,
so the written-count under-reports. Unchanged, pre-existing.)

## Speaker Identity Review — clips, second opinion, and a confidence gate

The review card asked you to confirm an identity you could not hear. Clips
already existed on disk and were already linked in `speaker_matches.snippet_paths`;
the card simply never called the endpoint that serves them.

- **▶ Listen (Ns)** plays every retained clip for a cluster back to back, and the
  label states the real duration. Clips are chosen ≥60s apart, so three clips are
  three different sentences rather than one sentence in thirds. 17 of 25 cards
  have audio; the other 8 say **"No audio retained"** instead of offering a dead
  control.
- **A second suggestion from the transcript.** The minutes stage already infers
  names from direct address ("thanks, Ruth") into `speaker_matches.llm_name`, and
  the card never used it. Now surfaced as `llm_suggestion` (majority vote across
  cluster members), offered *beside* the voiceprint match and promoted to the
  primary action when the voiceprint is weak.
- **"✓ Confirm N confident matches"** — bulk accept above 0.85, count in the
  label. `POST /api/voices/confirm-confident`.
- `SNIPPET_COUNT` **3 → 4** so new meetings retain 24s.

**The finding that matters more than the feature.** Only **7 people have an
enrolled voiceprint**, but the matcher scores all 25 clusters against them, so for
most clusters "best match" is the least-wrong of seven strangers. Median score is
**0.35**; only 1 cluster clears 0.85 and only 4 clear 0.70. The card was
rendering *"Best Match: Yuliya · 6% confidence"* beside a one-click Confirm that
writes that name into every meeting in the cluster.

One-click confirms are now gated at 0.70: **25 → 10** (4 voiceprint + 6
heard-in-meeting), and 15 cards honestly say there is no confident match. Fewer
buttons, more correctness. **Speaker naming remains the ceiling on this whole
feature area, and the bottleneck is enrollment count, not the matcher.**

## XSS in ten inline handlers — fixed

`escapeHtml` is an HTML-entity encoder, and a browser HTML-decodes an attribute
value **before** the JS parser reads it. So `&#039;` became a live quote inside
`onclick="confirmVoiceCluster('id','name')"`. Demonstrated against the real code:
a name of `x'),alert(1),confirmVoiceCluster('` reached the JS parser as
`confirmVoiceCluster('cluster-1', 'x'),alert(1),confirmVoiceCluster('')` and
executed.

Names reach that card from an LLM reading a transcript and from the people
registry. Four pre-existing meeting-card handlers had the same shape, and two of
them interpolate `m.title` — LLM-written — into the control that **deletes a
meeting**.

`onclick=` with an interpolated value is now gone from `app.js` entirely. All ten
run through two delegated `document` listeners reading `.dataset`. Watch for one
trap when doing this again: appending `class="js-…"` to a tag that already has a
`class` attribute produces two, and **HTML honours only the first** — three
buttons were silently dead until the duplicates were merged.

## Fixed: tests that had stopped testing anything

Three tests hard-coded values that the shipping code later caught up to. All now
derive from config, so they cannot silently stop testing:

- `test_recompile_rebuilds_without_retranscribing` hard-coded `"2"` as its
  *simulated* bump. Bumping the real `TEMPLATE_VERSION` to `"2"` made both sides
  equal, so nothing was stale and the whole recompile path went untested. Now
  `f"{CURRENT_VERSION}-next"`.
- `e2e_harness.MINUTES_DOC` stamped a literal `template_version: "1"`, so the
  fixture meeting was born stale. Now interpolates the real value at write time
  (deliberately at write time, not module import — a module-level
  `pipeline.config` import would run before conftest sets the `MMC_*` env).
- `test_dense_speech_is_accepted_at_full_quality` asserted `len(chosen) == 3`,
  which broke on the `SNIPPET_COUNT` bump. Now asserts `== SNIPPET_COUNT`.

## Ask AI now reads the registers, not just prose

`answer.structured_context()` is a new third retrieval source, placed **first**
in `_retrieve_context()` because ordering is the cheapest instruction available.
Until now the answer path was graph traversal plus a keyword scan of minutes
files on disk, so **642 parsed rows were never consulted** — the decision,
commitment and open-question registers, carrying owner, rationale, due date and
a timestamp. "What did we decide about X" was answered by scoring whole minutes
files, and rationale — the thing minutes are kept long in order to preserve —
never reached the model.

Two retrieval routes, because questions arrive in two shapes: keyword overlap
("what did we decide about CRUD access") and **owner match** ("what does Ali
owe", which shares no keyword with the commitment's own text — but owner is a
column). `db.list_open_questions()` is new; the other two registers already had
accessors that nothing in the answer path called.

Measured on the live corpus:

| question | before | after |
|---|---|---|
| "what did we decide about CRUD access?" | 6,983 chars, 3 sections | **3,285, 2 sections** |
| "what is Faraz on the hook for?" | 7,038 chars, 3 sections | **1,776, commitments only** |
| an unrelated question | ~7,000 chars | **0** |

**Two quality bugs found by reading the real output rather than the tests.**

1. **Scoring on total occurrences returned the whole table.** "access" appears in
   most of a security team's decisions, so every question got ~7k characters and
   all three registers. Now `_match()` returns *distinct* keywords matched and
   `_min_distinct()` requires two of them for any question with two or more
   keywords — one hit only suffices for a single-keyword question, which has
   nothing to corroborate against.
2. **Every citation quoted a mangled Drive file id.** `meetings.title_hint` holds
   values like `1orzS fOYO8qQnBfGwVkEmJ6PWkoxdCse 8 Aug 12 at 4 00 p`. A good
   resolver, `clean_meeting_title`, already existed buried in `dashboard.py` — so
   the fix was **not** to write a second one. It moved to new
   **`pipeline/titles.py`** and both callers import it, because two title
   resolvers will drift and an AI citation disagreeing with the card title is
   worse than either being wrong alone. Three UI sites were also still rendering
   `title_hint` raw, including the meeting chips on every voice-review card.

A prompt rule now states that a section marked "parsed, authoritative" outranks
the prose excerpts under it, and that owner and due date must carry through
verbatim.

Verified end to end: **19.6s** (4.95s retrieval, 14.7s synthesis, antigravity).
The answer cited every decision with its rationale and a readable title, and
**flagged a reversal on its own** — the 2026-08-18 AD-Groups decision contradicts
the 2026-08-13 OPA/ABAC decision, named with both sources. It could only do that
because the register hands it precise dated rows.

**Still worth knowing:** `decided_by` is frequently `SPEAKER_00`, so these answers
inherit the speaker-naming ceiling exactly as the commitment register does.
Naming voices remains the highest-leverage unblock in the system.

## Open — needs a human decision

1. **14 meetings sit at `minutes_compiled`, not `indexed`.** Their new minutes
   cannot be inserted because LightRAG holds a `failed` record under each new
   filename and refuses to delete it while its pipeline is busy — and its
   pipeline is *perpetually* busy, because its own extraction runs on qwen3:4b at
   3.6 tok/s and always times out. A retry loop that waited for idle got exactly
   **1 delete through per attempt, five times**. This is not a race that can be
   waited out; it needs the qwen3 fix (point a faster model at LightRAG, or raise
   `LLM_TIMEOUT`). Nothing user-facing is broken meanwhile: the minutes are
   correct on disk and in the manifest, and retrieval goes through
   `graph_sync.retrieve_context()`, not LightRAG's document store.
2. **`MINUTES_PROMPT_TOKEN_BUDGET` = 60k** is now the binding constraint on the
   four longest meetings (see the gradient note above). Raising it trades cost
   and prompt size for fidelity on exactly the meetings worth the most.
3. **Two name variants still unmerged** — `Faraz Mateen` and `Kathryn`. Both are
   empty registry rows, so merging is cosmetic. `Kathryn` is left alone because a
   similarity threshold loose enough to catch it also conflates Tarun/Varun
   (0.80), who are different people.

## Durable facts about this machine

- **`whisperx` IS installed** (3.8.6, `pyannote.audio` 4.0.7, `torch` 2.8.0+cpu,
  wespeaker weights cached). Replicate is the default ASR backend by choice.
- **`torchcodec` cannot load its FFmpeg DLLs**, so pyannote's file-path decoding
  fails here. Workaround used by `pipeline/enroll.py`: decode with
  `whisperx.load_audio`, pass pyannote `{"waveform", "sample_rate"}` in memory.
- **Audio is deleted in the same loop iteration as transcription.**
  `cmd_transcribe` → `capture.cleanup_transcribed_audio()`. Any future feature
  needing the waveform must run before that call. This is why voice clips cannot
  be re-cut for the existing corpus, and why `SNIPPET_COUNT` only helps forward.
- **Antigravity is an agentic CLI** with file/command/browser tools, reporting
  `permission_mode: proceed-in-sandbox`. Not sandboxed the way the `claude`
  provider's `allowed_tools=[]` is.
- A background orchestrator ingests new Drive recordings during sessions —
  meetings appeared mid-run twice on 2026-08-19. Expect row counts to move under
  you.
- The dashboard is `ThreadingHTTPServer`, so a slow request does not block the UI.

## Known-broken, not fixed

- LightRAG's own document extraction still fails; the graph is authored around it
  by `pipeline graph-sync`.
- `CLAUDE.md` describes a completely different project (an Obsidian-wiki compiler).
- `relations` (1,162 rows) are still never surfaced in the UI.
- 7 pre-existing `SIM105` lint nits (`try/except/pass`) in dashboard/capture.
- 61% of commitments are owned by an unresolved speaker label rather than a person.
