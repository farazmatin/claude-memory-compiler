"""Exit codes for `pipeline graph-sync`.

The graph is authored from the manifest's already-extracted entities rather than
by LightRAG's own extraction, which fails on the local 4B model. That makes this
command the only writer of the graph - so a run that writes nothing must say so.
"""

from __future__ import annotations

import argparse
import threading
import time


def test_graph_sync_exits_non_zero_when_every_write_was_refused(manifest, monkeypatch, capsys):
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
    monkeypatch.setattr(graph_sync, "sync", lambda **_kwargs: report)

    from .conftest import make_meeting

    make_meeting(
        manifest,
        "pending",
        "2026-08-10",
        status=cli.db.MINUTES_COMPILED,
        minutes_path="/pending.md",
    )
    manifest.commit()

    assert cli.cmd_graph_sync(argparse.Namespace()) == 1
    assert "wrote nothing" in capsys.readouterr().out


def test_a_name_lightrag_can_never_accept_is_dropped_not_failed():
    """One junk entity name must not wedge publication for the whole corpus.

    The minutes extractor emitted an entity named "1.0". LightRAG's naming
    contract filters digits-and-dots tokens, so every create returned
    HTTP 400 "Entity name cannot be empty after normalization" - on every run,
    forever. Retrying cannot help, so this is a drop, not a write failure.
    """
    import httpx

    from pipeline import graph_sync

    class _Resp:
        status_code = 400
        text = '{"detail":"Entity name cannot be empty after normalization"}'

    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    client.post = lambda *a, **k: _Resp()  # type: ignore[method-assign]

    outcome, detail = graph_sync._post(client, "/graph/entity/create", {})

    assert outcome == graph_sync.DROPPED, f"got {outcome}, which blocks the corpus"
    assert "normalization" in detail


def test_a_server_error_is_still_a_write_failure():
    """The counterpart: a 500 or a dropped connection must still block."""
    import httpx

    from pipeline import graph_sync

    class _Resp:
        status_code = 500
        text = '{"detail":"Internal server error"}'

    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    client.post = lambda *a, **k: _Resp()  # type: ignore[method-assign]

    assert graph_sync._post(client, "/graph/entity/create", {})[0] == graph_sync.FAILED


def test_an_expired_key_is_a_failure_not_a_drop():
    """401 and 403 are 4xx but recoverable - dropping them would hide a dead key."""
    import httpx

    from pipeline import graph_sync

    for code in (401, 403, 429):
        class _Resp:
            status_code = code
            text = '{"detail":"nope"}'

        client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
        client.post = lambda *a, **k: _Resp()  # type: ignore[method-assign]

        assert graph_sync._post(client, "/graph/entity/create", {})[0] == graph_sync.FAILED, (
            f"HTTP {code} must block, not drop"
        )


def test_graph_sync_publishes_when_the_only_refusals_are_drops(manifest, monkeypatch, capsys):
    """A converged corpus writes nothing new, so a drop must not read as failure.

    Once every entity already exists, `entities_written` is 0 on every later run.
    A single permanently-refused name then made the "wrote nothing" guard fire
    forever, and eight meetings sat at minutes_compiled with their minutes
    already on disk.
    """
    from pipeline import cli, graph_sync, index

    report = graph_sync.SyncReport(
        entities_skipped=1359,
        relations_skipped=1761,
        entities_dropped=1,
        drops=["entity 1.0: HTTP 400: Entity name cannot be empty after normalization"],
    )
    monkeypatch.setattr(index, "health", lambda: None)
    monkeypatch.setattr(graph_sync, "graph_labels", lambda: ["a"] * 1495)
    monkeypatch.setattr(graph_sync, "sync", lambda **_kwargs: report)

    from .conftest import make_meeting

    make_meeting(manifest, "m1", "2026-08-10")
    cli.db.advance(manifest, "m1", cli.db.MINUTES_COMPILED, minutes_path="/m.md")
    manifest.commit()

    rc = cli.cmd_graph_sync(argparse.Namespace(limit=None))

    out = capsys.readouterr().out
    assert rc == 0, "a corpus with nothing left to write must not report failure"
    assert "1.0" in out, "a dropped name must stay visible, not vanish silently"
    with cli.db.connect() as conn:
        assert cli.db.get_meeting(conn, "m1").status == cli.db.INDEXED, (
            "a meeting whose minutes are on disk must not sit behind a junk entity"
        )


