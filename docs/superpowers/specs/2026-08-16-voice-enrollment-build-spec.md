# Voice Enrollment — Build Specification

Date: 2026-08-16
Status: ready to build, pending review
Companions:
- `2026-08-16-voice-enrollment-design.md` — why the design is shaped this way
- `../VOICE_LABELLING_PLAN.md` — how the owner's day works

This document is the buildable one: schema, module map, API contracts, screen
specs, and per-stage acceptance criteria. It is written to be followed directly,
including by an AI coding agent.

---

## 1. Deployment profile

Confirmed with the owner. Every number downstream derives from these.

| Fact | Value | Consequence |
|---|---|---|
| Recording | **One phone on the table, in person** | Far-field, single channel. The hardest case for voice matching. |
| Volume | **~5 meetings/day**, 10–30 recurring people | Setup sitting ≈ 10–15 decisions. Night batch ≈ 5 audio-hours. |
| Host | Windows laptop, on 24/7, weak CPU, no GPU | Below-normal priority for daytime work; CPU-viable models only. |
| Phone | Android, Tailscale already connected | ntfy app for notifications; PWA for review. |
| Enrollment policy | **Everyone who appears is enrolled** | No exclusion list. Deletion is the remedy. Decided knowingly. |

### 1.1 What far-field changes

A phone in the middle of a table is the defining constraint, and it is not a
detail. Consequences that must be built in, not discovered later:

1. **Same-speaker similarity drops.** Published cosine thresholds assume clean
   close-mic enrollment audio. Every threshold here is set lower than the
   textbook figure and is explicitly provisional until calibration runs.
2. **One person varies by seat.** The same colleague sitting near the phone one
   week and across the table the next produces measurably different embeddings.
   **A person is therefore not eligible for auto-matching until they have
   confirmed samples from at least two different meetings** (`MIN_ENROLL_MEETINGS`).
   This is the single most important far-field rule in this document.
3. **Over-segmentation is likely.** In-room crosstalk splits one speaker into
   several labels. `IMPLAUSIBLE_SPEAKER_COUNT` (default 8) already warns;
   meetings carrying that warning are excluded from auto-enrollment.
4. **More speech is needed per embedding.** `MIN_SPEECH_SEC` is 30, not the 20 a
   close-mic setup would tolerate.
5. **Snippet quality varies wildly.** Selection must prefer high-energy regions,
   or the owner is asked to identify someone from six seconds of distant mumble.

### 1.2 Enrollment policy note

The owner chose to enroll every speaker, having been shown the alternative. This
is recorded because it is a standing decision, not an oversight: a permanent
voiceprint is created for every person who appears in a recording, including
customers and other external parties.

The remedies that must therefore exist and work properly:

- **Delete a person** removes every sample, match and snippet (§6.4).
- Voiceprints and clips never leave the machine (§9).
- Deletion is not retroactive to backups, and the UI must say so.

---

## 2. Schema

All additions go in `SCHEMA` in `pipeline/db.py`. New tables are created by the
existing `executescript(SCHEMA)` in `init_db()`; the `MIGRATIONS` column-patch
dict at `db.py:257` is not involved.

