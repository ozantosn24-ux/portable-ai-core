import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from wozto_ai_reference.adapters import DeterministicGroundedModel, InMemorySearchProvider, MemoryTelemetry
from wozto_ai_reference.domain import Document, Principal, RetrievalHit
from wozto_ai_reference.embedding import HashEmbeddingProvider, InMemoryHybridSearchProvider
from wozto_ai_reference.evaluation import (
    EvalCase,
    _parser,
    _validate_args,
    evaluate,
    evaluate_citations,
    load_cases,
)
from wozto_ai_reference.ingest import build_plan
from wozto_ai_reference.service import QueryService

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


_PRINCIPAL = {"tenant_id": "tenant-a", "user_id": "u"}


def test_abstain_reason_is_optional_closed_set_and_only_for_abstain_cases() -> None:
    labelled = EvalCase.model_validate(
        {
            "case_id": "x",
            "principal": _PRINCIPAL,
            "query": "q",
            "expected_abstain": True,
            "expected_abstain_reason": "unauthorized",
        }
    )
    assert labelled.expected_abstain_reason == "unauthorized"
    assert EvalCase.model_validate(
        {"case_id": "y", "principal": _PRINCIPAL, "query": "q", "expected_abstain": True}
    ).expected_abstain_reason is None
    with pytest.raises(ValidationError):
        EvalCase.model_validate(
            {
                "case_id": "z",
                "principal": _PRINCIPAL,
                "query": "q",
                "expected_abstain": True,
                "expected_abstain_reason": "because",
            }
        )
    with pytest.raises(ValidationError):
        EvalCase.model_validate(
            {
                "case_id": "w",
                "principal": _PRINCIPAL,
                "query": "q",
                "relevant_document_ids": ["doc"],
                "expected_abstain_reason": "no_source",
            }
        )


def _abstain_label_holds(case: EvalCase, *, search: InMemorySearchProvider, privileged: Principal) -> bool:
    """`unauthorized` => the same query finds the source for a privileged caller;
    `no_source` => it finds nothing even for a privileged caller."""
    hits = asyncio.run(search.search(principal=privileged, query=case.query, limit=5))
    if case.expected_abstain_reason == "unauthorized":
        return len(hits) >= 1
    if case.expected_abstain_reason == "no_source":
        return len(hits) == 0
    raise AssertionError("case carries no abstain reason")


def test_abstain_reason_labels_match_what_the_corpus_actually_does() -> None:
    restricted = Document(
        tenant_id="tenant-a",
        document_id="finance-policy::0001",
        version="v1",
        title="Finance policy",
        section="main",
        source_uri="memory://finance-policy",
        content="Finance approval policy.",
        content_hash="sha256:finance-policy",
        acl_roles=frozenset({"finance"}),
    )
    search = InMemorySearchProvider([restricted])
    employee = Principal(tenant_id="tenant-a", user_id="e", roles=frozenset({"employee"}))
    finance = Principal(tenant_id="tenant-a", user_id="f", roles=frozenset({"finance"}))

    def case(case_id: str, query: str, reason: str) -> EvalCase:
        return EvalCase(
            case_id=case_id,
            principal=employee,
            query=query,
            expected_abstain=True,
            expected_abstain_reason=reason,
        )

    unauthorized = case("unauthorized", "finance policy", "unauthorized")
    no_source = case("no-source", "zebra migration", "no_source")
    swapped_unauthorized = case("swapped-1", "finance policy", "no_source")
    swapped_no_source = case("swapped-2", "zebra migration", "unauthorized")

    # Both cases really abstain for the employee ...
    report = asyncio.run(evaluate(search=search, cases=(unauthorized, no_source)))
    assert report.abstain_accuracy == 1.0
    assert report.unauthorized_hits == 0
    # ... the labels agree with what a privileged caller sees ...
    assert _abstain_label_holds(unauthorized, search=search, privileged=finance)
    assert _abstain_label_holds(no_source, search=search, privileged=finance)
    # ... and the check is not vacuous: swapped labels are caught.
    assert not _abstain_label_holds(swapped_unauthorized, search=search, privileged=finance)
    assert not _abstain_label_holds(swapped_no_source, search=search, privileged=finance)


