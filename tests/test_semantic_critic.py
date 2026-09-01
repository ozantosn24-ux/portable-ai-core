from __future__ import annotations

import asyncio
import math

import pytest

from wozto_ai_reference.domain import (
    Document,
    EvidenceReference,
    Principal,
    RetrievalHit,
    StructuredAnswer,
    StructuredClaim,
    evidence_reference,
)
from wozto_ai_reference.semantic_critic import SemanticStructuredClaimSupportCritic


class _Scorer:
    def __init__(self, scores: list[float] | Exception) -> None:
        self._scores = scores
        self.calls: list[tuple[tuple[str, str], ...]] = []

    async def score_pairs(self, pairs):
        frozen = tuple(pairs)
        self.calls.append(frozen)
        if isinstance(self._scores, Exception):
            raise self._scores
        return self._scores


def test_structured_claim_schema_exposes_self_contained_text_contract() -> None:
    description = StructuredClaim.model_json_schema()["properties"]["text"]["description"]

    assert "self-contained" in description
    assert "without relying on the query" in description


def _principal() -> Principal:
    return Principal(tenant_id="tenant-a", user_id="reviewer", roles=frozenset({"reader"}))


def _document(
    *,
    document_id: str = "refund-policy",
    content: str = "Refund requests require human review before action.",
    tenant_id: str = "tenant-a",
) -> Document:
    return Document(
        tenant_id=tenant_id,
        document_id=document_id,
        version="v1",
        title="Policy",
        section="Review",
        source_uri=f"memory://{tenant_id}/{document_id}",
        content=content,
        content_hash=f"sha256:{tenant_id}-{document_id}-v1",
        acl_roles=frozenset({"reader"}),
    )


def _answer(*, text: str, references: frozenset[EvidenceReference]) -> StructuredAnswer:
    return StructuredAnswer(
        answer=text,
        claims=(
            StructuredClaim(
                claim_id="refund-review",
                text=text,
                supporting_evidence=references,
            ),
        ),
    )


def _critic(
    *,
    relevance: _Scorer,
    entailment: _Scorer,
    minimum_relevance: float = 0.7,
    minimum_entailment: float = 0.8,
) -> SemanticStructuredClaimSupportCritic:
    return SemanticStructuredClaimSupportCritic(
        relevance_scorer=relevance,
        entailment_scorer=entailment,
        minimum_relevance=minimum_relevance,
        minimum_entailment=minimum_entailment,
    )


def test_semantic_critic_accepts_relevant_paraphrase_and_returns_bound_evidence() -> None:
    document = _document()
    reference = evidence_reference(document)
    relevance = _Scorer([0.91])
    entailment = _Scorer([0.94])
    claim = "A human must review each refund request before action."

    decision = asyncio.run(
        _critic(relevance=relevance, entailment=entailment).evaluate(
            principal=_principal(),
            query="How are refund requests handled?",
            answer=_answer(text=claim, references=frozenset({reference})),
            hits=(RetrievalHit(document=document, score=1.0),),
        )
    )

    assert decision.supported is True
    assert decision.reason == "semantic_claim_support"
    assert decision.supporting_evidence == frozenset({reference})
    assert relevance.calls == [(("How are refund requests handled?", claim),)]
    assert entailment.calls == [((document.content, claim),)]


def test_semantic_critic_rejects_query_irrelevant_exact_extract_before_entailment() -> None:
    document = _document()
    relevance = _Scorer([0.2])
    entailment = _Scorer([0.99])

    decision = asyncio.run(
        _critic(relevance=relevance, entailment=entailment).evaluate(
            principal=_principal(),
            query="What is the advertising budget?",
            answer=_answer(
                text=document.content,
                references=frozenset({evidence_reference(document)}),
            ),
            hits=(RetrievalHit(document=document, score=1.0),),
        )
    )

    assert decision.supported is False
    assert decision.reason == "query_irrelevant_claim"
    assert entailment.calls == []


def test_semantic_critic_requires_every_cited_document_to_entail_the_claim() -> None:
    first = _document()
    second = _document(document_id="shipping-policy", content="Orders ship on weekdays.")
    references = frozenset({evidence_reference(first), evidence_reference(second)})

    decision = asyncio.run(
        _critic(relevance=_Scorer([0.9]), entailment=_Scorer([0.95, 0.1])).evaluate(
            principal=_principal(),
            query="How are refund requests handled?",
            answer=_answer(text="A human reviews refund requests.", references=references),
            hits=(
                RetrievalHit(document=first, score=1.0),
                RetrievalHit(document=second, score=0.9),
            ),
        )
    )

    assert decision.supported is False
    assert decision.reason == "unsupported_claim"


@pytest.mark.parametrize(
    ("scores", "reason"),
    [([math.nan], "semantic_scorer_failure"), ([1.1], "semantic_scorer_failure")],
)
def test_semantic_critic_fails_closed_on_invalid_scores(scores, reason) -> None:
    document = _document()
    decision = asyncio.run(
        _critic(relevance=_Scorer(scores), entailment=_Scorer([0.9])).evaluate(
            principal=_principal(),
            query="How are refunds handled?",
            answer=_answer(
                text=document.content,
                references=frozenset({evidence_reference(document)}),
            ),
            hits=(RetrievalHit(document=document, score=1.0),),
        )
    )

    assert decision.supported is False
    assert decision.reason == reason


def test_semantic_critic_fails_closed_on_scorer_exception() -> None:
    document = _document()
    decision = asyncio.run(
        _critic(relevance=_Scorer(RuntimeError("offline")), entailment=_Scorer([0.9])).evaluate(
            principal=_principal(),
            query="How are refunds handled?",
            answer=_answer(
                text=document.content,
                references=frozenset({evidence_reference(document)}),
            ),
            hits=(RetrievalHit(document=document, score=1.0),),
        )
    )

    assert decision.supported is False
    assert decision.reason == "semantic_scorer_failure"


def test_semantic_critic_rejects_unknown_reference_without_model_calls() -> None:
    document = _document()
    relevance = _Scorer([0.9])
    entailment = _Scorer([0.9])
    forged = evidence_reference(document).model_copy(update={"version": "v2"})

    decision = asyncio.run(
        _critic(relevance=relevance, entailment=entailment).evaluate(
            principal=_principal(),
            query="How are refunds handled?",
            answer=_answer(text=document.content, references=frozenset({forged})),
            hits=(RetrievalHit(document=document, score=1.0),),
        )
    )

    assert decision.reason == "unknown_evidence_reference"
    assert relevance.calls == []
    assert entailment.calls == []


def test_semantic_critic_rejects_cross_tenant_evidence_without_model_calls() -> None:
    foreign = _document(tenant_id="tenant-b")
    relevance = _Scorer([0.9])
    entailment = _Scorer([0.9])

    decision = asyncio.run(
        _critic(relevance=relevance, entailment=entailment).evaluate(
            principal=_principal(),
            query="How are refunds handled?",
            answer=_answer(
                text=foreign.content,
                references=frozenset({evidence_reference(foreign)}),
            ),
            hits=(RetrievalHit(document=foreign, score=1.0),),
        )
    )

    assert decision.reason == "unsafe_evidence_scope"
    assert relevance.calls == []
    assert entailment.calls == []


def test_semantic_critic_requires_explicit_valid_thresholds() -> None:
    scorer = _Scorer([1.0])
    for value in (-0.1, 1.1, math.inf, math.nan):
        with pytest.raises(ValueError, match="between 0 and 1"):
            _critic(relevance=scorer, entailment=scorer, minimum_relevance=value)