def _client_returning(status: int, body: str = '{"detail":"Internal server error"}'):
    import httpx

    class _Resp:
        status_code = status
        text = body

    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    client.post = lambda *a, **k: _Resp()  # type: ignore[method-assign]
    return client


def test_a_500_the_graph_actually_kept_is_not_a_refusal():
    """LightRAG commits the record, then fails upserting a vector it cannot embed.

    This deployment points the embedding binding at a closed port on purpose, so
    every graph write answers 500 after landing. Counting those as refused made
    graph-sync report "the graph still holds the PREVIOUS corpus" when all 293
    writes were present, and made `pipeline run` exit non-zero on every sync.
    """
    from pipeline import graph_sync

    outcome, detail = graph_sync._post(
        _client_returning(500), "/graph/entity/create", {}, verify=lambda: True
    )
    assert outcome == graph_sync.VERIFIED
    assert detail == ""


def test_a_500_the_graph_did_not_keep_is_still_a_refusal():
    """The verifier may only ever downgrade a failure it can actually disprove."""
    from pipeline import graph_sync

    outcome, detail = graph_sync._post(
        _client_returning(500), "/graph/entity/create", {}, verify=lambda: False
    )
    assert outcome == graph_sync.FAILED
    assert "500" in detail


def test_a_permanent_refusal_is_never_verified_away():
    """A 400 is the payload being unacceptable, not a half-completed write."""
    from pipeline import graph_sync

    outcome, _ = graph_sync._post(
        _client_returning(400, '{"detail":"bad name"}'),
        "/graph/entity/create", {}, verify=lambda: True,
    )
    assert outcome == graph_sync.DROPPED


def test_graph_sync_publishes_when_every_write_landed_despite_a_500(
    manifest, monkeypatch, capsys
):
    """Verified writes must satisfy the wrote-nothing guard, or nothing publishes.

    The live corpus produced exactly this shape: 293 writes answered 500, all 293
    present in the graph, and the run still refused to advance a single meeting.
    """
    from pipeline import cli, graph_sync, index

    report = graph_sync.SyncReport(
        entities_verified=293,
        entities_skipped=1383,
        relations_skipped=1785,
    )
    monkeypatch.setattr(index, "health", lambda: None)
    monkeypatch.setattr(graph_sync, "graph_labels", lambda: ["a"] * 1640)
    monkeypatch.setattr(graph_sync, "sync", lambda **_kwargs: report)

    from .conftest import make_meeting

    make_meeting(manifest, "m1", "2026-08-10")
    cli.db.advance(manifest, "m1", cli.db.MINUTES_COMPILED, minutes_path="/m.md")
    manifest.commit()

    rc = cli.cmd_graph_sync(argparse.Namespace(limit=None))

    out = capsys.readouterr().out
    assert rc == 0
    assert "wrote nothing" not in out
    assert "landed despite a 5xx" in out, "a verified write must stay visible, not read as clean"
    with cli.db.connect() as conn:
        assert cli.db.get_meeting(conn, "m1").status == cli.db.INDEXED


def test_collect_limits_normal_sync_to_the_requested_meetings(manifest):
    """A routine run must not republish the entire historical graph."""
    from pipeline import db, graph_sync

    from .conftest import make_meeting

    make_meeting(manifest, "old", "2026-08-09")
    make_meeting(manifest, "new", "2026-08-10")
    db.replace_entities(
        manifest,
        "old",
        [{"name": "Historical", "kind": "feature", "description": "old"}],
        [],
    )
    db.replace_entities(
        manifest,
        "new",
        [
            {"name": "Current", "kind": "feature", "description": "new"},
            {"name": "Shared", "kind": "system", "description": "dependency"},
        ],
        [{"subject": "Current", "predicate": "uses", "object": "Shared"}],
    )

    entities, relations = graph_sync.collect(manifest, meeting_ids={"new"})

    assert set(entities) == {"Current", "Shared"}
    assert [(r["source"], r["target"]) for r in relations] == [("Current", "Shared")]


