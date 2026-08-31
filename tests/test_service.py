import asyncio
from collections.abc import Sequence
from datetime import date

import pytest

from wozto_ai_reference.adapters import (
    DenyPhraseQueryPolicy,
    DeterministicGroundedModel,
    InMemorySearchProvider,
    MemoryTelemetry,
)
from wozto_ai_reference.domain import Document, Principal, QueryPolicyDecision, RetrievalHit
from wozto_ai_reference.service import QueryService


def _document(
    *,
    tenant_id: str,
    document_id: str,
    content: str,
    acl_roles: frozenset[str] = frozenset(),
    source_status: str = "unspecified",
    source_authority: str = "unspecified",
    valid_from: date | None = None,
    valid_through: date | None = None,
) -> Document:
    return Document(
        tenant_id=tenant_id,
        document_id=document_id,
        version="v1",
        title=document_id.replace("-", " "),
        section="main",
        source_uri=f"memory://{tenant_id}/{document_id}",
        content=content,
        content_hash=f"sha256:{tenant_id}-{document_id}-v1",
        acl_roles=acl_roles,
        source_status=source_status,
        source_authority=source_authority,
        valid_from=valid_from,
        valid_through=valid_through,
    )


def _run_query(service: QueryService, *, principal: Principal, query: str, **kwargs):
    return asyncio.run(service.query(principal=principal, query=query, **kwargs))


def test_search_and_service_enforce_tenant_and_acl() -> None:
    documents = [
        _document(tenant_id="tenant-a", document_id="public-policy", content="Policy is public."),
        _document(
            tenant_id="tenant-a",
            document_id="finance-policy",
            content="Policy is finance restricted.",
            acl_roles=frozenset({"finance"}),
        ),
        _document(tenant_id="tenant-b", document_id="other-policy", content="Policy belongs to tenant B."),
    ]
    service = QueryService(
        search=InMemorySearchProvider(documents),
        model=DeterministicGroundedModel(),
        telemetry=MemoryTelemetry(),
    )

    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1", roles=frozenset({"employee"})),
        query="policy",
    )

    assert result.abstained is False
    assert [citation.document_id for citation in result.citations] == ["public-policy"]
    assert "finance restricted" not in result.answer
    assert "tenant B" not in result.answer


def test_authorized_role_can_retrieve_restricted_document() -> None:
    document = _document(
        tenant_id="tenant-a",
        document_id="finance-policy",
        content="Finance approval policy.",
        acl_roles=frozenset({"finance"}),
    )
    service = QueryService(
        search=InMemorySearchProvider([document]),
        model=DeterministicGroundedModel(),
        telemetry=MemoryTelemetry(),
    )

    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="finance-1", roles=frozenset({"finance"})),
        query="finance policy",
    )

    assert result.abstained is False
    assert result.citations[0].document_id == "finance-policy"


def test_no_authorized_match_abstains_without_calling_model() -> None:
    class FailIfCalledModel:
        async def generate(self, *, query: str, hits: Sequence[RetrievalHit], trace_id: str) -> str:
            raise AssertionError("model must not be called without authorized evidence")

    restricted = _document(
        tenant_id="tenant-a",
        document_id="finance-policy",
        content="Finance approval policy.",
        acl_roles=frozenset({"finance"}),
    )
    telemetry = MemoryTelemetry()
    service = QueryService(
        search=InMemorySearchProvider([restricted]),
        model=FailIfCalledModel(),
        telemetry=telemetry,
    )

    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1", roles=frozenset({"employee"})),
        query="finance policy",
    )

    assert result.abstained is True
    assert result.citations == []
    assert telemetry.events[-1].name == "query.abstained"


def test_service_post_check_blocks_a_leaky_search_adapter() -> None:
    leaked = RetrievalHit(
        document=_document(
            tenant_id="tenant-b",
            document_id="leaked-secret",
            content="This must never reach the model.",
        ),
        score=1.0,
    )

    class LeakySearch:
        async def search(self, *, principal: Principal, query: str, limit: int) -> Sequence[RetrievalHit]:
            return [leaked]

    class FailIfCalledModel:
        async def generate(self, *, query: str, hits: Sequence[RetrievalHit], trace_id: str) -> str:
            raise AssertionError("defense-in-depth post-check failed")

    service = QueryService(search=LeakySearch(), model=FailIfCalledModel(), telemetry=MemoryTelemetry())
    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
        query="secret",
    )

    assert result.abstained is True
    assert result.citations == []


