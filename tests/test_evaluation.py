import asyncio
from pathlib import Path

from wozto_ai_reference.embedding import HashEmbeddingProvider, InMemoryHybridSearchProvider
from wozto_ai_reference.evaluation import evaluate, load_cases
from wozto_ai_reference.ingest import build_plan

PROJECT = Path(__file__).resolve().parents[1]
SAMPLE = PROJECT / "sample-corpus"


def test_seed_retrieval_gate_passes_without_unauthorized_hits() -> None:
    plan = build_plan(
        source_root=SAMPLE / "documents",
        manifest_path=SAMPLE / "manifest.json",
    )
    search = InMemoryHybridSearchProvider(
        plan.documents,
        embeddings=HashEmbeddingProvider(dimensions=256),
    )
    report = asyncio.run(evaluate(search=search, cases=load_cases(SAMPLE / "gold-set.json"), top_k=3))

    assert report.passes(minimum_recall=0.8, minimum_mrr=0.8)
    assert report.recall_at_k == 1.0
    assert report.mean_reciprocal_rank == 1.0
    assert report.unauthorized_hits == 0
