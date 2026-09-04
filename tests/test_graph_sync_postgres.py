"""Native PostgreSQL graph batches stay bounded, ordered, and graph-only."""

from __future__ import annotations

import asyncio
import json


def test_native_batches_match_lightrag_graph_properties(monkeypatch):
    from pipeline import graph_sync_postgres as writer

    monkeypatch.setattr(writer, "LIGHTRAG_WORKSPACE", "default")
    monkeypatch.setattr(writer, "LIGHTRAG_GRAPH_NAMESPACE", "chunk_entity_relation")

    entities = {
        "Faraz": {
            "entity_type": "person",
            "descriptions": ["Owner", "Decision maker"],
            "meetings": {"m2", "m1"},
        }
    }
    relations = [
        {
            "source": "Faraz",
            "target": "Product",
            "predicate": "owns",
            "meeting_id": "m2",
        }
    ]

    node_batch = writer._node_arguments(entities, batch_size=200)[0]
    edge_batch = writer._edge_arguments(relations, batch_size=200)[0]

    assert node_batch[:2] == ("default", "chunk_entity_relation")
    assert node_batch[2] == ["Faraz"]
    assert json.loads(node_batch[3][0]) | {"created_at": 0} == {
        "entity_id": "Faraz",
        "entity_type": "person",
        "description": "Owner Decision maker",
        "source_id": "m1|m2",
        "file_path": "meeting-memory",
        "created_at": 0,
    }
    assert edge_batch[2] == ["Faraz"]
    assert edge_batch[3] == ["Product"]
    assert edge_batch[5] == ["Faraz", "Product"]
    assert json.loads(edge_batch[4][0])["keywords"] == "owns"


def test_native_batches_run_in_parallel_but_keep_entities_before_relations(monkeypatch):
    from pipeline import graph_sync_postgres as writer

    events: list[tuple[str, str]] = []
    active = 0
    max_active = 0

    class Connection:
        async def execute(self, sql, *_arguments):
            nonlocal active, max_active
            phase = "entity" if sql == writer.NODE_UPSERT else "relation"
            events.append((phase, "start"))
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            events.append((phase, "end"))

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

        async def close(self):
            return None

    async def create_pool(**_kwargs):
        return Pool()

    import asyncpg

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    entities = {
        f"E{i}": {
            "entity_type": "thing",
            "descriptions": ["d"],
            "meetings": {"m"},
        }
        for i in range(5)
    }
    relations = [
        {"source": "E0", "target": f"E{i}", "predicate": "uses", "meeting_id": "m"}
        for i in range(1, 5)
    ]

    result = writer.sync(entities, relations, workers=3, batch_size=2)

    assert result.errors == []
    assert result.entities_written == 5
    assert result.relations_written == 4
    assert max_active == 3
    first_relation = events.index(("relation", "start"))
    before_relations = events[:first_relation]
    assert all(phase == "entity" for phase, _state in before_relations)
    assert sum(state == "start" for _phase, state in before_relations) == 3
    assert sum(state == "end" for _phase, state in before_relations) == 3
