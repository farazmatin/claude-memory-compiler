# Voiceprint auto-labeling, remote-only

Date: 2026-08-27
Status: design, awaiting owner approval of the embedding provider
Supersedes: `docs/VOICE_LABELLING_PLAN.md` and the 2026-08-16 enrollment specs
(deleted in 972abe1). Reuses their review model; replaces their mechanism.

## Problem

`voices.py` is a complete consumer with no producer. It can match, band,
cluster, confirm, and forget voiceprints — but nothing generates embeddings
anymore, so `speaker_matches` gets no new rows, `voice_clusters` stays empty,
and every meeting resolves speakers the slow way: LLM guess plus overrides,
never compounding.

The producer that existed (`pipeline/enroll.py`) was retired in 972abe1 because
it worked by loading WhisperX/Pyannote weights locally, and the repository had
just moved to remote-only transcription. The matching machinery was kept; only
the mechanism was deleted.

The goal: restore the producer without loading a single model weight locally,
so a person named once by ear is labelled automatically in every later meeting
— subject to the review workflow, never instead of it.

## The multi-pronged cascade this belongs to

Speaker identity was never resolved by one method. The design that evolved
through the 2026-08-16 specs and the old SPEAKER_GUIDE is a cascade of
independent evidence sources, weakest first, stronger evidence overwriting
weaker — and each prong is individually honest about not knowing:

| # | Prong | Evidence | State after 972abe1 |
|---|---|---|---|
| 1 | Filename hint | dominant-speaker + title hint, two-speaker calls only | alive (`speakers.py` candidates) |
| 2 | Glossary People | recurring attendees the owner already maintains | alive (`speakers.py` candidates) |
| 3 | Direct-address cues | "Thanks, Ruth" in the dialogue | alive (`speakers.py` candidates) |
| 4 | LLM opening pass | introductions and self-identification, returns null over guessing | alive (`speakers.py`, subscription CLI) |
| 5 | People registry | canonical spellings, alias folding | alive (`db.canonical_name`, `fold_into_existing_person`) |
| 6 | Manual overrides + per-label dashboard edits | ground truth, always wins | alive (`speaker-overrides.yaml`, `set_meeting_speaker`) |
| 7 | Voiceprint match | what a voice sounds like across meetings | **code alive, starved: no producer** |
| 8 | Cross-meeting clustering | one card per voice, not per appearance | **code alive, starved: no producer** |
| 9 | Human by ear | snippets on a voice card, confirm/split/dismiss | alive but mostly idle for new meetings — it consumes 7-8 |

Every prong writes candidates or suggestions, never a final identity — except
6 (human ground truth) and the auto-band of 7, which is why 7's auto-apply is
guarded by the margin test, the far-field rule, and the LLM veto, and why
auto-applied names stay `inferred` and correctable.

This design restores prongs 7-8 with a remote producer. It changes nothing
about prongs 1-6 and adds exactly one new evidence source (the embedding), so
the correction workflow — the part that makes the whole thing reviewable —
stays the one that already exists.

## What survived the retirement (kept as-is)

- `pipeline/voices.py` — voiceprints, `match`, `band` (auto/review/new with the
  margin test, far-field `min_enroll_meetings`, LLM-veto, over-segmentation
  veto), clustering, `confirm`/`dismiss`/`unsure`/`forget`.
- `speaker_matches` / `voice_samples` / `voice_clusters` schema, namespaced by
  embedding model. No migration needed.
- Dashboard review surface: `/api/speakers/queue`, voice cards, confirm with
  snippets, split-cluster, minutes refresh on confirm.
- The invariant `test_transcribe_has_no_voice_enrollment_path`.

## Constraints

- This repository never loads ASR, alignment, diarization, or speaker-embedding
  weights locally (replicate_asr.py contract).
- Replicate is the sole transcription provider. A speaker-embedding model is a
  new provider dependency and must be added explicitly to the provider policy —
  not silently.
- Nothing runs on a clock. The voice stage runs inside the existing on-demand
  flow (`run` / `watch` / explicit command) and never waits on the owner.
- An honest gap is fixable; a confident wrong name is not. Auto-apply may only
  fire when every guard in `voices.band()` passes, and what it writes must be
  visibly distinct from a human confirmation.
- Transcription remains the only paid-GPU gate the watcher opens silently. A
  new paid call per meeting must be owner-gated by configuration and visible in
  `doctor`.

## Design

### 1. Remote embedding provider (decision locked 2026-08-27)

