# Voice Enrollment and Speaker Identity — Design

Status: draft, pending user review
Date: 2026-08-16
Revised: 2026-08-16 — audio-first review; retained voice snippets

## What we are building

A persistent voice identity layer. You label a speaker once, **by listening to
them**; every later meeting recognises that voice automatically. Confident
matches are applied silently, uncertain ones queue for your confirmation in the
dashboard, and unrecognised voices are offered as "new person, or merge into
someone you already know".

Google Photos for faces, applied to voices: the system proposes, you confirm by
ear, and each confirmation makes the next proposal better.

## The requirement that drives the design

**Speakers can only be labelled by listening. Transcript text is not a usable
review surface.** Reading "so if we push that to Q4" tells you nothing about who
said it; hearing six seconds of the voice tells you immediately.

This collides head-on with the current retention policy. `capture.py:491`
deletes Drive-backed audio once the transcript exists, and `config.py:174`
states the intent plainly: this machine keeps audio "only long enough to make
the retained transcript". A review queue that depends on the original audio
would therefore be empty for every historical meeting and would race deletion on
every new one.

**Resolution: retain voice snippets, not audio.** During transcription, while
the audio is still on disk, cut a few short representative clips per diarized
label and keep those permanently. Full audio deletion continues unchanged.

```
3 clips × 6 s per speaker, 16 kHz mono Opus ≈ 30 KB per speaker
5 speakers ≈ 150 KB per meeting  ≈ 55 MB per 1,000 meetings
```

Against 2–4 MB per audio-hour, this is free. It makes labelling-by-ear
permanently available, decouples review from the audio retention policy, and
means a voice you name in 2027 can be confirmed against a 2026 meeting you never
got round to reviewing.

## Prior art: what Google Pixel Recorder gets right and wrong

The owner's previous tool is the clearest available specification of the target.

**Right: it asks immediately.** Record, save, label — while the conversation is
still in mind. That timing is why labelling there is tolerable at all, and it is
the behaviour this design copies.

**Wrong: it has no memory.** Every recording starts from zero, so the same people
are re-labelled forever. Effort scales linearly with meetings and never decays.
Its transcription and speaker separation are also weak, which is what makes the
labels unreliable even after the work of supplying them.

This design is Pixel Recorder's timing plus persistent voiceprints: ask while
fresh, but ask about each person **once**, then never again.

## The distinction this rests on

**Diarization** separates voices *within one meeting*. It produces `SPEAKER_00`,
meaningless in the next recording — pyannote has no memory between files.

**Speaker recognition** matches a voice *across meetings* against enrolled
voiceprints. The pipeline has none of this today.

`speakers.py:186` resolves names by asking an LLM to read the opening four
minutes for self-introductions and forms of address. That is a text signal about
identity, not an acoustic one, re-derived from scratch every time. It fails
silently whenever nobody says a name early — which is most recurring internal
meetings, where everyone already knows each other.

The fact that makes this cheap: **pyannote already computes speaker embeddings
during diarization and `asr.py` discards them.** Enrollment and diarization share
one vector space, so this keeps a number already being paid for rather than
adding a second acoustic model.

## Decisions already made

| Decision | Choice | Why |
|---|---|---|
| Review surface | Audio snippets, always | Labelling by ear is the stated requirement, not a nicety. Text-only review is not a degraded mode, it is a broken one. |
| Snippet retention | Permanent, ~30 KB/speaker | Survives audio deletion. Cheap enough that the retention debate does not arise. |
| Extraction point | During `transcribe`, before deletion | Embeddings *and* snippets. Anything not captured here costs a Drive re-download to recover. |
| Embedding source | Reuse pyannote's, same vector space | No new model, no second pass. Enrollment and clustering stay comparable by construction. |
| Truth storage | Individual samples, centroid derived | A wrong confirmation must be removable. Storing only a centroid makes a mistake permanent and untraceable. |
| Decision bands | auto / review / new, plus a margin test | Absolute similarity alone confuses two similar voices. Distance to the runner-up catches it. |
| Thresholds | Provisional defaults, calibrated once data exists | Calibration needs confirmed cross-meeting pairs, which do not exist on day one. Honest sequencing, not a constant pretending to be measured. |
| LLM resolver | Kept, as an independent second signal | Voice says who it sounds like; transcript says who got named. Agreement is stronger than either; disagreement forces review. |
| Voiceprint scope | Local only, never sent to a provider | Biometric data. Same rule as the audio. |

