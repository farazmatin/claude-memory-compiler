"""Native batch writer for LightRAG's PGTableGraphStorage.

LightRAG 1.5.6 implements graph batches as PostgreSQL array upserts, but its
HTTP graph routes expose only single-record creates and also invoke vector
embedding. This pipeline deliberately has no LightRAG embedder: graph storage
and traversal are the product. These statements mirror PGTableGraphStorage's
``upsert_nodes_batch`` and ``upsert_edges_batch`` contracts so publication does
the graph work in bounded batches without entering the disabled vector path.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from pipeline.config import (
    LIGHTRAG_GRAPH_NAMESPACE,
    LIGHTRAG_WORKSPACE,
    POSTGRES_DATABASE,
    POSTGRES_HOST,
    POSTGRES_PASSWORD,
    POSTGRES_PORT,
    POSTGRES_USER,
)

NODE_UPSERT = """
    INSERT INTO lightrag_graph_nodes (workspace, namespace, id, properties, updated_at)
    SELECT $1, $2, u.id, u.props::jsonb, now()
    FROM unnest($3::text[], $4::text[]) AS u(id, props)
    ORDER BY u.id
    ON CONFLICT (workspace, namespace, id)
    DO UPDATE SET
        properties = lightrag_graph_nodes.properties || EXCLUDED.properties,
        updated_at = now()
"""

EDGE_UPSERT = """
    WITH endpoints AS (
        INSERT INTO lightrag_graph_nodes (workspace, namespace, id, properties, updated_at)
        SELECT $1, $2, u.id, jsonb_build_object('entity_id', u.id), now()
        FROM unnest($6::text[]) AS u(id)
        ON CONFLICT (workspace, namespace, id) DO NOTHING
        RETURNING id
    ),
    endpoint_write AS (
        SELECT COUNT(*) AS inserted_count FROM endpoints
    )
    INSERT INTO lightrag_graph_edges
        (workspace, namespace, src_id, tgt_id, properties, updated_at)
    SELECT $1, $2, u.src, u.tgt, u.props::jsonb, now()
    FROM unnest($3::text[], $4::text[], $5::text[]) AS u(src, tgt, props)
    CROSS JOIN endpoint_write
    ORDER BY u.src, u.tgt
    ON CONFLICT (workspace, namespace, src_id, tgt_id)
    DO UPDATE SET properties = EXCLUDED.properties, updated_at = now()
"""


@dataclass
class BatchResult:
    entities_written: int = 0
    relations_written: int = 0
    errors: list[str] = field(default_factory=list)


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[offset : offset + size] for offset in range(0, len(items), size)]


def _node_arguments(
    entities: dict[str, dict[str, Any]], batch_size: int
) -> list[tuple[Any, ...]]:
    created_at = int(time.time())
    rows: list[tuple[str, str]] = []
    for name, data in sorted(entities.items()):
        props = {
            "entity_id": name,
            "entity_type": data["entity_type"],
            "description": " ".join(data["descriptions"])[:2000]
            or f"{data['entity_type']} mentioned in the meeting record",
            "source_id": "|".join(sorted(data["meetings"])),
            "file_path": "meeting-memory",
            "created_at": created_at,
        }
        rows.append((name, json.dumps(props, ensure_ascii=False)))
    return [
        (
            LIGHTRAG_WORKSPACE,
            LIGHTRAG_GRAPH_NAMESPACE,
            [row[0] for row in chunk],
            [row[1] for row in chunk],
        )
        for chunk in _chunks(rows, batch_size)
    ]


def _edge_arguments(relations: list[dict[str, Any]], batch_size: int) -> list[tuple[Any, ...]]:
    created_at = int(time.time())
    # PGTableGraphStorage is undirected and keeps one row per canonical endpoint
    # pair. Last-write-wins matches its native batch method.
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for relation in relations:
        key = tuple(sorted((relation["source"], relation["target"])))
        deduped[key] = relation

    rows: list[tuple[str, str, str]] = []
    for (source, target), relation in sorted(deduped.items()):
        props = {
            "description": (
                f"{relation['source']} {relation['predicate']} {relation['target']}"
            ),
            "keywords": relation["predicate"],
            "weight": 1.0,
            "source_id": relation["meeting_id"],
            "file_path": "meeting-memory",
            "created_at": created_at,
        }
        rows.append((source, target, json.dumps(props, ensure_ascii=False)))
    return [
        (
            LIGHTRAG_WORKSPACE,
            LIGHTRAG_GRAPH_NAMESPACE,
            [row[0] for row in chunk],
            [row[1] for row in chunk],
            [row[2] for row in chunk],
            sorted({node for row in chunk for node in row[:2]}),
        )
        for chunk in _chunks(rows, batch_size)
    ]


async def _execute_phase(pool, sql: str, batches: list[tuple[Any, ...]], label: str):
    async def execute(index: int, arguments: tuple[Any, ...]):
        try:
            async with pool.acquire() as conn:
                await conn.execute(sql, *arguments)
            # The record arrays are always the third positional SQL argument.
            return len(arguments[2]), None
        except Exception as exc:  # the caller must keep partial batches retryable
            return 0, f"{label} batch {index + 1}/{len(batches)}: {type(exc).__name__}: {exc}"

    return await asyncio.gather(
        *(execute(index, arguments) for index, arguments in enumerate(batches))
    )


async def _sync_async(
    entities: dict[str, dict[str, Any]],
    relations: list[dict[str, Any]],
    workers: int,
    batch_size: int,
) -> BatchResult:
    import asyncpg

    result = BatchResult()
    pool = await asyncpg.create_pool(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        database=POSTGRES_DATABASE,
        min_size=1,
        max_size=max(1, workers),
    )
    try:
        node_results = await _execute_phase(
            pool, NODE_UPSERT, _node_arguments(entities, batch_size), "entity"
        )
        result.entities_written = sum(count for count, _error in node_results)
        result.errors.extend(error for _count, error in node_results if error)
        if result.errors:
            return result

        edge_results = await _execute_phase(
            pool, EDGE_UPSERT, _edge_arguments(relations, batch_size), "relation"
        )
        result.relations_written = sum(count for count, _error in edge_results)
        result.errors.extend(error for _count, error in edge_results if error)
        return result
    finally:
        await pool.close()


def sync(
    entities: dict[str, dict[str, Any]],
    relations: list[dict[str, Any]],
    workers: int,
    batch_size: int,
) -> BatchResult:
    """Publish one graph batch through LightRAG's local PostgreSQL tables."""
    try:
        return asyncio.run(_sync_async(entities, relations, workers, batch_size))
    except Exception as exc:
        return BatchResult(errors=[f"postgres graph connection: {type(exc).__name__}: {exc}"])
