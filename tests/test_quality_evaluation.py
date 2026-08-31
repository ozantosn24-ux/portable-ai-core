import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from wozto_ai_reference.adapters import (
    ConfiguredPhraseScopeResolver,
    ExactEvidenceSupportCritic,
    ExactStructuredClaimSupportCritic,
    PhraseScopeRule,
)
from wozto_ai_reference.domain import (
    Document,
    EvidenceReference,
    EvidenceSupportDecision,
    Principal,
    QueryConstraints,
    QueryScopeDecision,
    RetrievalHit,
    StructuredAnswer,
    StructuredClaim,
    evidence_reference,
)
from wozto_ai_reference.quality_evaluation import (
    CRITIC_EVAL_SCHEMA,
    SCOPE_EVAL_SCHEMA,
    STRUCTURED_CRITIC_EVAL_SCHEMA,
    CriticEvalCase,
    ScopeEvalCase,
    StructuredCriticEvalCase,
    evaluate_evidence_critic,
    evaluate_scope_resolver,
    evaluate_structured_claim_critic,
    load_critic_cases,
    load_scope_cases,
    load_scope_rules,
    load_structured_critic_cases,
)


def _document(*, tenant_id: str = "tenant-a", document_id: str = "policy") -> Document:
    return Document(
        tenant_id=tenant_id,
        document_id=document_id,
        version="v1",
        title="Policy",
        section="Main",
        source_uri=f"memory://{tenant_id}/{document_id}",
        content="Refund requests require human review.",
        content_hash=f"sha256:{tenant_id}-{document_id}",
    )


def _principal() -> Principal:
    return Principal(tenant_id="tenant-a", user_id="employee-1")


def test_scope_eval_is_separate_from_retrieval_and_passes_exact_contract() -> None:
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
        ]
    )
    cases = (
        ScopeEvalCase(
            case_id="resolved",
            principal=_principal(),
            query="Güncel ve onaylanmış politika nedir?",
            expected_constraints=QueryConstraints(
                source_status="current",
                source_authority="authoritative",
            ),
        ),
        ScopeEvalCase(
            case_id="unconstrained",
            principal=_principal(),
            query="Arşivde hangi yaklaşım önerilmişti?",
        ),
    )

    report, results = asyncio.run(evaluate_scope_resolver(resolver=resolver, cases=cases))

    assert report.accuracy == 1.0
    assert report.passes()
    assert all(result.exact_match for result in results)


def test_scope_eval_fails_on_unsafe_allow_and_duplicate_id() -> None:
    class AlwaysAllowResolver:
        async def resolve(self, *, principal, query):
            return QueryScopeDecision(allowed=True, reason="unconstrained")

    case = ScopeEvalCase(
        case_id="must-refuse",
        principal=_principal(),
        query="Ambiguous current or historical request",
        expected_allowed=False,
    )

    report, _ = asyncio.run(evaluate_scope_resolver(resolver=AlwaysAllowResolver(), cases=(case, case)))

    assert report.false_allows == 2
    assert report.duplicate_case_ids == 1
    assert not report.passes(minimum_accuracy=0.0)


def test_evidence_critic_eval_uses_frozen_hits_and_detects_false_accepts() -> None:
    authorized_hit = RetrievalHit(document=_document(), score=1.0)
    unsafe_hit = RetrievalHit(
        document=_document(tenant_id="tenant-b", document_id="foreign-policy"),
        score=1.0,
    )
    cases = (
        CriticEvalCase(
            case_id="supported",
            principal=_principal(),
            query="What is the refund rule?",
            answer="Refund requests require human review.",
            hits=(authorized_hit,),
            expected_supported=True,
            expected_supporting_evidence=frozenset(
                {
                    EvidenceReference(
                        document_id="policy",
                        version="v1",
                        content_hash="sha256:tenant-a-policy",
                    )
                }
            ),
        ),
        CriticEvalCase(
            case_id="hallucinated",
            principal=_principal(),
            query="What is the refund rule?",
            answer="Refunds are automatic.",
            hits=(authorized_hit,),
            expected_supported=False,
        ),
        CriticEvalCase(
            case_id="cross-tenant",
            principal=_principal(),
            query="What is the refund rule?",
            answer="Refund requests require human review.",
            hits=(unsafe_hit,),
            expected_supported=False,
        ),
    )

    report, results = asyncio.run(evaluate_evidence_critic(critic=ExactEvidenceSupportCritic(), cases=cases))

    assert report.accuracy == 1.0
    assert report.passes()
    assert all(result.exact_match for result in results)

    class AlwaysSupportCritic:
        async def evaluate(self, *, principal, query, answer, hits):
            return EvidenceSupportDecision(
                supported=True,
                reason="claimed_support",
                supporting_evidence=frozenset(
                    {
                        EvidenceReference(
                            document_id=hits[0].document.document_id,
                            version=hits[0].document.version,
                            content_hash=hits[0].document.content_hash,
                        )
                    }
                ),
            )

    unsafe_report, _ = asyncio.run(
        evaluate_evidence_critic(
            critic=AlwaysSupportCritic(),
            cases=(cases[1],),
        )
    )
    assert unsafe_report.false_accepts == 1
    assert not unsafe_report.passes(minimum_accuracy=0.0)


