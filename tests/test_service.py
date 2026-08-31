import asyncio
from collections.abc import Sequence
from datetime import date

import pytest

from wozto_ai_reference.adapters import (
    ConfiguredPhraseScopeResolver,
    DenyPhraseQueryPolicy,
    DeterministicGroundedModel,
    ExactEvidenceSupportCritic,
    InMemorySearchProvider,
    MemoryTelemetry,
    PhraseScopeRule,
)
from wozto_ai_reference.domain import (
    Document,
    EvidenceReference,
    EvidenceSupportDecision,
    Principal,
    QueryConstraints,
    QueryPolicyDecision,
    QueryScopeDecision,
    RetrievalHit,
)
from wozto_ai_reference.service import QueryService, merge_query_constraints


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


def test_scope_and_critic_decision_models_reject_inconsistent_payloads() -> None:
    with pytest.raises(ValueError, match="refused scope"):
        QueryScopeDecision(
            allowed=False,
            reason="ambiguous",
            constraints=QueryConstraints(source_status="current"),
        )
    with pytest.raises(ValueError, match="supporting evidence"):
        EvidenceSupportDecision(supported=True, reason="claimed_support")


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


def test_configured_scope_resolver_merges_compatible_phrase_rules() -> None:
    resolver = ConfiguredPhraseScopeResolver(
        [
            PhraseScopeRule(
                phrase="güncel",
                constraints=QueryConstraints(source_status="current"),
            ),
            PhraseScopeRule(
                phrase="onaylanmış",
                constraints=QueryConstraints(source_authority="authoritative"),
            ),
            PhraseScopeRule(
                phrase="2027 yılı",
                constraints=QueryConstraints(as_of=date(2027, 1, 1)),
            ),
        ]
    )

    decision = asyncio.run(
        resolver.resolve(
            principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
            query="2027 YILI için güncel ve onaylanmış bütçe nedir?",
        )
    )

    assert decision == QueryScopeDecision(
        allowed=True,
        reason="scope_resolved",
        constraints=QueryConstraints(
            as_of=date(2027, 1, 1),
            source_status="current",
            source_authority="authoritative",
        ),
    )


def test_configured_scope_resolver_refuses_conflicting_matches() -> None:
    resolver = ConfiguredPhraseScopeResolver(
        [
            PhraseScopeRule(
                phrase="policy",
                constraints=QueryConstraints(source_status="current"),
            ),
            PhraseScopeRule(
                phrase="policy",
                constraints=QueryConstraints(source_status="historical"),
            ),
        ]
    )

    decision = asyncio.run(
        resolver.resolve(
            principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
            query="policy",
        )
    )

    assert decision == QueryScopeDecision(
        allowed=False,
        reason="conflicting_scope_rules",
    )


def test_scope_phrase_matching_handles_punctuation_but_not_word_fragments() -> None:
    resolver = ConfiguredPhraseScopeResolver(
        [
            PhraseScopeRule(
                phrase="güncel",
                constraints=QueryConstraints(source_status="current"),
            )
        ]
    )
    principal = Principal(tenant_id="tenant-a", user_id="employee-1")

    punctuated = asyncio.run(resolver.resolve(principal=principal, query="GÜNCEL, politika nedir?"))
    fragment = asyncio.run(resolver.resolve(principal=principal, query="Güncelleme planı nedir?"))

    assert punctuated.constraints.source_status == "current"
    assert fragment == QueryScopeDecision(allowed=True, reason="unconstrained")


def test_scope_resolver_constraints_filter_historical_evidence() -> None:
    historical = _document(
        tenant_id="tenant-a",
        document_id="historical-policy",
        content="The historical policy contains budget guidance.",
        source_status="historical",
        source_authority="advisory",
        valid_through=date(2026, 8, 24),
    )

    class FailIfCalledModel:
        async def generate(self, *, query: str, hits: Sequence[RetrievalHit], trace_id: str) -> str:
            raise AssertionError("model must not run without scope-compatible evidence")

    service = QueryService(
        search=InMemorySearchProvider([historical]),
        model=FailIfCalledModel(),
        telemetry=MemoryTelemetry(),
        scope_resolver=ConfiguredPhraseScopeResolver(
            [
                PhraseScopeRule(
                    phrase="güncel",
                    constraints=QueryConstraints(source_status="current"),
                )
            ]
        ),
    )

    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
        query="Güncel policy bütçesi nedir?",
    )

    assert result.abstained is True
    assert result.citations == []


def test_explicit_and_resolved_scope_conflict_abstains_before_search() -> None:
    class FailIfCalledSearch:
        async def search(self, *, principal: Principal, query: str, limit: int):
            raise AssertionError("search must not run after a scope conflict")

    telemetry = MemoryTelemetry()
    service = QueryService(
        search=FailIfCalledSearch(),
        model=DeterministicGroundedModel(),
        telemetry=telemetry,
        scope_resolver=ConfiguredPhraseScopeResolver(
            [
                PhraseScopeRule(
                    phrase="current",
                    constraints=QueryConstraints(source_status="current"),
                )
            ]
        ),
    )

    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
        query="current policy",
        source_status="historical",
    )

    assert result.abstained is True
    assert telemetry.events[-1].attributes == {"reason": "scope_constraint_conflict"}