## Architecture

```
DAY  ── meeting ends → Drive upload → handoff poll (~15 min, 08:00-22:00)
     listen pass (below-normal priority, NO ASR)
       normalize → diarize → embed → snip → match
                     │          │       └─ snippets/<meeting>/<label>-N.opus
                     │          └─ per-label embedding + speech duration
                     └─ diarization persisted for the night pass
                                   ↓
     identify (pipeline/voices.py)                                     NEW
       embedding × enrolled voiceprints → cosine → band
           auto   ≥ auto and margin ok      → applied silently
           review ≥ review, or thin margin  → card
           new    below review              → card, unknown voice
                                   ↓
     ntfy → phone (quiet hours enforced) → PWA card → label while fresh
                                   ↓
NIGHT ── 01:00-07:00, machine only
     compile pass
       ASR → align → merge stored diarization → resolve names → minutes → index
       re-match pending · re-cluster · recompile day's corrections · calibrate
                                   ↓
                   capture.py:491 deletes source audio
                   (snippets and embeddings already retained)
```

Names are usually known before the compile pass runs, so minutes are written
correct the first time rather than repaired afterwards.

## Schema

Three tables added to `SCHEMA` in `pipeline/db.py`, keyed off `people.canonical`
so voice identity normalises through the registry that already stops "Mike" and
"Michael" becoming two graph nodes.

```sql
-- One row per enrolled utterance. The voiceprint is the duration-weighted mean
-- of these, computed on read: a confirmation you later regret is one DELETE and
-- the voiceprint corrects itself.
CREATE TABLE IF NOT EXISTS voice_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical   TEXT NOT NULL,
    meeting_id  TEXT,               -- nullable ON DELETE SET NULL, see below
    label       TEXT,
    embedding   BLOB NOT NULL,      -- float32 vector, np.ndarray.tobytes()
    dim         INTEGER NOT NULL,
    model       TEXT NOT NULL,      -- embedding model id
    speech_sec  REAL NOT NULL,
    source      TEXT NOT NULL,      -- confirmed | merged | bootstrap
    created_at  TEXT NOT NULL,
    FOREIGN KEY (canonical)  REFERENCES people(canonical) ON DELETE CASCADE,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id)      ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_voice_samples_canonical ON voice_samples(canonical);

-- Every diarized label with its embedding and snippets, named or not. This is
-- what lets you name a voice today and have last spring's meetings re-match
-- offline, without the original audio.
CREATE TABLE IF NOT EXISTS speaker_matches (
    meeting_id      TEXT NOT NULL,
    label           TEXT NOT NULL,
    embedding       BLOB,
    dim             INTEGER,
    model           TEXT,
    speech_sec      REAL,
    snippet_paths   TEXT,           -- JSON array, relative to SNIPPETS_DIR
    best_canonical  TEXT,
    best_score      REAL,
    next_canonical  TEXT,           -- runner-up, for the margin test
    next_score      REAL,
    llm_name        TEXT,           -- what the transcript pass thought
    band            TEXT,           -- auto | review | new
    state           TEXT NOT NULL,  -- pending | resolved | dismissed
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (meeting_id, label),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_speaker_matches_state ON speaker_matches(state);
```

`meeting_id` on `voice_samples` is deliberately **`ON DELETE SET NULL`, not
`CASCADE`**. Cascading would mean deleting one old meeting silently degrades the
voiceprint of everyone who spoke in it — enrollment evidence quietly
disappearing is exactly the kind of failure nobody notices until names start
going wrong. The embedding is the asset; its provenance is nice to have.

`model` is not decoration. Embeddings from different models are not comparable,
and a silent model upgrade would corrupt every score. Matching filters on it, and
a changed model means re-enrollment rather than nonsense similarities.

These are new tables, so `executescript(SCHEMA)` creates them; the column-patch
`MIGRATIONS` mechanism at `db.py:257` is not involved.

## Snippet selection

Naive selection ruins the review surface. The first six seconds of a label is
usually "yeah — sorry, can you hear me?" over crosstalk.

Pick clips that are:

- **Contiguous single-speaker**, from the longest uninterrupted runs for that
  label — no speaker change inside the clip.