```sql
-- ── Voice enrollment ────────────────────────────────────────────────

-- One row per enrolled utterance. A person's voiceprint is the
-- duration-weighted mean of their samples, computed on read rather than stored,
-- so removing a bad confirmation corrects the voiceprint immediately.
CREATE TABLE IF NOT EXISTS voice_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical   TEXT NOT NULL,          -- people.canonical
    meeting_id  TEXT,                   -- nullable: see ON DELETE SET NULL
    label       TEXT,                   -- diarization label it came from
    embedding   BLOB NOT NULL,          -- float32 LE, np.ndarray.tobytes()
    dim         INTEGER NOT NULL,
    model       TEXT NOT NULL,          -- embedding model id; never mix models
    speech_sec  REAL NOT NULL,
    source      TEXT NOT NULL,          -- confirmed | merged | bootstrap
    created_at  TEXT NOT NULL,
    FOREIGN KEY (canonical)  REFERENCES people(canonical) ON DELETE CASCADE,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id)      ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_voice_samples_canonical ON voice_samples(canonical);
CREATE INDEX IF NOT EXISTS idx_voice_samples_model     ON voice_samples(model);

-- Every diarized label, with its embedding and clips, named or not. Retaining
-- unnamed rows is what allows a voice named today to be matched against last
-- spring's meetings without the original audio.
CREATE TABLE IF NOT EXISTS speaker_matches (
    meeting_id      TEXT NOT NULL,
    label           TEXT NOT NULL,
    embedding       BLOB,
    dim             INTEGER,
    model           TEXT,
    speech_sec      REAL,
    snippet_paths   TEXT,               -- JSON array of paths under SNIPPETS_DIR
    snippet_quality TEXT,               -- ok | low
    best_canonical  TEXT,
    best_score      REAL,
    next_canonical  TEXT,
    next_score      REAL,
    llm_name        TEXT,               -- what the transcript pass concluded
    band            TEXT,               -- auto | review | new
    state           TEXT NOT NULL,      -- pending | resolved | dismissed
    cluster_id      TEXT,               -- groups the same voice across meetings
    resolved_as     TEXT,               -- canonical, once answered
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (meeting_id, label),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_speaker_matches_state   ON speaker_matches(state);
CREATE INDEX IF NOT EXISTS idx_speaker_matches_cluster ON speaker_matches(cluster_id);

-- Rebuilt nightly. One row per group of pending labels believed to be the same
-- person. This is the unit the owner is asked about.
CREATE TABLE IF NOT EXISTS voice_clusters (
    id              TEXT PRIMARY KEY,   -- uuid4 hex
    size            INTEGER NOT NULL,   -- labels in the cluster
    total_speech    REAL NOT NULL,      -- seconds, drives queue ordering
    best_canonical  TEXT,
    best_score      REAL,
    next_canonical  TEXT,
    next_score      REAL,
    band            TEXT NOT NULL,      -- review | new
    created_at      TEXT NOT NULL
);
```

### 2.1 Settings

Written to the existing `pipeline_settings` table (`db.py:105`). Environment
variables remain as an override for debugging and are not surfaced in the UI.

| Key | Default | Env override | Meaning |
|---|---|---|---|
| `voice.auto` | `0.62` | `MMC_VOICE_AUTO` | Auto-apply at or above |
| `voice.review` | `0.38` | `MMC_VOICE_REVIEW` | Queue a card at or above |
| `voice.margin` | `0.12` | `MMC_VOICE_MARGIN` | Required gap to runner-up |
| `voice.min_speech_sec` | `30` | `MMC_VOICE_MIN_SPEECH_SEC` | Below this, never auto-apply |
| `voice.min_enroll_meetings` | `2` | `MMC_VOICE_MIN_ENROLL_MEETINGS` | Meetings needed before a person auto-matches |
| `voice.cluster_threshold` | `0.72` | `MMC_VOICE_CLUSTER` | Tighter than `auto`, deliberately |
| `voice.model` | `pyannote/wespeaker-voxceleb-resnet34-LM` | `MMC_VOICE_MODEL` | Embedding model id |
| `voice.sensitivity` | `balanced` | — | `cautious` \| `balanced` \| `confident` — UI slider |
| `voice.quiet_start` | `22:00` | — | Notifications suppressed from |
| `voice.quiet_end` | `08:00` | — | Notifications resume |
| `voice.phone_access` | `0` | — | Bind beyond loopback |
| `voice.calibrated_at` | — | — | ISO timestamp of last calibration |

**The three thresholds are provisional.** They are set below textbook values
because of far-field audio (§1.1). Calibration (§7, stage 05) replaces them with
values measured on the owner's own recordings. The sensitivity slider scales all
three together:

| `voice.sensitivity` | auto | review | margin |
|---|---|---|---|
| `cautious` | +0.06 | +0.05 | +0.04 |
| `balanced` | baseline | baseline | baseline |
| `confident` | −0.05 | −0.04 | −0.03 |

### 2.2 Files on disk

```
SNIPPETS_DIR/<meeting_id[:12]>/<label>-<n>.opus
```

New config in `pipeline/config.py`:

```python
SNIPPETS_DIR = Path(os.environ.get("MMC_SNIPPETS", ROOT_DIR / "snippets"))
SNIPPET_COUNT = 3            # clips per label
SNIPPET_SEC = 6.0            # length of each clip
SNIPPET_SKIP_OPENING_SEC = 90.0
```

