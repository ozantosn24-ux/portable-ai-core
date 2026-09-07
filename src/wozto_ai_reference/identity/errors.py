"""Fail-closed error taxonomy for the identity subpackage.

Every failure carries a short machine `reason` code and nothing else. That is not
tidiness: a reason code crosses two boundaries where free text is dangerous — it is
written into the authorization ledger, and it is returned to the caller in an HTTP body.
An exception message built from the thing that failed would sooner or later carry an
authorization code, an ID token, a claim value or a client secret out with it.

The pattern is the same one `domain.DecisionReasonCode` enforces on policy decisions, so
a reason from this layer and a reason from the query layer read alike in a log.

Callers may attach a `detail` for a *local* log line; it never reaches an HTTP response
body, because the route handlers read `.reason`, not `str(exc)`.
"""

from __future__ import annotations

import re

_REASON_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class IdentityError(RuntimeError):
    """Base class. `reason` is a machine code; it must never carry request material."""

    def __init__(self, reason: str, *, detail: str = "") -> None:
        if not _REASON_PATTERN.fullmatch(reason):
            raise ValueError(f"identity reason must match {_REASON_PATTERN.pattern!r}")
        super().__init__(detail or reason)
        self.reason = reason


class IdentityDependencyMissing(IdentityError):
    """An optional `auth` extra dependency is not installed."""


class OidcConfigurationError(IdentityError):
    """The relying party itself is misconfigured; refuse before contacting the IdP."""


class OidcDiscoveryError(IdentityError):
    """Provider metadata or JWKS could not be fetched, or contradicted the configuration."""


class TokenEndpointError(IdentityError):
    """The token endpoint refused the exchange, or answered something unusable."""


class InvalidIdToken(IdentityError):
    """Signature, claim, clock or nonce validation refused the ID token."""


class AuthorizationFlowError(IdentityError):
    """The redirect back from the IdP does not match the login this session started."""


class SessionError(IdentityError):
    """A session or CSRF precondition failed."""
