# Voice Enrollment and Speaker Identity — Design

Status: draft, pending user review
Date: 2026-08-16

## What we are building

A persistent voice identity layer. You label a speaker once; every later meeting
recognises that voice automatically. Matches the pipeline is confident about are
applied silently, uncertain ones are queued for your confirmation in the
dashboard, and unrecognised voices are offered as "new person, or merge into
someone you already know".

The model is Google Photos for faces, applied to voices: the system proposes,
you confirm, and each confirmation makes the next proposal better.

## The distinction this rests on

**Diarization** separates voices *within a single meeting*. It produces
`SPEAKER_00`, which is meaningless in the next recording — pyannote has no
memory between files.

**Speaker recognition** matches a voice *across meetings* against enrolled
voiceprints. The pipeline has none of this today.

`speakers.py:186` currently resolves names by asking an LLM to read the opening
four minutes for self-introductions and forms of address. That is a text signal
about identity, not an acoustic one, and it is re-derived from scratch every
time. It fails silently whenever nobody says a name early on — which is most
recurring internal meetings, where everyone already knows each other.

The load-bearing fact that makes this cheap: **pyannote already computes speaker
embeddings during diarization, and `asr.py` discards them.** Enrollment and
diarization share one vector space (WeSpeaker ResNet34), so this feature keeps a
number already being paid for rather than adding a second acoustic model.

## Decisions already made

| Decision | Choice | Why |
|---|---|---|
| Embedding source | Reuse pyannote's, same vector space | No new model, no second pass over audio. Enrollment and clustering stay comparable by construction. |
| Extraction point | During `transcribe`, while audio is present | Drive-backed audio is deleted after transcription (`AGENTS.md:197`). An embedding not captured then costs a re-download to recover. |
| Truth storage | Individual samples, centroid derived | A wrong confirmation must be removable. Storing only a centroid makes a mistake permanent and untraceable. |
| Decision bands | auto / review / new, plus a margin test | Absolute similarity alone confuses two similar voices. Distance to the runner-up is the signal that catches it. |
| Thresholds | Calibrated from your recordings, not shipped as constants | Published cosine thresholds assume clean enrollment audio. Far-field meeting audio moves them enough that any default would be a guess. |
| LLM resolver | Kept, as an independent second signal | Voice says who it sounds like; transcript says who got named. Agreement is stronger than either; disagreement is exactly what deserves review. |
| Voiceprint scope | Local only, never sent to a provider | Biometric data. It stays under the same rule the audio does. |

## Architecture

```
transcribe (pipeline/asr.py)
  audio → ASR → align → diarize
                          └─ NEW: per-label embedding + speech duration
                                   ↓  (audio may be deleted after this point)
identify (pipeline/voices.py)          NEW
  embedding × enrolled voiceprints → cosine → band
      auto   ≥ auto_threshold and margin ok  → label applied, confidence=inferred
      review ≥ review_threshold              → queued for confirmation
      new    below review_threshold          → queued as unknown voice
                          ↓
speakers (pipeline/speakers.py)
  voice match + existing LLM pass + speaker-overrides.yaml → name
                          ↓
dashboard (pipeline/dashboard.py)
  /api/voices/pending → confirm | reject | name | merge
                          ↓
  confirmation writes a voice_sample → centroid recomputed → next match sharper
```

## Schema

Three tables, added to `SCHEMA` in `pipeline/db.py`. All three key off
`people.canonical`, so voice identity normalises through the registry that
already prevents "Mike" and "Michael" becoming two graph nodes.

