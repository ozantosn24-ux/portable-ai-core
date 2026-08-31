"""Deterministic local adapters used before any live cloud resource exists."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from unicodedata import combining, normalize

from .domain import (
    Document,
    EvidenceSupportDecision,
    Principal,
    QueryConstraints,
    QueryPolicyDecision,
    QueryScopeDecision,
    RetrievalHit,
    TelemetryEvent,
    evidence_reference,
)
from .ports import IdentityUnavailable

_TERM_PATTERN = re.compile(r"[\wçğıöşü]+", re.IGNORECASE)


def _terms(value: str) -> set[str]:
    return {term.casefold() for term in _TERM_PATTERN.findall(value)}


def _normalized_text(value: str) -> str:
    folded = normalize("NFKD", value.casefold())
    without_marks = "".join(character for character in folded if not combining(character))
    # Unicode casefold is locale-independent: Turkish I/i and I/ı therefore need
    # one explicit equivalence for operator rules to survive normal capitalization.
    return " ".join(without_marks.replace("ı", "i").split())


def _normalized_phrase(value: str) -> str:
    return " ".join(re.findall(r"\w+", _normalized_text(value), flags=re.UNICODE))


def is_authorized(principal: Principal, document: Document) -> bool:
    """Return whether the principal may retrieve the document."""

    if document.tenant_id != principal.tenant_id:
        return False
    return not document.acl_roles or bool(document.acl_roles.intersection(principal.roles))


class InMemorySearchProvider:
    """Small deterministic lexical adapter for tests and local architecture work."""

    def __init__(self, documents: Iterable[Document]) -> None:
        self._documents = tuple(documents)

    async def search(
        self,
        *,
        principal: Principal,
        query: str,
        limit: int,
    ) -> Sequence[RetrievalHit]:
        query_terms = _terms(query)
        if not query_terms:
            return []

        hits: list[RetrievalHit] = []
        for document in self._documents:
            if not is_authorized(principal, document):
                continue
            document_terms = _terms(f"{document.title} {document.section} {document.content}")
            overlap = len(query_terms.intersection(document_terms))
            if overlap == 0:
                continue
            score = overlap / len(query_terms)
            hits.append(RetrievalHit(document=document, score=score))

        hits.sort(key=lambda hit: (-hit.score, hit.document.document_id, hit.document.version))
        return hits[:limit]


class DeterministicGroundedModel:
    """Non-LLM adapter that makes authorization and citation behavior testable."""

    async def generate(
        self,
        *,
        query: str,
        hits: Sequence[RetrievalHit],
        trace_id: str,
    ) -> str:
        del query, trace_id
        snippets = " ".join(hit.document.content for hit in hits)
        return f"Yetkili kaynaklara göre: {snippets}"


class DenyPhraseQueryPolicy:
    """Explicit opt-in hard deny list; no query text is emitted to telemetry."""

    def __init__(self, phrases: Iterable[str], *, reason: str = "configured_hard_deny") -> None:
        normalized = frozenset(" ".join(phrase.casefold().split()) for phrase in phrases)
        if not normalized or any(not phrase for phrase in normalized):
            raise ValueError("deny phrases must contain at least one non-empty phrase")
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("policy reason must not be empty")
        self._phrases = normalized
        self._reason = QueryPolicyDecision(allowed=False, reason=clean_reason).reason

    def evaluate(self, *, principal: Principal, query: str) -> QueryPolicyDecision:
        del principal
        normalized_query = " ".join(query.casefold().split())
        denied = any(phrase in normalized_query for phrase in self._phrases)
        return QueryPolicyDecision(
            allowed=not denied,
            reason=self._reason if denied else "allowed",
        )


@dataclass(frozen=True)
class PhraseScopeRule:
    """Operator-reviewed phrase mapped to explicit evidence constraints."""

    phrase: str
    constraints: QueryConstraints
    _normalized_phrase: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        normalized_phrase = _normalized_phrase(self.phrase)
        if not normalized_phrase:
            raise ValueError("scope rule phrase must not be empty")
        if not self.constraints.constrained:
            raise ValueError("scope rule must add at least one constraint")
        object.__setattr__(self, "_normalized_phrase", normalized_phrase)


class ConfiguredPhraseScopeResolver:
    """Merge matching operator rules; conflicting matches fail closed."""

    def __init__(self, rules: Iterable[PhraseScopeRule]) -> None:
        self._rules = tuple(rules)
        if not self._rules:
            raise ValueError("scope resolver requires at least one rule")

    async def resolve(self, *, principal: Principal, query: str) -> QueryScopeDecision:
        del principal
        query_tokens = _normalized_phrase(query).split()
        matches = []
        for rule in self._rules:
            phrase_tokens = rule._normalized_phrase.split()
            if any(
                query_tokens[index : index + len(phrase_tokens)] == phrase_tokens
                for index in range(len(query_tokens) - len(phrase_tokens) + 1)
            ):
                matches.append(rule)
        if not matches:
            return QueryScopeDecision(allowed=True, reason="unconstrained")

        resolved: dict[str, object] = {}
        for field_name in ("as_of", "source_status", "source_authority"):
            values = {value for rule in matches if (value := getattr(rule.constraints, field_name)) is not None}
            if len(values) > 1:
                return QueryScopeDecision(allowed=False, reason="conflicting_scope_rules")
            if values:
                resolved[field_name] = values.pop()
        return QueryScopeDecision(
            allowed=True,
            reason="scope_resolved",
            constraints=QueryConstraints(**resolved),
        )


class ExactEvidenceSupportCritic:
    """Fail-closed extractive baseline; not a semantic entailment judge."""

    def __init__(self, *, allowed_prefixes: Iterable[str] = ()) -> None:
        prefixes = tuple(_normalized_text(prefix) for prefix in allowed_prefixes)
        if any(not prefix for prefix in prefixes):
            raise ValueError("allowed evidence prefixes must not be empty")
        self._allowed_prefixes = prefixes

    async def evaluate(
        self,
        *,
        principal: Principal,
        query: str,
        answer: str,
        hits: Sequence[RetrievalHit],
    ) -> EvidenceSupportDecision:
        del query
        if not hits:
            return EvidenceSupportDecision(supported=False, reason="no_evidence")
        if any(not is_authorized(principal, hit.document) for hit in hits):
            return EvidenceSupportDecision(supported=False, reason="unsafe_evidence_scope")

        candidate = _normalized_text(answer)
        for prefix in self._allowed_prefixes:
            if candidate == prefix:
                candidate = ""
                break
            if candidate.startswith(prefix + " "):
                candidate = candidate[len(prefix) :].strip()
                break
        if not candidate:
            return EvidenceSupportDecision(supported=False, reason="empty_answer")

        for hit in hits:
            if candidate == _normalized_text(hit.document.content):
                return EvidenceSupportDecision(
                    supported=True,
                    reason="exact_evidence_match",
                    supporting_evidence=frozenset({evidence_reference(hit.document)}),
                )

        combined = " ".join(_normalized_text(hit.document.content) for hit in hits)
        if candidate == combined:
            return EvidenceSupportDecision(
                supported=True,
                reason="exact_evidence_match",
                supporting_evidence=frozenset(evidence_reference(hit.document) for hit in hits),
            )
        return EvidenceSupportDecision(supported=False, reason="unsupported_answer")


class LocalHeaderIdentityProvider:
    """Explicitly enabled local-only identity adapter; never a production trust boundary."""

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled

    @property
    def ready(self) -> bool:
        return self._enabled

    async def resolve(
        self,
        *,
        tenant_id: str | None,
        user_id: str | None,
        roles: str | None,
    ) -> Principal:
        if not self._enabled:
            raise IdentityUnavailable("Local header identity is disabled")
        if not tenant_id or not user_id:
            raise ValueError("X-Tenant-ID and X-User-ID are required")
        parsed_roles = frozenset(role.strip() for role in (roles or "").split(",") if role.strip())
        return Principal(tenant_id=tenant_id, user_id=user_id, roles=parsed_roles)


class MemoryTelemetry:
    """Collect structured telemetry locally without exporting customer data."""

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []

    def record(self, event: TelemetryEvent) -> None:
        self.events.append(event)
