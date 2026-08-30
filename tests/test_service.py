import asyncio
from collections.abc import Sequence

import pytest

from wozto_ai_reference.adapters import DeterministicGroundedModel, InMemorySearchProvider, MemoryTelemetry
from wozto_ai_reference.domain import Document, Principal, RetrievalHit
from wozto_ai_reference.service import QueryService


def _document(
    *,
    tenant_id: str,
    document_id: str,
    content: str,
    acl_roles: frozenset[str] = frozenset(),
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
    )


def _run_query(service: QueryService, *, principal: Principal, query: str):
    return asyncio.run(service.query(principal=principal, query=query))


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


@pytest.mark.parametrize("minimum_score", [-0.1, float("nan"), float("inf")])
def test_score_gate_rejects_invalid_thresholds(minimum_score: float) -> None:
    with pytest.raises(ValueError, match="minimum_score"):
        QueryService(
            search=InMemorySearchProvider([]),
            model=DeterministicGroundedModel(),
            telemetry=MemoryTelemetry(),
            minimum_score=minimum_score,
        )