def _citation_service(*documents: Document) -> QueryService:
    return QueryService(
        search=InMemorySearchProvider(documents),
        model=DeterministicGroundedModel(),
        telemetry=MemoryTelemetry(),
    )


def _citation_document(document_id: str, content: str) -> Document:
    return Document(
        tenant_id="tenant-a",
        document_id=document_id,
        version="v1",
        title=document_id,
        section="main",
        source_uri=f"memory://{document_id}",
        content=content,
        content_hash=f"sha256:{document_id}",
    )


def test_citation_correct_compares_cited_document_ids_with_gold() -> None:
    refund = _citation_document("refund::0001", "refund requests need review")
    shipping = _citation_document("shipping::0001", "refund shipping labels are free")
    principal = Principal(tenant_id="tenant-a", user_id="u")
    service = _citation_service(refund, shipping)

    right = EvalCase(
        case_id="right", principal=principal, query="refund review", relevant_document_ids=frozenset({"refund::0001"})
    )
    wrong = EvalCase(
        case_id="wrong", principal=principal, query="refund review", relevant_document_ids=frozenset({"shipping::0001"})
    )
    by_source = EvalCase(
        case_id="by-source",
        principal=principal,
        query="refund review",
        relevant_source_document_ids=frozenset({"refund"}),
    )
    should_abstain = EvalCase(
        case_id="abstain", principal=principal, query="zebra migration", expected_abstain=True
    )
    answered_when_must_abstain = EvalCase(
        case_id="no-abstain", principal=principal, query="refund review", expected_abstain=True
    )
    cases = (right, wrong, by_source, should_abstain, answered_when_must_abstain)

    report = asyncio.run(evaluate_citations(service=service, cases=cases))

    # `wrong` is cited too (both documents match the query) so it counts as correct:
    # the metric is "a gold document is cited", noise is reported separately.
    assert report.incorrect_case_ids == ["no-abstain"]
    assert report.citation_correct == 4
    assert report.citation_correct_rate == 0.8
    assert report.irrelevant_citations >= 1


def test_citation_correct_fails_when_gold_document_is_not_cited() -> None:
    other = _citation_document("other::0001", "completely unrelated pangolin facts")
    principal = Principal(tenant_id="tenant-a", user_id="u")
    case = EvalCase(
        case_id="missing", principal=principal, query="pangolin", relevant_document_ids=frozenset({"refund::0001"})
    )

    report = asyncio.run(evaluate_citations(service=_citation_service(other), cases=(case,)))

    assert report.incorrect_case_ids == ["missing"]
    assert report.citation_correct_rate == 0.0


def test_seed_corpus_citations_are_correct() -> None:
    plan = build_plan(source_root=SAMPLE / "documents", manifest_path=SAMPLE / "manifest.json")
    service = QueryService(
        search=InMemoryHybridSearchProvider(plan.documents, embeddings=HashEmbeddingProvider(dimensions=256)),
        model=DeterministicGroundedModel(),
        telemetry=MemoryTelemetry(),
    )

    report = asyncio.run(evaluate_citations(service=service, cases=load_cases(SAMPLE / "gold-set.json"), top_k=3))

    assert report.citation_correct_rate == 1.0
    assert report.incorrect_case_ids == []


def test_citations_flag_refuses_top_k_above_the_service_limit(capsys: pytest.CaptureFixture[str]) -> None:
    common = ["--source-root", "s", "--manifest", "m", "--gold-set", "g"]
    parser = _parser()

    with pytest.raises(SystemExit) as exit_info:
        _validate_args(parser, parser.parse_args([*common, "--citations", "--top-k", "21"]))
    assert exit_info.value.code == 2
    assert "--top-k up to 20" in capsys.readouterr().err

    # Not refused: the search-only gate keeps its 1..100 range, and 20 is the boundary.
    _validate_args(parser, parser.parse_args([*common, "--top-k", "50"]))
    _validate_args(parser, parser.parse_args([*common, "--citations", "--top-k", "20"]))