def test_score_gate_abstains_without_calling_model() -> None:
    candidate = RetrievalHit(
        document=_document(
            tenant_id="tenant-a",
            document_id="weak-candidate",
            content="A weakly related candidate.",
        ),
        score=0.49,
    )

    class WeakSearch:
        async def search(self, *, principal: Principal, query: str, limit: int):
            return [candidate]

    class FailIfCalledModel:
        async def generate(self, *, query: str, hits: Sequence[RetrievalHit], trace_id: str) -> str:
            raise AssertionError("model must not be called below the calibrated score gate")

    service = QueryService(
        search=WeakSearch(),
        model=FailIfCalledModel(),
        telemetry=MemoryTelemetry(),
        minimum_score=0.5,
    )
    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
        query="unrelated question",
    )

    assert result.abstained is True
    assert result.citations == []


def test_configured_hard_policy_abstains_before_search_and_model() -> None:
    class FailIfCalledSearch:
        async def search(self, *, principal: Principal, query: str, limit: int):
            raise AssertionError("search must not run after a hard policy denial")

    telemetry = MemoryTelemetry()
    service = QueryService(
        search=FailIfCalledSearch(),
        model=DeterministicGroundedModel(),
        telemetry=telemetry,
        query_policy=DenyPhraseQueryPolicy(["admin password", "müşteri telefonu"]),
    )

    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
        query="Güncel admin password nedir?",
    )

    assert result.abstained is True
    assert result.citations == []
    assert telemetry.events[-1].attributes == {
        "reason": "request_policy",
        "policy_reason": "configured_hard_deny",
    }


def test_policy_reason_must_be_a_secret_safe_telemetry_code() -> None:
    with pytest.raises(ValueError, match="reason"):
        QueryPolicyDecision(allowed=False, reason="request text must not be echoed")
    with pytest.raises(ValueError, match="reason"):
        DenyPhraseQueryPolicy(["blocked"], reason="unsafe reason")


def test_source_constraints_fail_closed_outside_validity_window() -> None:
    historical = _document(
        tenant_id="tenant-a",
        document_id="historical-policy",
        content="The policy was active through the archive cutoff.",
        source_status="historical",
        source_authority="authoritative",
        valid_from=date(2026, 1, 1),
        valid_through=date(2026, 8, 24),
    )
    service = QueryService(
        search=InMemorySearchProvider([historical]),
        model=DeterministicGroundedModel(),
        telemetry=MemoryTelemetry(),
    )
    principal = Principal(tenant_id="tenant-a", user_id="employee-1")

    covered = _run_query(
        service,
        principal=principal,
        query="archive cutoff policy",
        as_of=date(2026, 8, 24),
        source_status="historical",
        source_authority="authoritative",
    )
    after_cutoff = _run_query(
        service,
        principal=principal,
        query="archive cutoff policy",
        as_of=date(2026, 8, 25),
        source_status="historical",
        source_authority="authoritative",
    )

    assert covered.abstained is False
    assert covered.citations[0].valid_through == date(2026, 8, 24)
    assert after_cutoff.abstained is True
    assert after_cutoff.citations == []


def test_as_of_constraint_rejects_documents_with_unknown_validity() -> None:
    unspecified = _document(
        tenant_id="tenant-a",
        document_id="unknown-validity",
        content="Policy with no validity metadata.",
    )
    service = QueryService(
        search=InMemorySearchProvider([unspecified]),
        model=DeterministicGroundedModel(),
        telemetry=MemoryTelemetry(),
    )

    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
        query="validity metadata policy",
        as_of=date(2026, 8, 24),
    )

    assert result.abstained is True


@pytest.mark.parametrize("minimum_score", [-0.1, float("nan"), float("inf")])
def test_score_gate_rejects_invalid_thresholds(minimum_score: float) -> None:
    with pytest.raises(ValueError, match="minimum_score"):
        QueryService(
            search=InMemorySearchProvider([]),
            model=DeterministicGroundedModel(),
            telemetry=MemoryTelemetry(),
            minimum_score=minimum_score,
        )