```sql
-- One row per enrolled utterance. The voiceprint is the duration-weighted mean
-- of these, computed on read: a confirmation you later regret is one DELETE,
-- and the voiceprint corrects itself.
CREATE TABLE IF NOT EXISTS voice_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical   TEXT NOT NULL,
    meeting_id  TEXT NOT NULL,
    label       TEXT NOT NULL,      -- SPEAKER_00 this came from
    embedding   BLOB NOT NULL,      -- float32 vector, np.ndarray.tobytes()
    dim         INTEGER NOT NULL,
    model       TEXT NOT NULL,      -- embedding model id
    speech_sec  REAL NOT NULL,      -- speech backing this vector
    source      TEXT NOT NULL,      -- confirmed | merged | bootstrap
    created_at  TEXT NOT NULL,
    FOREIGN KEY (canonical)  REFERENCES people(canonical)  ON DELETE CASCADE,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id)       ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_voice_samples_canonical ON voice_samples(canonical);

-- Every diarized label, with its embedding, whether or not it was ever named.
-- This is what makes retroactive enrollment possible without the original audio:
-- name a voice today and last spring's meetings can be re-matched offline.
CREATE TABLE IF NOT EXISTS speaker_matches (
    meeting_id      TEXT NOT NULL,
    label           TEXT NOT NULL,
    embedding       BLOB,
    dim             INTEGER,
    model           TEXT,
    speech_sec      REAL,
    best_canonical  TEXT,
    best_score      REAL,
    next_canonical  TEXT,           -- runner-up, for the margin test
    next_score      REAL,
    band            TEXT,           -- auto | review | new
    state           TEXT NOT NULL,  -- pending | resolved | dismissed
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (meeting_id, label),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_speaker_matches_state ON speaker_matches(state);
```

`model` is not decoration. Embeddings from different models are not comparable,
and a silent model upgrade would otherwise corrupt every score. Matching filters
on it, and a changed model means re-enrollment rather than nonsense similarities.

`init_db()` applies these through `executescript(SCHEMA)`; they are new tables,
so the `MIGRATIONS` column-patch mechanism at `db.py:257` is not involved.

## Matching

For each diarized label with at least `min_speech_sec` of speech:

1. Mean-pool the label's embedding across its speech, L2-normalise.
2. Cosine against every enrolled voiceprint sharing the same `model`.
3. Take best and runner-up.

```
best ≥ auto   AND (best − next) ≥ margin   → auto    apply, confidence=inferred
best ≥ review                              → review  queue "Is this Ali?"
otherwise                                  → new     queue as unknown voice
```

The margin test is what stops the failure mode that matters. Two people in the
same office on the same microphone can both score highly; without a margin, the
system confidently picks the wrong one, which is precisely the outcome
`speakers.py:7` was written to avoid — *"a visible gap is fixable; a confident
wrong name is not."*

Labels with less than `min_speech_sec` never auto-apply regardless of score. A
four-second embedding is noise, and someone who says "yeah, agreed" once should
not enroll anybody.

### Thresholds are calibrated, not guessed

`pipeline voices calibrate` scores every already-confirmed label against every
other and reports the same-person and different-person score distributions from
*your* recordings, with the equal-error point and a suggested triple. Shipping
constants from a paper measured on clean enrollment audio would mis-set this on
far-field meeting audio in an unknown direction.

Config, all overridable: `MMC_VOICE_AUTO`, `MMC_VOICE_REVIEW`,
`MMC_VOICE_MARGIN`, `MMC_VOICE_MIN_SPEECH_SEC`, `MMC_VOICE_MODEL`.

## Confirmation queue — dashboard

`dashboard.py` gains one GET and one POST alongside the existing routes at
`dashboard.py:157`:

- `GET  /api/voices/pending` — grouped by meeting: label, proposed name, score,
  runner-up, speech duration, and a transcript excerpt for that label.
- `POST /api/voices/resolve` — one of `confirm` (proposal correct),
  `assign` (different existing person), `create` (new person, with name),
  `merge` (this person is that person), `dismiss` (not a real speaker —
  crosstalk, a recording artefact, someone on a video call).

Each action that names a voice writes a `voice_sample` and marks the match
resolved. `merge` reassigns every sample from one canonical to the other, adds
the alias through the existing `person_aliases` table, and recomputes.

