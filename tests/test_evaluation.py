import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from wozto_ai_reference.domain import Document, Principal, RetrievalHit
from wozto_ai_reference.embedding import HashEmbeddingProvider, InMemoryHybridSearchProvider
from wozto_ai_reference.evaluation import EvalCase, evaluate, load_cases
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
    assert report.retrieval_cases == 5
    assert report.abstain_cases == 0
    assert report.abstain_accuracy == 1.0
    assert report.unauthorized_hits == 0


def _hit(document_id: str, *, score: float, acl_roles: frozenset[str] = frozenset()):
    return RetrievalHit(
        document=Document(
            tenant_id="tenant-a",
            document_id=document_id,
            version="v1",
            title=document_id,
            section="main",
            source_uri=f"memory://{document_id}",
            content="evaluation fixture",
            content_hash=f"sha256:{document_id}",
            acl_roles=acl_roles,
        ),
        score=score,
    )


def test_negative_cases_measure_the_same_score_gate_as_serving() -> None:
    principal = Principal(tenant_id="tenant-a", user_id="evaluator")

    class FixedSearch:
        async def search(
            self, *, principal: Principal, query: str, limit: int
        ) -> Sequence[RetrievalHit]:
            del principal, limit
            if query == "answerable":
                return [_hit("policy::0001", score=0.9)]
            return [_hit("other::0001", score=0.49)]

    cases = (
        EvalCase(
            case_id="positive",
            principal=principal,
            query="answerable",
            relevant_source_document_ids=frozenset({"policy"}),
        ),
        EvalCase(
            case_id="negative",
            principal=principal,
            query="unanswerable",
            expected_abstain=True,
        ),
    )
    report = asyncio.run(
        evaluate(search=FixedSearch(), cases=cases, top_k=5, minimum_score=0.5)
    )

    assert report.retrieval_cases == 1
    assert report.abstain_cases == 1
    assert report.recall_at_k == 1.0
    assert report.mean_reciprocal_rank == 1.0
    assert report.abstain_accuracy == 1.0
    assert report.unexpected_answers == 0
    assert report.passes(minimum_recall=1.0, minimum_mrr=1.0)


def test_negative_case_fails_when_search_returns_an_accepted_candidate() -> None:
    principal = Principal(tenant_id="tenant-a", user_id="evaluator")

    class FixedSearch:
        async def search(self, *, principal: Principal, query: str, limit: int):
            return [_hit("other::0001", score=0.9)]

    report = asyncio.run(
        evaluate(
            search=FixedSearch(),
            cases=(
                EvalCase(
                    case_id="positive",
                    principal=principal,
                    query="answerable",
                    relevant_source_document_ids=frozenset({"other"}),
                ),
                EvalCase(
                    case_id="negative",
                    principal=principal,
                    query="unanswerable",
                    expected_abstain=True,
                ),
            ),
            top_k=5,
        )
    )

    assert report.abstain_accuracy == 0.0
    assert report.unexpected_answers == 1
    assert not report.passes(minimum_recall=1.0, minimum_mrr=1.0)


def test_leaky_negative_hit_is_counted_even_when_post_check_abstains() -> None:
    principal = Principal(tenant_id="tenant-a", user_id="evaluator")

    class LeakySearch:
        async def search(self, *, principal: Principal, query: str, limit: int):
            if query == "answerable":
                return [_hit("public::0001", score=0.9)]
            return [_hit("restricted::0001", score=0.9, acl_roles=frozenset({"finance"}))]

    report = asyncio.run(
        evaluate(
            search=LeakySearch(),
            cases=(
                EvalCase(
                    case_id="positive",
                    principal=principal,
                    query="answerable",
                    relevant_source_document_ids=frozenset({"public"}),
                ),
                EvalCase(
                    case_id="negative",
                    principal=principal,
                    query="restricted",
                    expected_abstain=True,
                ),
            ),
        )
    )

    assert report.abstain_accuracy == 1.0
    assert report.unauthorized_hits == 1
    assert not report.passes(minimum_recall=1.0, minimum_mrr=1.0)


@pytest.mark.parametrize(
    "case",
    [
        {
            "case_id": "missing-target",
            "principal": {"tenant_id": "tenant-a", "user_id": "u"},
            "query": "q",
        },
        {
            "case_id": "conflicting-negative",
            "principal": {"tenant_id": "tenant-a", "user_id": "u"},
            "query": "q",
            "expected_abstain": True,
            "relevant_document_ids": ["doc"],
        },
        {
            "case_id": "two-target-kinds",
            "principal": {"tenant_id": "tenant-a", "user_id": "u"},
            "query": "q",
            "relevant_document_ids": ["doc::0001"],
            "relevant_source_document_ids": ["doc"],
        },
    ],
)
def test_eval_case_rejects_ambiguous_expectations(case: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EvalCase.model_validate(case)
