# Minutes Template v1

> This file is the compiler specification for stage 4. Editing it changes how
> every future meeting is compiled. If you change it semantically, bump
> `TEMPLATE_VERSION` in `pipeline/config.py` — that marks existing minutes stale
> so `pipeline minutes --recompile` rebuilds them from retained transcripts
> without re-running ASR.

## Design constraints

These are deliberate. Do not "improve" the template by making it shorter.

- **Target 600–1200 words. This is not an executive summary.** Five tidy bullets
  are useless for retrieval, because summaries drop *rationale*, and rationale is
  what answers "why did we deprioritize X?" — the most common question asked of a
  PM knowledge base months later.
- **Preserve entities verbatim.** Feature names, people, customers, releases, and
  numbers must appear exactly as spoken. The graph index builds nodes from these
  strings; paraphrasing "Project Atlas" into "the platform project" creates a
  second disconnected node for the same thing.
- **Rationale over conclusion.** "Chose Postgres" is nearly worthless. "Chose
  Postgres over DynamoDB because the team already runs it and the access pattern
  is relational" answers future questions.
- **Never invent.** If something was not discussed, omit the section. An empty
  Risks section is information; a fabricated one is corruption.
- **Attribute decisions.** Who decided matters as much as what was decided.

## Output format

Emit exactly this structure. YAML frontmatter first, then the body sections in
this order. Omit any section with no genuine content.

```markdown
---
date: YYYY-MM-DD
time: "HH:MM"
title: Short descriptive title of the meeting
type: standup | one-on-one | stakeholder | discovery | review | planning | other
attendees: [Name, Name]
entities: [Feature or project names, customers, releases discussed]
template_version: "N"
source_audio: relative/path/to/audio-or-private-drive-link
source_transcript: relative/path/to/transcript
---

# {title}

## Context
One short paragraph: what this meeting was for and what preceded it.

## Topics Discussed
- **Topic name** — what was covered, with enough substance to be useful alone.

## Decisions
- **What was decided** — decided by {name}. Rationale: {why, including the
  alternatives considered and why they lost}. [{timestamp}]

## Changed From Previous Position
- **What changed** — previously {old position}, now {new position}, because
  {reason}. [{timestamp}]

## Open Questions
- Question, and who needs to resolve it.

## Action Items
- [ ] **{owner}** — task. Due: {date or "unspecified"}. [{timestamp}]

## Customer / User Signals
- What a customer or user said, as close to verbatim as the transcript supports,
  with who said it and in what context. [{timestamp}]

## Risks, Blockers & Dependencies
- **{Risk or blocker}** — impact, and what it depends on.

## Product Entities Touched
- **{Feature / epic / release}** — what happened to it in this meeting.
```

## Section notes

**Changed From Previous Position** is the temporal layer. You will be given
excerpts from earlier minutes. Use them only to detect genuine reversals or
material evolutions of a previously recorded decision. Do not restate history for
its own sake, and do not flag a change unless the prior minutes actually
contradict what was said in this meeting. This section is how "when did we
reverse on this?" becomes answerable without a dedicated temporal database.

**Timestamps** cite the moment in the recording, in `[H:MM:SS]` form, taken from
the transcript's own timestamps. They make every claim verifiable against the
audio — which matters, because a PM gets asked "did we actually agree to that?"

**Attendees** should use resolved names. If a speaker is still an unresolved
`SPEAKER_xx` label, list it as `Unknown speaker (SPEAKER_xx)` rather than
guessing. Consistent spelling across meetings matters more than completeness.
