"""A run the dashboard did not start is still a run, and must look like one.

Three things drive the same stages: `pipeline run` in a shell, the watcher, and
the dashboard's Sync & Process Recordings button. Only the third one used to be
visible. A shell run left the badge saying "Processing Idle" and the log panel
empty, while the button - correctly refused by the run lease - reported
"Pipeline stage 'all' crashed". A working pipeline and a broken button were
indistinguishable from the dashboard, which is exactly how this was reported.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pipeline import cli, dashboard, watcher


@pytest.fixture()
def lease_dir(tmp_path, monkeypatch):
    """Point every lease and run-log path at a scratch directory."""
    monkeypatch.setattr(cli, "DB_DIR", tmp_path)
    monkeypatch.setattr(cli, "RUN_LOG_FILE", tmp_path / "pipeline-run.log")
    monkeypatch.setattr(dashboard, "run_lock_path", lambda: tmp_path / "pipeline-run.lock")
    return tmp_path


def _claim_lease_for_a_foreign_pid(lease_dir, pid: int) -> None:
    """Write the lock exactly as a live run in another process would."""
    (lease_dir / "pipeline-run.lock").write_text(str(pid), encoding="utf-8")


# ── The lease knows who holds it ──────────────────────────────────────

def test_lease_holder_names_the_live_run(lease_dir) -> None:
    with cli.pipeline_lease():
        holder = watcher.lease_holder(lease_dir / "pipeline-run.lock")
    assert holder is not None
    assert holder.pid == os.getpid()
    assert holder.started_at  # an ISO timestamp the dashboard can render


def test_lease_holder_is_none_once_the_run_is_gone(lease_dir) -> None:
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    _claim_lease_for_a_foreign_pid(lease_dir, dead.pid)
    assert watcher.lease_holder(lease_dir / "pipeline-run.lock") is None


# ── The dashboard reports a run it did not start ──────────────────────

def test_status_reports_a_shell_run_as_running(lease_dir, monkeypatch) -> None:
    """The badge must not say idle while a shell run works the queue."""
    monkeypatch.setattr(dashboard, "_queue_detail", lambda: {
        "queue": {"speakers_resolved": 12},
        "queue_total": 12,
        "in_flight": {"stage": "transcribe", "label": "2026-09-02 standup", "started_at": "x"},
    })
    # A pid that is alive but is not this process: the parent will do.
    _claim_lease_for_a_foreign_pid(lease_dir, os.getppid())

    status = dashboard.get_pipeline_status()
    assert status["running"] is True
    assert status["owner"] == "external"
    assert status["holder_pid"] == os.getppid()
    assert status["stage"] == "transcribe"
    assert status["in_flight"]["label"] == "2026-09-02 standup"
    assert status["queue"] == {"speakers_resolved": 12}


def test_status_is_idle_when_no_lease_is_held(lease_dir, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "_queue_detail", lambda: {
        "queue": {}, "queue_total": 0, "in_flight": None,
    })
    status = dashboard.get_pipeline_status()
    assert status["running"] is False
    assert status["owner"] is None


# ── A refused click is information, not a crash ───────────────────────

def test_a_second_run_is_refused_with_the_time_the_first_started(
    lease_dir, monkeypatch
) -> None:
    monkeypatch.setattr(dashboard, "_queue_detail", lambda: {
        "queue": {}, "queue_total": 0, "in_flight": None,
    })
    _claim_lease_for_a_foreign_pid(lease_dir, os.getppid())

    with pytest.raises(ValueError) as refusal:
        dashboard.trigger_pipeline_run("all")

    message = str(refusal.value)
    assert "already running" in message
    assert "crash" not in message.lower()
    # The whole point: a wall clock the operator can compare against.
    assert "started " in message
    assert str(os.getppid()) in message


# ── A run's output reaches every process ──────────────────────────────

def test_a_run_writes_a_log_any_process_can_read(lease_dir) -> None:
    with cli.pipeline_lease():
        print("=== transcribe ===")
        print("[1/3] 2026-09-02 standup")

    assert cli.read_run_log() == ["=== transcribe ===", "[1/3] 2026-09-02 standup"]


def test_the_run_log_is_readable_while_the_run_is_still_going(lease_dir) -> None:
    """Buffered output would show a tailing dashboard nothing until exit."""
    with cli.pipeline_lease():
        print("[1/3] 2026-09-02 standup")
        mid_run = cli.read_run_log()
    assert mid_run == ["[1/3] 2026-09-02 standup"]


def test_each_run_starts_a_fresh_log(lease_dir) -> None:
    with cli.pipeline_lease():
        print("first run")
    with cli.pipeline_lease():
        print("second run")
    assert cli.read_run_log() == ["second run"]


def test_stage_output_still_reaches_the_real_stream(lease_dir, capsys) -> None:
    """Teeing must not swallow the console output an operator is watching."""
    with cli.pipeline_lease():
        print("=== minutes ===")
    assert "=== minutes ===" in capsys.readouterr().out


def test_the_dashboard_shows_a_shell_runs_log(lease_dir, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "_queue_detail", lambda: {
        "queue": {}, "queue_total": 0, "in_flight": None,
    })
    with cli.pipeline_lease():
        print("[2/8] transcribing")
    _claim_lease_for_a_foreign_pid(lease_dir, os.getppid())

    assert dashboard.get_pipeline_status()["logs"] == ["[2/8] transcribing"]