A self-deployed cog, `farazmatin/speaker-embed` on Replicate, hardware L40S,
built and pushed by GitHub Actions CI — this machine builds nothing and loads
no weights. The cog's interface removes the label-mapping problem entirely:
it embeds the whisperx labels' own speech regions server-side and returns one
duration-weighted centroid per label.

    input:  audio (Replicate files API URI, reused from transcription)
            + regions: [{label, start, end}] from the stored transcript
            + encoder: which encoder to serve
    output: {embeddings: {label: [floats]}, dim, encoder, speech_sec}

Three encoders ship behind one interface, chosen per request:

1. `wespeaker-resnet34-lm` — the pyannote 3.1-family embedding (same
   representation family as the diarization whisperx already runs remotely)
2. `titanet-large` — SOTA-class, served from an ONNX export produced by a
   manual CI dispatch job (keeps `nemo_toolkit` out of the serving image)
3. `ecapa-voxceleb` — proven baseline control

The winner is picked by a paid benchmark pass over 5-10 real meetings,
scored against the corpus's human-confirmed labels (same/different-speaker
separation plus simulated auto-band correctness), then pinned by version
hash. Until then `REMOTE_VOICE_MODEL` stays unset and the whole stage
no-ops; the pipeline is exactly today's pipeline.

Config:

    MMC_REMOTE_VOICE_MODEL     e.g. farazmatin/speaker-embed (unset -> no-op)
    MMC_REMOTE_VOICE_VERSION   pinned version hash, set after the benchmark
    MMC_REMOTE_VOICE_ENCODER   default wespeaker-resnet34-lm

Namespacing: writes never land in the quarantined "historical" namespace.
The active namespace is `encoder@version`, resolved once by the stage and
stored in the manifest (`voice.active_namespace`), so matching and clustering
read the same namespace the producer wrote.

### 2. New stage: `pipeline/voice_embed.py` (the producer)

Runs after `speakers`, because `band()` consumes `llm_name` — the LLM pass's
guess — as a veto signal, never a source of truth.

Per meeting in `transcribed` or later state with audio on disk:

    for each diarization label:
      confirmed by a human already?
        -> bootstrap: one voice_samples row, source="bootstrap",
           source audio regions only, mark the match resolved. (This is what
           lets every name confirmed since the beginning compound the moment
           the stage first runs.)
      else:
        -> cut snippets (ffmpeg, no weights) via voices.choose_snippets
        -> request the label's embedding from REMOTE_VOICE_MODEL
        -> upsert speaker_matches: embedding, dim, model, speech_sec,
           llm_name, snippet_paths, snippet_quality, state=pending
    voices.rematch_pending()   # newly-bootstrapped prints resolve old unknowns
    voices.apply_auto()        # see §3
    voices.cluster_pending()   # what remains becomes one card per person

Idempotent: labels that already carry an embedding are skipped unless
`--force`. Re-running never re-pays for an embedded label.

If the provider call fails, the meeting keeps whatever embeddings it already
has, the stage reports the failure for that meeting only, and the run
continues — identical failure semantics to every other stage.

### 3. Closing the loop: `voices.apply_auto()`

Today nothing applies `BAND_AUTO` to `speakers` — the band was a flag with no
consequence. New function, called by the stage and by `rematch_pending()`
whenever a pending label promotes to auto:

    set_speaker(meeting, label, best_canonical, confidence="inferred")
    upsert_speaker_match(state=resolved, resolved_as=best_canonical)
    queue_minutes_refresh([meeting])

Deliberate choices:

- Confidence is `inferred`, never `confirmed`. `confirmed` means a human ear
  decided, and the merge guard in speakers.py treats it as un-degradable. An
  auto-applied name must always be correctable by exactly the review flow that
  exists today.
- The LLM-veto, margin test, `min_speech_sec`, and `min_enroll_meetings` guards
  already inside `band()` are the entire auto-apply policy. No threshold is
  added here; one dial (the sensitivity setting) still governs.
- Auto-applied labels resolve `state=resolved` but keep their embeddings, so a
  later correction rewrites the voiceprint through the normal `forget` path.

### 4. Evidence-retention guard on delete-audio

`POST /api/meetings/{id}/delete-audio` currently unlinks audio regardless of
review state. It gains a refusal (with an explicit override flag) when the
meeting still has unembedded unresolved labels or labels whose snippets were
never cut — deleting the audio then would be deleting the only evidence a
future voice card could use. Mirrors the merge spec's missing-file handling:
report, do not destroy.

### 5. CLI, run order, doctor

    uv run pipeline voice --owner "Faraz" [--force] [--meeting ID]

