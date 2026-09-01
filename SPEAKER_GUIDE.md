# Speaker guide

Speaker attribution is a reviewable correction workflow. Replicate returns
speaker-labelled transcript turns remotely; Meeting Memory then resolves labels
using speaker overrides, meeting context, and the subscription provider chain.

## Review workflow

1. Open the authenticated loopback dashboard.
2. Select a meeting with speaker review.
3. Listen to the retained review snippets and inspect the transcript context.
4. Confirm a known person, create a person, or mark a label as unresolved.
5. Regenerate derived minutes through the pipeline after corrections.

Use the dashboard rather than editing transcripts or generated minutes directly.

## Automatic labelling

A match the voice matcher is confident about is banded `auto`. `pipeline voice
--apply-auto` writes those into the speakers table as `inferred` names; without
`--apply` it only reports what it would do, because the first real run against a
mature corpus requeues dozens of meetings for a minutes recompile and a reindex.

Three guarantees hold whatever the score says:

- A label you confirmed by ear is never overwritten or downgraded. The match
  resolves to your name, not the matcher's.
- A meeting with an implausible number of speaker labels is over-segmented - one
  person split across several labels - and nothing in it auto-applies.
- A match that is declined for any reason is sent to the review queue rather
  than dropped, so it stays visible as a card.

An auto-applied name is an inference, not evidence. It never enrols a voice
sample, so one wrong match cannot poison a person's voiceprint, and the row stays
in the queue so you can still correct it by ear.

`pipeline voice --rematch` re-scores and re-clusters without naming anyone. It is
safe to run at any time. `--apply` implies it, so a name is never committed from
a band nobody just computed.

## Quality rules

- Do not fabricate a speaker identity from weak evidence.
- Preserve uncertain labels rather than assigning a confident wrong name.
- Treat manually confirmed names and approved overrides as the correction record.
- Review ownership before relying on an assigned action.

## If the review queue is empty

Run `pipeline doctor`. Voice vectors are namespaced by the encoder that produced
them, and if the active namespace is not the one your stored vectors use, every
read returns nothing and the queue rebuilds itself as empty on each dashboard
load - with no error anywhere. The doctor check fails loudly when the two
disagree; the fix is the manifest setting `voice.active_namespace`.

## If speaker labels are missing

Run pipeline doctor and confirm the Replicate configuration. The remote provider
must return diarized segments for speaker review to be available. You can still
compile minutes when attribution is incomplete, but ownership should remain
unresolved until reviewed.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for provider policy and
repository boundaries.
