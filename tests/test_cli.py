"""CLI reporting surfaces that don't need the full e2e harness.

test_e2e.py already covers `pipeline status` end to end against a real run;
these are narrower and cheaper checks of the failure-visibility section, run
directly against a manifest fixture instead of a full pipeline pass.
"""

from __future__ import annotations

import argparse

from pipeline import cli, db

from .conftest import make_meeting


def test_status_reports_recent_stage_failures(manifest, capsys):
    """A failed-and-later-retried stage has no other trace anywhere a human
    looks - `pipeline status` must surface it explicitly."""
    make_meeting(manifest, "m1", "2026-08-10", title_hint="Roadmap review")
    run_id = db.start_stage(manifest, "m1", "transcribe")
    db.finish_stage(manifest, run_id, False, "TimeoutError: replicate call timed out")
    manifest.commit()

    assert cli.cmd_status(argparse.Namespace()) == 0
    out = capsys.readouterr().out

    assert "Recent stage failures" in out
    assert "transcribe" in out
    assert "TimeoutError: replicate call timed out" in out
    assert "Roadmap review" in out


def test_status_omits_stage_failures_section_when_there_are_none(manifest, capsys):
    make_meeting(manifest, "m1", "2026-08-10")
    manifest.commit()

    assert cli.cmd_status(argparse.Namespace()) == 0
    out = capsys.readouterr().out

    assert "Recent stage failures" not in out


# ── Alerting gate: skip vs. genuine failure ─────────────────────────
#
# cmd_minutes counts a junk-recording park separately from `failures` and
# still returns 0 for a skip-only run (see cmd_minutes's `return 1 if failures
# else 0`). _run_all's `failed` list - and therefore alert.send - is driven
# entirely by each stage handler's return code, so that distinction is what
# keeps a routine park (~5/week of accidental phone recordings) from paging
# anyone. These stub out the stage handlers rather than running a full batch,
# to isolate _run_all's routing logic from the stages themselves.

def _run_all_args() -> argparse.Namespace:
    return argparse.Namespace(limit=None, no_llm=False, owner=None)


def test_run_all_does_not_alert_on_a_skip_only_batch(tmp_path, monkeypatch):
    from pipeline import alert

    alert_log = tmp_path / "alert.txt"
    monkeypatch.setattr(alert, "ALERT_COMMAND", f"tee {alert_log}")
    monkeypatch.setattr(cli, "cmd_transcribe", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_speakers", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_minutes", lambda args: 0)  # skip-only run still exits 0
    monkeypatch.setattr(cli, "cmd_index", lambda args: 0)

    rc = cli._run_all(_run_all_args(), include_ingest=False)

    assert rc == 0
    assert not alert_log.exists(), "a routine skip must not invoke the alert command"


def test_run_all_alerts_on_a_genuine_stage_failure(tmp_path, monkeypatch):
    """The counterpart to the skip case above: the real alert.send path (not a
    mock) really does invoke the configured command when a stage crashes."""
    from pipeline import alert

    alert_log = tmp_path / "alert.txt"
    monkeypatch.setattr(alert, "ALERT_COMMAND", f"tee {alert_log}")
    monkeypatch.setattr(cli, "cmd_transcribe", lambda args: 1)  # a real crash
    monkeypatch.setattr(cli, "cmd_speakers", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_minutes", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_index", lambda args: 0)

    rc = cli._run_all(_run_all_args(), include_ingest=False)

    assert rc == 1
    assert alert_log.exists(), "a genuine failure must invoke the alert command"
    assert "transcribe" in alert_log.read_text(encoding="utf-8")
