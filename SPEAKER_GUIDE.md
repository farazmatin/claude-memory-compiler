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

## Quality rules

- Do not fabricate a speaker identity from weak evidence.
- Preserve uncertain labels rather than assigning a confident wrong name.
- Treat manually confirmed names and approved overrides as the correction record.
- Review ownership before relying on an assigned action.

## If speaker labels are missing

Run pipeline doctor and confirm the Replicate configuration. The remote provider
must return diarized segments for speaker review to be available. You can still
compile minutes when attribution is incomplete, but ownership should remain
unresolved until reviewed.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for provider policy and
repository boundaries.
