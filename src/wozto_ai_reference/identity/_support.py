"""Injectable wall clock, constant-time comparison, and the `auth` extra's import gate."""

from __future__ import annotations

import hmac
import importlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .errors import IdentityDependencyMissing


def constant_time_equals(left: str, right: str) -> bool:
    """Constant-time string comparison that survives non-ASCII input.

    🔴 `hmac.compare_digest` / `secrets.compare_digest` raise
    `TypeError: comparing strings with non-ASCII characters is not supported` when either
    `str` operand contains a codepoint above U+007F. Every value this package compares —
    cookie MAC, CSRF token, `state`, `nonce`, `iss` — arrives from the network, and
    Starlette latin-1-decodes header bytes, so a single `\\xff` byte in a cookie is enough
    to produce that TypeError. An unhandled TypeError inside a comparison is a **500**,
    which turns an authentication *rejection* into a server error: the attacker learns the
    request reached the comparison, and the fail-closed reason code never gets written.

    Encoding both operands to UTF-8 first makes the comparison total. Bytes compare
    identically to the strings they came from, and `compare_digest` on bytes has no ASCII
    restriction. `surrogatepass` keeps a lone surrogate from raising on the way in — the
    point is that NO input may make this function raise.
    """

    return hmac.compare_digest(
        left.encode("utf-8", "surrogatepass"),
        right.encode("utf-8", "surrogatepass"),
    )

# Duvar saati, monotonik saat DEĞİL. Aynı ayrım `llm_gateway.ledger`da da var ve aynı
# sebeple: monotonik saatin başlangıcı keyfidir, onu `ts` diye yazmak defterdeki her
# satırı 1970'e düşürür. Burada süre ölçülmüyor, "ne zaman oldu" yazılıyor — ve token
# `exp`/`iat`/`nbf` doğrulaması da yalnızca duvar saatiyle anlamlıdır, çünkü karşı taraf
# (IdP) da duvar saatiyle imzalar.
WallClock = Callable[[], datetime]


def system_wall_clock() -> datetime:
    """Timezone-aware UTC now. Tests inject a frozen clock in its place."""

    return datetime.now(UTC)


def isoformat_utc(moment: datetime) -> str:
    """Render a ledger timestamp. A naive datetime is refused rather than guessed."""

    if moment.tzinfo is None:
        raise ValueError("wall clock must return a timezone-aware datetime")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def epoch_seconds(moment: datetime) -> float:
    if moment.tzinfo is None:
        raise ValueError("wall clock must return a timezone-aware datetime")
    return moment.timestamp()


def import_optional(module_name: str, *, extra: str = "auth") -> Any:
    """Import an optional dependency, or fail with a message that names the extra.

    ⚠️ Deliberately NOT `llm_gateway.providers._sdk_common.import_sdk`, even though the
    two look alike. That one raises `ProviderDependencyMissing` — an *LLM gateway* error
    class whose meaning is "a model provider cannot be constructed" — and it lives in a
    private module of that subpackage. Reusing it would make an identity failure surface
    as a gateway failure, and would couple two subpackages that share nothing else.
    `importlib.import_module` rather than `__import__` because this caller wants
    submodules (`joserfc.jwt`), not the top-level package.
    """

    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise IdentityDependencyMissing(
            "auth_extra_not_installed",
            detail=(
                f"{module_name!r} paketi kurulu degil. Kurulum: pip install -e \".[{extra}]\" "
                f"— cekirdek bagimliliklara DAHIL DEGILDIR, cunku bu paketin hedefi bulut-notr "
                f"ve hafif bir cekirdektir."
            ),
        ) from exc
