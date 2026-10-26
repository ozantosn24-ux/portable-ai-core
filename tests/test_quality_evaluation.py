import asyncio
import dataclasses
import json
from datetime import date
from pathlib import Path

import pytest

import wozto_ai_reference.service as service_module
from wozto_ai_reference.adapters import (
    ConfiguredPhraseScopeResolver,
    DenyPhraseQueryPolicy,
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
    QueryPolicyDecision,
    QueryScopeDecision,
    RetrievalHit,
    StructuredAnswer,
    StructuredClaim,
    evidence_reference,
)
from wozto_ai_reference.quality_evaluation import (
    ADVERSARIAL_CATEGORIES,
    CRITIC_EVAL_SCHEMA,
    SCOPE_EVAL_SCHEMA,
    STRUCTURED_CRITIC_EVAL_SCHEMA,
    AdversarialCase,
    CriticEvalCase,
    ScopeEvalCase,
    StructuredCriticEvalCase,
    _parser,
    _run,
    _ScriptedSearch,
    evaluate_adversarial_suite,
    evaluate_evidence_critic,
    evaluate_scope_resolver,
    evaluate_structured_claim_critic,
    load_adversarial_suite,
    load_critic_cases,
    load_scope_cases,
    load_scope_rules,
    load_structured_critic_cases,
)

ADVERSARIAL_FILE = Path(__file__).resolve().parents[1] / "sample-corpus" / "adversarial-eval.json"


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


def test_semantic_critic_cli_requires_explicit_models_revisions_and_thresholds() -> None:
    from wozto_ai_reference.quality_evaluation import _parser

    parser = _parser()
    args = parser.parse_args(
        [
            "semantic-structured-critic",
            "--cases",
            "cases.json",
            "--relevance-model",
            "example/relevance",
            "--relevance-revision",
            "rev-a",
            "--entailment-model",
            "example/nli",
            "--entailment-revision",
            "rev-b",
            "--entailment-positive-label-index",
            "0",
            "--minimum-relevance",
            "0.7",
            "--minimum-entailment",
            "0.8",
        ]
    )

    assert args.kind == "semantic-structured-critic"
    assert args.relevance_revision == "rev-a"
    assert args.entailment_revision == "rev-b"
    assert args.entailment_positive_label_index == 0
    assert args.minimum_relevance == 0.7
    assert args.minimum_entailment == 0.8


def _adversarial_report():
    report, _ = asyncio.run(evaluate_adversarial_suite(load_adversarial_suite(ADVERSARIAL_FILE)))
    return report


def test_adversarial_suite_is_fail_closed_and_covers_every_category() -> None:
    report = _adversarial_report()

    assert report.passes()
    assert report.false_allows == 0
    assert report.false_refusals == 0
    assert report.uncovered_categories == []
    assert set(report.false_allows_by_category) == set(ADVERSARIAL_CATEGORIES)
    # Not a vacuum: every category has >= 2 cases, and the positive controls prove the
    # harness can say "answer".
    assert all(report.cases_by_category[c] >= 2 for c in ADVERSARIAL_CATEGORIES)
    assert report.control_cases >= 2


def test_adversarial_cli_exit_code_follows_the_report() -> None:
    args = _parser().parse_args(["adversarial", "--cases", str(ADVERSARIAL_FILE)])

    assert asyncio.run(_run(args)) == 0


# Positive controls: remove ONE protection at a time; the gate must go red in the
# category that protection owns. Every category has at least one owner mutant, and the
# sweep below proves that every case labelled `measures: service` is turned red by SOME
# mutant, so a case that stays green without any service protection cannot hide.
async def _always_supported(self, *, principal, query, answer, hits):
    return EvidenceSupportDecision(
        supported=True,
        reason="mutated",
        supporting_evidence=frozenset(evidence_reference(hit.document) for hit in hits),
    )


def _mutate_acl_post_check(mp: pytest.MonkeyPatch) -> None:
    mp.setattr("wozto_ai_reference.service.is_authorized", lambda principal, document: True)