- **Mid-meeting**, skipping the first ~90 seconds where join noise and
  overlapping greetings dominate.
- **Spread apart**, so three clips are not three slices of one sentence.
- **Speech, not silence** — reject clips whose aligned words cover less than
  half their duration.

If a label cannot yield a clean clip, keep the best available and mark it
`low_quality` so the UI can warn rather than silently offering six seconds of
noise for a decision.

## Matching

For each diarized label with at least `min_speech_sec` of speech:

1. Mean-pool the label's embedding across its speech regions, L2-normalise.
2. Cosine against every enrolled voiceprint sharing the same `model`.
3. Take best and runner-up.

```
best ≥ auto AND (best − next) ≥ margin AND LLM does not contradict → auto
best ≥ review, OR margin thin, OR LLM contradicts                  → review
otherwise                                                           → new
```

The margin test prevents the failure that matters. Two colleagues in the same
room on the same microphone can both score highly; without a margin the system
confidently picks the wrong one — precisely the outcome `speakers.py:7` was
written against: *"a visible gap is fixable; a confident wrong name is not."*

Labels under `min_speech_sec` never auto-apply at any score. A four-second
embedding is noise, and someone who says "yeah, agreed" once should not enroll
anybody. The cost is that a near-silent attendee may never auto-resolve; that is
the correct trade.

### Precedence, explicitly

`speakers.resolve()` currently layers LLM output then overrides. With a third
signal the order must be stated rather than emergent:

```
speaker-overrides.yaml   ground truth, always wins        → confirmed
voice match (auto band)  acoustic, sample-backed          → inferred
LLM transcript pass      text evidence, no voice memory   → inferred
unresolved                                                → unknown
```

Where voice and LLM disagree, neither is applied — the label goes to review with
both candidates shown. Disagreement is information, and silently preferring one
would throw it away.

### Thresholds

Cosine over L2-normalised embeddings. Provisional defaults, deliberately biased
toward review over auto for the first weeks:

| Setting | Provisional | Meaning |
|---|---|---|
| `MMC_VOICE_AUTO` | 0.70 | auto-apply above this |
| `MMC_VOICE_REVIEW` | 0.45 | queue for confirmation above this |
| `MMC_VOICE_MARGIN` | 0.10 | required gap to the runner-up |
| `MMC_VOICE_MIN_SPEECH_SEC` | 20 | below this, never auto-apply |

These are starting points, not measurements — far-field meeting audio moves the
same-speaker distribution down relative to the clean enrollment audio the
published numbers assume.

Calibration is a **nightly job, not a command**. Once enough confirmed pairs
exist it computes the same-person and different-person score distributions from
the owner's own recordings, finds the equal-error point, and proposes a tuned
triple. The owner sees "Matching has been tuned to your recordings — accept?",
never a cosine value. It needs several people confirmed across several meetings
before it can say anything, so it is a month-two event.

The only threshold control the owner ever touches is one sensitivity slider,
mapping to all four values at once.

## The unit of work is a voice, not a meeting

The scarce resource in this system is no longer CPU. It is the owner's
attention, and it is scarce in a way CPU never was: a review chore that takes
twenty minutes a day is abandoned inside a week, and an abandoned queue makes
the whole feature worthless.

An earlier draft grouped the queue by meeting. That is the wrong unit and it
does not survive contact with the arithmetic. If Ali appears in twelve meetings,
meeting-grouping asks twelve times. The same voice, the same decision, twelve
times — and the twelfth is no more informative than the first.

**Pending labels are therefore clustered across the whole corpus before any
human sees them.** Agglomerative clustering over the pending embeddings, at a
threshold tighter than the match threshold, so a cluster is "almost certainly one
person". One card per cluster. Answering it labels every appearance at once.

This is the same move Google Photos makes: it shows a face group, never every
photograph.

Cards are ordered by **total speaking time across the corpus**, highest first, so
the earliest decisions resolve the most history. In a personal archive the
distribution is steep — a handful of recurring colleagues dominate — which is what
makes the first sitting short and the payoff immediate.

## Review workflow — where the human actually fits

**Labelling happens during the working day, minutes after the meeting — never at
night.** The owner is asleep from 01:00 to 07:00, and a system that asks then
gets no answer.

This is the single most important scheduling fact in the design, and it points
the same way as good practice anyway: a voice is easiest to identify while the
conversation is still fresh. The owner just spoke to these people.

