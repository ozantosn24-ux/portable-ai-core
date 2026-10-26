"""Offline gates for scope resolution, frozen-evidence answer criticism and adversarial fail-closed checks."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .adapters import (
    ConfiguredPhraseScopeResolver,
    DenyPhraseQueryPolicy,
    ExactEvidenceSupportCritic,
    ExactStructuredClaimSupportCritic,
    InMemorySearchProvider,
    MemoryTelemetry,
    PhraseScopeRule,
)
from .domain import (
    Document,
    EvidenceReference,
    ModelOutput,
    NonEmptyText,
    Principal,
    QueryConstraints,
    RetrievalHit,
    StructuredAnswer,
)
from .pairwise_scorer import TransformersTextPairScorer
from .ports import EvidenceSupportCritic, QueryScopeResolver
from .semantic_critic import SemanticStructuredClaimSupportCritic
from .service import QueryService

SCOPE_EVAL_SCHEMA = "wozto-scope-eval/v1"
CRITIC_EVAL_SCHEMA = "wozto-evidence-critic-eval/v1"
STRUCTURED_CRITIC_EVAL_SCHEMA = "wozto-structured-claim-critic-eval/v1"
ADVERSARIAL_EVAL_SCHEMA = "wozto-adversarial-eval/v1"

AdversarialCategory = Literal[
    "embedded_instruction",
    "cross_tenant_reference",
    "fabricated_citation",
    "retrieval_failure",
    "repeated_search",
    "unsupported_or_ambiguous",
    "unauthorized_action",
    "control",
]
# `control` is the non-adversarial positive control (it must be ANSWERED); the gate
# requires at least one case in every other category.
ADVERSARIAL_CATEGORIES: tuple[str, ...] = tuple(c for c in get_args(AdversarialCategory) if c != "control")


class ScopeEvalCase(BaseModel):
    """Frozen query and expected resolver outcome; no retrieval is performed."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    case_id: NonEmptyText
    principal: Principal
    query: NonEmptyText
    expected_allowed: bool = True
    expected_constraints: QueryConstraints = Field(default_factory=QueryConstraints)

    @model_validator(mode="after")
    def refused_case_has_no_constraints(self) -> ScopeEvalCase:
        if not self.expected_allowed and self.expected_constraints.constrained:
            raise ValueError("a refused scope case must not expect constraints")
        return self


class ScopeEvalCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    exact_match: bool
    false_allow: bool
    false_refusal: bool
    constraint_mismatch: bool


class ScopeEvalReport(BaseModel):
    cases: int
    exact_matches: int
    accuracy: float = Field(ge=0.0, le=1.0)
    false_allows: int = Field(ge=0)
    false_refusals: int = Field(ge=0)
    constraint_mismatches: int = Field(ge=0)
    duplicate_case_ids: int = Field(ge=0)

    def passes(self, *, minimum_accuracy: float = 1.0) -> bool:
        if not 0.0 <= minimum_accuracy <= 1.0:
            raise ValueError("minimum_accuracy must be between 0 and 1")
        return (
            self.cases > 0
            and self.accuracy >= minimum_accuracy
            and self.false_allows == 0
            and self.false_refusals == 0
            and self.constraint_mismatches == 0
            and self.duplicate_case_ids == 0
        )


class CriticEvalCase(BaseModel):
    """Frozen evidence and candidate answer; the evaluator never retrieves."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    case_id: NonEmptyText
    principal: Principal
    query: NonEmptyText
    answer: NonEmptyText
    hits: tuple[RetrievalHit, ...]
    expected_supported: bool
    expected_supporting_evidence: frozenset[EvidenceReference] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def validate_expected_support(self) -> CriticEvalCase:
        if self.expected_supported and not self.expected_supporting_evidence:
            raise ValueError("a supported critic case must identify supporting evidence")
        if not self.expected_supported and self.expected_supporting_evidence:
            raise ValueError("an unsupported critic case must not identify supporting evidence")
        return self


class StructuredCriticEvalCase(BaseModel):
    """Frozen evidence and structured candidate; the evaluator never retrieves."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    case_id: NonEmptyText
    principal: Principal
    query: NonEmptyText
    answer: StructuredAnswer
    hits: tuple[RetrievalHit, ...]
    expected_supported: bool
    expected_supporting_evidence: frozenset[EvidenceReference] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def validate_expected_support(self) -> StructuredCriticEvalCase:
        if self.expected_supported and not self.expected_supporting_evidence:
            raise ValueError("a supported structured critic case must identify supporting evidence")
        if not self.expected_supported and self.expected_supporting_evidence:
            raise ValueError("an unsupported structured critic case must not identify supporting evidence")
        return self


class CriticEvalCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    exact_match: bool
    false_accept: bool
    false_reject: bool
    support_mismatch: bool


class CriticEvalReport(BaseModel):
    cases: int
    exact_matches: int
    accuracy: float = Field(ge=0.0, le=1.0)
    false_accepts: int = Field(ge=0)
    false_rejects: int = Field(ge=0)
    support_mismatches: int = Field(ge=0)
    duplicate_case_ids: int = Field(ge=0)

    def passes(self, *, minimum_accuracy: float = 1.0) -> bool:
        if not 0.0 <= minimum_accuracy <= 1.0:
            raise ValueError("minimum_accuracy must be between 0 and 1")
        return (
            self.cases > 0
            and self.accuracy >= minimum_accuracy
            and self.false_accepts == 0
            and self.false_rejects == 0
            and self.support_mismatches == 0
            and self.duplicate_case_ids == 0
        )


def _duplicate_count(case_ids: Sequence[str]) -> int:
    seen: set[str] = set()
    duplicates = 0
    for case_id in case_ids:
        if case_id in seen:
            duplicates += 1
        seen.add(case_id)
    return duplicates


async def evaluate_scope_resolver(
    *,
    resolver: QueryScopeResolver,
    cases: Sequence[ScopeEvalCase],
) -> tuple[ScopeEvalReport, tuple[ScopeEvalCaseResult, ...]]:
    if not cases:
        raise ValueError("scope evaluation requires at least one case")
    results: list[ScopeEvalCaseResult] = []
    for case in cases:
        actual = await resolver.resolve(principal=case.principal, query=case.query)
        constraint_mismatch = (
            actual.allowed and case.expected_allowed and actual.constraints != case.expected_constraints
        )
        exact_match = actual.allowed == case.expected_allowed and not constraint_mismatch
        results.append(
            ScopeEvalCaseResult(
                case_id=case.case_id,
                exact_match=exact_match,
                false_allow=actual.allowed and not case.expected_allowed,
                false_refusal=not actual.allowed and case.expected_allowed,
                constraint_mismatch=constraint_mismatch,
            )
        )
    frozen_results = tuple(results)
    exact_matches = sum(result.exact_match for result in frozen_results)
    report = ScopeEvalReport(
        cases=len(frozen_results),
        exact_matches=exact_matches,
        accuracy=exact_matches / len(frozen_results),
        false_allows=sum(result.false_allow for result in frozen_results),
        false_refusals=sum(result.false_refusal for result in frozen_results),
        constraint_mismatches=sum(result.constraint_mismatch for result in frozen_results),
        duplicate_case_ids=_duplicate_count([result.case_id for result in frozen_results]),
    )
    return report, frozen_results


async def evaluate_evidence_critic(
    *,
    critic: EvidenceSupportCritic,
    cases: Sequence[CriticEvalCase],
) -> tuple[CriticEvalReport, tuple[CriticEvalCaseResult, ...]]:
    if not cases:
        raise ValueError("critic evaluation requires at least one case")
    results: list[CriticEvalCaseResult] = []
    for case in cases:
        actual = await critic.evaluate(
            principal=case.principal,
            query=case.query,
            answer=case.answer,
            hits=case.hits,
        )
        support_mismatch = (
            actual.supported
            and case.expected_supported
            and actual.supporting_evidence != case.expected_supporting_evidence
        )
        exact_match = actual.supported == case.expected_supported and not support_mismatch
        results.append(
            CriticEvalCaseResult(
                case_id=case.case_id,
                exact_match=exact_match,
                false_accept=actual.supported and not case.expected_supported,
                false_reject=not actual.supported and case.expected_supported,
                support_mismatch=support_mismatch,
            )
        )
    frozen_results = tuple(results)
    exact_matches = sum(result.exact_match for result in frozen_results)
    report = CriticEvalReport(
        cases=len(frozen_results),
        exact_matches=exact_matches,
        accuracy=exact_matches / len(frozen_results),
        false_accepts=sum(result.false_accept for result in frozen_results),
        false_rejects=sum(result.false_reject for result in frozen_results),
        support_mismatches=sum(result.support_mismatch for result in frozen_results),
        duplicate_case_ids=_duplicate_count([result.case_id for result in frozen_results]),
    )
    return report, frozen_results


