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
from datetime import datetime
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
        except (OSError, ValueError):
            self.path.unlink(missing_ok=True)
            return
        if not process_alive(pid):
            self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class LeaseHolder:
    """The process currently holding a lease, and since when."""

    pid: int
    started_at: str


def lease_holder(path: Path) -> LeaseHolder | None:
    """Who holds this lease right now, or None if it is free.

    The dashboard needs this to tell "a run is already going" from "the run
    crashed". Both used to surface as the same red line, so a correctly refused
    second click read as a broken button. `started_at` comes from the lock
    file's mtime, which is written once when the lease is taken.
    """
    path = Path(path)
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        started = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except (OSError, ValueError):
        return None
    if not process_alive(pid):
        return None
    return LeaseHolder(pid=pid, started_at=started.isoformat(timespec="seconds"))


def process_alive(pid: int) -> bool:
    """Whether `pid` still names a running process.

    Deliberately not ``os.kill(pid, 0)``. On Windows CPython routes every signal
    but CTRL_C/CTRL_BREAK to OpenProcess + TerminateProcess, so that call is not
    a probe at all: against a live run it kills it, and against a dead pid that
    once existed it raises SystemError rather than OSError. The SystemError
    escaped `_clear_stale_lease`, left db/pipeline-run.lock behind forever, and
    crashed every Sync & Process run before a single stage started.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        return True

    import ctypes
    from ctypes import wintypes

    # SYNCHRONIZE as well as the query right: without it the handle cannot be
    # waited on and WaitForSingleObject answers WAIT_FAILED for every process,
    # including this one.
    synchronize_and_query = 0x00100000 | 0x1000
    error_access_denied = 5
    wait_object_0 = 0x0

    if pid > 0xFFFFFFFF:
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(synchronize_and_query, False, pid)
    if not handle:
        # A process we are not allowed to open is still a running process, and
        # refusing the lease is the safe reading of that.
        return ctypes.get_last_error() == error_access_denied
    try:
        # A process object is signalled the moment the process exits, so only
        # WAIT_OBJECT_0 is proof of death; a running process times out and
        # anything else is an answer we did not get. Read every other result as
        # alive, because refusing a lease costs a button press and clearing a
        # live one orphans minutes. GetExitCodeProcess is not used here: it
        # cannot tell a real exit code of 259 from STILL_ACTIVE.
        return kernel32.WaitForSingleObject(handle, 0) != wait_object_0
    finally:
        kernel32.CloseHandle(handle)


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
