"""Exit codes for `pipeline graph-sync`.

The graph is authored from the manifest's already-extracted entities rather than
by LightRAG's own extraction, which fails on the local 4B model. That makes this
command the only writer of the graph - so a run that writes nothing must say so.
"""

from __future__ import annotations

import argparse


def test_graph_sync_exits_non_zero_when_every_write_was_refused(monkeypatch, capsys):
    """A sync that wrote nothing is a failure, even against a populated graph.

    `return 0 if after else 1` asked only whether the graph has any entities at
    all, so a run where LightRAG refused all 1,604 writes with 409 "Pipeline is
    busy" still reported success - while the graph silently kept serving the
    entities of the previous corpus.
    """
    from pipeline import cli, graph_sync, index

    report = graph_sync.SyncReport(
        entities_skipped=751,
        relations_skipped=853,
        errors=["entity Faraz: HTTP 409: Pipeline is busy"] * 1604,
    )
    monkeypatch.setattr(index, "health", lambda: None)
    monkeypatch.setattr(graph_sync, "graph_labels", lambda: ["a"] * 307)
    monkeypatch.setattr(graph_sync, "sync", lambda: report)
    monkeypatch.setattr(cli.db, "init_db", lambda: None)

    assert cli.cmd_graph_sync(argparse.Namespace()) == 1
    assert "wrote nothing" in capsys.readouterr().out
