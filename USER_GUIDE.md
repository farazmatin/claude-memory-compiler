# User Guide: Automatic Meeting Capture

This system records meetings on your Pixel, stores the original audio privately
in Google Drive, and compiles the recording into searchable minutes overnight.
It does not delete or alter anything in Google Drive.

## Your normal routine

1. Open **Easy Voice Recorder Pro** and record with the Pixel's built-in microphone.
2. Stop the recording when the meeting ends.
3. Nothing else is required: the app uploads the finished audio to Drive, and the
   computer collects and processes it during the nightly run.

The first time you set it up, complete the sections below.

## 1. Set up Drive folders

Create two private folders in your personal Google Drive:

- `Easy Voice Recorder` — all new Easy Voice Recorder Pro recordings.
- `backfill-approved` — a temporary one-time folder for historical Pixel Recorder files.

Open each folder in a browser and copy the part of its URL after `/folders/`.
Those strings are the two folder IDs used below. Do not share either folder.

## 2. Easy Voice Recorder Pro settings

Install **Easy Voice Recorder Pro** from Google Play. Its Pro feature set includes
automatic upload of new recordings to Google Drive. [Google Play listing](https://play.google.com/store/apps/details?id=com.coffeebeanventures.easyvoicerecorder)

In the app:

1. Open **Settings** and select the **Meeting** preset, or use the equivalent
   recording settings screen.
2. Set the format to **MP4/AAC**. It balances clear speech and small files; the
   pipeline accepts it directly.
3. In the Pro cloud/automatic-upload setting, connect the same Google account
   that owns `Easy Voice Recorder`.
4. Select `Easy Voice Recorder` as the destination and enable **automatic upload
   for new recordings**.
5. Set upload to Wi-Fi when available. The app retries later if a meeting ends
   away from Wi-Fi; the computer picks it up on the next nightly run.
6. Make one 20-second test recording. Confirm the resulting audio file appears in
   `Easy Voice Recorder` before relying on automation.

App labels can vary slightly by version. The required outcome is one completed
MP4/AAC file per recording in `Easy Voice Recorder`, uploaded without using the
Pixel Recorder share menu.

## 3. Authorize the computer

1. In Google Cloud Console, create a project, enable **Google Drive API**, and
   create an **OAuth client ID** of type **Desktop app**.
2. Download the client JSON and save it outside this repository, for example:
   `%LOCALAPPDATA%\MeetingMinutesCompiler\drive-client.json`.
3. In PowerShell, set the permanent user settings. Replace the two folder IDs:

```powershell
[Environment]::SetEnvironmentVariable("MMC_DRIVE_FUTURE_FOLDER_ID", "future-folder-id", "User")
[Environment]::SetEnvironmentVariable("MMC_DRIVE_BACKFILL_FOLDER_ID", "backfill-folder-id", "User")
[Environment]::SetEnvironmentVariable(
  "MMC_DRIVE_CREDENTIALS",
  "$env:LOCALAPPDATA\MeetingMinutesCompiler\drive-client.json",
  "User"
)
```

4. Open a new PowerShell window in this project and run:

```powershell
uv sync
uv run pipeline init
uv run pipeline auth-drive
```

The browser sign-in grants read-only Drive access. The refresh token stays outside
the repository at `%LOCALAPPDATA%\MeetingMinutesCompiler\drive-token.json`.

## 4. Import existing Pixel Recorder audio

Pixel Recorder backup lives at `recorder.google.com`; Google documents audio-file
export as a deliberate share action. [Pixel Recorder sharing](https://support.google.com/pixelphone/answer/16267696?hl=en)

On the Pixel, select only recordings dated **June 9, 2026 or later** and export
their audio files to `backfill-approved`. Do not export older recordings.

Run:

```powershell
uv run pipeline capture --dry-run
uv run pipeline run --owner "Your Name"
uv run pipeline capture --complete-backfill
```

The dry run must show pre-June 9 files as excluded and date-less backfill files
as ambiguous. Resolve ambiguous files by renaming them with an ISO date such as
`2026-06-09T1100_customer-review.m4a`, then upload again. Only run
`--complete-backfill` after the approved batch is fully processed.

## 5. End-to-end functional test

Use a new 20-second test recording with a unique name, such as
`2026-08-12T1100_capture-test`.

Before the full test, make sure ASR and the local index are available:

```powershell
uv sync --extra asr
docker compose up -d
docker compose exec ollama ollama pull qwen3:4b
docker compose exec ollama ollama pull mxbai-embed-large
```

1. Record and stop it in Easy Voice Recorder Pro.
2. Confirm the audio appears in `Easy Voice Recorder` in Drive.
3. On the computer, preview without changing local files:

```powershell
uv run pipeline capture --dry-run
```

4. Collect and process it:

```powershell
uv run pipeline capture
uv run pipeline ingest
uv run pipeline transcribe --limit 1
uv run pipeline speakers --owner "Your Name" --limit 1
uv run pipeline minutes --limit 1
uv run pipeline index --limit 1
uv run pipeline status
```

For speaker labels, identity resolution, manual overrides, and new-guest
handling, see [SPEAKER_GUIDE.md](SPEAKER_GUIDE.md).

Success means: Drive still has the original recording; `pipeline status` shows
the meeting advanced through the stages; `transcripts/` and `minutes/` contain
the artifacts; and the local `audio/` copy is removed after transcription. If
transcription is retried later, the system downloads the unchanged Drive source
again.

## 6. Automate the overnight run

Install the Windows scheduled task once:

```powershell
.\scripts\install-nightly-task.ps1 -Owner "Your Name"
```

It runs at 1:00 a.m., starts after a missed time when the PC is available, and
does not start a second run while the first is still active. Check progress with:

```powershell
uv run pipeline status
```

## 7. Review minutes and search the archive

Open the local Meeting Memory dashboard after a run:

```powershell
uv run pipeline dashboard --open
```

The browser shows every meeting, its compiled minutes, a link back to the
original private Drive audio, and any speaker-review signal. Enter a question
such as “What did we decide about the Drive capture approach?” to search the
same RAG knowledge base used by `pipeline query`.

The dashboard is read-only and defaults to `http://127.0.0.1:8765`; it does not
upload, alter, or delete recordings, minutes, or speaker names. Leave the
terminal open while using it and press `Ctrl+C` when finished. If the default
port is in use, choose another one:

```powershell
uv run pipeline dashboard --port 8766 --open
```

If a meeting says **Speaker review** or **No diarization**, the minutes are still
available, but ownership should be checked before relying on assigned actions.
Follow [SPEAKER_GUIDE.md](SPEAKER_GUIDE.md) for the one-time identity and new
speaker process.

## Troubleshooting

- **Drive is not authorized:** run `uv run pipeline auth-drive` again.
- **No Drive files found:** verify the folder ID, correct Google account, and that
  Easy Voice Recorder Pro uploaded an audio file rather than only a share link.
- **Backfill file is ambiguous:** rename it with an ISO timestamp and upload it
  again; the system deliberately will not guess a historical date.
- **Drive source changed:** upload the changed recording as a new file. The system
  refuses to re-transcribe different bytes under the old meeting record.
