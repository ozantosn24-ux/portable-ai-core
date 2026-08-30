"""Compare pgvector exact search, HNSW, and IVFFlat on one deterministic corpus.

This is an index-mechanics benchmark, not a semantic retrieval-quality benchmark. It
creates an isolated unlogged table, derives exact top-k ground truth, builds each ANN
index in turn, measures overlap/latency/build cost, and drops the table before exit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wozto_ai_reference.asyncio_compat import run as run_async  # noqa: E402
from wozto_ai_reference.pgvector_store import vector_literal  # noqa: E402


@dataclass(frozen=True)
class FilterSlice:
    name: str
    divisor: int
    where_sql: sql.SQL


FILTER_SLICES = (
    FilterSlice("all", 1, sql.SQL("")),
    FilterSlice("ten_percent", 10, sql.SQL("WHERE bucket_10")),
    FilterSlice("one_percent", 100, sql.SQL("WHERE bucket_100")),
)


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        raise ValueError("samples must not be empty")
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _unit_vector(*, seed: int, item_id: int, dimensions: int) -> tuple[float, ...]:
    rng = random.Random((seed << 32) ^ item_id)
    values = [rng.uniform(-1.0, 1.0) for _ in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


def _query_vector(
    *,
    seed: int,
    item_id: int,
    query_id: int,
    dimensions: int,
) -> tuple[float, ...]:
    base = _unit_vector(seed=seed, item_id=item_id, dimensions=dimensions)
    rng = random.Random((seed << 40) ^ (item_id << 8) ^ query_id)
    values = [value + rng.uniform(-0.02, 0.02) for value in base]
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


def _query_vectors(
    *,
    rows: int,
    queries: int,
    dimensions: int,
    seed: int,
    filter_slice: FilterSlice,
) -> list[str]:
    eligible = rows // filter_slice.divisor
    if eligible < 1:
        raise ValueError(f"row count is too small for {filter_slice.name}")
    vectors: list[str] = []
    for query_id in range(queries):
        position = (query_id * 7_919 + seed) % eligible
        item_id = position * filter_slice.divisor
        vector = _query_vector(
            seed=seed,
            item_id=item_id,
            query_id=query_id,
            dimensions=dimensions,
        )
        vectors.append(vector_literal(vector, dimensions=dimensions))
    return vectors


def _top_k_overlap(expected: list[int], actual: list[int]) -> float:
    if not expected:
        raise ValueError("expected result must not be empty")
    return len(set(expected).intersection(actual)) / len(expected)


def _validate_database_url(database_url: str) -> str:
    if not database_url.strip():
        raise SystemExit("WOZTO_REFERENCE_DATABASE_URL must be set")
    try:
        parameters = conninfo_to_dict(database_url)
    except psycopg.Error:
        raise SystemExit("WOZTO_REFERENCE_DATABASE_URL must be valid libpq connection information") from None
    if parameters.get("password"):
        raise SystemExit("WOZTO_REFERENCE_DATABASE_URL must not embed a password; use a libpq passfile")
    return database_url


def _query_sql(table_name: str, filter_slice: FilterSlice) -> sql.Composed:
    return sql.SQL("SELECT item_id FROM {table} {where_clause} ORDER BY embedding <=> %s::vector LIMIT %s").format(
        table=sql.Identifier(table_name),
        where_clause=filter_slice.where_sql,
    )


def _plan_summary(plan: Any) -> dict[str, list[str]]:
    root = plan[0]["Plan"] if isinstance(plan, list) else plan["Plan"]
    node_types: list[str] = []
    index_names: list[str] = []

    def visit(node: dict[str, Any]) -> None:
        node_types.append(str(node.get("Node Type", "unknown")))
        if node.get("Index Name"):
            index_names.append(str(node["Index Name"]))
        for child in node.get("Plans", []):
            visit(child)

    visit(root)
    return {"node_types": node_types, "index_names": index_names}


async def _explain(
    connection: psycopg.AsyncConnection[Any],
    *,
    table_name: str,
    filter_slice: FilterSlice,
    query_vector: str,
    top_k: int,
) -> dict[str, list[str]]:
    statement = sql.SQL("EXPLAIN (FORMAT JSON) ") + _query_sql(table_name, filter_slice)
    cursor = await connection.execute(statement, (query_vector, top_k))
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("EXPLAIN returned no row")
    return _plan_summary(row["QUERY PLAN"])


async def _run_slice(
    connection: psycopg.AsyncConnection[Any],
    *,
    table_name: str,
    filter_slice: FilterSlice,
    query_vectors: list[str],
    warmup: int,
    top_k: int,
    ground_truth: list[list[int]] | None,
) -> tuple[dict[str, Any], list[list[int]]]:
    statement = _query_sql(table_name, filter_slice)
    for query_vector in query_vectors[:warmup]:
        await connection.execute(statement, (query_vector, top_k))

    samples_ms: list[float] = []
    results: list[list[int]] = []
    for query_vector in query_vectors:
        started = time.perf_counter_ns()
        cursor = await connection.execute(statement, (query_vector, top_k))
        rows = await cursor.fetchall()
        samples_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        results.append([int(row["item_id"]) for row in rows])

    short_results = sum(len(result) < top_k for result in results)
    overlaps = (
        [_top_k_overlap(expected, actual) for expected, actual in zip(ground_truth, results, strict=True)]
        if ground_truth is not None
        else [1.0 for _ in results]
    )
    plan = await _explain(
        connection,
        table_name=table_name,
        filter_slice=filter_slice,
        query_vector=query_vectors[0],
        top_k=top_k,
    )
    return (
        {
            "matching_rows": None,
            "queries": len(query_vectors),
            "short_results": short_results,
            "mean_top_k_overlap": round(statistics.fmean(overlaps), 6),
            "min_top_k_overlap": round(min(overlaps), 6),
            "latency_ms": {
                "p50": round(_percentile(samples_ms, 0.50), 3),
                "p95": round(_percentile(samples_ms, 0.95), 3),
                "mean": round(statistics.fmean(samples_ms), 3),
            },
            "plan": plan,
        },
        results,
    )


async def _cached_index_bytes(connection: psycopg.AsyncConnection[Any], index_name: str) -> int | None:
    try:
        await connection.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
        cursor = await connection.execute(
            """
            SELECT COALESCE(COUNT(*) * current_setting('block_size')::bigint, 0) AS bytes
            FROM pg_buffercache AS buffers
            JOIN pg_class AS relation
              ON pg_relation_filenode(relation.oid) = buffers.relfilenode
            WHERE relation.oid = %s::regclass
              AND buffers.reldatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
            """,
            (index_name,),
        )
        row = await cursor.fetchone()
        return int(row["bytes"]) if row else None
    except psycopg.Error:
        return None


async def _index_costs(connection: psycopg.AsyncConnection[Any], index_name: str) -> dict[str, int | None]:
    cursor = await connection.execute(
        "SELECT pg_relation_size(%s::regclass) AS bytes",
        (index_name,),
    )
    row = await cursor.fetchone()
    return {
        "index_bytes": int(row["bytes"]),
        "cached_index_bytes_after_queries": await _cached_index_bytes(connection, index_name),
    }


async def _set_method_settings(
    connection: psycopg.AsyncConnection[Any],
    *,
    method: str,
    hnsw_ef_search: int,
    ivfflat_probes: int,
    ivfflat_lists: int,
) -> None:
    if method == "exact":
        await connection.execute("SET enable_indexscan = off")
        await connection.execute("SET enable_bitmapscan = off")
        await connection.execute("SET enable_seqscan = on")
    else:
        await connection.execute("SET enable_indexscan = on")
        await connection.execute("SET enable_bitmapscan = on")
        await connection.execute("SET enable_seqscan = off")
    if method == "hnsw":
        await connection.execute("SET hnsw.iterative_scan = 'strict_order'")
        await connection.execute(
            "SELECT set_config('hnsw.ef_search', %s, false)",
            (str(hnsw_ef_search),),
        )
    if method == "ivfflat":
        await connection.execute("SET ivfflat.iterative_scan = 'relaxed_order'")
        await connection.execute(
            "SELECT set_config('ivfflat.probes', %s, false)",
            (str(ivfflat_probes),),
        )
        await connection.execute(
            "SELECT set_config('ivfflat.max_probes', %s, false)",
            (str(ivfflat_lists),),
        )


async def _measure_method(
    connection: psycopg.AsyncConnection[Any],
    *,
    method: str,
    table_name: str,
    index_name: str | None,
    query_sets: dict[str, list[str]],
    ground_truth: dict[str, list[list[int]]] | None,
    warmup: int,
    top_k: int,
    rows: int,
    hnsw_ef_search: int,
    ivfflat_probes: int,
    ivfflat_lists: int,
    build_ms: float | None,
) -> tuple[dict[str, Any], dict[str, list[list[int]]]]:
    await _set_method_settings(
        connection,
        method=method,
        hnsw_ef_search=hnsw_ef_search,
        ivfflat_probes=ivfflat_probes,
        ivfflat_lists=ivfflat_lists,
    )
    slices: dict[str, Any] = {}
    result_sets: dict[str, list[list[int]]] = {}
    for filter_slice in FILTER_SLICES:
        metrics, results = await _run_slice(
            connection,
            table_name=table_name,
            filter_slice=filter_slice,
            query_vectors=query_sets[filter_slice.name],
            warmup=warmup,
            top_k=top_k,
            ground_truth=None if ground_truth is None else ground_truth[filter_slice.name],
        )
        metrics["matching_rows"] = rows // filter_slice.divisor
        slices[filter_slice.name] = metrics
        result_sets[filter_slice.name] = results

    if index_name:
        for filter_slice in FILTER_SLICES:
            if index_name not in slices[filter_slice.name]["plan"]["index_names"]:
                raise RuntimeError(f"{method} plan did not use its ANN index for {filter_slice.name}")
        costs = await _index_costs(connection, index_name)
    else:
        costs = {"index_bytes": 0, "cached_index_bytes_after_queries": 0}

    return (
        {
            "build_ms": None if build_ms is None else round(build_ms, 3),
            **costs,
            "slices": slices,
        },
        result_sets,
    )


async def _create_index(
    connection: psycopg.AsyncConnection[Any],
    *,
    method: str,
    table_name: str,
    index_name: str,
    ivfflat_lists: int,
) -> float:
    if method == "hnsw":
        statement = sql.SQL(
            "CREATE INDEX {index} ON {table} USING hnsw "
            "(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        ).format(
            index=sql.Identifier(index_name),
            table=sql.Identifier(table_name),
        )
    elif method == "ivfflat":
        statement = sql.SQL(
            "CREATE INDEX {index} ON {table} USING ivfflat (embedding vector_cosine_ops) WITH (lists = {lists})"
        ).format(
            index=sql.Identifier(index_name),
            table=sql.Identifier(table_name),
            lists=sql.Literal(ivfflat_lists),
        )
    else:
        raise ValueError(f"unsupported index method: {method}")
    started = time.perf_counter_ns()
    await connection.execute(statement)
    return (time.perf_counter_ns() - started) / 1_000_000


async def _load_rows(
    connection: psycopg.AsyncConnection[Any],
    *,
    table_name: str,
    rows: int,
    dimensions: int,
    seed: int,
) -> float:
    await connection.execute(
        sql.SQL(
            "CREATE UNLOGGED TABLE {table} ("
            "item_id bigint PRIMARY KEY, bucket_10 boolean NOT NULL, "
            "bucket_100 boolean NOT NULL, embedding vector({dimensions}) NOT NULL)"
        ).format(
            table=sql.Identifier(table_name),
            dimensions=sql.Literal(dimensions),
        )
    )
    started = time.perf_counter_ns()
    async with connection.cursor() as cursor:
        copy_statement = sql.SQL("COPY {table} (item_id, bucket_10, bucket_100, embedding) FROM STDIN").format(
            table=sql.Identifier(table_name)
        )
        async with cursor.copy(copy_statement) as copy:
            for item_id in range(rows):
                vector = _unit_vector(seed=seed, item_id=item_id, dimensions=dimensions)
                await copy.write_row(
                    (
                        item_id,
                        item_id % 10 == 0,
                        item_id % 100 == 0,
                        vector_literal(vector, dimensions=dimensions),
                    )
                )
    await connection.execute(sql.SQL("ANALYZE {table}").format(table=sql.Identifier(table_name)))
    return (time.perf_counter_ns() - started) / 1_000_000


async def _benchmark(args: argparse.Namespace) -> dict[str, Any]:
    database_url = _validate_database_url(os.getenv("WOZTO_REFERENCE_DATABASE_URL", ""))
    suffix = uuid4().hex[:12]
    table_name = f"ann_benchmark_{suffix}"
    hnsw_index = f"ann_hnsw_{suffix}"
    ivfflat_index = f"ann_ivfflat_{suffix}"
    ivfflat_lists = max(1, args.rows // 1_000)
    ivfflat_probes = min(ivfflat_lists, max(1, math.ceil(math.sqrt(ivfflat_lists))))
    query_sets = {
        filter_slice.name: _query_vectors(
            rows=args.rows,
            queries=args.queries,
            dimensions=args.dimensions,
            seed=args.seed,
            filter_slice=filter_slice,
        )
        for filter_slice in FILTER_SLICES
    }

    connection = await psycopg.AsyncConnection.connect(
        database_url,
        autocommit=True,
        row_factory=dict_row,
    )
    try:
        await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        version_row = await (
            await connection.execute(
                "SELECT current_setting('server_version') AS postgres_version, "
                "extversion AS pgvector_version FROM pg_extension WHERE extname = 'vector'"
            )
        ).fetchone()
        load_ms = await _load_rows(
            connection,
            table_name=table_name,
            rows=args.rows,
            dimensions=args.dimensions,
            seed=args.seed,
        )
        table_row = await (
            await connection.execute(
                "SELECT pg_relation_size(%s::regclass) AS bytes",
                (table_name,),
            )
        ).fetchone()

        exact, ground_truth = await _measure_method(
            connection,
            method="exact",
            table_name=table_name,
            index_name=None,
            query_sets=query_sets,
            ground_truth=None,
            warmup=args.warmup,
            top_k=args.top_k,
            rows=args.rows,
            hnsw_ef_search=args.hnsw_ef_search,
            ivfflat_probes=ivfflat_probes,
            ivfflat_lists=ivfflat_lists,
            build_ms=None,
        )

        hnsw_build_ms = await _create_index(
            connection,
            method="hnsw",
            table_name=table_name,
            index_name=hnsw_index,
            ivfflat_lists=ivfflat_lists,
        )
        await connection.execute(sql.SQL("ANALYZE {table}").format(table=sql.Identifier(table_name)))
        hnsw, _ = await _measure_method(
            connection,
            method="hnsw",
            table_name=table_name,
            index_name=hnsw_index,
            query_sets=query_sets,
            ground_truth=ground_truth,
            warmup=args.warmup,
            top_k=args.top_k,
            rows=args.rows,
            hnsw_ef_search=args.hnsw_ef_search,
            ivfflat_probes=ivfflat_probes,
            ivfflat_lists=ivfflat_lists,
            build_ms=hnsw_build_ms,
        )
        await connection.execute(sql.SQL("DROP INDEX {index}").format(index=sql.Identifier(hnsw_index)))

        ivfflat_build_ms = await _create_index(
            connection,
            method="ivfflat",
            table_name=table_name,
            index_name=ivfflat_index,
            ivfflat_lists=ivfflat_lists,
        )
        await connection.execute(sql.SQL("ANALYZE {table}").format(table=sql.Identifier(table_name)))
        ivfflat, _ = await _measure_method(
            connection,
            method="ivfflat",
            table_name=table_name,
            index_name=ivfflat_index,
            query_sets=query_sets,
            ground_truth=ground_truth,
            warmup=args.warmup,
            top_k=args.top_k,
            rows=args.rows,
            hnsw_ef_search=args.hnsw_ef_search,
            ivfflat_probes=ivfflat_probes,
            ivfflat_lists=ivfflat_lists,
            build_ms=ivfflat_build_ms,
        )

        script_path = Path(__file__).resolve()
        return {
            "provenance": {
                "generated_at": datetime.now(UTC).isoformat(),
                "benchmark_script_sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
                "postgres_version": version_row["postgres_version"],
                "pgvector_version": version_row["pgvector_version"],
                "dataset": "deterministic normalized random vectors with perturbed-row queries",
                "semantic_quality_claim": False,
            },
            "configuration": {
                "rows": args.rows,
                "dimensions": args.dimensions,
                "queries_per_slice": args.queries,
                "warmup_per_slice": min(args.warmup, args.queries),
                "top_k": args.top_k,
                "seed": args.seed,
                "hnsw": {"m": 16, "ef_construction": 64, "ef_search": args.hnsw_ef_search},
                "ivfflat": {
                    "lists": ivfflat_lists,
                    "probes": ivfflat_probes,
                    "max_probes": ivfflat_lists,
                },
                "filters": {item.name: args.rows // item.divisor for item in FILTER_SLICES},
            },
            "load_ms": round(load_ms, 3),
            "table_bytes": int(table_row["bytes"]),
            "methods": {"exact": exact, "hnsw": hnsw, "ivfflat": ivfflat},
            "notes": [
                "Exact results are the top-k ground truth for ANN overlap.",
                "ANN plans are required to name the index; the run fails if PostgreSQL does not use it.",
                "cached_index_bytes_after_queries is a shared-buffer residency proxy, not peak build memory.",
                "The production hybrid SQL is not benchmarked here because score normalization "
                "requires the full authorized candidate set.",
            ],
        }
    finally:
        try:
            await connection.execute(sql.SQL("DROP TABLE IF EXISTS {table}").format(table=sql.Identifier(table_name)))
        finally:
            await connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--dimensions", type=int, default=64)
    parser.add_argument("--queries", type=int, default=60)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20_260_830)
    parser.add_argument("--hnsw-ef-search", type=int, default=100)
    parser.add_argument("--output", type=Path)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.rows < 500:
        raise SystemExit("rows must be at least 500")
    if not 8 <= args.dimensions <= 2_000:
        raise SystemExit("dimensions must be between 8 and 2000")
    if args.queries < 1:
        raise SystemExit("queries must be positive")
    if args.warmup < 0 or args.warmup > args.queries:
        raise SystemExit("warmup must be between 0 and queries")
    if not 1 <= args.top_k <= 100:
        raise SystemExit("top-k must be between 1 and 100")
    if args.rows // 100 < args.top_k:
        raise SystemExit("rows must leave at least top-k items in the one-percent slice")
    if args.hnsw_ef_search < args.top_k:
        raise SystemExit("hnsw-ef-search must be at least top-k")


def main() -> int:
    args = _parser().parse_args()
    _validate_args(args)
    result = run_async(_benchmark(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
