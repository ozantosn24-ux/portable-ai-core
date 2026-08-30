"""Measure local pgvector search round-trip latency without making a quality claim.

The probe uses the deterministic hash embedding provider so the reported latency covers
query embedding, PostgreSQL hybrid-search SQL, network round trip, and result mapping. It
does not represent E5/model latency or production-scale index performance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
from itertools import cycle, islice
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wozto_ai_reference.asyncio_compat import run as run_async  # noqa: E402
from wozto_ai_reference.domain import Document, Principal  # noqa: E402
from wozto_ai_reference.embedding import HashEmbeddingProvider  # noqa: E402
from wozto_ai_reference.pgvector_store import PgVectorStore  # noqa: E402


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        raise ValueError("samples must not be empty")
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _load_fixture(data_dir: Path, tenant_id: str) -> tuple[list[Document], list[str]]:
    corpus = json.loads((data_dir / "corpus.json").read_text(encoding="utf-8"))["documents"]
    cases = json.loads((data_dir / "cases.json").read_text(encoding="utf-8"))["cases"]
    documents = []
    for index, item in enumerate(corpus):
        content = str(item["content"])
        documents.append(
            Document(
                tenant_id=tenant_id,
                document_id=f"latency::{index:04d}",
                version="v1",
                title=str(item["title"]),
                section=str(item["section"]),
                source_uri=f"dataset://xquad-tr/{item['document_id']}",
                content=content,
                content_hash="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
                acl_roles=frozenset(),
            )
        )
    queries = [str(item["query"]) for item in cases]
    if not documents or not queries:
        raise ValueError("benchmark fixture must contain documents and queries")
    return documents, queries


async def _benchmark(args: argparse.Namespace) -> dict[str, object]:
    database_url = os.getenv("WOZTO_REFERENCE_DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("WOZTO_REFERENCE_DATABASE_URL must be set")

    tenant_id = f"latency-{uuid4().hex}"
    documents, queries = _load_fixture(args.data, tenant_id)
    embeddings = HashEmbeddingProvider(dimensions=64)
    store = PgVectorStore(
        database_url=database_url,
        embeddings=embeddings,
        vector_weight=args.vector_weight,
        text_search_config=args.text_search_config,
    )
    principal = Principal(tenant_id=tenant_id, user_id="latency-probe", roles=frozenset())
    inserted = False
    try:
        await store.initialize()
        await store.replace_source(
            tenant_id=tenant_id,
            source_document_id="latency",
            documents=documents,
        )
        inserted = True

        for query in islice(cycle(queries), args.warmup):
            await store.search(principal=principal, query=query, limit=args.top_k)

        samples_ms: list[float] = []
        empty_results = 0
        for query in islice(cycle(queries), args.iterations):
            started = time.perf_counter_ns()
            hits = await store.search(principal=principal, query=query, limit=args.top_k)
            samples_ms.append((time.perf_counter_ns() - started) / 1_000_000)
            empty_results += not hits

        return {
            "scope": "hash embedding + local pgvector hybrid SQL + result mapping",
            "quality_claim": False,
            "documents": len(documents),
            "queries_available": len(queries),
            "warmup": args.warmup,
            "iterations": args.iterations,
            "top_k": args.top_k,
            "empty_results": empty_results,
            "latency_ms": {
                "min": round(min(samples_ms), 3),
                "p50": round(_percentile(samples_ms, 0.50), 3),
                "p95": round(_percentile(samples_ms, 0.95), 3),
                "max": round(max(samples_ms), 3),
                "mean": round(statistics.fmean(samples_ms), 3),
            },
        }
    finally:
        if inserted:
            for document in documents:
                await store.delete(
                    tenant_id=tenant_id,
                    document_id=document.document_id,
                    version=document.version,
                )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/xquad-tr"))
    parser.add_argument("--iterations", type=int, default=150)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--vector-weight", type=float, default=0.7)
    parser.add_argument(
        "--text-search-config",
        choices=("simple", "turkish", "english"),
        default="turkish",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.iterations < 1 or args.warmup < 0:
        raise SystemExit("iterations must be positive and warmup must be non-negative")
    print(json.dumps(run_async(_benchmark(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
