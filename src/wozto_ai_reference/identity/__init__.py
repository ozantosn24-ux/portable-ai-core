"""OIDC login (authorization code + PKCE) and resource-level authorization.

Importing this package never imports `httpx` or `joserfc`. Both are pulled in lazily, at
the point of use, so the core and its tests run on the base dependency set alone —
`tests/test_identity_optional_dependency.py` proves it by blanking them out of
`sys.modules`. Install them with:

    pip install -e ".[auth]"

Three boundaries live here, and they are separate on purpose:

* **`oidc`** — "who is this?" Answered only by a signature-verified ID token whose `iss`,
  `aud`, `exp`/`iat`/`nbf`, and `nonce` all check out against an injectable clock.
* **`session`** — "is this browser still that person?" Answered by a server-side record
  keyed by an opaque, signed cookie. Login rotates the id; logout deletes the record.
* **`authz`** — "may this person do this to this mailbox?" Answered from a grant table,
  never from request input, and written to an append-only ledger either way.
"""

from .authz import (
    ROLE_ACTIONS,
    Decision,
    DecisionLedger,
    DecisionRecord,
    GrantTable,
    InMemoryDecisionLedger,
    InMemoryGrantTable,
    JsonlDecisionLedger,
    MailboxAction,
    MailboxGrant,
    MailboxResource,
    MailboxRole,
    authorize,
    grants_from_mapping,
)
from .errors import (
    AuthorizationFlowError,
    IdentityDependencyMissing,
    IdentityError,
    InvalidIdToken,
    OidcConfigurationError,
    OidcDiscoveryError,
    SessionError,
    TokenEndpointError,
)
from .oidc import (
    AuthorizationRequest,
    OidcConfig,
    OidcIdentityProvider,
    ProviderMetadata,
    code_challenge_s256,
)
from .session import (
    DEFAULT_COOKIE_NAME,
    InMemorySessionStore,
    PendingLogin,
    SessionManager,
    SessionRecord,
    SessionStore,
    SignedCookieCodec,
)
from .web import IdentityWeb, build_identity_router

__all__ = [
    "DEFAULT_COOKIE_NAME",
    "ROLE_ACTIONS",
    "AuthorizationFlowError",
    "AuthorizationRequest",
    "Decision",
    "DecisionLedger",
    "DecisionRecord",
    "GrantTable",
    "IdentityDependencyMissing",
    "IdentityError",
    "IdentityWeb",
    "InMemoryDecisionLedger",
    "InMemoryGrantTable",
    "InMemorySessionStore",
    "InvalidIdToken",
    "JsonlDecisionLedger",
    "MailboxAction",
    "MailboxGrant",
    "MailboxResource",
    "MailboxRole",
    "OidcConfig",
    "OidcConfigurationError",
    "OidcDiscoveryError",
    "OidcIdentityProvider",
    "PendingLogin",
    "ProviderMetadata",
    "SessionError",
    "SessionManager",
    "SessionRecord",
    "SessionStore",
    "SignedCookieCodec",
    "TokenEndpointError",
    "authorize",
    "build_identity_router",
    "code_challenge_s256",
    "grants_from_mapping",
]