**Ask on arrival, not on completion.** A recording uploads to Drive as soon as
the meeting ends. That upload — not the nightly batch — is the trigger. Within
minutes the card is on the phone, while the meeting is still in mind.

**1. On arrival, during the day.** The listen pass runs, the card appears, the
owner labels it between meetings. Fresh memory does the work that acoustic
similarity would otherwise have to do alone.

**2. The enrollment sitting, once.** At cold start there are no voiceprints, so
everything is "new" and a naive queue would demand hundreds of names. The first
run clusters the *entire backlog* and presents the top voices by speaking time.
Roughly ten decisions covers most of the archive. Framed in the UI as setup with
a finish line — "10 voices cover 80% of your meetings" — not as an inbox. Setup
that ends is tolerable; an inbox that never empties is not.

Start with the owner's own voice: it is in every recording, and labelling it once
removes one of the two labels from every 1:1 in the corpus. `OWNER_NAME` already
exists in config to seed the prompt.

**3. Steady state, a trickle.** Once the recurring cast is enrolled, most labels
auto-resolve. What surfaces is genuinely new people — a new customer, a new hire —
a decision only a human can make and one worth being asked about. One or two a
week, not a daily chore.

### Labelling can never block the batch

Stated as a guarantee, because the consequence of getting it wrong is no minutes
the next morning:

- The nightly compile runs on whatever is known at 01:00. Unlabelled speakers
  compile as `SPEAKER_01`. The batch never waits, never pauses, never asks.
- An unanswered card is a normal state, not an error, and never an alert.
- A label supplied later rewrites the transcript, recompiles the minutes and
  replaces the indexed document.

The daytime loop exists so that in the common case labels are already in hand by
01:00 and the minutes are **born correct** — but nothing depends on it.

### Notifications respect sleep

Notification reuses the `MMC_ALERT_COMMAND` pattern (`config.py:130`), delivered
via self-hosted ntfy: *"3 new voices to label"* with a link.

**Quiet hours are enforced in code, not left to phone settings.** Nothing is sent
between 22:00 and 08:00 by default; anything the night batch generates is held
and delivered with the morning's first notification. A batch failure alert is the
one exception, and it is still held rather than waking the owner — a failed batch
at 03:00 cannot be fixed at 03:00.

## The listening interaction

Optimising for the hundredth decision, not the first. Every extra click multiplies
by the size of the backlog.

- **Audio plays on card open**, automatically, and loops. No click-to-play.
- **The proposal is pre-filled** — "Sounds like Ali · 0.78" — so the common case is
  a single keystroke: `Enter` to accept.
- **The runner-up is offered as a second button**, so an ambiguous pair becomes an
  A/B comparison by ear rather than a recall task.
- **Keyboard first**: `Enter` accept, `1`–`9` pick a known person, `N` new, `M`
  merge, `S` skip, `Space` replay.
- **Context is shown, and it does the heavy lifting**: meeting date, title hint,
  who else is in the room. "Your Aug 10 call with Ali" narrows a voice far faster
  than the audio alone.
- **Multiple snippets per card**, so one bad clip does not force a guess.

**"I'm not sure" is a first-class button, not a failure.** People are unreliable
at identifying voices they know only slightly, and a UI that pressures a decision
manufactures confident wrong labels — the exact outcome `speakers.py:7` exists to
prevent. Skipping returns the cluster to the queue, where it may resolve itself
later once a neighbouring voice is named.

## Phone-first, and no terminal

Two hard constraints from the owner, and they reshape more than the CSS.

**The phone is the primary review device.** Listening to six-second clips is a
couch activity. A desktop-only queue would be reviewed rarely, which is the same
as never.

**There is no terminal in this workflow.** The dashboard exists precisely to
retire the CLI. Any capability specified as a `pipeline voices ...` command is,
for this owner, a capability that does not exist. Every one of them needs a
screen — that is a requirement, not polish.

### Reaching the dashboard from the phone

`DASHBOARD_HOST` stays `127.0.0.1` as the default for anyone else. Phone access
is opt-in and goes over **Tailscale**: a private WireGuard mesh where the laptop
and the phone join one tailnet and the dashboard becomes reachable at a stable
tailnet address. No port forwarding, no public DNS, nothing exposed to the
internet, and the device list is the access list.

