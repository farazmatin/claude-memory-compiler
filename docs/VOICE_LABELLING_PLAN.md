# Voice Labelling — Operating Plan

Date: 2026-08-16
Status: design approved in outline, not yet built
Technical detail: `docs/superpowers/specs/2026-08-16-voice-enrollment-design.md`

Speakers get named once, by ear, on the phone, during working hours — minutes
after the meeting, while the room is still remembered. Heavy processing waits
until the owner is asleep. Nothing ever waits on the owner.

Nothing about how meetings are recorded changes: Easy Voice Recorder Pro on the
phone, auto-uploading to the private Drive folder, exactly as today.

## A day in the workflow

| Time | Who | What happens |
|---|---|---|
| 09:00 | you | Record the meeting. Unchanged from today. |
| 09:45 | — | Meeting ends, file uploads to Drive. **This upload is the trigger** — not a clock, not the night batch. |
| by 10:00 | machine | Laptop notices the new recording. Drive is polled every 15 min, 08:00–22:00. The laptop runs 24/7, so this needs nothing. |
| 10:00–10:30 | machine | **Listen pass.** Split by speaker, take a voiceprint of each, cut three short clips per person, compare against everyone known. Low process priority so the laptop stays responsive. No transcription yet — recognising a voice needs audio, not words. |
| ~10:30 | you | **Phone buzzes, only if needed.** "2 voices to label." If everyone was recognised, nothing is sent. Silence is the goal state. |
| 10:31 | you | **Label — about ten seconds.** Card opens, six seconds of audio plays, usually the guess is right and one tap accepts it. The meeting ended 45 minutes ago, so recall is easy. |
| 22:00 | — | Polling stops, notifications go quiet. |
| 01:00–07:00 | machine | **Night batch.** Full transcription, minutes, indexing. Reuses the voice separation already done during the day, so nothing is computed twice. |
| 07:00 | machine | Minutes ready, with real names in them. |
| 08:00 | — | Quiet hours end; anything held overnight arrives in one notification. |

### The batch never waits

If every card is ignored for a week, the night batch still runs and minutes still
arrive every morning — speakers simply show as `SPEAKER_01` until labelled.
Labelling later rewrites the transcript, rebuilds the minutes and updates the
search index automatically.

An unanswered card is a normal state, not an error, and never triggers an alert.

## Direct answers

**When does the batch run?** Two jobs. The listen pass runs on arrival of a new
recording, checked every 15 minutes from 08:00 to 22:00. The night batch runs
01:00–07:00 and does transcription, minutes and indexing.

**When does labelling happen?** Roughly 30–45 minutes after a meeting ends,
during working hours. Never at night — nothing is sent 22:00–08:00.

**Where does labelling happen?** In the dashboard on the phone, installed to the
home screen like an app. No address to type, no browser tab to find.

**Where does the alert arrive?** A push notification to the ntfy app on Android,
over the existing Tailscale connection. Tapping it opens the card. The message
says how many voices need labelling and nothing else — no names, no meeting
titles, because notifications appear on lock screens.

**What address and port?** The laptop's Tailscale name on port 8765 — roughly
`http://your-laptop.your-tailnet.ts.net:8765`. Reachable only from tailnet
devices, behind a login. Nothing exposed to the internet.

**Which device?** Android phone for day-to-day labelling; laptop for the one-time
setup session and for reading minutes. Same dashboard, same login, both devices.

**How is a label verified?** By listening, never by reading. Three places, below.

**How much work is it?** One setup session of roughly ten decisions covers most
of the archive. After that, one or two cards a week — only genuinely new people.

## What the card shows

One card, full screen, audio already playing and looping.

- Meeting date, time and title hint — "Fri 14 Aug · 10:00 · Ali roadmap"
- The proposal, pre-filled: "Sounds like Ali", with confidence and how much
  speech backs it
- Three clips, not one, so an unclear clip does not force a guess
- "Also heard in 11 other meetings" — one answer labels every appearance
- Progress — "2 of 3" — because a task with a visible end gets finished