def test_merge_query_constraints_keeps_compatible_narrowing() -> None:
    merged = merge_query_constraints(
        QueryConstraints(source_status="current"),
        QueryConstraints(
            as_of=date(2026, 8, 31),
            source_status="current",
            source_authority="authoritative",
        ),
    )

    assert merged == QueryConstraints(
        as_of=date(2026, 8, 31),
        source_status="current",
        source_authority="authoritative",
    )


def test_exact_evidence_critic_accepts_extract_and_limits_citations() -> None:
    first = _document(
        tenant_id="tenant-a",
        document_id="first-policy",
        content="Refund requests require human review.",
    )
    second = _document(
        tenant_id="tenant-a",
        document_id="second-policy",
        content="Refund requests are logged for audit.",
    )

    class FirstEvidenceModel:
        async def generate(self, *, query: str, hits: Sequence[RetrievalHit], trace_id: str) -> str:
            return f"Grounded answer: {hits[0].document.content}"

    service = QueryService(
        search=InMemorySearchProvider([first, second]),
        model=FirstEvidenceModel(),
        telemetry=MemoryTelemetry(),
        evidence_critic=ExactEvidenceSupportCritic(allowed_prefixes=["Grounded answer:"]),
    )

    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
        query="refund requests",
    )

    assert result.abstained is False
    assert [citation.document_id for citation in result.citations] == ["first-policy"]


def test_evidence_critic_rejects_unsupported_answer_and_hides_it() -> None:
    document = _document(
        tenant_id="tenant-a",
        document_id="refund-policy",
        content="Refund requests require human review.",
    )

    class HallucinatingModel:
        async def generate(self, *, query: str, hits: Sequence[RetrievalHit], trace_id: str) -> str:
            return "Refunds are always approved automatically."

    telemetry = MemoryTelemetry()
    service = QueryService(
        search=InMemorySearchProvider([document]),
        model=HallucinatingModel(),
        telemetry=telemetry,
        evidence_critic=ExactEvidenceSupportCritic(),
    )

    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
        query="refund requests",
    )

    assert result.abstained is True
    assert result.answer == "Yeterli ve yetkili kaynak bulunamadı."
    assert result.citations == []
    assert telemetry.events[-1].attributes == {
        "reason": "evidence_support",
        "critic_reason": "unsupported_answer",
    }


def test_evidence_critic_support_is_bound_to_version_and_content_hash() -> None:
    old = _document(
        tenant_id="tenant-a",
        document_id="refund-policy",
        content="Old refund policy requires human review.",
    )
    new = old.model_copy(
        update={
            "version": "v2",
            "content": "New refund policy requires manager review.",
            "content_hash": "sha256:tenant-a-refund-policy-v2",
        }
    )

    class VersionedSearch:
        async def search(self, *, principal, query, limit):
            return [
                RetrievalHit(document=old, score=1.0),
                RetrievalHit(document=new, score=0.9),
            ]

    class OldVersionModel:
        async def generate(self, *, query, hits, trace_id):
            return old.content

    service = QueryService(
        search=VersionedSearch(),
        model=OldVersionModel(),
        telemetry=MemoryTelemetry(),
        evidence_critic=ExactEvidenceSupportCritic(),
    )

    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
        query="refund policy review",
    )

    assert result.abstained is False
    assert [(citation.document_id, citation.version) for citation in result.citations] == [("refund-policy", "v1")]


def test_service_rejects_critic_support_outside_authorized_hits() -> None:
    document = _document(
        tenant_id="tenant-a",
        document_id="refund-policy",
        content="Refund requests require human review.",
    )

    class InvalidSupportCritic:
        async def evaluate(self, *, principal, query, answer, hits):
            return EvidenceSupportDecision(
                supported=True,
                reason="claimed_support",
                supporting_evidence=frozenset(
                    {
                        EvidenceReference(
                            document_id="not-retrieved",
                            version="v1",
                            content_hash="sha256:not-retrieved",
                        )
                    }
                ),
            )

    telemetry = MemoryTelemetry()
    service = QueryService(
        search=InMemorySearchProvider([document]),
        model=DeterministicGroundedModel(),
        telemetry=telemetry,
        evidence_critic=InvalidSupportCritic(),
    )

    result = _run_query(
        service,
        principal=Principal(tenant_id="tenant-a", user_id="employee-1"),
        query="refund requests",
    )

    assert result.abstained is True
    assert telemetry.events[-1].attributes["critic_reason"] == "critic_invalid_support"


@pytest.mark.parametrize("minimum_score", [-0.1, float("nan"), float("inf")])
def test_score_gate_rejects_invalid_thresholds(minimum_score: float) -> None:
    with pytest.raises(ValueError, match="minimum_score"):
        QueryService(
            search=InMemorySearchProvider([]),
            model=DeterministicGroundedModel(),
            telemetry=MemoryTelemetry(),
            minimum_score=minimum_score,
        )
