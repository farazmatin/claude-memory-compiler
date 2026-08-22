"""Failure notification for the unattended batch.

`pipeline run` exits non-zero on failure, which is necessary but not sufficient.
Cron's default behaviour is to mail stdout to the local user, and on a headless
server nobody reads local mail. The realistic failure mode is that transcription
breaks in month four and surfaces in month nine as an empty query result.

So a failing batch invokes a command with a summary. Deliberately a *command*
rather than built-in email or webhook support: whatever the server already has -
`mail`, `curl` to a webhook, `ntfy`, `systemd-cat` - is better than a second
notification stack to configure and maintain.

Configure with MMC_ALERT_COMMAND. The summary arrives on stdin, and the subject is
substituted into {subject} if present:

    MMC_ALERT_COMMAND=curl -s -d @- https://ntfy.sh/my-topic
    MMC_ALERT_COMMAND=mail -s "{subject}" me@example.com
"""

from __future__ import annotations

import os
import shlex
import subprocess

from pipeline.config import ALERT_COMMAND, ALERT_TIMEOUT_SEC, now_iso


def split_command(command: str) -> list[str]:
    """Split a command string into argv, correctly on Windows too.

    `shlex.split` defaults to POSIX mode, where a backslash is an escape
    character. On Windows that silently destroys every path in the command:
    `tee C:\\Users\\me\\alert.txt` becomes `tee C:Usersmealert.txt`, and the alert
    is written to a garbage filename in the current directory instead of failing.

    This was found in the wild - it left two files with mangled names in the repo
    root of a Windows checkout. Non-POSIX mode keeps backslashes literal, which is
    what a Windows path needs.
    """
    return shlex.split(command, posix=os.name != "nt")


def build_summary(failed_stages: list[str], detail: str = "") -> tuple[str, str]:
    """(subject, body) for a failed batch."""
    subject = f"meeting pipeline FAILED: {', '.join(failed_stages)}"
    body = "\n".join(
        [
            subject,
            f"When: {now_iso()}",
            "",
            f"Failed stages: {', '.join(failed_stages)}",
            "",
            detail.strip() or "(no additional detail)",
            "",
            "Investigate with:",
            "  pipeline status     # per-meeting state and stage timings",
            "  pipeline doctor     # environment problems",
            "  pipeline retry      # requeue parked meetings",
            "",
            "Nothing is lost - transcripts and audio are retained, and stages are",
            "resumable. The next run picks up where this one stopped.",
        ]
    )
    return subject, body


def _send_windows_notification(subject: str, detail: str) -> bool:
    """Show a native desktop balloon/toast notification on Windows."""
    import sys
    if sys.platform != "win32":
        return False
    try:
        import base64
        import json
        msg = detail.strip().splitlines()[0] if detail.strip() else subject
        ps_code = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Warning
$n.BalloonTipTitle = {json.dumps(subject[:60])}
$n.BalloonTipText = {json.dumps(msg[:200])}
$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Error
$n.Visible = $true
$n.ShowBalloonTip(5000)
Start-Sleep -Milliseconds 400
$n.Dispose()
"""
        encoded = base64.b64encode(ps_code.encode("utf-16le")).decode("ascii")
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return True
    except Exception:
        return False


def send(failed_stages: list[str], detail: str = "") -> bool:
    """Fire the alert command and show a desktop alert on Windows.

    Never raises: an alerting failure must not mask the pipeline failure it is
    reporting, which would be a strictly worse outcome than no alert.
    """
    subject, body = build_summary(failed_stages, detail)

    # Always attempt desktop notification on Windows
    windows_alerted = _send_windows_notification(subject, detail)

    if not ALERT_COMMAND:
        return windows_alerted

    try:
        argv = [part.replace("{subject}", subject) for part in split_command(ALERT_COMMAND)]
    except ValueError as exc:
        print(f"  alert command is not parseable ({exc}); not sent")
        return windows_alerted

    if not argv:
        return windows_alerted

    try:
        result = subprocess.run(
            argv,
            input=body,
            capture_output=True,
            text=True,
            timeout=ALERT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  alert delivery failed: {type(exc).__name__}: {exc}")
        return windows_alerted

    if result.returncode != 0:
        print(f"  alert command exited {result.returncode}: {result.stderr.strip()[:200]}")
        return windows_alerted

    print(f"  alert sent via {argv[0]}")
    return True