def _mutate_exact_critic(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(ExactEvidenceSupportCritic, "evaluate", _always_supported)


def _mutate_structured_critic(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(ExactStructuredClaimSupportCritic, "evaluate", _always_supported)


def _mutate_policy(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(
        DenyPhraseQueryPolicy,
        "evaluate",
        lambda self, *, principal, query: QueryPolicyDecision(allowed=True, reason="allowed"),
    )


def _mutate_source_eligibility(mp: pytest.MonkeyPatch) -> None:
    mp.setattr("wozto_ai_reference.service.source_is_eligible", lambda document, **kwargs: True)


def _mutate_score_gate(mp: pytest.MonkeyPatch) -> None:
    original = service_module.eligible_hits
    mp.setattr(
        service_module,
        "eligible_hits",
        lambda *, minimum_score, **kwargs: original(minimum_score=0.0, **kwargs),
    )


def _mutate_scope_resolver(mp: pytest.MonkeyPatch) -> None:
    async def allow_everything(self, *, principal, query):
        return QueryScopeDecision(allowed=True, reason="mutated")

    mp.setattr(ConfiguredPhraseScopeResolver, "resolve", allow_everything)


def _mutate_search_twice(mp: pytest.MonkeyPatch) -> None:
    original = service_module.QueryService.query

    async def searches_twice(self, *, principal, query, **kwargs):
        await self._search.search(principal=principal, query=query, limit=5)
        return await original(self, principal=principal, query=query, **kwargs)

    mp.setattr(service_module.QueryService, "query", searches_twice)


_MUTANTS = {
    "acl_post_check": (_mutate_acl_post_check, "cross_tenant_reference"),
    "exact_critic": (_mutate_exact_critic, "embedded_instruction"),
    "structured_critic": (_mutate_structured_critic, "fabricated_citation"),
    "policy": (_mutate_policy, "unauthorized_action"),
    "source_eligibility": (_mutate_source_eligibility, "retrieval_failure"),
    "score_gate": (_mutate_score_gate, "unsupported_or_ambiguous"),
    "scope_resolver": (_mutate_scope_resolver, "unsupported_or_ambiguous"),
    "search_twice": (_mutate_search_twice, "repeated_search"),
}


def _mutated_run(mutate):
    with pytest.MonkeyPatch.context() as mp:
        mutate(mp)
        return asyncio.run(evaluate_adversarial_suite(load_adversarial_suite(ADVERSARIAL_FILE)))


@pytest.mark.parametrize("name", sorted(_MUTANTS))
def test_adversarial_gate_goes_red_when_a_protection_is_removed(name: str) -> None:
    mutate, category = _MUTANTS[name]

    report, _ = _mutated_run(mutate)

    assert not report.passes()
    assert report.false_allows_by_category[category] >= 1


def test_every_adversarial_category_has_an_owner_mutant() -> None:
    assert {category for _, category in _MUTANTS.values()} == set(ADVERSARIAL_CATEGORIES)


def test_every_service_case_is_turned_red_by_some_mutant_and_the_rest_are_labelled() -> None:
    suite = load_adversarial_suite(ADVERSARIAL_FILE)
    turned_red: set[str] = set()
    for name, (mutate, _) in _MUTANTS.items():
        if name == "search_twice":
            # Orthogonal property (one search per query): it flips EVERY case that reaches
            # the search, so counting it would make every case look protected.
            continue
        _, results = _mutated_run(mutate)
        turned_red |= {result.case_id for result in results if result.false_allow}

    service_cases = {c.case_id for c in suite.cases if c.measures == "service" and c.category != "control"}
    harness_cases = {c.case_id for c in suite.cases if c.measures == "provider_or_harness"}

    assert service_cases - turned_red == set(), "a `service` case stays green with every protection removed"
    # The label is not decoration: a case marked provider_or_harness must really be
    # untouched by every mutant, otherwise it should be promoted to `service`.
    assert harness_cases & turned_red == set()
    assert harness_cases == {
        "tenant-foreign-chunk-id-in-query",
        "retrieval-search-timeout",
        "retrieval-empty-result",
        "unsupported-topic-not-in-corpus",
    }


def test_harness_failure_is_not_counted_as_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bug in the scripted leaky search used to turn into `rejected` (== "no answer")
    # and the two cross-tenant cases never reached the service while the gate stayed green.
    original = _ScriptedSearch.search

    async def leaky_mode_crashes(self, *, principal, query, limit):
        if self._mode == "leaky":
            raise RuntimeError("harness bug")
        return await original(self, principal=principal, query=query, limit=limit)

    monkeypatch.setattr(_ScriptedSearch, "search", leaky_mode_crashes)

    report, _ = asyncio.run(evaluate_adversarial_suite(load_adversarial_suite(ADVERSARIAL_FILE)))

    assert report.unexpected_rejections == 2
    assert report.false_allows == 0
    assert not report.passes()


def test_declared_rejection_must_really_be_a_rejection_of_the_declared_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _ScriptedSearch.search

    async def timeout_mode_quietly_empty(self, *, principal, query, limit):
        if self._mode == "timeout":
            return []
        return await original(self, principal=principal, query=query, limit=limit)

    monkeypatch.setattr(_ScriptedSearch, "search", timeout_mode_quietly_empty)
    report, _ = asyncio.run(evaluate_adversarial_suite(load_adversarial_suite(ADVERSARIAL_FILE)))
    assert report.unexpected_abstentions == 1
    assert not report.passes()

    async def timeout_mode_wrong_error(self, *, principal, query, limit):
        if self._mode == "timeout":
            raise RuntimeError("not a timeout")
        return await original(self, principal=principal, query=query, limit=limit)

    monkeypatch.setattr(_ScriptedSearch, "search", timeout_mode_wrong_error)
    report, _ = asyncio.run(evaluate_adversarial_suite(load_adversarial_suite(ADVERSARIAL_FILE)))
    assert report.unexpected_rejections == 1
    assert not report.passes()


def test_a_category_with_a_single_case_is_uncovered() -> None:
    suite = load_adversarial_suite(ADVERSARIAL_FILE)
    one_action = [c for c in suite.cases if c.category == "unauthorized_action"][0]
    thin = dataclasses.replace(
        suite,
        cases=tuple(c for c in suite.cases if c.category != "unauthorized_action") + (one_action,),
    )

    report, _ = asyncio.run(evaluate_adversarial_suite(thin))

    assert report.uncovered_categories == ["unauthorized_action"]
    assert not report.passes()


def test_adversarial_gate_rejects_missing_category_and_duplicate_ids() -> None:
    suite = load_adversarial_suite(ADVERSARIAL_FILE)
    without_actions = dataclasses.replace(
        suite, cases=tuple(c for c in suite.cases if c.category != "unauthorized_action")
    )
    duplicated = dataclasses.replace(suite, cases=(*suite.cases, suite.cases[0]))

    missing_report, _ = asyncio.run(evaluate_adversarial_suite(without_actions))
    duplicate_report, _ = asyncio.run(evaluate_adversarial_suite(duplicated))

    assert missing_report.uncovered_categories == ["unauthorized_action"]
    assert not missing_report.passes()
    assert duplicate_report.duplicate_case_ids == 1
    assert not duplicate_report.passes()


def test_adversarial_case_rejects_inconsistent_expectations() -> None:
    base = {
        "case_id": "x",
        "principal": {"tenant_id": "t", "user_id": "u"},
        "query": "q",
    }
    with pytest.raises(ValueError, match="control category"):
        AdversarialCase.model_validate({**base, "category": "control"})
    with pytest.raises(ValueError, match="control category"):
        AdversarialCase.model_validate({**base, "category": "retrieval_failure", "expected": "answer"})
    with pytest.raises(ValueError, match="script the model answer"):
        AdversarialCase.model_validate({**base, "category": "control", "expected": "answer"})


def test_adversarial_runner_refuses_unknown_document_reference() -> None:
    suite = load_adversarial_suite(ADVERSARIAL_FILE)
    bad_case = suite.cases[0].model_copy(update={"document_ids": ("does-not-exist",)})

    with pytest.raises(ValueError, match="unknown documents"):
        asyncio.run(evaluate_adversarial_suite(dataclasses.replace(suite, cases=(bad_case,))))