Add `SNIPPETS_DIR` to `ALL_DIRS`. Add `snippets/` to `.gitignore` and to the
`backup.py` include list.

Encoding: 16 kHz mono Opus in Ogg, ~24 kbps, via the ffmpeg already required for
`normalize_audio`. Chrome on Android plays this natively.

Budget: 3 × 6 s ≈ 30 KB per speaker; 5 speakers ≈ 150 KB per meeting; at 5
meetings/day ≈ 275 MB/year.

---

## 3. Module map

### 3.1 `pipeline/voices.py` — new

The whole matching layer. No I/O beyond the database and snippet files; no LLM.

```python
# ── Embeddings ──
def embed_labels(audio, diarization, sample_rate) -> dict[str, LabelEmbedding]
    """Mean-pooled, L2-normalised embedding per diarization label.

    LabelEmbedding = (vector: np.ndarray, speech_sec: float, regions: list[tuple[float,float]])
    Labels below MIN_SPEECH_SEC still get an embedding; the caller decides.
    """

def pack(vec: np.ndarray) -> tuple[bytes, int]      # float32 LE + dim
def unpack(blob: bytes, dim: int) -> np.ndarray

# ── Voiceprints ──
def voiceprint(conn, canonical: str, model: str) -> np.ndarray | None
    """Duration-weighted mean of that person's samples, L2-normalised.
    Returns None if they have no samples for this model."""

def enrolled(conn, model: str) -> dict[str, np.ndarray]
def meetings_enrolled(conn, canonical: str) -> int
    """Distinct meetings backing this person. Gates auto-matching (§1.1)."""

# ── Matching ──
def cosine(a: np.ndarray, b: np.ndarray) -> float
def match(vec, prints, conn) -> MatchResult
    """MatchResult = (best, best_score, next, next_score)."""

def band(result, speech_sec, llm_name, settings, conn) -> str
    """Returns 'auto' | 'review' | 'new'. Rules in §4."""

# ── Clustering ──
def cluster_pending(conn, model: str) -> int
    """Agglomerative, average linkage, cosine, at voice.cluster_threshold.
    Rebuilds voice_clusters and stamps speaker_matches.cluster_id.
    Returns cluster count. Idempotent; safe to re-run nightly."""

def split_cluster(conn, cluster_id: str) -> list[str]
    """Dissolve into one single-label cluster each."""

# ── Snippets ──
def choose_snippets(regions, words, duration) -> list[tuple[float, float, str]]
    """(start, end, quality) triples. Rules in §5."""

def cut_snippets(audio_path, meeting_id, label, spans) -> list[Path]

# ── Resolution ──
def confirm(conn, cluster_id, canonical, *, create=False) -> None
    """Write a voice_sample per constituent label, resolve the matches,
    register the person, invalidate the cached voiceprint."""

def dismiss(conn, cluster_id) -> None      # reversible; embeddings retained
def unsure(conn, cluster_id) -> None       # back to pending, deprioritised
def merge_people(conn, source: str, target: str) -> None
def forget_person(conn, canonical: str) -> int   # returns snippets deleted

# ── Re-matching ──
def rematch_pending(conn, model: str) -> int
    """Re-score every pending match against current voiceprints.
    Promotes to auto where now confident. Returns promotions."""

def calibrate(conn, model: str) -> Calibration | None
    """Same/different score distributions from confirmed data, equal-error
    point, suggested thresholds. None if fewer than MIN_CALIBRATION_PAIRS."""
```

### 3.2 `pipeline/asr.py` — extend

- `Transcript` gains `diarization: list[DiarSpan] | None` so the daytime pass can
  persist speaker turns without ASR, and the night pass can reuse them.
- New `diarize_only(audio_path, meeting_id) -> DiarizationResult`, containing the
  spans, per-label embeddings and the normalised WAV path. This is the daytime
  entry point and **must not load Whisper**.
- `WhisperXBackend.transcribe()` gains `diarization: DiarizationResult | None`.
  When supplied it skips `_diarize()` and merges the stored spans via
  `assign_word_speakers`.
- `render_markdown()` unchanged; it already accepts `speaker_names`.

The lazy-import discipline at `asr.py:246` extends to pyannote: `diarize_only`
must be importable and callable on a machine without Whisper installed.

### 3.3 `pipeline/speakers.py` — extend

