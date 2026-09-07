"""Environment-driven wiring for the identity layer. Default: OFF.

Fail-closed in both directions. The switch off means the app is byte-for-byte the app it
was before this subpackage existed. The switch on with anything missing raises at
**startup**, not at the first login — a deployment that cannot authenticate should break
while it is being wired, not hours later when the first user arrives.

⛔ No value here has a default that could be guessed. There is no fallback issuer, no
generated session secret, no "localhost" redirect URI: every one of those would be a
working-looking misconfiguration.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .authz import DecisionLedger, GrantTable, JsonlDecisionLedger, grants_from_mapping
from .errors import OidcConfigurationError
from .oidc import OidcConfig, TokenEndpointAuthMethod
from .session import DEFAULT_COOKIE_NAME, InMemorySessionStore, SessionManager

ENABLED_ENV = "WOZTO_REFERENCE_OIDC_ENABLED"
ISSUER_ENV = "WOZTO_REFERENCE_OIDC_ISSUER"
CLIENT_ID_ENV = "WOZTO_REFERENCE_OIDC_CLIENT_ID"
CLIENT_SECRET_ENV = "WOZTO_REFERENCE_OIDC_CLIENT_SECRET"
CLIENT_SECRET_FILE_ENV = "WOZTO_REFERENCE_OIDC_CLIENT_SECRET_FILE"
REDIRECT_URI_ENV = "WOZTO_REFERENCE_OIDC_REDIRECT_URI"
SCOPES_ENV = "WOZTO_REFERENCE_OIDC_SCOPES"
ROLES_CLAIM_ENV = "WOZTO_REFERENCE_OIDC_ROLES_CLAIM"
TENANT_CLAIM_ENV = "WOZTO_REFERENCE_OIDC_TENANT_CLAIM"
TENANT_ID_ENV = "WOZTO_REFERENCE_OIDC_TENANT_ID"
AUTH_METHOD_ENV = "WOZTO_REFERENCE_OIDC_TOKEN_AUTH_METHOD"
SESSION_SECRET_ENV = "WOZTO_REFERENCE_SESSION_SECRET"
SESSION_SECRET_FILE_ENV = "WOZTO_REFERENCE_SESSION_SECRET_FILE"
SESSION_TTL_ENV = "WOZTO_REFERENCE_SESSION_TTL_SECONDS"
COOKIE_SECURE_ENV = "WOZTO_REFERENCE_SESSION_COOKIE_SECURE"
LEDGER_PATH_ENV = "WOZTO_REFERENCE_AUTHZ_LEDGER_PATH"
GRANTS_PATH_ENV = "WOZTO_REFERENCE_AUTHZ_GRANTS_PATH"

_VALID_AUTH_METHODS = ("none", "client_secret_basic", "client_secret_post")


@dataclass(frozen=True)
class IdentitySettings:
    """Everything `api.py` needs to mount the router, already validated."""

    oidc: OidcConfig
    session_secret: bytes
    session_ttl_seconds: float
    cookie_secure: bool
    grants: GrantTable
    ledger: DecisionLedger
    cookie_name: str = DEFAULT_COOKIE_NAME

    def session_manager(self) -> SessionManager:
        return SessionManager(
            store=InMemorySessionStore(),
            secret=self.session_secret,
            ttl_seconds=self.session_ttl_seconds,
            cookie_name=self.cookie_name,
            cookie_secure=self.cookie_secure,
        )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise OidcConfigurationError("missing_required_env", detail=f"{name} is required when {ENABLED_ENV}=1")
    return value


def _secret_value(inline_env: str, file_env: str) -> str | None:
    """Prefer the file form. A secret in a process environment leaks into `docker inspect`."""

    path = os.getenv(file_env, "").strip()
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    inline = os.getenv(inline_env, "").strip()
    return inline or None


def _flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise OidcConfigurationError("unparsable_boolean_env", detail=f"{name} must be a boolean-ish value")


def _positive_float(name: str, *, default: float) -> float:
    """Parse a numeric env var into a typed config error, not a bare `ValueError`.

    `float("abc")` raises `ValueError`, which escapes the `OidcConfigurationError` family
    and reaches the caller as an unrelated exception type with no reason code.
    """

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise OidcConfigurationError("unparsable_numeric_env", detail=f"{name} must be a number") from exc
    if value <= 0 or value != value or value == float("inf"):
        raise OidcConfigurationError("non_positive_numeric_env", detail=f"{name} must be a positive finite number")
    return value


def _load_grants(path_value: str) -> GrantTable:
    raw = json.loads(Path(path_value).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise OidcConfigurationError("grants_file_not_object")
    return grants_from_mapping(raw)


def identity_enabled() -> bool:
    return _flag(ENABLED_ENV, default=False)


def identity_settings_from_env() -> IdentitySettings | None:
    """Return `None` when the switch is off; raise when it is on but incomplete."""

    if not identity_enabled():
        return None

    auth_method = os.getenv(AUTH_METHOD_ENV, "none").strip().casefold() or "none"
    if auth_method not in _VALID_AUTH_METHODS:
        raise OidcConfigurationError("unknown_token_auth_method")

    scopes_raw = os.getenv(SCOPES_ENV, "openid profile email").replace(",", " ").split()
    roles_claim = os.getenv(ROLES_CLAIM_ENV, "realm_access.roles").strip()
    tenant_claim = os.getenv(TENANT_CLAIM_ENV, "").strip()

    oidc = OidcConfig(
        issuer=_required(ISSUER_ENV),
        client_id=_required(CLIENT_ID_ENV),
        redirect_uri=_required(REDIRECT_URI_ENV),
        client_secret=_secret_value(CLIENT_SECRET_ENV, CLIENT_SECRET_FILE_ENV),
        token_endpoint_auth_method=auth_method,  # type: ignore[arg-type]
        scopes=tuple(scopes_raw),
        roles_claim_path=tuple(part for part in roles_claim.split(".") if part),
        tenant_claim_path=tuple(part for part in tenant_claim.split(".") if part) or None,
        tenant_id=os.getenv(TENANT_ID_ENV, "").strip() or None,
    )

    session_secret = _secret_value(SESSION_SECRET_ENV, SESSION_SECRET_FILE_ENV)
    if not session_secret:
        raise OidcConfigurationError(
            "missing_session_secret",
            detail=f"{SESSION_SECRET_ENV} or {SESSION_SECRET_FILE_ENV} is required when {ENABLED_ENV}=1",
        )

    # 🔴 Yol ZORUNLUDUR, `InMemoryDecisionLedger`a düşülmez. Eski hâli düşüyordu ve bu
    # SESSİZ bir denetim kaybıydı: her şey çalışır görünür, `/me` cevap verir, yetki
    # kararları doğru alınır — ama sürecin ömrü boyunca biriken bütün izin/ret satırları
    # yeniden başlatmada yok olur. Defterin varlık sebebi tam olarak "sonradan bakmak"
    # olduğu için, kalıcı olmayan bir defter defter değildir. Bellek-içi defter yalnız
    # testler ve kütüphane çağıranları içindir; `api.py` yolundan asla seçilemez.
    ledger_path = os.getenv(LEDGER_PATH_ENV, "").strip()
    if not ledger_path:
        raise OidcConfigurationError(
            "missing_ledger_path",
            detail=(
                f"{LEDGER_PATH_ENV} is required when {ENABLED_ENV}=1: an in-memory decision "
                f"ledger loses every authorization row on restart"
            ),
        )
    ledger: DecisionLedger = JsonlDecisionLedger(ledger_path)

    grants_path = os.getenv(GRANTS_PATH_ENV, "").strip()
    if not grants_path:
        raise OidcConfigurationError(
            "missing_grants_file",
            detail=f"{GRANTS_PATH_ENV} is required: an empty grant table would deny every request silently",
        )

    return IdentitySettings(
        oidc=oidc,
        session_secret=session_secret.encode("utf-8"),
        session_ttl_seconds=_positive_float(SESSION_TTL_ENV, default=3600.0),
        # Varsayılan True. Tatbikatta düz HTTP kullanıldığı için AÇIKÇA kapatılabilir —
        # ama varsayılanın kendisi asla gevşetilmez.
        cookie_secure=_flag(COOKIE_SECURE_ENV, default=True),
        grants=_load_grants(grants_path),
        ledger=ledger,
    )


__all__ = [
    "IdentitySettings",
    "TokenEndpointAuthMethod",
    "identity_enabled",
    "identity_settings_from_env",
]
