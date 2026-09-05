"""Resilient multi-provider LLM gateway: retry -> circuit breaker -> failover.

Importing this package never imports a provider SDK. The Anthropic and OpenAI adapters
live under `providers/` and load their SDK when constructed, so the core and its tests
run with the base dependency set alone.
"""

from .errors import (
    AllProvidersUnavailable,
    AmbiguousOutcomeError,
    AmbiguousServerError,
    AmbiguousTimeoutError,
    AuthError,
    BadRequestError,
    ContentPolicyError,
    GatewayError,
    NonRetryableError,
    ProviderConnectionError,
    ProviderDependencyMissing,
    ProviderTimeoutError,
    RateLimitError,
    RetryableError,
    ServerError,
    UnclassifiedProviderError,
    reissue_allowed,
)
from .ledger import (
    AttemptLedger,
    AttemptRecord,
    InMemoryAttemptLedger,
    JsonlAttemptLedger,
)
from .policy import (
    SAVINGS_MODE_PROVIDER_ID,
    CircuitBreaker,
    RetryPolicy,
    SavingsMode,
    StaticTemplateSavingsMode,
)
from .ports import ChatProvider, StreamUsageReporter
from .router import FailoverRouter
from .types import (
    ChatMessage,
    ChatRequest,
    Completion,
    StreamEnd,
    StreamEvent,
    StreamRestarted,
    TextDelta,
    Usage,
)

__all__ = [
    "SAVINGS_MODE_PROVIDER_ID",
    "AllProvidersUnavailable",
    "AmbiguousOutcomeError",
    "AmbiguousServerError",
    "AmbiguousTimeoutError",
    "AttemptLedger",
    "AttemptRecord",
    "AuthError",
    "BadRequestError",
    "ChatMessage",
    "ChatProvider",
    "ChatRequest",
    "CircuitBreaker",
    "Completion",
    "ContentPolicyError",
    "FailoverRouter",
    "GatewayError",
    "InMemoryAttemptLedger",
    "JsonlAttemptLedger",
    "NonRetryableError",
    "ProviderConnectionError",
    "ProviderDependencyMissing",
    "ProviderTimeoutError",
    "RateLimitError",
    "RetryPolicy",
    "RetryableError",
    "SavingsMode",
    "ServerError",
    "StaticTemplateSavingsMode",
    "StreamEnd",
    "StreamEvent",
    "StreamRestarted",
    "StreamUsageReporter",
    "TextDelta",
    "UnclassifiedProviderError",
    "Usage",
    "reissue_allowed",
]
