"""Continuous, single-flight Drive intake for the remote-only pipeline.

This is deliberately a watcher, not a timed batch. It polls the approved
private Drive folders at a short interval and starts the normal pipeline only
when capture staged a newly-arrived recording. Existing pending recordings are
left alone until an operator explicitly asks for ``--catch-up`` so enabling the
watcher cannot silently start a previously-approved paid transcription.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class WatcherAlreadyRunning(RuntimeError):
    """Another watcher holds the single-flight lease."""


@dataclass(frozen=True)
class CycleResult:
    scanned: int
    downloaded: int
    processed: bool
    failed: bool


class WatchLease:
    """An atomic, recoverable cross-process lease for one Drive watcher.

    Also used to serialize pipeline runs, under its own path - see
    cli.pipeline_lease - so `label` names whatever the caller is guarding.
    """

    def __init__(self, path: Path, *, label: str = "Drive watcher") -> None:
        self.path = Path(path)
        self.label = label
        self._held = False

    def __enter__(self) -> WatchLease:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clear_stale_lease()
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise WatcherAlreadyRunning(
                f"{self.label} is already running ({self.path})."
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        self._held = True
        return self

    def __exit__(self, *_: object) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def _clear_stale_lease(self) -> None:
        if not self.path.exists():
            return
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            self.path.unlink(missing_ok=True)


def run_cycle(
    capture_run: Callable[[], dict[str, int]],
    ingest_run: Callable[[], int],
    process_run: Callable[[], int],
) -> CycleResult:
    """Capture once and process only newly staged Drive recordings."""
    counts = capture_run()
    scanned = int(counts.get("scanned", 0))
    downloaded = int(counts.get("downloaded", 0))
    failed = int(counts.get("failed", 0)) > 0
    if failed:
        return CycleResult(scanned, downloaded, processed=False, failed=True)
    if not downloaded:
        return CycleResult(scanned, downloaded, processed=False, failed=False)

    if ingest_run():
        return CycleResult(scanned, downloaded, processed=False, failed=True)
    return CycleResult(scanned, downloaded, processed=True, failed=bool(process_run()))


def watch(
    cycle: Callable[[], CycleResult], *, interval_sec: float, sleep: Callable[[float], None] = time.sleep
) -> int:
    """Run cycles forever, keeping failures visible without dropping the watcher."""
    while True:
        result = cycle()
        state = "failed" if result.failed else "processed" if result.processed else "waiting"
        print(
            f"Drive watcher: {state}; scanned {result.scanned}, "
            f"downloaded {result.downloaded}."
        )
        sleep(interval_sec)