Defence in depth, because this serves recorded voice:

1. Tailscale — only enrolled devices can route to it at all.
2. Session authentication from `2026-08-14-desktop-app-design.md`.
3. Host-header validation from that same spec, which closes the DNS-rebinding
   finding of the 2026-08-13 review.

Bind address becomes a dashboard setting rather than an environment variable,
with plain language — "Allow access from my phone (Tailscale only)" — and it
stays off until deliberately switched on.

### The server is always up

The laptop runs 24 hours a day, lid closed included. An earlier draft treated
laptop sleep as a design problem and justified an offline-sync architecture around
it; that justification does not hold, and the complexity it bought should not be
built.

The dashboard still ships as an **installable PWA**, for one reason that survives:
a home-screen icon rather than a URL to remember and a browser tab to find. For a
review loop meant to take seconds, the cost of *getting to it* dominates.

**Offline caching and decision sync are explicitly deferred.** They solve
signal-loss on a train, not laptop uptime, and they carry a genuine correctness
risk — a decision queued against a cluster the server has since re-clustered. Not
worth it for a phone sitting on the same tailnet as an always-on server. Revisit
only if real usage shows review being blocked by connectivity.

### The card, on a phone

- **One card, full screen.** No list to scroll, no density.
- **Audio starts on its own and loops.** The primary action is listening.
- **Actions sit in the bottom third**, inside thumb reach — not the top bar.
- **Accept is the biggest target**, since the pre-filled proposal is usually right.
- **No swipe-to-decide.** A mis-swipe on a moving bus enrolls a wrong voiceprint;
  destructive-by-accident is not acceptable when the cost is silent mislabelling.
- **Progress is visible and finite** — "4 of 11" — because a bounded task gets
  finished and an unbounded one gets abandoned.
- Snippets encode as **Opus in Ogg**, natively supported by Chrome on Android.

### Notifications

Self-hosted **ntfy**, reachable over the tailnet, with the Android app subscribed
to a topic. This slots into machinery that already exists: `config.py:130`
documents `MMC_ALERT_COMMAND` with an ntfy example, so the pattern is already the
project's own.

Self-hosted rather than public `ntfy.sh` deliberately. The public server is a
third party, and the Play-flavour app routes through Firebase; neither belongs
between this archive and its owner. It runs as one more service block in the
`docker-compose.yml` already hosting LightRAG and Postgres.

**Payloads carry no content** — no names, no meeting titles, no transcript text.
"3 voices to label" and a link. A notification is a lock-screen artifact and gets
mirrored to watches and laptops; it is the wrong place for anything private, even
with a private server.

### Every command becomes a screen

| Previously specified as CLI | Becomes |
|---|---|
| `pipeline voices bootstrap` | **Setup screen.** "Find voices in my past meetings", a progress bar, and a plain-language note when old audio must be re-fetched from Drive. |
| `pipeline voices calibrate` | **Automatic.** Runs in the nightly window once enough confirmed pairs exist; surfaces as "Matching has been tuned to your recordings" with a one-tap accept. Never asks the owner to reason about cosine thresholds. |
| `pipeline voices forget <person>` | **Delete on the person's page**, with an explicit confirmation naming what is destroyed, and the honest caveat that backups are not rewritten. |
| Threshold environment variables | **A single sensitivity control** — "Ask me more often ⇄ Label automatically more often" — writing to the existing `pipeline_settings` table (`db.py:105`). Env vars remain as an advanced override, undocumented in the UI. |

The people registry needs a screen regardless: list, rename, merge, play their
enrolled samples. Merging two people is a normal correction, not an edge case,
and it currently has no non-CLI expression at all.

## Routes

`dashboard.py` gains, alongside those at `dashboard.py:157`:

- `GET  /api/voices/pending` — clusters, ordered by corpus speaking time: proposed
  name, score, runner-up, what the LLM thought, appearance count, and **snippet
  URLs** from across the cluster's meetings.
- `GET  /api/voices/snippet/<meeting>/<label>/<n>` — serves the clip, path-checked
  against `SNIPPETS_DIR` the way `_remove_handoff_file` guards the handoff root.
- `POST /api/voices/resolve` — `confirm` | `assign` | `create` | `merge` |
  `dismiss` | `unsure`, applied to a whole cluster.