`resolve()` gains a voice-match layer with explicit precedence:

```
speaker-overrides.yaml   → confirmed   (ground truth, always wins)
voice match, band=auto   → inferred
LLM transcript pass      → inferred
neither                  → unknown, label stays SPEAKER_nn
```

Where the voice match and the LLM disagree on a label, **neither is applied** and
the label is forced to `band='review'` with both candidates recorded. The
existing conservatism at `speakers.py:7` is the governing principle.

`resolve_with_llm()` is unchanged. `resolve()` writes `llm_name` into
`speaker_matches` whether or not it is applied, because disagreement is signal.

### 3.4 `pipeline/db.py` — extend

Add the three tables to `SCHEMA`, plus accessors mirroring existing style:

```python
def upsert_speaker_match(conn, meeting_id, label, **fields) -> None
def pending_clusters(conn, limit=50) -> list[sqlite3.Row]   # ordered by total_speech DESC
def cluster_labels(conn, cluster_id) -> list[sqlite3.Row]
def add_voice_sample(conn, canonical, meeting_id, label, blob, dim, model, speech_sec, source) -> int
def person_samples(conn, canonical) -> list[sqlite3.Row]
def delete_voice_sample(conn, sample_id) -> None
def get_setting_float(conn, key, default) -> float
def set_setting(conn, key, value) -> None      # if not already present
```

`add_person()` and `canonical_name()` (`db.py:494`, `db.py:529`) are reused
unchanged — every name written by this feature normalises through them.

### 3.5 `pipeline/dashboard.py` — extend

Routes in §6. Also required:

- **Session gate on every route**, from `2026-08-14-desktop-app-design.md`. This
  is a hard dependency: snippet audio must never be served unauthenticated.
- **Host header validation**, same source.
- Bind address from `voice.phone_access`: loopback when `0`, `0.0.0.0` when `1`.
  Tailscale provides the network boundary; the session gate provides
  authorisation. Never `0.0.0.0` without the gate.

### 3.6 `pipeline/capture.py` — extend

`_remove_handoff_file()` (`capture.py:491`) is the audio-deletion point. Add a
precondition: **refuse to delete until snippets and embeddings exist** for that
meeting. Losing the audio before the clips are cut makes the meeting permanently
unlabellable, and that failure would be silent.

### 3.7 `pipeline/cli.py` — extend

Commands exist for debugging and for the scheduled tasks to call. The owner never
runs them; every one has a UI equivalent (§6).

```
pipeline listen [--once]     # daytime pass; the daytime task calls this
pipeline voices rematch
pipeline voices cluster
pipeline voices calibrate [--apply]
pipeline voices bootstrap [--from-drive]
```

### 3.8 `scripts/install-daytime-task.ps1` — new

Mirrors `install-nightly-task.ps1`, which registers a single 01:00 trigger at
line 17. The new task:

- Runs `pipeline listen --once`
- Trigger: every 15 minutes between 08:00 and 22:00
- `-MultipleInstances IgnoreNew` (as the nightly task does)
- Below-normal priority: `New-ScheduledTaskSettingsSet -Priority 7`

### 3.9 `docker-compose.yml` — extend

Add ntfy alongside LightRAG and Postgres, bound to loopback and reached over
Tailscale. Self-hosted rather than public `ntfy.sh`: the public server is a third
party and the Play-store client routes via Firebase.

---

## 4. Matching rules

Evaluated per label; the cluster inherits the strongest band of its members.

```
Eligibility for auto:
    speech_sec        >= voice.min_speech_sec            (30 s)
    meetings_enrolled(best) >= voice.min_enroll_meetings (2)
    meeting is NOT flagged for implausible speaker count
    model matches the stored samples' model

Band:
    auto    best_score >= auto
            AND (best_score - next_score) >= margin
            AND NOT (llm_name and llm_name != best)
    review  best_score >= review
            OR margin too thin
            OR llm_name contradicts the voice match
    new     otherwise
```

Three rules deserve emphasis because each prevents a specific, observed failure:

1. **The margin test.** Two people at the same table on the same microphone can
   both score highly. Absolute similarity alone picks one confidently and wrongly.
2. **`min_enroll_meetings`.** A person enrolled from a single meeting is enrolled
   from a single seat. Auto-matching them invites a distance-driven mismatch.
