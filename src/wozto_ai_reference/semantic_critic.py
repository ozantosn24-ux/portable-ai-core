"""Fail-closed semantic support for structured, evidence-bound model claims."""

from __future__ import annotations

import math
from collections.abc import Sequence

from .adapters import is_authorized
from .domain import (
    EvidenceReference,
    EvidenceSupportDecision,
    ModelOutput,
    Principal,
    RetrievalHit,
    StructuredAnswer,
    evidence_reference,
)
from .ports import TextPairScorer


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _valid_threshold(value: float, *, name: str) -> float:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a finite number between 0 and 1")
    return value


async def _safe_scores(
    scorer: TextPairScorer,
    pairs: Sequence[tuple[str, str]],
) -> tuple[float, ...] | None:
    if not pairs:
        return ()
    try:
        raw_scores = await scorer.score_pairs(pairs)
        scores = tuple(raw_scores)
    except Exception:
        return None
    if len(scores) != len(pairs):
        return None
    if any(
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0.0 <= score <= 1.0
        for score in scores
    ):
        return None
    return tuple(float(score) for score in scores)


class SemanticStructuredClaimSupportCritic:
    """Require query relevance and evidence entailment for every structured claim.

    Thresholds have no defaults: they must come from a frozen, human-reviewed
    calibration set for the exact scorer revisions and corpus. Every cited evidence
    pair is judged; a substring alone is not treated as semantic proof.
    """

    def __init__(
        self,
        *,
        relevance_scorer: TextPairScorer,
        entailment_scorer: TextPairScorer,
        minimum_relevance: float,
        minimum_entailment: float,
    ) -> None:
        self._relevance_scorer = relevance_scorer
        self._entailment_scorer = entailment_scorer
        self._minimum_relevance = _valid_threshold(
            minimum_relevance,
            name="minimum_relevance",
        )
        self._minimum_entailment = _valid_threshold(
            minimum_entailment,
            name="minimum_entailment",
        )

    async def evaluate(
        self,
        *,
        principal: Principal,
        query: str,
        answer: ModelOutput,
        hits: Sequence[RetrievalHit],
    ) -> EvidenceSupportDecision:
        if not isinstance(answer, StructuredAnswer):
            return EvidenceSupportDecision(supported=False, reason="structured_answer_required")
        if not hits:
            return EvidenceSupportDecision(supported=False, reason="no_evidence")
        if any(not is_authorized(principal, hit.document) for hit in hits):
            return EvidenceSupportDecision(supported=False, reason="unsafe_evidence_scope")

        expected_answer = _normalized_text(" ".join(claim.text for claim in answer.claims))
        if _normalized_text(answer.answer) != expected_answer:
            return EvidenceSupportDecision(supported=False, reason="unclaimed_answer_text")

        evidence_by_reference = {evidence_reference(hit.document): hit.document for hit in hits}
        claim_evidence: list[tuple[str, EvidenceReference, str]] = []
        for claim in answer.claims:
            for reference in claim.supporting_evidence:
                document = evidence_by_reference.get(reference)
                if document is None:
                    return EvidenceSupportDecision(supported=False, reason="unknown_evidence_reference")
                claim_evidence.append((claim.text, reference, document.content))

        relevance_pairs = tuple((query, claim.text) for claim in answer.claims)
        relevance_scores = await _safe_scores(self._relevance_scorer, relevance_pairs)
        if relevance_scores is None:
            return EvidenceSupportDecision(supported=False, reason="semantic_scorer_failure")
        if any(score < self._minimum_relevance for score in relevance_scores):
            return EvidenceSupportDecision(supported=False, reason="query_irrelevant_claim")

        semantic_evidence = tuple((content, claim_text) for claim_text, _, content in claim_evidence)
        entailment_scores = await _safe_scores(self._entailment_scorer, semantic_evidence)
        if entailment_scores is None:
            return EvidenceSupportDecision(supported=False, reason="semantic_scorer_failure")
        if any(score < self._minimum_entailment for score in entailment_scores):
            return EvidenceSupportDecision(supported=False, reason="unsupported_claim")

        return EvidenceSupportDecision(
            supported=True,
            reason="semantic_claim_support",
            supporting_evidence=frozenset(reference for _, reference, _ in claim_evidence),
        )