async def evaluate_structured_claim_critic(
    *,
    critic: EvidenceSupportCritic,
    cases: Sequence[StructuredCriticEvalCase],
) -> tuple[CriticEvalReport, tuple[CriticEvalCaseResult, ...]]:
    """Evaluate structured generation against frozen hits, independent of retrieval."""

    if not cases:
        raise ValueError("structured critic evaluation requires at least one case")
    results: list[CriticEvalCaseResult] = []
    for case in cases:
        actual = await critic.evaluate(
            principal=case.principal,
            query=case.query,
            answer=case.answer,
            hits=case.hits,
        )
        support_mismatch = (
            actual.supported
            and case.expected_supported
            and actual.supporting_evidence != case.expected_supporting_evidence
        )
        exact_match = actual.supported == case.expected_supported and not support_mismatch
        results.append(
            CriticEvalCaseResult(
                case_id=case.case_id,
                exact_match=exact_match,
                false_accept=actual.supported and not case.expected_supported,
                false_reject=not actual.supported and case.expected_supported,
                support_mismatch=support_mismatch,
            )
        )
    frozen_results = tuple(results)
    exact_matches = sum(result.exact_match for result in frozen_results)
    report = CriticEvalReport(
        cases=len(frozen_results),
        exact_matches=exact_matches,
        accuracy=exact_matches / len(frozen_results),
        false_accepts=sum(result.false_accept for result in frozen_results),
        false_rejects=sum(result.false_reject for result in frozen_results),
        support_mismatches=sum(result.support_mismatch for result in frozen_results),
        duplicate_case_ids=_duplicate_count([result.case_id for result in frozen_results]),
    )
    return report, frozen_results