The UI leads with a play button. Text sits underneath as context, never as the
basis for the decision.

Each naming action writes a `voice_sample` per constituent label and resolves the
matches. `merge` reassigns every sample from one canonical to the other, writes
the alias through the existing `person_aliases` table, and recomputes.

**Cluster splitting is required.** Clustering will occasionally group two people,
and a confirmation would then enroll a poisoned voiceprint. The card shows its
appearance count and offers "these aren't all the same person", which returns the
constituents as individual cards.

`dismiss` is **reversible and does not delete the embedding**. Over-segmentation
means a "not a real speaker" fragment is sometimes a real person who barely
spoke; a dismissal that destroyed evidence would be unrecoverable.

### Propagation after a late correction

Naming a speaker weeks later must reach the documents, not just the database:

1. Rewrite the transcript markdown with the resolved names.
2. Recompile minutes from the retained transcript — no ASR (`AGENTS.md:422`).
3. Delete the stale LightRAG document via `meetings.lightrag_doc_id` and
   re-insert, the path `cli.py:396` already handles.

Without step 3 the index keeps answering with `SPEAKER_01`, and the graph keeps
the entities it extracted under the wrong owner. This is the step most likely to
be skipped in implementation and the one that makes the feature real.

## Two passes: listen by day, compile by night

The machine is on 24 hours a day, lid closed included. The scheduling constraint
is not the hardware, it is the owner's sleep — so the work splits by *who it needs*
rather than by what is convenient.

`install-nightly-task.ps1:17` currently registers a single daily trigger at
01:00. This adds a second, daytime task.

### Pass 1 — the listen pass, on arrival (working hours)

Triggered by a new file appearing in the Drive handoff folder, polled every ~15
minutes between 08:00 and 22:00.

```
normalize → diarize → embed → snip → match → notify if uncertain
```

**No ASR.** This is the key economy: pyannote needs only audio, so speaker turns,
embeddings and snippets are all obtainable without transcribing a word.
Identifying a voice by ear does not require the transcript — the owner is
listening, not reading. Skipping ASR and alignment removes the most expensive
part of the pipeline from the owner's working hours.

The diarization output is persisted and reused by pass 2, so nothing is computed
twice.

Runs at **below-normal process priority**. The laptop is the owner's working
machine and a background job must never make it feel slow; finishing within the
hour is entirely adequate, since the card only has to arrive while the meeting is
still fresh.

Card context comes from `title_hint` — "your 10:00 with Ali" — which needs no
transcript. The LLM cross-check is unavailable until pass 2; voice is the primary
signal and any disagreement surfaces the following morning.

### Pass 2 — the compile pass (01:00–07:00)

```
ASR → align → merge with stored diarization → resolve names → minutes → index
```

Plus the jobs that only make sense in bulk:

| Job | Why it belongs at night |
|---|---|
| **Re-match pending voices** | Yesterday's unknowns may resolve against voiceprints improved today — work that disappears before the owner ever sees it. |
| **Re-cluster the queue** | Keeps one card per person as new appearances arrive, instead of fragmenting. |
| **Recompile + reindex** | Meetings labelled during the day. Deferring here also removes the SQLite contention in risk 5: the dashboard enqueues, the batch performs. |
| **Calibration** | Once enough confirmed pairs exist. |

Because pass 1 already asked, most meetings reach 01:00 with their speakers
known, and the minutes are **born with correct names** rather than being written
wrong and repaired later. The repair path still exists; it just stops being the
normal case.

Re-matching is the compounding job. Each label the owner supplies improves the
voiceprints, resolving adjacent unknowns unasked, shrinking tomorrow's queue.
Effort per meeting should fall over time — and if it plateaus, that is the signal
the thresholds are wrong.

### What the freed budget buys

`config.py:55` chose `large-v3-turbo` because CPU time was binding. With ASR now
alone in a six-hour window, the budget is roughly an hour of compute per
meeting-hour on a five-meeting night. That still does not stretch to `large-v3`,
but it comfortably buys the **diarization** upgrade — and diarization, not ASR,
governs how much labelling the owner does. Better clusters mean fewer, cleaner
cards.

Note the ordering consequence: diarization now runs in the *daytime* pass, so a
heavier model spends its cost during working hours. That argues for the CPU-viable
`pyannote 4.0 community-1` over the WavLM-large DiariZen variants until there is a
GPU, regardless of benchmark scores. Measure on real recordings before committing.