3. **LLM contradiction forces review.** Two independent signals disagreeing is
   information; silently preferring one discards it.

---

## 5. Snippet selection

Input: the label's speech regions, the aligned words if available, the audio.

```
1. Discard regions starting before SNIPPET_SKIP_OPENING_SEC (90 s).
2. Keep only regions fully inside one label — never spanning a speaker change.
3. Score each candidate window of SNIPPET_SEC:
       energy      RMS of the window, normalised across the meeting
       density     aligned-word coverage; reject below 0.5
       isolation   gap to the nearest other-speaker turn
4. Take the top SNIPPET_COUNT windows, forcing at least 60 s between chosen
   starts so three clips are not three slices of one sentence.
5. If fewer than SNIPPET_COUNT qualify, take the best available and set
   snippet_quality='low'.
```

Energy weighting exists specifically for the far-field profile: with one phone on
a table, a distant speaker's clips must come from their loudest moments or the
owner is asked to identify a mumble.

---

## 6. API contracts

All routes require a valid session (§3.5). All respond `application/json` except
the snippet route. Errors: `{"error": "<message>"}` with the status code.

### 6.1 `GET /api/voices/pending`

Query: `limit` (default 25).

```json
{
  "clusters": [
    {
      "cluster_id": "9f2c...",
      "band": "review",
      "proposed": "Ali",
      "score": 0.78,
      "runner_up": "Sara",
      "runner_up_score": 0.61,
      "llm_name": "Ali",
      "appearances": 12,
      "total_speech_sec": 1840.5,
      "quality": "ok",
      "context": {
        "meeting_id": "a1b2c3d4e5f6",
        "date": "2026-08-14",
        "time": "10:00",
        "title_hint": "Ali roadmap"
      },
      "snippets": [
        "/api/voices/snippet/a1b2c3d4e5f6/SPEAKER_01/0",
        "/api/voices/snippet/a1b2c3d4e5f6/SPEAKER_01/1",
        "/api/voices/snippet/7d8e9f0a1b2c/SPEAKER_00/0"
      ]
    }
  ],
  "total_pending": 3,
  "known_people": ["Ali", "Sara", "Faraz"]
}
```

Ordered by `total_speech_sec` descending. `known_people` populates the "someone
else" picker without a second request.

### 6.2 `GET /api/voices/snippet/<meeting_id>/<label>/<n>`

Returns `audio/ogg`, `Cache-Control: private, max-age=3600`.

The path is resolved and checked to be inside `SNIPPETS_DIR`, mirroring the guard
in `capture.py:491`. `404` if absent, `400` on traversal.

### 6.3 `POST /api/voices/resolve`

```json
{ "cluster_id": "9f2c...", "action": "confirm" }
```

| `action` | Extra fields | Effect |
|---|---|---|
| `confirm` | — | Accept `proposed`. Writes a sample per label. |
| `assign` | `canonical` | Existing person other than the proposal. |
| `create` | `name` | New person via `add_person()`, then assign. |
| `merge` | `canonical` | Assign, and merge `proposed` into `canonical`. |
| `dismiss` | — | Not a real speaker. Reversible; embeddings kept. |
| `unsure` | — | Back to pending, deprioritised for 7 days. |
| `split` | — | Dissolve into one cluster per label. |

Response:

```json
{ "ok": true, "labels_resolved": 12, "recompile_queued": ["a1b2c3d4e5f6"], "next_cluster_id": "3e7a..." }
```

Recompiles are **queued, not executed** — the night batch performs them (§7,
stage 04). This is what keeps a dashboard tap off the same SQLite file as a
running batch.

### 6.4 People

```
GET    /api/people                  → [{canonical, role, sample_count, meeting_count, last_heard}]
GET    /api/people/<canonical>      → detail + samples[] with snippet URLs
POST   /api/people/<canonical>/rename   {"name": "..."}
POST   /api/people/<canonical>/merge    {"into": "..."}
DELETE /api/people/<canonical>/samples/<sample_id>
DELETE /api/people/<canonical>          → {"deleted_samples": n, "deleted_snippets": n}
```

`DELETE` on a person is the enrollment-policy remedy (§1.2) and must actually
remove snippet files from disk, not only rows.

### 6.5 Setup, settings, status

