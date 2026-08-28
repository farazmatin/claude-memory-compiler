"""CLI reporting surfaces that don't need the full e2e harness.

test_e2e.py already covers `pipeline status` end to end against a real run;
these are narrower and cheaper checks of the failure-visibility section, run
directly against a manifest fixture instead of a full pipeline pass.
"""

from __future__ import annotations

import argparse
import json

from pipeline import cli, db, graph_sync, index, people_merge

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


def test_status_distinguishes_document_vector_and_graph_health(
    manifest, monkeypatch, capsys
):
    make_meeting(manifest, "m1", "2026-08-10", status=db.MINUTES_COMPILED)
    manifest.commit()
    monkeypatch.setattr(
        cli.index,
        "document_health",
        lambda: index.DocumentHealth(
            documents_stored=4,
            documents_processed=2,
            vector_chunks_ready=1,
            failed=1,
            active=1,
            status_counts={"failed": 1, "pending": 1, "processed": 2},
            pipeline_busy=False,
            recovery_required=False,
            latest_message="idle",
        ),
    )
    monkeypatch.setattr(graph_sync, "graph_labels", lambda: ["Atlas", "USC"])

    assert cli.cmd_status(argparse.Namespace()) == 0

    out = capsys.readouterr().out
    assert "documents stored        4" in out
    assert "documents processed     2" in out
    assert "vector chunks ready     1" in out
    assert "graph entities ready    2" in out
    assert "failed                  1" in out


def test_people_merge_cli_previews_without_mutating(manifest, capsys):
    db.add_person(manifest, "Mike")
    manifest.commit()

    assert cli.main(["people", "--merge", "Mike", "Michael"]) == 0

    out = capsys.readouterr().out
    assert "Merge preview" in out
    assert "Nothing was changed" in out
    assert "Digest:" in out
    assert [person["canonical"] for person in db.list_people(manifest)] == ["Mike"]


def test_people_merge_cli_apply_requires_the_exact_digest(manifest, capsys):
    db.add_person(manifest, "Mike")
    manifest.commit()
    approved = people_merge.preview(["Mike"], "Michael")

    assert cli.main(
        [
            "people",
            "--merge",
            "Mike",
            "Michael",
            "--apply",
            "--expected-digest",
            approved.digest,
        ]
    ) == 0

    assert "Merged 'Mike' into 'Michael'" in capsys.readouterr().out
    assert [person["canonical"] for person in db.list_people(manifest)] == ["Michael"]


def test_people_merge_cli_apply_without_a_digest_is_non_mutating(
    manifest, capsys
):
    db.add_person(manifest, "Mike")
    manifest.commit()

    assert cli.main(
        ["people", "--merge", "Mike", "Michael", "--apply"]
    ) == 2

    assert "requires --expected-digest" in capsys.readouterr().err
    assert [person["canonical"] for person in db.list_people(manifest)] == ["Mike"]


def test_people_merge_cli_can_resume_rewrite_jobs(manifest, capsys):
    manifest.commit()

    assert cli.main(["people", "--resume-merge-rewrites"]) == 0

    assert "0 pending" in capsys.readouterr().out


def test_people_merge_repair_cli_previews_then_applies_exact_artifact(
    manifest, capsys, tmp_path
):
    db.add_person(manifest, "Michael", aliases=["Mike"])
    manifest.commit()
    preview_path = tmp_path / "merge-repair.json"

    assert cli.main(
        [
            "people",
            "--repair-merges",
            "--preview-to",
            str(preview_path),
        ]
    ) == 0
    preview_output = capsys.readouterr().out
    artifact = json.loads(preview_path.read_text(encoding="utf-8"))
    assert "Nothing was changed" in preview_output
    assert db.canonical_name(manifest, "Mike") == "Michael"
    assert db.resolve_merged_name(manifest, "Mike") is None

    assert cli.main(
        [
            "people",
            "--repair-merges",
            "--apply",
            str(preview_path),
            "--expected-digest",
            artifact["digest"],
        ]
    ) == 0

    assert "1 alias(es) retired" in capsys.readouterr().out
    assert db.resolve_merged_name(manifest, "Mike") == "Michael"


def test_query_has_no_local_model_mode():
    query = next(
        action for action in cli.build_parser()._actions if action.dest == "command"
    ).choices["query"]
    assert "--local" not in query.format_help()


def test_index_advances_only_after_document_processing(
    manifest, monkeypatch, capsys, tmp_path
):
    minutes_path = tmp_path / "m1.md"
    minutes_path.write_text("minutes", encoding="utf-8")
    make_meeting(
        manifest,
        "m1",
        "2026-08-10",
        status=db.MINUTES_COMPILED,
        minutes_path=str(minutes_path),
    )
    manifest.commit()
    monkeypatch.setattr(cli.index, "health", lambda: {})
    monkeypatch.setattr(
        cli.index,
        "replace_minutes",
        lambda *a, **k: ("doc-ready", True),
    )

    assert cli.cmd_index(argparse.Namespace(limit=None)) == 0
    assert db.get_meeting(manifest, "m1").status == db.INDEXED
    assert db.get_meeting(manifest, "m1").lightrag_doc_id == "doc-ready"
    assert "indexed" in capsys.readouterr().out