## Bootstrapping

Cold start is the weak point of any enrollment system, and this one has a real
obstacle: **historic meetings have neither embeddings nor snippets**, and their
audio is gone.

1. **Forward only** — new meetings enroll as they run. Zero cost, useful within a
   handful of recordings. This is the practical start.
2. **Local audio backfill** — meetings whose audio is still present get embedded
   and snipped without a download.
3. **Drive re-fetch** — `drive_sources` retains `drive_file_id` and
   `web_view_link`, so historic audio can be re-downloaded, embedded, snipped,
   and dropped again.

Paths 2 and 3 run from the **setup screen**, not a command: one button, a
progress bar, and a clear statement that re-fetching old audio from Drive is what
makes historic meetings reviewable at all — without snippets there is nothing to
listen to. Re-fetch is bandwidth-heavy and interruptible, and it should be
resumable rather than all-or-nothing.

## Model selection

**Embeddings — `pyannote/wespeaker-voxceleb-resnet34-LM`.** Chosen because it is
the model pyannote already loads for diarization, so enrollment lands in the same
vector space for free, with no second download and no CPU cost beyond what is
already paid. ResNet34, 256-dim, CPU-viable.

Alternatives, if measured EER on your own recordings turns out poor:

| Model | Note |
|---|---|
| [`speechbrain/spkrec-ecapa-voxceleb`](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb) | ECAPA-TDNN, 192-dim, 0.69% EER VoxCeleb. The most widely deployed baseline; CPU-fine. |
| [ReDimNet](https://arxiv.org/pdf/2407.18223) (IDRnD) | Newer architecture, SOTA on public benchmarks at *lower* inference time and model size. Separate vector space — costs a second pass. |
| [3D-Speaker](https://github.com/modelscope/3D-Speaker) CAM++ / ERes2NetV2 | Strong CPU-viable family, includes diarization recipes. |

**Diarization — upgrade to pyannote 4.0 community-1 first.** It supersedes the
3.1 whisperx bundles, same powerset architecture with training improvements, and
stays CPU-viable. Sequencing is load-bearing: **do this before enrolling anyone**,
because a diarization change that moves the embedding model invalidates every
stored voiceprint.

Stretch option once that lands:
[`BUT-FIT/diarizen-wavlm-base-s80-md`](https://huggingface.co/BUT-FIT/diarizen-wavlm-base-s80-md)
— DiariZen is built on the pyannote 3.1 framework, so embeddings stay reachable,
and it is trained on AMI, AliMeeting, NOTSOFAR-1, DIHARD3 and VoxConverse, i.e.
far-field meeting audio rather than read speech. The `wavlm-large` variants
benchmark better (~13.3% DER overall, 5.2% on VoxConverse) but want a GPU.
NVIDIA Sortformer v2 leads on AliMeeting (7.0% DER) with streaming support but is
NeMo/GPU-bound — parked until there is a GPU.

Diarization quality is upstream of everything here: bad clusters produce bad
embeddings, which produce bad voiceprints that then mislabel confidently.

## Privacy

Voiceprints are biometric identifiers, and a durable one for every customer who
has joined a call is a materially larger retention question than transcripts.
Snippets raise it further: they are actual recorded voice, kept indefinitely.

- Voiceprints and snippets never leave the machine. The LLM resolver keeps
  receiving text only.
- **Delete** on a person's page removes every sample, match and snippet for them,
  behind a confirmation that names what is destroyed.
- Snippets live under `SNIPPETS_DIR`, gitignored, covered by `backup.py`.
- **Deletion is not retroactive to existing backups.** The UI must say so at the
  point of deletion rather than implying a guarantee it cannot make.
- Phone review copies snippets onto the phone. That is a second device holding
  recorded voice, and the cache should clear on sign-out.

## Testing

- **cosine**: identical → 1.0; orthogonal → 0.0; normalisation stable.
- **banding**: fixtures for auto, review, new; a high score with a thin margin
  forced to review; voice/LLM disagreement forced to review.
- **min speech**: a short label never auto-applies at any score.
- **centroid**: duration-weighted; deleting a sample changes it; bootstrap and
  confirmed samples combine.
- **sample survival**: deleting a meeting nulls `meeting_id` and leaves the
  sample and the voiceprint intact.
- **merge**: samples reassign, alias written, losing canonical gone.
- **model guard**: samples with a different `model` excluded from matching.
- **round trip**: embedding → BLOB → embedding preserves dtype and dim.
- **snippets**: selection avoids the opening window; rejects mostly-silent
  candidates; refuses to span a speaker change; path traversal on the snippet
  route is rejected.
- **propagation**: resolving a name rewrites the transcript, recompiles minutes
  and replaces the LightRAG document.
- **clustering**: two meetings with the same voice yield one card; a split
  returns the constituents; cluster ordering follows corpus speaking time.
- **offline sync**: a decision queued on the phone applies on reconnect; a
  decision against a cluster the server has since split is re-presented rather
  than misapplied; the human answer beats a nightly re-match.
- **access control**: no session → 401 on every voices route including snippet
  audio; bad Host header → 421; snippet path traversal rejected.
- The existing 57 tests must continue to pass.

Manual verification on a real Android phone is a required step, not optional:
autoplay behaviour, Opus playback, thumb reach, and the install-to-home-screen
flow cannot be established from unit tests.

## Explicitly out of scope

Real-time or streaming identification. Anti-spoofing. Voice-based authentication
of any kind — this names speakers in a private archive and must never gate access
to anything. Cross-language voice matching. Automatic re-clustering of history
when a voiceprint changes; re-matching is an explicit command, not a background
job.

## Open risks

1. **whisperx may not expose pyannote's embeddings** through
   `DiarizationPipeline`. Implementation starts with a spike: recover them from
   the pipeline's intermediate output, or run `pyannote.audio`'s `Inference` with
   `window="whole"` over each label's regions on audio already in memory. Fallback
   is a separate embedding pass in `asr.py` while the normalised WAV exists —
   costs time, not accuracy.
2. **Same-person mismatch across devices.** In-room vs phone dial-in vs headset
   can push one person's embeddings far enough apart to look like two people.
   The confirm loop accumulates samples across devices and fixes this over time,
   but early review volume will be higher than it eventually settles at.
3. **Over-segmentation poisons enrollment.** `asr.py:73` already warns when the
   speaker count is implausible. Meetings carrying that warning must be excluded
   from auto-enrollment: one person split across four labels would enroll four
   bad voiceprints.
4. **Review abandonment is the real failure mode**, and it is a design risk
   rather than a technical one. If the queue outgrows the owner's willingness,
   the system degrades to the status quo plus storage. Cross-corpus clustering,
   value ordering, a bounded enrollment sitting and nightly re-matching all exist
   to attack this. The metric to watch is *decisions per meeting over time*: it
   must fall. If it plateaus, thresholds are mis-set or diarization is
   over-segmenting.
5. **Concurrent write during the nightly batch.** Resolved by deferring
   recompile and reindex into the nightly window rather than running them inline
   from the dashboard.
6. **Cluster contamination.** Grouping two similar voices into one card means a
   single confirmation enrolls a poisoned voiceprint that then mislabels
   confidently. Mitigated by clustering tighter than the match threshold and by
   the split action, but it is the most damaging wrong answer the UI can accept
   and deserves a conservative threshold.
7. **Phone access widens the attack surface on purpose.** Serving recorded voice
   to a second device is a real change, mitigated by the Tailscale + session +
   Host-header stack above rather than by LAN exposure. The authentication from
   `2026-08-14-desktop-app-design.md` is a hard prerequisite, not a parallel
   track: phone review must not ship before it.
8. **Daytime CPU contention.** The listen pass runs on the owner's working
   machine during working hours. Below-normal priority is the mitigation, but a
   weak laptop plus a heavier diarization model could still be felt. If it is,
   the fix is a lighter diarization model or a delay to the end of the working
   day — never moving the ask into the night, which defeats the purpose.
9. **A meeting recorded late in the evening misses its own freshness window.**
   The last poll is 22:00 and notifications are suppressed until 08:00, so a
   21:50 meeting is labelled the next morning rather than minutes later. Correct
   behaviour, but it means the freshness benefit is not universal.
10. **PWA audio autoplay is restricted on mobile browsers.** Chrome on Android
    blocks autoplay until the user has interacted with the page. In practice the
    first card needs one tap and the rest follow, but this must be verified on a
    real device early — the whole review loop assumes audio starts by itself.
