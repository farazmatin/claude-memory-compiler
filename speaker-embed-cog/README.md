# speaker-embed

One vector per diarized speaker label, one call per meeting.

Intended to live in its own private repo (`speaker-embed-cog`) and be pushed to
Replicate as `farazmatin/speaker-embed`. It sits here only until it is extracted;
nothing in the meeting pipeline imports it, and the pipeline machine never builds
or runs it.

## Why it exists

`pipeline/voices.py` compares voiceprints, bands matches and clusters the
leftovers. It has never produced a vector. The producer that used to do that
loaded pyannote and torch on the pipeline machine and was retired, and since then
no new meeting has entered voice review at all. This is the replacement producer,
kept remote so the pipeline machine loads no model weights.

## Interface

    input   audio     meeting audio, any container ffmpeg reads
            regions   JSON array of {"label", "start", "end"} in seconds
            encoder   which encoder to serve

    output  {"embeddings": {label: [float, ...]},
             "dim": int, "encoder": str,
             "speech_sec": {label: float}, "skipped": {label: reason}}

Labels are opaque strings. No identity crosses the wire - the cog answers only
"what does this label sound like".

## The one thing that must not change

A label's regions are **concatenated into a single clip and embedded in one
forward pass** with `window="whole"`.

The corpus already holds vectors made exactly that way by the retired local
stage, and whether they stay comparable decides whether ~23 already-enrolled
people carry over or enrollment restarts from zero. Embedding each region
separately and averaging - a "duration-weighted centroid", which is how the
design document described it - produces a *different vector from the same audio*.
It would look plausible, score badly against the stored corpus, and be diagnosed
as "the new encoder isn't comparable" when only the procedure differed.

Same weights, 16 kHz mono float32, concatenate, one pass. That is the contract.

## Encoders

| name | state |
|---|---|
| `wespeaker-resnet34-lm` | shipped - `pyannote/wespeaker-voxceleb-resnet34-LM`, the weights the corpus already uses |
| `titanet-large` | not built; benchmark candidate |
| `ecapa-voxceleb` | not built; benchmark candidate |

Only the first is built, deliberately: it is the one that makes the existing
corpus reusable, and the other two are only worth their build weight once
something is measurably wrong with it. Adding them changes no interface.

An unknown encoder name is refused rather than substituted - serving different
weights under a requested name would silently poison the namespace the caller
stores the result under.

## Building

CI only, manually triggered (`workflow_dispatch`). A push mints a new version
hash and the pipeline pins vectors to it, so an automatic build on every commit
would keep inventing namespaces nobody asked for.

Repo secrets: `HF_TOKEN` (the pyannote checkpoints are gated),
`REPLICATE_API_TOKEN`. Repo variable: `REPLICATE_MODEL`, e.g.
`farazmatin/speaker-embed`.

Weights are fetched by `scripts/fetch_weights.py` *before* the image build and
baked in. That keeps the gated download out of the image, means a cold start
needs no token, and makes the published version hash pin the weights rather than
just the code.

The workflow's last step prints the line to paste into the pipeline's `.env`:

    MMC_REMOTE_VOICE_VERSION=<hash>

## Wiring it up

    MMC_REMOTE_VOICE_MODEL=farazmatin/speaker-embed
    MMC_REMOTE_VOICE_VERSION=<hash from the build>
    MMC_REMOTE_VOICE_ENCODER=wespeaker-resnet34-lm

Then, in order:

1. `scripts/probe_voice_comparability.py <model>:<hash>` - confirms the vectors
   match the stored ones before anything is backfilled.
2. `pipeline voice --embed`
3. `pipeline voice --rematch`
4. `pipeline voice --apply-auto --apply`