```
GET  /api/voices/setup            → {eligible_meetings, with_local_audio, needs_drive_fetch, done}
POST /api/voices/setup/start      {"from_drive": bool}   → 202, progress via /api/status
GET  /api/settings                → all pipeline_settings the UI exposes
POST /api/settings                {"sensitivity": "cautious"} etc.
GET  /api/status                  → last night's run, queue depth, failures, running job
```

---

## 7. Screen specifications

Existing dashboard screens (`dashboard.py:157`) are unchanged except where noted.
Navigation gains a **To label** item carrying a count badge.

### 7.1 To label

The primary screen. Phone-first; the same markup serves the laptop.

**Layout, one cluster at a time, full viewport height on mobile:**

```
┌─────────────────────────────────┐
│  To label              [ 2 of 3 ]│   progress, tabular-nums
├─────────────────────────────────┤
│  Fri 14 Aug · 10:00              │   context line
│  "Ali roadmap"                   │
│                                  │
│  Sounds like Ali                 │   proposal, largest type
│  confidence 0.78 · 18s of speech │
│                                  │
│  ▁▃▅█▆▄▂  ●───────────  0:02/0:06│   waveform + scrub
│  [ clip 1 ]  clip 2   clip 3     │   tap to switch clips
│                                  │
│  Also heard in 11 other meetings │
├─────────────────────────────────┤
│  ┌───────────────────────────┐   │
│  │   Yes, that's Ali          │   │   primary, thumb zone
│  ├───────────────────────────┤   │
│  │   No — someone else        │   │
│  ├───────────────────────────┤   │
│  │   I'm not sure             │   │   ghost, equal legitimacy
│  └───────────────────────────┘   │
│  Not a person · Split this group │   secondary text actions
└─────────────────────────────────┘
```

**Behaviour**

- Clip 1 plays on open and loops. On mobile the first card needs one tap because
  Chrome blocks autoplay before interaction; subsequent cards inherit the gesture.
- Tapping a clip chip switches source and restarts playback.
- `Space` replays, `Enter` accepts, `1`–`9` pick a known person, `N` new,
  `S` unsure, `→` skip. Keyboard is laptop-only but must work.
- "No — someone else" opens a picker ordered: runner-up first, then other people
  by recency, then "Add a new person" with a text field.
- On any resolution, advance to the next cluster without a page load.

**States**

| State | Presentation |
|---|---|
| Empty | "Nothing to label. Every voice in your recent meetings was recognised." Not an error, not an empty table. |
| Loading | Skeleton card; never a spinner over a blank page. |
| Audio fails | "This clip didn't load" with a retry, and the other clips still selectable. Never blocks the decision on one broken file. |
| Low quality | Badge: "Distant audio — hard to hear." Sets expectation before the owner blames themselves. |
| Offline | Banner "Can't reach your laptop", queue frozen. No local queueing (deferred by design). |

**Copy rules.** No jargon on this screen. Never "embedding", "cluster",
"diarization", "cosine". "Confidence" is the only number shown, and only because
it helps calibrate trust in the proposal.

### 7.2 People

List: name, sample count, meeting count, last heard, small waveform. Search box
once the list exceeds 20 — reachable at 30 recurring people.

**Person detail**

- Name, with rename inline.
- Every sample: meeting, date, duration, play button, and a remove control.
  Removing recomputes the voiceprint immediately and shows the new sample count.
- "Merge into another person" with a picker.
- "Delete this person" — destructive, requires typing the name, and states
  plainly: every clip and voiceprint removed, existing minutes keep the name,
  backups are not rewritten.

This screen is how the owner audits what the system believes. It must be possible
to listen to every clip filed under a name in under a minute.

### 7.3 Set up voices

Used once, then rarely.

- Explains in two sentences what enrollment is and why it is worth ten minutes.
- "Find voices in my past meetings" → counts eligible meetings, splits into
  *ready now* (audio present or already snipped) and *needs re-downloading from
  Drive*.
- Re-download is opt-in, shows an estimated size, and is resumable.
- Progress bar with meeting counts; the queue fills as it goes.
- On completion: "N voices found. M cover 80% of your meetings." Then straight
  into the queue.

### 7.4 Settings

Every control in plain language. No thresholds, no model names, no file paths.

