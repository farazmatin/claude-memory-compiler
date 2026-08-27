# User Guide: Meeting Capture

Record meetings on your Pixel, keep the original audio private in Google Drive,
and process recordings when you choose.

## Normal routine

1. Record with Easy Voice Recorder Pro.
2. Stop the recording when the meeting ends.
3. Confirm the completed audio file appears in your private Drive folder.
4. On the computer, start processing when ready.

## Drive folders

Create private Drive folders:

- Easy Voice Recorder for new recordings.
- backfill-approved for a deliberate historical import.

Copy each folder ID from the URL and keep both folders private.

## Authorize the computer

1. Create a Google Cloud Desktop OAuth client with Drive API enabled.
2. Save its client JSON outside the repository.
3. Set the permanent user settings:

    [Environment]::SetEnvironmentVariable("MMC_DRIVE_FUTURE_FOLDER_ID", "future-folder-id", "User")
    [Environment]::SetEnvironmentVariable("MMC_DRIVE_BACKFILL_FOLDER_ID", "backfill-folder-id", "User")
    [Environment]::SetEnvironmentVariable("MMC_DRIVE_CREDENTIALS", "$env:LOCALAPPDATA\MeetingMinutesCompiler\drive-client.json", "User")

4. In a new PowerShell window:

    uv sync --extra dev
    uv run pipeline init
    uv run pipeline auth-drive

The browser grants read-only Drive access. Keep the client and token files
outside the repository.

## Process recordings

Configure REPLICATE_API_TOKEN in .env. Then check capture without mutation:

    uv run pipeline capture --dry-run

If the check reports authorization failure, run pipeline auth-drive and complete
browser sign-in. When the preview is correct, process the ready recordings:

    uv run pipeline run --owner "Your Name"
    uv run pipeline status

Replicate performs transcription remotely. Codex, Claude, and Antigravity
subscription CLIs compile meeting meaning after transcription.

## Historical import

Place only approved historical audio in backfill-approved. Preview and process:

    uv run pipeline capture --dry-run
    uv run pipeline run --owner "Your Name"
    uv run pipeline capture --complete-backfill

Resolve ambiguous historical filenames with an ISO date before processing. Do not
complete the backfill until the approved set is fully processed.

## Review

Open the local dashboard:

    .\scripts\open-dashboard.ps1 -Port 8765

The authenticated loopback dashboard shows compiled minutes, Drive links,
speaker-review work, and bounded context search. Use dashboard actions for
speaker correction; do not edit generated minutes directly.

## Troubleshooting

- Drive authorization error: run pipeline auth-drive.
- No Drive files: verify folder ID, Google account, and completed upload.
- Replicate configuration error: configure REPLICATE_API_TOKEN, then run
  pipeline doctor.
- Missing speaker attribution: use the speaker-review workflow before relying
  on an assigned action.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for repository responsibilities
and the Product Manager context boundary.