Sequence in `run` and `watch`:

    capture -> ingest -> transcribe -> speakers -> voice -> minutes
    -> graph-sync -> ContextProvider

- `REMOTE_VOICE_MODEL` unset: the voice stage emits one doctor-visible note and
  no-ops; `run` and `watch` proceed exactly as today.
- Explicit-first rollout: `run`/`watch` include the stage only once the
  manifest setting `voice.stage_in_run` is on; until then the stage runs only
  as `pipeline voice`.
- `doctor` gains one check: the configured model id resolves on Replicate with
  the current token. Report-only, no prediction.
- `--no-voice` on `run`/`watch` skips the stage for a single invocation.

### 6. Cost and volume

One embedding call per new meeting, over the same audio already uploaded for
transcription. At the current meeting cadence this is a rounding error next to
the transcription call it accompanies, and it is behind the same on-demand
gate: nothing the watcher does not already do.

## Order of operations

```text
per meeting:
 1. read transcript; compute per-label speech regions and speech_sec
 2. bootstrap-embed human-confirmed labels directly into voice_samples
 3. cut snippets for unresolved labels
 4. request per-label embeddings from REMOTE_VOICE_MODEL (one call)
 5. upsert speaker_matches (embedding, llm_name, snippets)
 6. rematch_pending -> apply_auto -> cluster_pending
 7. meeting advances; minutes compile with every name already applied
```

Steps 2-6 are individually idempotent; the stage may be killed and re-run at
any point.

## Testing

- Unit, over the same fakes the ASR tests use: a `VoiceEmbeddingBackend`
  protocol seam with a scripted fake.
  - stage writes rows with the right namespace, skips already-embedded labels,
    and `--force` re-embeds;
  - bootstrap path enrolls confirmed labels with `source="bootstrap"` exactly
    once per (canonical, meeting, label);
  - `apply_auto` resolves auto-band rows, writes confidence `inferred`, queues
    a minutes refresh, and refuses when `llm_name` disagrees;
  - namespace isolation: a new-model vector never matches a "historical"
    voiceprint;
  - provider failure leaves prior embeddings intact and does not advance the
    meeting.
- Invariants: `test_transcribe_has_no_voice_enrollment_path` is untouched and
  still passes; a new test asserts no torch/whisperx/pyannote import anywhere
  under `pipeline/`.
- E2E: fake embedding provider over the real CLI — audio in, minutes out with
  the auto-applied name, second meeting auto-labelled with no owner action.

## Docs to update with the code

- `docs/ARCHITECTURE.md`: provider policy gains the embedding model; flow line
  gains the voice stage.
- `docs/TESTING.md`: "no voice-enrollment stage" becomes "no speaker-embedding
  weights load locally; enrollment is remote-only".
- `SPEAKER_GUIDE.md`: auto-apply semantics (auto names are `inferred` and
  reversible via the people page / `forget`), and the delete-audio guard.
- `AGENTS.md`: sequence line and the processing-policy paragraph.
- `config.py` comment block: un-retire, restated for the remote mechanism.

## Out of scope

- The 2026-08-16 day-workflow UX (phone notifications, ntfy, PWA). The review
  surface is the dashboard that exists today.
- Re-scoring or migrating the quarantined "historical" vectors.
- Changing any threshold, the sensitivity dial, or the merge/confirm machinery.
- Reviving tombstoned spellings for auto-applied names (goes through the
  existing people-merge workflow).

## Open decisions — resolved 2026-08-27

1. Embedding model: **self-deployed `farazmatin/speaker-embed`** (L40S, CI
   built), winner picked by benchmark from wespeaker-resnet34-lm /
   titanet-large (ONNX) / ecapa-voxceleb. `meronym/speaker-diarization`
   (~$0.003/run, ECAPA, own diarization labels) is demoted to an emergency
   fallback, not a dependency. `konieshadow/speaker-diarization` was verified
   segments-only and plays no part.
2. The voice stage ships **explicit-first**: available as `pipeline voice`,
   included in `run`/`watch` only after the manifest setting
   `voice.stage_in_run` is switched on following a week of clean cards.
   `--no-voice` overrides.
3. Auto-applied names carry confidence **`inferred`**, never `confirmed`.
4. Local ffmpeg (audio decode, snippet cutting) stays on this machine. It is
   not model processing and already runs for transcription normalization;
   dashboard playback needs local clips. All model inference is Replicate.
5. The cog lives in its own private repo (`farazmatin/speaker-embed-cog`),
   built via GitHub Actions; this repo holds only the model id, version pin,
   and encoder choice.