def test_sync_uses_bounded_parallel_workers(manifest, monkeypatch):
    """The graph writer must overlap local HTTP waits without exceeding its bound."""
    from pipeline import graph_sync

    entities = {
        f"Entity {i}": {
            "entity_type": "feature",
            "descriptions": [f"description {i}"],
            "meetings": {"m1"},
        }
        for i in range(8)
    }
    monkeypatch.setattr(
        graph_sync,
        "collect",
        lambda _conn, meeting_ids=None: (entities, []),
    )

    lock = threading.Lock()
    active = 0
    max_seen = 0

    def slow_post(_client, _path, _payload, verify=None):
        nonlocal active, max_seen
        with lock:
            active += 1
            max_seen = max(max_seen, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return graph_sync.WRITTEN, ""

    monkeypatch.setattr(graph_sync, "_post", slow_post)

    report = graph_sync.sync(manifest, meeting_ids={"m1"}, workers=3, backend="http")

    assert report.entities_written == 8
    assert 1 < max_seen <= 3


def test_cli_syncs_only_pending_meetings(manifest, monkeypatch):
    """The normal graph stage publishes its queue, not all historical rows."""
    from pipeline import cli, db, graph_sync, index

    from .conftest import make_meeting

    make_meeting(
        manifest,
        "pending",
        "2026-08-10",
        status=db.MINUTES_COMPILED,
        minutes_path="/pending.md",
    )
    make_meeting(
        manifest,
        "old",
        "2026-08-09",
        status=db.INDEXED,
        minutes_path="/old.md",
    )
    manifest.commit()

    captured = {}

    def fake_sync(*, meeting_ids=None, workers=None, batch_size=None, backend=None):
        captured["meeting_ids"] = meeting_ids
        captured["workers"] = workers
        captured["batch_size"] = batch_size
        captured["backend"] = backend
        return graph_sync.SyncReport(entities_skipped=1)

    monkeypatch.setattr(index, "health", lambda: None)
    monkeypatch.setattr(graph_sync, "graph_labels", lambda: ["existing"])
    monkeypatch.setattr(graph_sync, "sync", fake_sync)

    rc = cli.cmd_graph_sync(
        argparse.Namespace(
            limit=None, full=False, workers=3, batch_size=50, backend="postgres"
        )
    )

    assert rc == 0
    assert captured == {
        "meeting_ids": ["pending"],
        "workers": 3,
        "batch_size": 50,
        "backend": "postgres",
    }
    with db.connect() as conn:
        assert db.get_meeting(conn, "pending").status == db.INDEXED
        assert db.get_meeting(conn, "old").status == db.INDEXED


def test_partial_graph_failure_does_not_publish_the_queue(manifest, monkeypatch):
    """A partly written incremental batch remains retryable as one unit."""
    from pipeline import cli, db, graph_sync, index

    from .conftest import make_meeting

    make_meeting(
        manifest,
        "pending",
        "2026-08-10",
        status=db.MINUTES_COMPILED,
        minutes_path="/pending.md",
    )
    manifest.commit()
    monkeypatch.setattr(index, "health", lambda: None)
    monkeypatch.setattr(graph_sync, "graph_labels", lambda: ["existing"])
    monkeypatch.setattr(
        graph_sync,
        "sync",
        lambda **_kwargs: graph_sync.SyncReport(
            entities_written=1,
            relations_skipped=1,
            errors=["relation A->B: HTTP 500"],
        ),
    )

    rc = cli.cmd_graph_sync(argparse.Namespace(limit=None, full=False, workers=3))

    assert rc == 1
    with db.connect() as conn:
        assert db.get_meeting(conn, "pending").status == db.MINUTES_COMPILED


def test_empty_incremental_queue_skips_lightrag(manifest, monkeypatch, capsys):
    """A pipeline run with no graph work should finish without touching the service."""
    from pipeline import cli, graph_sync, index

    monkeypatch.setattr(
        index,
        "health",
        lambda: (_ for _ in ()).throw(AssertionError("empty queue must not need LightRAG")),
    )
    monkeypatch.setattr(
        graph_sync,
        "sync",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("nothing should be written")),
    )

    assert cli.cmd_graph_sync(argparse.Namespace(limit=None, full=False)) == 0
    assert "Nothing to publish" in capsys.readouterr().out