The queue lives in the dashboard specifically because it can play the audio
snippet for a pending label — you hear the voice before answering, which is the
difference between a confirmation and a coin flip. Where the source audio has
already been deleted, the excerpt renders as text only and the UI says so.

Resolving a match re-runs name resolution for that meeting and rewrites the
transcript markdown; minutes already rebuild from retained transcripts without
re-running ASR (`AGENTS.md:422`), so a late correction propagates.

## Bootstrapping

Cold start is the weak point of any enrollment system, and this one has a real
obstacle: **audio for Drive-backed meetings is deleted after transcription**, so
historic embeddings cannot simply be computed from disk.

Three paths, cheapest first:

1. **Forward only** — new meetings enroll as they run. Zero cost, useful within
   a handful of recordings.
2. **Local audio backfill** — any meeting whose audio is still present gets
   embedded without a download.
3. **Drive re-fetch** — `drive_sources` retains `drive_file_id` and
   `web_view_link`, so historic audio can be re-downloaded, embedded, and the
   local copy dropped again. Bounded by bandwidth, and worth it only for
   meetings whose speakers were already confirmed.

`pipeline voices bootstrap [--from-drive]` covers 2 and 3, seeding samples from
labels already marked `confirmed` in the `speakers` table.

## Privacy

Voiceprints are biometric identifiers, and a durable one for every customer who
has ever joined a call is a materially larger retention question than
transcripts. This spec commits to:

- Voiceprints never leave the machine. The LLM resolver keeps receiving text
  only; no embedding is ever sent to any provider.
- `pipeline voices forget <person>` deletes every sample and match for one
  person.
- The tables live in `db/manifest.db`, already gitignored and already covered by
  `pipeline/backup.py`.

## Testing

- **cosine**: identical vectors → 1.0; orthogonal → 0.0; normalisation is stable.
- **banding**: fixtures for each of auto, review, new, and the case where a high
  score with a thin margin is forced to review rather than auto-applied.
- **min speech**: a short-duration label never auto-applies at any score.
- **centroid**: duration-weighted mean; deleting a sample changes it; a
  bootstrap sample and a confirmed sample combine.
- **merge**: samples reassign, alias is written, the losing canonical is gone.
- **model guard**: samples with a different `model` are excluded from matching.
- **round trip**: embedding → BLOB → embedding preserves dtype and dim.
- **dashboard**: pending shape, each resolve action, unknown meeting → 404.
- The existing 57 tests must continue to pass.

## Explicitly out of scope

Real-time or streaming identification. Anti-spoofing. Voice-based
authentication of any kind — this names speakers in a private archive, it must
never gate access to anything. Cross-language voice matching. Automatic
re-clustering of history when a voiceprint changes; re-matching is an explicit
command, not a background job.

## Open risks

1. **whisperx does not expose pyannote's embeddings** through
   `DiarizationPipeline`. The implementation must begin with a spike confirming
   they can be recovered — either from the pipeline's intermediate output or by
   running `pyannote.audio`'s `Inference` with `window="whole"` over each
   label's speech regions on audio already in memory. If neither works cleanly,
   the fallback is a separate embedding pass in `asr.py` while the normalised
   WAV still exists, which costs time but no accuracy.
2. **Same-person mismatch across devices.** In-room versus phone-dial-in versus
   headset can push one person's embeddings apart far enough to look like two
   people. Expected mitigation is that the confirm loop naturally accumulates
   several samples per person covering their usual devices — but it means early
   review volume will be higher than it eventually settles at.
3. **pyannote 4.0 community-1 supersedes the 3.1 whisperx bundles**
   (`AGENTS.md:441`). Better clusters produce cleaner embeddings, so that
   upgrade compounds with this work. Doing it *after* enrollment starts means
   re-enrollment, since the vector space changes. Sequencing matters.
4. **Over-segmentation poisons enrollment.** `asr.py:73` already warns when the
   speaker count looks implausible. Meetings carrying that warning should be
   excluded from auto-enrollment: fragments of one person spread across four
   labels would enroll four bad voiceprints.
