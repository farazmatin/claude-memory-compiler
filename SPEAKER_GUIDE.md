# Speaker guide

Speaker attribution is a reviewable correction workflow. Replicate returns
speaker-labelled transcript turns remotely; Meeting Memory then resolves labels
using speaker overrides, meeting context, the subscription provider chain, and -
once voice embedding is configured - what the person sounded like in earlier
meetings.

## Review workflow

1. Open the authenticated loopback dashboard.
2. Select a meeting with speaker review.
3. Listen to the retained review snippets and inspect the transcript context.
4. Confirm a known person, create a person, or mark a label as unresolved.
5. Regenerate derived minutes through the pipeline after corrections.

Use the dashboard rather than editing transcripts or generated minutes directly.

## Voices that carry across meetings

Diarization labels mean nothing between recordings: `SPEAKER_00` in today's
meeting is not `SPEAKER_00` in tomorrow's. `pipeline voice` embeds each label's
own speech remotely and matches it against the people already enrolled, so a
voice named once by ear is recognised afterwards.

What that changes for review:

- A name applied by voice match is recorded with confidence `inferred`, never
  `confirmed`. `confirmed` means a person listened and decided. An inferred name
  is corrected exactly like any other inferred name - name the label in the
  dashboard, and the correction wins.
- A match is applied silently only when every guard passes: enough speech, a
  clear gap to the runner-up, the person enrolled from more than one meeting,
  and no disagreement with the transcript pass. Anything short of that becomes a
  review card instead of a name.
- Confirming a voice card names every appearance of that voice at once, and
  enrolls it, so the next meeting needs less review than the last.
- A wrong enrollment is removed on the people page, which deletes that person's
  samples and clips. It is not retroactive to backups.

## Deleting a meeting's audio

The audio is the only source of both things a voice card needs: the embedding
and the clips. Deleting it while a label is still unresolved and unembedded does
not degrade the card - it removes the possibility of ever having one, so the
dashboard refuses and names the labels at risk. Running `pipeline voice` on the
meeting first clears the refusal. The deletion can still be forced deliberately.

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