| Control | Type | Backing key |
|---|---|---|
| How often should I ask? | 3-stop slider: *Ask me more* / Balanced / *Label more automatically* | `voice.sensitivity` |
| Let me label from my phone | Toggle, with a line explaining Tailscale-only | `voice.phone_access` |
| Don't disturb me between | Two time pickers | `voice.quiet_start` / `voice.quiet_end` |
| Your name | Text | `MMC_OWNER_NAME` |

When calibration has proposed new thresholds, a card appears here: "Matching has
been tuned to your recordings" with Accept and Keep current. Never a cosine value.

### 7.5 Status

Replaces reading `flush.log`.

- Last night's run: started, finished, meetings processed, failures.
- Today's listen passes: per meeting, state.
- Queue depth and how many were auto-labelled without asking — this is the number
  that shows the system is working.
- Any failure with its message and a Retry.

### 7.6 Meetings and minutes — modified

In the existing meeting detail (`dashboard.py:83`), each speaker name gains a
small play button sourcing that speaker's clip, plus a "wrong person?" link that
opens the labelling card for that label. This is verification point 3 in the
operating plan and the only place a mislabelled name gets caught after the fact.

---

## 8. Build stages and acceptance criteria

Each stage is complete only when every criterion holds.

### Stage 00 — Groundwork (blocker)

**Build:** session auth and Host validation from
`2026-08-14-desktop-app-design.md`. Upgrade diarization to pyannote 4.0
community-1.

**Accept when**
- No dashboard route returns data without a valid session; bad Host → 421.
- Diarization runs end to end on three real meetings with no regression in
  speaker count versus the current 3.1 pipeline.
- `voice.model` is recorded, and its value is the embedding model the new
  diarization actually loads.

**Why first:** the embedding model changes with the diarization upgrade. Enrolling
anyone beforehand means enrolling them again.

### Stage 01 — Capture (silent)

**Build:** schema, `voices.embed_labels`, `choose_snippets`, `cut_snippets`,
wiring into the transcribe stage, the `capture.py` deletion guard.

**Accept when**
- Every newly transcribed meeting produces one `speaker_matches` row per label,
  with a non-null embedding and 1–3 snippet files.
- Round trip: `unpack(pack(v)) == v` for dtype, dim and values.
- Audio is not deleted before snippets exist — verified by forcing a snippet
  failure and confirming the audio survives.
- Snippets are playable in Chrome on Android.
- Two weeks of real meetings accumulate with no UI and no owner involvement.

**Gate:** at least 10 meetings and 20 labels captured before stage 02 opens.

### Stage 02 — Labelling on the laptop (the decision point)

**Build:** matching, banding, clustering, `To label` and `People` screens,
resolution actions, propagation.

**Accept when**
- A voice appearing in N meetings produces **one** card, not N.
- Confirming resolves every label in the cluster.
- Split returns constituents as individual cards.
- Removing a sample changes that person's voiceprint and the change is visible.
- Precedence holds: overrides beat voice, voice beats LLM, disagreement forces
  review — with a test per branch.
- A resolution rewrites the transcript, queues the recompile, and the replacement
  LightRAG document replaces rather than duplicates the stale one.
- The owner completes a real enrollment sitting and reports the time it took.

**The measurement that decides whether to continue:**

> Over at least 30 labels from real far-field recordings, record how often the
> top proposal was correct, how often the owner answered "not sure", and the
> false-auto rate — auto-applied labels later corrected.

Targets: **top-1 correct ≥ 70%**, **false-auto ≤ 2%**.

False-auto is the one that matters. It is a wrong name applied silently, which is
the failure this whole design exists to avoid. If it exceeds 2%, raise `voice.auto`
and `voice.margin` and re-measure before building anything else.

If top-1 sits below 50% on far-field audio, stop. Stages 03–05 are polish on a
system that does not work, and the honest next step is better diarization or an
enrollment-quality fix, not a nicer UI.

### Stage 03 — Phone

**Build:** PWA manifest and service worker (shell only, no queue caching), touch
card layout, `voice.phone_access` binding, ntfy service, quiet hours.

**Accept when**
- Installs to the Android home screen and opens without browser chrome.
- Audio plays and loops on a real Android device; the autoplay-gesture path is
  confirmed on hardware, not assumed.
- All actions reachable one-handed; no action requires the top third of a 6.5"
  screen.
