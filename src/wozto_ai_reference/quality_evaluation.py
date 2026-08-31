"""Offline gates for scope resolution and frozen-evidence answer criticism."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .adapters import (
    ConfiguredPhraseScopeResolver,
    ExactEvidenceSupportCritic,
    ExactStructuredClaimSupportCritic,
    PhraseScopeRule,
)
from .domain import (
    EvidenceReference,
    NonEmptyText,
    Principal,
    QueryConstraints,
    RetrievalHit,
    StructuredAnswer,
)
from .ports import EvidenceSupportCritic, QueryScopeResolver

SCOPE_EVAL_SCHEMA = "wozto-scope-eval/v1"
CRITIC_EVAL_SCHEMA = "wozto-evidence-critic-eval/v1"
STRUCTURED_CRITIC_EVAL_SCHEMA = "wozto-structured-claim-critic-eval/v1"


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
    return parser


async def _run(args: argparse.Namespace) -> int:
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
    else:
        report, _ = await evaluate_structured_claim_critic(
            critic=ExactStructuredClaimSupportCritic(),
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
