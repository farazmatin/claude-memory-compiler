# PRD — Meeting Minutes Compiler

**Status:** implemented and review-complete; not yet run on real data
**Last updated:** 2026-08-12 (all 20 review findings addressed)

## Problem

A product manager records roughly five meetings a day and wants them to stay
answerable for years. The recordings exist; the knowledge in them does not survive
contact with time.

Three failures compound:

1. **On-device transcription is poor.** Google Pixel Recorder runs a small
   battery-optimised model. The audio is fine; the transcript is not usable as a
   record.
2. **Transcripts are not answerable.** Even a perfect transcript answers no
   question worth asking. An hour of speech is ~10,000 words containing maybe 500
   words of durable signal.
3. **Retrieval degrades as the archive grows.** The questions a PM actually asks
   are entity-centric and aggregative — *why did we deprioritise X*, *what has this
   customer told us*, *when did we reverse on that* — and their answers span dozens
   of meetings.

## Who this is for

One product manager, self-hosted, on an always-on CPU-only server. Not a team
product, not multi-tenant. That single-user assumption is load-bearing: it is why
loopback-only binding is acceptable and why there is no auth model beyond an API
key.

## Goals

| # | Goal | Measure |
|---|---|---|
| G1 | Recordings become structured minutes without manual effort | A nightly batch converts the day's audio unattended |
| G2 | Answers cite their source | Every claim carries an audio timestamp; every retrieval cites a meeting |
| G3 | Aggregative questions work | "Everything customer Y said" returns material from many meetings, not five chunks |
| G4 | The archive improves retroactively | A better minutes template rebuilds all history with no re-transcription |
| G5 | Marginal cost per meeting ≈ 0 | Subscription LLMs and local models only; no metered API |
| G6 | Content never leaves the host | No cloud service holds the corpus |

## Non-goals

- **Real-time or in-meeting anything.** This is an archive, not an assistant.
- **Multi-user, sharing, permissions.** One person's corpus.
- **Getting audio off the phone.** Explicitly out of scope; the pipeline starts
  from "a file in a directory". See [Capture](#capture-deferred).
- **Perfect transcription.** Good enough that names, numbers and product terms are
  right. Filler-word errors do not matter.
- **Replacing the PM's judgement.** Minutes record what was said and decided; they
  do not recommend.

## Requirements

### Functional

- **R1** Ingest audio from a watched directory, deduplicated by content hash.
- **R2** Transcribe with speaker attribution and word-level timestamps.
- **R3** Resolve speaker labels to real names, or leave them visibly unresolved.
- **R4** Compile structured minutes preserving **decisions with rationale**,
  action items with owners, customer signals, risks, and entities named.
- **R5** Flag decisions that reverse a previously recorded position.
- **R6** Index minutes into a graph + vector store and answer natural-language
  questions with citations.
- **R7** Rebuild minutes from retained transcripts when the template changes,
  without re-transcribing.
- **R8** Back up everything irreplaceable, restorably.

### Non-functional

- **N1** The nightly batch fits in one night on CPU-only hardware (~4 h for five
  meetings).
- **N2** Any stage is resumable; a crash never repeats transcription.
- **N3** Failure is loud. A batch that fails must not report success.
- **N4** Retrieval quality must not degrade as the corpus grows past thousands of
  documents.
- **N5** No service listens on a non-loopback interface.

## Constraints

These are given, not chosen, and several design decisions follow directly from
them:

| Constraint | Consequence |
|---|---|
| CPU-only server, no GPU | `large-v3-turbo` not `large-v3`; nightly batch not real-time |
| Subscription LLMs (Gemini Flash → Codex → Claude), **no metered API** | Graph extraction cannot use them and runs on local Ollama |
| No embedding model in any subscription | Embeddings are local Ollama, unconditionally |
| ~5 meetings/day, indefinitely | Postgres storage from day one; ~9k documents within two years |
| Sensitive business content | Loopback binding, local models, no cloud index |

**The most important consequence:** subscriptions cannot serve LightRAG, which needs
an HTTP endpoint, so its extraction runs on a small local model. Two jobs were
therefore moved to where the subscription does reach — the compiler emits the graph
explicitly (D17) and synthesis is split from retrieval (D18). What remains local is
graph traversal. See
[ARCHITECTURE.md](ARCHITECTURE.md#the-subscription-ceiling).

## Success criteria

Ordered by when they can be evaluated:

1. **Week 1** — one real meeting produces minutes whose decisions, owners and
   timestamps are correct on inspection. Measured word error rate on *names,
   product terms, numbers and dates* is acceptable; filler errors ignored.
2. **Week 1** — measured stage timings match the CPU budget within ~2x.
3. **Month 1** — the nightly batch runs unattended for four weeks; every failure
   was visible without being looked for.
4. **Month 3** — a question whose answer spans several meetings is answered
   correctly with citations.
5. **Month 6** — a template change rebuilds all history with no re-transcription.
6. **Month 6+** — a genuine decision reversal is flagged without being prompted.

## Capture (deferred)

Out of scope by decision, but the intended path is recorded so it is not
re-litigated:

Pixel Recorder syncs to `recorder.google.com`, which has no public API, so its
transcripts are unreachable programmatically. The plan is **Fossify Voice
Recorder** (FOSS, configurable filename patterns — deterministic parsing by
construction) plus **FolderSync** to object storage or SFTP. Syncing anywhere
other than Drive removes Google from the critical path entirely and reduces
ingestion to a filesystem watcher.

The highest-leverage improvement available anywhere in this system is not software:
a ~$30 USB-C boundary microphone. Room acoustics and mic placement move word error
rate by 10–20 points; model choice moves it by less than one.

## Open questions

Only answerable with real data on real hardware. `pipeline doctor` confirms the
environment; none of these are environment questions.

- Does `large-v3-turbo` lose accuracy that matters on real far-field meeting audio?
  Measure on one recording, counting errors only on names, product terms, numbers
  and dates.
- Is graph traversal on a 4B local model good enough once entities are supplied
  explicitly? Compiler-emitted entities (D17) removed the identification half of
  this; the traversal half is untested.
- What is query latency at ~9k documents? `--timing` instruments it; the corpus is
  empty so far.
- Are meetings in-person or virtual? Virtual would make Google Meet's Drive
  recordings (which *do* have an API) a better capture path than the phone.