def test_index_does_not_advance_when_processing_fails(
    manifest, monkeypatch, capsys, tmp_path
):
    minutes_path = tmp_path / "m1.md"
    minutes_path.write_text("minutes", encoding="utf-8")
    make_meeting(
        manifest,
        "m1",
        "2026-08-10",
        status=db.MINUTES_COMPILED,
        minutes_path=str(minutes_path),
    )
    manifest.commit()
    monkeypatch.setattr(cli.index, "health", lambda: {})
    monkeypatch.setattr(cli.index, "replace_minutes", lambda *a, **k: ("doc-failed", False))

    assert cli.cmd_index(argparse.Namespace(limit=None)) == 1
    assert db.get_meeting(manifest, "m1").status == db.MINUTES_COMPILED
    assert "SKIPPED" in capsys.readouterr().out


def test_index_repair_preview_writes_exact_read_only_plan(
    manifest, monkeypatch, capsys, tmp_path
):
    minutes_path = tmp_path / "meeting.md"
    minutes_path.write_text("---\ndate: 2026-08-10\n---\nMinutes", encoding="utf-8")
    make_meeting(
        manifest,
        "m1",
        "2026-08-10",
        status=db.MINUTES_COMPILED,
        minutes_path=str(minutes_path),
        lightrag_doc_id="doc-stale-manifest",
    )
    manifest.commit()
    monkeypatch.setattr(
        index,
        "_document_records",
        lambda: [index.DocumentRecord("doc-source-owner", minutes_path.name, "failed", 0)],
    )
    monkeypatch.setattr(
        index,
        "pipeline_status",
        lambda: {"busy": False, "recovery_required": False, "latest_message": "idle"},
    )
    preview_path = tmp_path / "index-repair.json"

    assert cli.main(
        ["index-repair-preview", "--to", str(preview_path)]
    ) == 0

    artifact = json.loads(preview_path.read_text(encoding="utf-8"))
    assert artifact["fingerprint"]
    assert artifact["items"][0]["meeting_id"] == "m1"
    assert artifact["items"][0]["action"] == "delete_then_insert"
    assert artifact["items"][0]["delete_doc_id"] == "doc-source-owner"
    assert db.get_meeting(manifest, "m1").status == db.MINUTES_COMPILED
    assert "Nothing was changed" in capsys.readouterr().out


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
    monkeypatch.setattr(cli, "cmd_graph_sync", lambda args: 0)

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
    monkeypatch.setattr(cli, "cmd_graph_sync", lambda args: 0)

    rc = cli._run_all(_run_all_args(), include_ingest=False)

    assert rc == 1
    assert alert_log.exists(), "a genuine failure must invoke the alert command"
    assert "transcribe" in alert_log.read_text(encoding="utf-8")


def test_run_all_requeues_a_network_failure_before_transcribing(manifest, monkeypatch):
    """The next run picks up a dropped upload without anyone reading the manifest.

    Six recordings were parked at FAILED by `Server disconnected without sending
    a response`; every later run said "Nothing to transcribe" and no minutes were
    ever compiled for them.
    """
    from .conftest import make_meeting

    make_meeting(manifest, "m1", "2026-08-10")
    db.mark_failed(manifest, "m1", "ReplicateError: Audio upload failed", retryable=True)
    manifest.commit()  # _run_all reads through its own connection

    seen: list[str] = []

    def record(_args):
        with db.connect() as conn:
            seen.extend(m.id for m in db.pending(conn, db.DISCOVERED))
        return 0

    monkeypatch.setattr(cli, "cmd_transcribe", record)
    monkeypatch.setattr(cli, "cmd_speakers", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_minutes", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_graph_sync", lambda args: 0)

    cli._run_all(_run_all_args(), include_ingest=False)

    assert seen == ["m1"], "a network-failed meeting must be queued for the next run"


def test_run_all_leaves_a_permanent_failure_parked(manifest, monkeypatch):
    """A missing file repeats identically; requeueing it would spin every run."""
    from .conftest import make_meeting

    make_meeting(manifest, "m1", "2026-08-10")
    db.mark_failed(manifest, "m1", "audio file missing and could not be restored")
    manifest.commit()

    monkeypatch.setattr(cli, "cmd_transcribe", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_speakers", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_minutes", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_graph_sync", lambda args: 0)

    cli._run_all(_run_all_args(), include_ingest=False)

    with db.connect() as conn:
        assert db.get_meeting(conn, "m1").status == db.FAILED
