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
transcribe (pipeline/asr.py)
  audio → ASR → align → diarize
                          ├─ NEW: per-label embedding + speech duration
                          └─ NEW: per-label voice snippets → snippets/<meeting>/<label>-N.opus
                                   ↓  ← capture.py:491 deletes source audio AFTER this
identify (pipeline/voices.py)                                              NEW
  embedding × enrolled voiceprints → cosine → band
      auto   ≥ auto and margin ok and LLM agrees → applied, confidence=inferred
      review ≥ review, or margin thin, or LLM disagrees → queued
      new    below review                        → queued as unknown voice
                          ↓
speakers (pipeline/speakers.py)
  precedence: overrides > voice match > LLM pass
                          ↓
dashboard (pipeline/dashboard.py)
  /api/voices/pending  → play snippet, then confirm | assign | create | merge | dismiss
                          ↓
  writes voice_sample → centroid recomputed → re-resolve → recompile → reindex
```

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
published numbers assume. `pipeline voices calibrate` reports the same-person and
different-person score distributions from your own confirmed data with the
equal-error point and a suggested triple. It needs several people confirmed
across several meetings before it can say anything, so it is a month-two tool.

## Confirmation queue — dashboard

`dashboard.py` gains routes alongside those at `dashboard.py:157`:

- `GET  /api/voices/pending` — grouped by meeting: label, proposed name, score,
  runner-up, what the LLM thought, speech duration, and **snippet URLs**.
- `GET  /api/voices/snippet/<meeting>/<label>/<n>` — serves the clip, path-checked
  against `SNIPPETS_DIR` the way `_remove_handoff_file` guards the handoff root.
- `POST /api/voices/resolve` — `confirm` | `assign` (different existing person) |
  `create` (new person) | `merge` (this person is that person) | `dismiss`.

The UI leads with a play button, not a transcript. Text is shown underneath as
context, never as the basis for the decision.

Each naming action writes a `voice_sample` and resolves the match. `merge`
reassigns every sample from one canonical to the other, writes the alias through
the existing `person_aliases` table, and recomputes.

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

`pipeline voices bootstrap [--from-drive]` covers 2 and 3. Note that path 3 is
the only way to make old meetings reviewable at all, since without snippets
there is nothing to listen to.

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
- `pipeline voices forget <person>` deletes every sample, match and snippet for
  one person.
- Snippets live under `SNIPPETS_DIR`, gitignored, covered by `backup.py`.
- **`forget` is not retroactive to existing backups.** Anyone treating this as a
  deletion guarantee needs to prune backups too.

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
- The existing 57 tests must continue to pass.

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
4. **Review backlog is the real failure mode.** If pending items accumulate
   faster than they are cleared, the system degrades to the status quo with extra
   storage. The queue should be ordered by *value* — a voice appearing across many
   meetings first, since one confirmation there resolves the most history.
5. **Concurrent write during the nightly batch.** Resolving from the dashboard
   triggers recompile and reindex, which can collide with a running batch on the
   same SQLite file. Resolution should enqueue the recompile rather than perform
   it inline.