class AdversarialCase(BaseModel):
    """Hostile or degenerate request run end to end through `QueryService`.

    Everything the scenario needs is data: which synthetic documents exist, how the
    search stub misbehaves, what a (possibly hijacked) model would answer. Nothing is
    retrieved from outside the file.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")

    case_id: NonEmptyText
    category: AdversarialCategory
    principal: Principal
    query: NonEmptyText
    document_ids: tuple[str, ...] = ()
    # normal = tenant/ACL-filtering lexical search; leaky = returns every selected
    # document ignoring the principal (a broken adapter); timeout = raises; empty = [].
    search: Literal["normal", "leaky", "timeout", "empty"] = "normal"
    # What the model would say. None = the model must never be reached.
    model_answer: str | StructuredAnswer | None = None
    critic: Literal["none", "exact", "structured"] = "none"
    use_policy: bool = False
    use_scope_rules: bool = False
    as_of: date | None = None
    minimum_score: float = Field(default=0.01, ge=0.0)
    max_search_calls: int = Field(default=1, ge=1)
    # abstain = service returns an abstention; reject = service raises (only for failures
    # the service cannot absorb, e.g. a search timeout); answer = positive control.
    # An exception where an abstention is expected is NOT fail-closed credit: it may be a
    # harness bug that kept the case from ever reaching the service.
    expected: Literal["abstain", "reject", "answer"] = "abstain"
    expected_error: str | None = None
    # What the case actually exercises. "provider_or_harness" cases stay green even with
    # every service protection removed (they measure the scripted provider / the harness,
    # not a service guard); they are kept as smoke cases and are NOT counted as proof of a
    # protection. The mutation sweep in tests/test_quality_evaluation.py enforces the label.
    measures: Literal["service", "provider_or_harness"] = "service"

    @model_validator(mode="after")
    def validate_expectation(self) -> AdversarialCase:
        if (self.category == "control") != (self.expected == "answer"):
            raise ValueError("exactly the control category expects an answer")
        if self.expected == "answer" and self.model_answer is None:
            raise ValueError("a control case must script the model answer")
        if (self.expected == "reject") != (self.expected_error is not None):
            raise ValueError("expected_error is required for, and only for, reject cases")
        return self


@dataclass(frozen=True)
class AdversarialSuite:
    documents: tuple[Document, ...]
    cases: tuple[AdversarialCase, ...]
    deny_phrases: tuple[str, ...] = ()
    scope_rules: tuple[PhraseScopeRule, ...] = ()


class AdversarialCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    category: str
    outcome: Literal["answered", "abstained", "rejected"]
    search_calls: int
    model_calls: int
    false_allow: bool
    false_refusal: bool
    error_type: str | None = None
    unexpected_rejection: bool = False
    unexpected_abstention: bool = False


# A category with fewer cases than this is reported as uncovered and fails the gate.
MINIMUM_CASES_PER_CATEGORY = 2


class AdversarialReport(BaseModel):
    cases: int
    adversarial_cases: int
    control_cases: int
    cases_by_category: dict[str, int]
    false_allows: int = Field(ge=0)
    false_allows_by_category: dict[str, int]
    false_refusals: int = Field(ge=0)
    # Exception where an abstention was expected (or a different exception than the
    # declared one): the case did not test what it claims, so the gate is red.
    unexpected_rejections: int = Field(default=0, ge=0)
    # Abstention where the case declared a rejection.
    unexpected_abstentions: int = Field(default=0, ge=0)
    uncovered_categories: list[str]
    duplicate_case_ids: int = Field(ge=0)

    def passes(self) -> bool:
        # No tolerance knob on purpose: a single false allow is a security failure, and a
        # suite that cannot answer its own control (false refusal), that skips a
        # category or whose cases do not end the way they declare proves nothing about
        # fail-closed behavior.
        return (
            self.adversarial_cases > 0
            and self.false_allows == 0
            and self.false_refusals == 0
            and self.unexpected_rejections == 0
            and self.unexpected_abstentions == 0
            and not self.uncovered_categories
            and self.duplicate_case_ids == 0
        )


class _ScriptedSearch:
    def __init__(self, documents: Sequence[Document], mode: str) -> None:
        self._documents = tuple(documents)
        self._mode = mode
        self.calls = 0

    async def search(self, *, principal: Principal, query: str, limit: int) -> Sequence[RetrievalHit]:
        self.calls += 1
        if self._mode == "timeout":
            raise TimeoutError("search timed out")
        if self._mode == "empty":
            return []
        if self._mode == "leaky":
            return [RetrievalHit(document=document, score=1.0) for document in self._documents][:limit]
        return await InMemorySearchProvider(self._documents).search(principal=principal, query=query, limit=limit)


class _ScriptedModel:
    def __init__(self, answer: ModelOutput | None) -> None:
        self._answer = answer
        self.calls = 0

    async def generate(self, *, query: str, hits: Sequence[RetrievalHit], trace_id: str) -> ModelOutput:
        del query, hits, trace_id
        self.calls += 1
        return "unscripted model output" if self._answer is None else self._answer


async def evaluate_adversarial_suite(
    suite: AdversarialSuite,
) -> tuple[AdversarialReport, tuple[AdversarialCaseResult, ...]]:
    """Run each case through the real `QueryService`; count answers that must not exist.

    `false_allow`: a fail-closed case released an answer, reached the model with no
    scripted answer, or searched more often than allowed. An exception is `rejected`;
    it is accepted only for cases that declare `expected: reject` with the matching
    `expected_error`, otherwise it is an `unexpected_rejection` (a harness bug would
    otherwise look like fail-closed behavior).
    """

    if not suite.cases:
        raise ValueError("adversarial evaluation requires at least one case")
    pool = {document.document_id: document for document in suite.documents}
    results: list[AdversarialCaseResult] = []
    for case in suite.cases:
        unknown = [document_id for document_id in case.document_ids if document_id not in pool]
        if unknown:
            raise ValueError(f"case {case.case_id} references unknown documents: {unknown}")
        search = _ScriptedSearch([pool[document_id] for document_id in case.document_ids], case.search)
        model = _ScriptedModel(case.model_answer)
        critic: EvidenceSupportCritic | None = {
            "none": None,
            "exact": ExactEvidenceSupportCritic(),
            "structured": ExactStructuredClaimSupportCritic(),
        }[case.critic]
        service = QueryService(
            search=search,
            model=model,
            telemetry=MemoryTelemetry(),
            minimum_score=case.minimum_score,
            query_policy=DenyPhraseQueryPolicy(suite.deny_phrases) if case.use_policy else None,
            scope_resolver=ConfiguredPhraseScopeResolver(suite.scope_rules) if case.use_scope_rules else None,
            evidence_critic=critic,
        )
        error_type: str | None = None
        try:
            result = await service.query(principal=case.principal, query=case.query, as_of=case.as_of)
            outcome = "abstained" if result.abstained else "answered"
        except Exception as exc:
            outcome = "rejected"
            error_type = type(exc).__name__
        fail_closed = case.expected != "answer"
        false_allow = fail_closed and (
            outcome == "answered"
            or (case.model_answer is None and model.calls > 0)
            or search.calls > case.max_search_calls
        )
        results.append(
            AdversarialCaseResult(
                case_id=case.case_id,
                category=case.category,
                outcome=outcome,
                search_calls=search.calls,
                model_calls=model.calls,
                false_allow=false_allow,
                false_refusal=not fail_closed and outcome != "answered",
                error_type=error_type,
                unexpected_rejection=outcome == "rejected"
                and (case.expected == "abstain" or error_type != case.expected_error),
                unexpected_abstention=case.expected == "reject" and outcome == "abstained",
            )
        )
    frozen = tuple(results)
    adversarial = [r for r in frozen if r.category != "control"]
    report = AdversarialReport(
        cases=len(frozen),
        adversarial_cases=len(adversarial),
        control_cases=len(frozen) - len(adversarial),
        cases_by_category={c: sum(r.category == c for r in frozen) for c in (*ADVERSARIAL_CATEGORIES, "control")},
        false_allows=sum(r.false_allow for r in frozen),
        false_allows_by_category={
            c: sum(r.false_allow for r in frozen if r.category == c) for c in ADVERSARIAL_CATEGORIES
        },
        false_refusals=sum(r.false_refusal for r in frozen),
        unexpected_rejections=sum(r.unexpected_rejection for r in frozen),
        unexpected_abstentions=sum(r.unexpected_abstention for r in frozen),
        uncovered_categories=[
            c
            for c in ADVERSARIAL_CATEGORIES
            if sum(r.category == c for r in adversarial) < MINIMUM_CASES_PER_CATEGORY
        ],
        duplicate_case_ids=_duplicate_count([r.case_id for r in frozen]),
    )
    return report, frozen


def _load_root(path: Path, *, schema: str) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != schema:
        raise ValueError(f"expected schema_version={schema}")
    return raw


def _records(raw: dict[str, object], *, field_name: str) -> list[dict[str, object]]:
    value = raw.get(field_name)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} must be a non-empty list")
    if any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{field_name} entries must be objects")
    return value


def load_scope_cases(path: Path) -> tuple[ScopeEvalCase, ...]:
    raw = _load_root(path, schema=SCOPE_EVAL_SCHEMA)
    return tuple(ScopeEvalCase.model_validate(case) for case in _records(raw, field_name="cases"))


def load_scope_rules(path: Path) -> tuple[PhraseScopeRule, ...]:
    raw = _load_root(path, schema=SCOPE_EVAL_SCHEMA)
    rules: list[PhraseScopeRule] = []
    for rule in _records(raw, field_name="rules"):
        phrase = rule.get("phrase")
        if not isinstance(phrase, str):
            raise ValueError("scope rule phrase must be a string")
        rules.append(
            PhraseScopeRule(
                phrase=phrase,
                constraints=QueryConstraints.model_validate(rule.get("constraints", {})),
            )
        )
    return tuple(rules)


def load_critic_cases(path: Path) -> tuple[CriticEvalCase, ...]:
    raw = _load_root(path, schema=CRITIC_EVAL_SCHEMA)
    return tuple(CriticEvalCase.model_validate(case) for case in _records(raw, field_name="cases"))


def load_structured_critic_cases(path: Path) -> tuple[StructuredCriticEvalCase, ...]:
    raw = _load_root(path, schema=STRUCTURED_CRITIC_EVAL_SCHEMA)
    return tuple(StructuredCriticEvalCase.model_validate(case) for case in _records(raw, field_name="cases"))


def load_adversarial_suite(path: Path) -> AdversarialSuite:
    raw = _load_root(path, schema=ADVERSARIAL_EVAL_SCHEMA)
    rules = raw.get("scope_rules", [])
    if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
        raise ValueError("scope_rules entries must be objects")
    return AdversarialSuite(
        documents=tuple(Document.model_validate(d) for d in _records(raw, field_name="documents")),
        deny_phrases=tuple(raw.get("deny_phrases", [])),
        scope_rules=tuple(
            PhraseScopeRule(
                phrase=str(rule.get("phrase")),
                constraints=QueryConstraints.model_validate(rule.get("constraints", {})),
            )
            for rule in rules
        ),
        cases=tuple(AdversarialCase.model_validate(c) for c in _records(raw, field_name="cases")),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimum-accuracy", type=float, default=1.0)
    subparsers = parser.add_subparsers(dest="kind", required=True)

    scope = subparsers.add_parser("scope")
    scope.add_argument("--rules", required=True, type=Path)
    scope.add_argument("--cases", required=True, type=Path)

    critic = subparsers.add_parser("critic")
    critic.add_argument("--cases", required=True, type=Path)
    critic.add_argument("--allowed-prefix", action="append", default=[])

    structured_critic = subparsers.add_parser("structured-critic")
    structured_critic.add_argument("--cases", required=True, type=Path)

    adversarial = subparsers.add_parser("adversarial")
    adversarial.add_argument("--cases", required=True, type=Path)

    semantic_critic = subparsers.add_parser("semantic-structured-critic")
    semantic_critic.add_argument("--cases", required=True, type=Path)
    semantic_critic.add_argument("--relevance-model", required=True)
    semantic_critic.add_argument("--relevance-revision", required=True)
    semantic_critic.add_argument("--entailment-model", required=True)
    semantic_critic.add_argument("--entailment-revision", required=True)
    semantic_critic.add_argument("--entailment-positive-label-index", required=True, type=int)
    semantic_critic.add_argument("--minimum-relevance", required=True, type=float)
    semantic_critic.add_argument("--minimum-entailment", required=True, type=float)
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.kind == "adversarial":
        # --minimum-accuracy is deliberately ignored: any false allow fails.
        adversarial_report, _ = await evaluate_adversarial_suite(load_adversarial_suite(args.cases))
        passed = adversarial_report.passes()
        print(json.dumps({"passed": passed, **adversarial_report.model_dump()}, ensure_ascii=False, sort_keys=True))
        return 0 if passed else 2
    if args.kind == "scope":
        report, _ = await evaluate_scope_resolver(
            resolver=ConfiguredPhraseScopeResolver(load_scope_rules(args.rules)),
            cases=load_scope_cases(args.cases),
        )
    elif args.kind == "critic":
        report, _ = await evaluate_evidence_critic(
            critic=ExactEvidenceSupportCritic(allowed_prefixes=args.allowed_prefix),
            cases=load_critic_cases(args.cases),
        )
    elif args.kind == "structured-critic":
        report, _ = await evaluate_structured_claim_critic(
            critic=ExactStructuredClaimSupportCritic(),
            cases=load_structured_critic_cases(args.cases),
        )
    else:
        report, _ = await evaluate_structured_claim_critic(
            critic=SemanticStructuredClaimSupportCritic(
                relevance_scorer=TransformersTextPairScorer(
                    model_name=args.relevance_model,
                    revision=args.relevance_revision,
                    positive_label_index=None,
                ),
                entailment_scorer=TransformersTextPairScorer(
                    model_name=args.entailment_model,
                    revision=args.entailment_revision,
                    positive_label_index=args.entailment_positive_label_index,
                ),
                minimum_relevance=args.minimum_relevance,
                minimum_entailment=args.minimum_entailment,
            ),
            cases=load_structured_critic_cases(args.cases),
        )
    passed = report.passes(minimum_accuracy=args.minimum_accuracy)
    print(
        json.dumps(
            {"passed": passed, **report.model_dump()},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if passed else 2


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