Three buttons: accept, "No — someone else" (which offers the runner-up first,
then other people, then new person), and "I'm not sure".

**"I'm not sure" is a real answer.** The card returns to the queue and often
resolves itself once a nearby voice is named. Guessing would put a wrong name on
someone's action items, which is worse than no name.

No swipe gestures — a mis-swipe would silently teach the system the wrong voice.

## How labels are verified

1. **When it asks.** Three separate clips per voice; skip to the next if one is
   unclear.
2. **On the person's page.** Every clip ever filed under that person, each
   playable. Removing a wrong one corrects the voiceprint immediately. This is
   how to audit what the system thinks it knows.
3. **On the meeting page.** A small play button beside each name in the minutes.
   Doubting "Ali agreed to own this", hear the voice and correct it there.

**The system will not guess to avoid asking.** A confident wrong name silently
assigns someone else's commitments to a real person and nobody notices; a blank
`SPEAKER_01` is obviously incomplete and gets fixed.

## Dashboard screens

Everything runs from these. No terminal, no commands, no config files.

| Screen | Status | Purpose |
|---|---|---|
| **To label** | new | The card queue with a count badge, ordered so voices appearing in the most meetings come first. |
| **People** | new | Everyone known. Rename, merge duplicates, play their clips, delete. |
| **Set up voices** | new | One button to find voices in past meetings, with a progress bar. Used once. |
| **Settings** | new | One slider — *ask me more often* ⇄ *label automatically more often*. Phone access on/off. Quiet hours. |
| **Status** | new | What ran last night, what is waiting, what failed. Replaces reading log files. |
| **Meetings & minutes** | exists | Gains a play button beside each speaker name. |
| **Ask** | exists | Unchanged, but answers improve as names become correct. |

## How it gets built

Six stages in dependency order. Each must be finished before the next is worth
starting, and every stage after the first ends in something usable.

### 00 — Groundwork (blocker)

- **Dashboard login**, from `2026-08-14-desktop-app-design.md`. Phone access
  means recorded voice leaves the laptop; that cannot happen on an
  unauthenticated page.
- **Diarization upgrade.** Must happen *before* anyone is labelled — it changes
  how voices are measured, so doing it later means labelling everyone twice.

Nothing visible changes for the owner.

### 01 — Start keeping the voice clips (silent)

Embeddings and snippets stored during every transcribe. New tables. No screens,
no notifications.

Deliberately first: when the labelling screen appears it opens with a real
backlog from actual meetings rather than an empty page, and matching can be
sanity-checked against real recordings before anything depends on it.

### 02 — Labelling on the laptop (first usable version)

Matching, cross-meeting clustering, and the *To label* and *People* screens in
the desktop browser. The one-time enrollment sitting happens here.

Laptop first, because it proves the matching is good before any effort goes into
phone plumbing.

### 03 — Move it to the phone

PWA install, full-screen card with looping audio and thumb-reach buttons,
Tailscale access toggle, ntfy notifications with quiet hours.

### 04 — Ask the same day

The daytime scheduled task: watch Drive through working hours, run diarization on
arrival, separately from the night transcription.

Last, because until the earlier stages exist there is nothing to notify about.
Before this lands, labelling happens the next morning — workable, just less fresh.

### 05 — Finish the edges

Automatic calibration, *Status* screen, sensitivity slider, and the setup screen
that re-fetches old audio from Drive so historic meetings become labellable.

### Where this can go wrong

**Stage 02 is the decision point.** If matching proves unreliable on real
recordings — far-field audio, speakerphone, phone dial-in — then stages 03 and 04
are polish on something that does not work, and the right move is to stop and fix
matching first.

Stage 02 therefore ends with a measurement, not a demo: how often the top guess
is correct on the owner's own meetings.

### Note on old recordings

Already-processed meetings have no stored clips and their audio was deleted after
transcription. Making them labellable means re-fetching from Drive once (stage
05). Worth it for recurring people, not for one-off calls.