- Nothing is delivered between `quiet_start` and `quiet_end`; a notification
  generated at 03:00 arrives at 08:00, once, coalesced.
- Notification bodies contain no names, titles or transcript text.
- With `voice.phone_access=0` the dashboard is unreachable from the phone.
- With it on and no session, every route including snippet audio returns 401.

### Stage 04 — Daytime listen pass

**Build:** `diarize_only`, `pipeline listen`, the scheduled task, diarization
reuse in the night pass, queued-recompile execution.

**Accept when**
- `pipeline listen` completes without importing Whisper — asserted in a test.
- A file dropped in the handoff folder yields a card within 20 minutes.
- The night pass consumes the stored diarization; no meeting is diarized twice.
- The listen pass runs at below-normal priority and a subjective check confirms
  the laptop stays usable.
- The night batch runs to completion with cards outstanding, producing minutes
  with `SPEAKER_nn` for unlabelled speakers — **the non-blocking guarantee,
  tested explicitly.**
- Queued recompiles execute in the night window, not on the dashboard thread.

### Stage 05 — Edges

**Build:** calibration, Status screen, sensitivity slider, setup and Drive
backfill.

**Accept when**
- Calibration produces a suggestion only above the minimum pair count, and the
  suggestion is applied only on explicit accept.
- The sensitivity slider changes observed ask-rate in the expected direction.
- Drive backfill is resumable and does not re-download completed meetings.
- Status surfaces a real failure with a working retry.
- **No workflow requires a terminal.** Walk every capability in this document and
  confirm a screen exists for it.

---

## 9. Privacy

- Voiceprints, snippets and embeddings never leave the machine. The LLM providers
  receive text only — no audio, no vectors. This holds regardless of provider.
- Every speaker is enrolled (§1.2), so deletion must work properly: rows and
  files, verified by test.
- Snippets are served only over an authenticated session.
- `snippets/` is gitignored and included in `backup.py`.
- Deletion does not rewrite backups, and the UI says so at the point of deletion.

---

## 10. Test plan

Beyond the per-stage criteria. The existing 57 tests must continue to pass.

**`tests/test_voices.py`** — cosine identities; pack/unpack round trip;
duration-weighted centroid; sample removal; `min_enroll_meetings` gate;
model-mismatch exclusion; every band branch including thin-margin and
LLM-contradiction; short-label never auto-applies.

**`tests/test_clusters.py`** — same voice across meetings yields one cluster;
split; ordering by total speech; idempotent re-clustering; a resolved cluster
does not reappear.

**`tests/test_snippets.py`** — opening window skipped; mostly-silent rejected;
no clip spans a speaker change; minimum separation honoured; `low` quality when
candidates are poor.

**`tests/test_voice_api.py`** — every action's response shape; 401 without a
session including the snippet route; 421 on bad Host; path traversal rejected;
404 for a missing snippet.

**`tests/test_voice_propagation.py`** — resolution rewrites the transcript,
queues a recompile, and replaces rather than duplicates the indexed document.

**Manual, on real hardware** — Android autoplay, Opus playback, one-handed reach,
home-screen install, notification timing across quiet hours.

---

## 11. Open risks

1. **whisperx may not expose pyannote's embeddings** through
   `DiarizationPipeline`. Stage 01 starts with a spike; the fallback is a separate
   `Inference(window="whole")` pass over each label's regions while the normalised
   WAV is in memory. Costs time, not accuracy.
2. **Far-field accuracy is the project risk.** One phone on a table is the hardest
   realistic case, and every threshold here is a prediction until stage 02
   measures it. The stage 02 gate exists precisely to catch this before more is
   built on top.
3. **Cross-seat mismatch.** `min_enroll_meetings=2` mitigates but does not solve
   it. Expect early review volume above the steady state, and do not mistake that
   for failure.
4. **Review abandonment.** The metric is decisions per meeting over time; it must
   fall. If it plateaus, thresholds are wrong or diarization is over-segmenting.
5. **Daytime CPU contention** on a weak laptop. Below-normal priority is the
   mitigation; if it is still felt, use a lighter diarization model or defer to
   end of day — never move the ask into the night.
6. **A late-evening meeting misses its freshness window.** Recorded at 21:50, it
   is labelled next morning. Correct, but the benefit is not universal.
