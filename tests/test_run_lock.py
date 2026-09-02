"""The pipeline-run lease keeps two runs off the same queue.

The watcher, the dashboard's Sync & Process Recordings button, and `pipeline
run` can all start the same stages. Nothing used to stop two of them running at
once, and a second run re-compiling a meeting the first had already retitled is
how minutes end up orphaned on disk.
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys

import pytest

from pipeline import cli, watcher


def test_second_run_is_refused_while_one_holds_the_lease(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "DB_DIR", tmp_path)
    with cli.pipeline_lease():
        with pytest.raises(watcher.WatcherAlreadyRunning):
            with cli.pipeline_lease():
                pass


def test_lease_is_released_so_the_next_run_can_start(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "DB_DIR", tmp_path)
    with cli.pipeline_lease():
        pass
    with cli.pipeline_lease():
        assert (tmp_path / "pipeline-run.lock").exists()
    assert not (tmp_path / "pipeline-run.lock").exists()


def test_lease_recovers_from_a_run_killed_without_cleanup(tmp_path, monkeypatch) -> None:
    """A run stopped by sign-out or a battery event leaves its lock behind."""
    monkeypatch.setattr(cli, "DB_DIR", tmp_path)
    (tmp_path / "pipeline-run.lock").write_text("99999999", encoding="utf-8")
    with cli.pipeline_lease():
        assert (tmp_path / "pipeline-run.lock").read_text(encoding="utf-8") == str(os.getpid())


def test_run_lease_is_a_different_lease_than_the_watcher_poll_loop(tmp_path, monkeypatch) -> None:
    """The watcher holds its own lease for its whole life.

    If it shared one with the run lease, its first poll would block every later
    run for as long as the watcher stayed up.
    """
    monkeypatch.setattr(cli, "DB_DIR", tmp_path)
    with watcher.WatchLease(tmp_path / "drive-watcher.lock"):
        with cli.pipeline_lease():
            pass


def test_lease_recovers_from_a_pid_that_once_named_a_real_process(tmp_path, monkeypatch) -> None:
    """The stale-lease probe has to survive a plausible dead pid, not just a silly one.

    99999999 sits outside the Windows pid space, so the probe's OpenProcess is
    rejected with a plain OSError and the recovery test above takes an easy
    branch. A pid that really did name a process takes the other branch, and
    that is the one that left db/pipeline-run.lock in place and crashed every
    Sync & Process run with "<class 'OSError'> returned a result with an
    exception set".
    """
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    gc.collect()  # drop Popen's handle so the pid is genuinely reaped on Windows

    monkeypatch.setattr(cli, "DB_DIR", tmp_path)
    (tmp_path / "pipeline-run.lock").write_text(str(dead.pid), encoding="utf-8")
    with cli.pipeline_lease():
        assert (tmp_path / "pipeline-run.lock").read_text(encoding="utf-8") == str(os.getpid())
