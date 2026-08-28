from __future__ import annotations

import os

import pytest

from pipeline import watcher


def test_cycle_does_not_process_when_capture_finds_nothing() -> None:
    called: list[str] = []
    result = watcher.run_cycle(
        lambda: {"scanned": 4, "downloaded": 0, "failed": 0},
        lambda: called.append("ingest") or 0,
        lambda: called.append("process") or 0,
    )
    assert result == watcher.CycleResult(4, 0, processed=False, failed=False)
    assert called == []


def test_cycle_ingests_and_processes_new_capture() -> None:
    called: list[str] = []
    result = watcher.run_cycle(
        lambda: {"scanned": 4, "downloaded": 1, "failed": 0},
        lambda: called.append("ingest") or 0,
        lambda: called.append("process") or 0,
    )
    assert result == watcher.CycleResult(4, 1, processed=True, failed=False)
    assert called == ["ingest", "process"]


def test_cycle_never_processes_after_capture_failure() -> None:
    called: list[str] = []
    result = watcher.run_cycle(
        lambda: {"scanned": 4, "downloaded": 1, "failed": 1},
        lambda: called.append("ingest") or 0,
        lambda: called.append("process") or 0,
    )
    assert result.failed is True
    assert called == []


def test_lease_excludes_second_process_and_recovers_dead_pid(tmp_path) -> None:
    path = tmp_path / "watcher.lock"
    with (
        watcher.WatchLease(path),
        pytest.raises(watcher.WatcherAlreadyRunning),
        watcher.WatchLease(path),
    ):
        pass
    path.write_text("99999999", encoding="utf-8")
    with watcher.WatchLease(path):
        assert path.read_text(encoding="utf-8") == str(os.getpid())
