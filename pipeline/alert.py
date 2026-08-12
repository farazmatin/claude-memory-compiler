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

import shlex
import subprocess

from pipeline.config import ALERT_COMMAND, ALERT_TIMEOUT_SEC, now_iso


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


def send(failed_stages: list[str], detail: str = "") -> bool:
    """Fire the alert command. Returns False if it could not be delivered.

    Never raises: an alerting failure must not mask the pipeline failure it is
    reporting, which would be a strictly worse outcome than no alert.
    """
    if not ALERT_COMMAND:
        return False

    subject, body = build_summary(failed_stages, detail)
    try:
        argv = [part.replace("{subject}", subject) for part in shlex.split(ALERT_COMMAND)]
    except ValueError as exc:
        print(f"  alert command is not parseable ({exc}); not sent")
        return False

    if not argv:
        return False

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
        return False

    if result.returncode != 0:
        print(f"  alert command exited {result.returncode}: {result.stderr.strip()[:200]}")
        return False

    print(f"  alert sent via {argv[0]}")
    return True