def test_quality_eval_json_loaders_validate_versioned_files(tmp_path: Path) -> None:
    scope_path = tmp_path / "scope.json"
    scope_path.write_text(
        json.dumps(
            {
                "schema_version": SCOPE_EVAL_SCHEMA,
                "rules": [
                    {
                        "phrase": "after cutoff",
                        "constraints": {"as_of": "2026-08-25"},
                    }
                ],
                "cases": [
                    {
                        "case_id": "after-cutoff",
                        "principal": {
                            "tenant_id": "tenant-a",
                            "user_id": "employee-1",
                        },
                        "query": "What changed after cutoff?",
                        "expected_constraints": {"as_of": "2026-08-25"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    critic_path = tmp_path / "critic.json"
    critic_path.write_text(
        json.dumps(
            {
                "schema_version": CRITIC_EVAL_SCHEMA,
                "cases": [
                    {
                        "case_id": "supported",
                        "principal": {
                            "tenant_id": "tenant-a",
                            "user_id": "employee-1",
                        },
                        "query": "What is the refund rule?",
                        "answer": "Refund requests require human review.",
                        "hits": [
                            {
                                "document": _document().model_dump(mode="json"),
                                "score": 1.0,
                            }
                        ],
                        "expected_supported": True,
                        "expected_supporting_evidence": [
                            {
                                "document_id": "policy",
                                "version": "v1",
                                "content_hash": "sha256:tenant-a-policy",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    rules = load_scope_rules(scope_path)
    scope_cases = load_scope_cases(scope_path)
    critic_cases = load_critic_cases(critic_path)

    assert rules[0].constraints.as_of == date(2026, 8, 25)
    assert scope_cases[0].expected_constraints.as_of == date(2026, 8, 25)
    assert critic_cases[0].hits[0].document.document_id == "policy"


def test_quality_eval_loader_rejects_non_object_records(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps({"schema_version": SCOPE_EVAL_SCHEMA, "rules": ["not-an-object"]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="entries must be objects"):
        load_scope_rules(path)

    path.write_text(
        json.dumps(
            {
                "schema_version": SCOPE_EVAL_SCHEMA,
                "rules": [{"phrase": 2027, "constraints": {"source_status": "current"}}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="phrase must be a string"):
        load_scope_rules(path)


def test_structured_claim_eval_uses_frozen_evidence_and_detects_false_accepts() -> None:
    hit = RetrievalHit(document=_document(), score=1.0)
    reference = evidence_reference(hit.document)
    supported = StructuredCriticEvalCase(
        case_id="supported-extract",
        principal=_principal(),
        query="What is the refund rule?",
        answer=StructuredAnswer(
            answer="Refund requests require human review.",
            claims=(
                StructuredClaim(
                    claim_id="refund-review",
                    text="Refund requests require human review.",
                    supporting_evidence=frozenset({reference}),
                ),
            ),
        ),
        hits=(hit,),
        expected_supported=True,
        expected_supporting_evidence=frozenset({reference}),
    )
    unrelated_query = supported.model_copy(
        update={
            "case_id": "query-irrelevant-extract",
            "query": "What is the approved advertising budget?",
            "expected_supported": False,
            "expected_supporting_evidence": frozenset(),
        }
    )

    report, results = asyncio.run(
        evaluate_structured_claim_critic(
            critic=ExactStructuredClaimSupportCritic(),
            cases=(supported, unrelated_query),
        )
    )

    assert report.accuracy == 0.5
    assert report.false_accepts == 1
    assert not report.passes(minimum_accuracy=0.5)
    assert results[1].false_accept is True


def test_structured_claim_eval_loader_has_separate_versioned_schema(tmp_path: Path) -> None:
    document = _document()
    reference = evidence_reference(document)
    path = tmp_path / "structured-critic.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": STRUCTURED_CRITIC_EVAL_SCHEMA,
                "cases": [
                    {
                        "case_id": "supported",
                        "principal": _principal().model_dump(mode="json"),
                        "query": "What is the refund rule?",
                        "answer": {
                            "answer": document.content,
                            "claims": [
                                {
                                    "claim_id": "refund-review",
                                    "text": document.content,
                                    "supporting_evidence": [reference.model_dump(mode="json")],
                                }
                            ],
                        },
                        "hits": [
                            {
                                "document": document.model_dump(mode="json"),
                                "score": 1.0,
                            }
                        ],
                        "expected_supported": True,
                        "expected_supporting_evidence": [reference.model_dump(mode="json")],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cases = load_structured_critic_cases(path)

    assert cases[0].answer.claims[0].supporting_evidence == frozenset({reference})
