"""MCP server exposing the tenant-scoped query service over stdio.

WHY THIS EXISTS
---------------
Wrapping a search function in MCP is trivial and proves nothing. The interesting
problem is that **MCP has no authorization model of its own**: a tool call is just
a name plus a JSON object, and that object is composed by a model which may have
read untrusted content (a web page, a document, a retrieved chunk). Anything the
model can be talked into putting in an argument is, effectively, attacker-controlled.

This core's whole point is tenant + ACL filtered retrieval that abstains when no
authorized source exists. Carrying that guarantee across MCP therefore requires one
rule:

    **Identity comes from server startup. It is never a tool argument.**

That mirrors the HTTP surface, where `QueryPayload` deliberately carries no identity
and the principal is resolved from headers by an IdentityProvider. Here the principal
is resolved once, from the process environment, before any client can speak.

TWO DESIGN CHOICES WORTH DEFENDING
----------------------------------
1. Identity-shaped arguments are **rejected, not ignored.** Silently dropping a
   `tenant_id` argument would leave the caller believing it took effect, and would
   make a prompt-injection attempt indistinguishable from normal traffic. A refusal
   is both safer and observable.

2. Abstention is a **successful result, not an error.** "No authorized source" is a
   correct answer produced by the authorization path. Marking it `isError` would
   invite clients to retry it like a transient failure, which is exactly wrong.

The server ships no customer data: the default corpus is the same synthetic demo
used by the HTTP app.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from .adapters import (
    DeterministicGroundedModel,
    InMemorySearchProvider,
    MemoryTelemetry,
)
from .domain import Document, Principal
from .service import QueryService

TENANT_ENV = "WOZTO_MCP_TENANT_ID"
USER_ENV = "WOZTO_MCP_USER_ID"
ROLES_ENV = "WOZTO_MCP_ROLES"

#: Argument names a caller must never be able to set. Kept broad on purpose: the
#: cost of rejecting a harmless synonym is an error message, while the cost of
#: missing one is a tenant boundary crossed by a JSON key.
IDENTITY_ARGUMENT_NAMES = frozenset(
    {
        "tenant",
        "tenant_id",
        "tenantid",
        "user",
        "user_id",
        "userid",
        "role",
        "roles",
        "acl",
        "acl_roles",
        "principal",
        "identity",
        "auth",
        "authorization",
    }
)


class IdentityNotConfigured(RuntimeError):
    """Raised at startup when the server has no principal to act as."""


def resolve_principal(env: Mapping[str, str] | None = None) -> Principal:
    """Build the principal from the environment. Fail closed.

    A server that starts without an identity would have to either invent one or
    accept one from the client later -- both are the failure this module exists to
    prevent, so the process refuses to start instead.
    """
    source = os.environ if env is None else env
    tenant = (source.get(TENANT_ENV) or "").strip()
    user = (source.get(USER_ENV) or "").strip()
    if not tenant or not user:
        raise IdentityNotConfigured(
            f"{TENANT_ENV} and {USER_ENV} are required. "
            "Identity is configured at startup and is never accepted from a tool call."
        )
    raw_roles = (source.get(ROLES_ENV) or "").strip()
    roles = frozenset(part.strip() for part in raw_roles.split(",") if part.strip())
    return Principal(tenant_id=tenant, user_id=user, roles=roles)


def demo_documents() -> list[Document]:
    """Synthetic corpus. No customer data, by construction."""
    return [
        Document(
            tenant_id="tenant-demo",
            document_id="refund-policy",
            version="v1",
            title="Refund Policy",
            section="Eligibility",
            source_uri="memory://tenant-demo/refund-policy#eligibility",
            content="Refund requests are reviewed by a human operator before any action.",
            content_hash="sha256:demo-refund-v1",
        ),
        Document(
            tenant_id="tenant-demo",
            document_id="finance-policy",
            version="v1",
            title="Finance Policy",
            section="Restricted",
            source_uri="memory://tenant-demo/finance-policy#restricted",
            content="Finance-only approval limits are restricted to the finance role.",
            content_hash="sha256:demo-finance-v1",
            acl_roles=frozenset({"finance"}),
        ),
        Document(
            tenant_id="tenant-other",
            document_id="rival-roadmap",
            version="v1",
            title="Rival Roadmap",
            section="Secret",
            source_uri="memory://tenant-other/rival-roadmap#secret",
            content="This document belongs to a different tenant and must never leak.",
            content_hash="sha256:demo-other-v1",
        ),
    ]


def build_service(documents: list[Document] | None = None) -> QueryService:
    return QueryService(
        search=InMemorySearchProvider(documents or demo_documents()),
        model=DeterministicGroundedModel(),
        telemetry=MemoryTelemetry(),
    )


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

TOOL_ANSWER = "answer_from_authorized_sources"
TOOL_WHOAMI = "describe_identity"


def tool_definitions() -> list[dict[str, Any]]:
    """Tool schemas. Note what is absent: no tenant, user, role or ACL parameter.

    The schema is part of the security argument. A client cannot ask for something
    the schema does not offer, and a reviewer can verify the boundary by reading it.
    """
    return [
        {
            "name": TOOL_ANSWER,
            "description": (
                "Answer a question using only sources the configured principal is "
                "authorized to read. Returns citations with document id, version and "
                "content hash. If no authorized source matches, it abstains instead "
                "of guessing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The question to answer."},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Maximum authorized sources to use (default 5).",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": TOOL_WHOAMI,
            "description": (
                "Report which tenant and roles this server acts as. Read-only. Useful "
                "for confirming the boundary; it cannot change it."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


def reject_identity_arguments(arguments: Mapping[str, Any]) -> str | None:
    """Return a refusal message if the caller tried to supply identity, else None."""
    offending = sorted(
        name for name in arguments if name.strip().casefold().replace("-", "_") in IDENTITY_ARGUMENT_NAMES
    )
    if not offending:
        return None
    return (
        "REFUSED: identity cannot be supplied by a tool call. "
        f"Offending argument(s): {', '.join(offending)}. "
        "This server resolves tenant, user and roles from its own startup "
        "configuration; accepting them here would let any caller -- including a "
        "model influenced by untrusted content -- cross a tenant boundary."
    )


async def dispatch(
    name: str,
    arguments: Mapping[str, Any] | None,
    *,
    service: QueryService,
    principal: Principal,
) -> dict[str, Any]:
    """Route one tool call. Returns a plain dict so it is testable without a client."""
    args: Mapping[str, Any] = arguments or {}

    refusal = reject_identity_arguments(args)
    if refusal is not None:
        return {"ok": False, "refused": True, "message": refusal}

    if name == TOOL_WHOAMI:
        return {
            "ok": True,
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "roles": sorted(principal.roles),
            "note": "Configured at startup; not settable through this protocol.",
        }

    if name != TOOL_ANSWER:
        return {"ok": False, "message": f"Unknown tool: {name}"}

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "message": "'query' must be a non-empty string."}
    limit = args.get("limit", 5)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        return {"ok": False, "message": "'limit' must be an integer between 1 and 20."}

    result = await service.query(principal=principal, query=query, limit=limit)
    return {
        # ⭐ An abstention is ok=True: it is the authorization path working, not a
        #   fault. Clients must not retry it as if it were transient.
        "ok": True,
        "abstained": result.abstained,
        "answer": result.answer,
        # JSON mode converts domain dates to ISO-8601 strings before the stdio
        # renderer sees them.  Plain model_dump() would leave date objects in
        # the mapping and make otherwise valid citations fail serialization.
        "citations": [c.model_dump(mode="json") for c in result.citations],
        "trace_id": result.trace_id,
    }


def render(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)


async def main() -> None:  # pragma: no cover - process entry point
    """stdio entry point. Import of the MCP SDK is deferred so the core package
    stays importable (and testable) without the optional dependency installed."""
    import mcp.types as types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    principal = resolve_principal()
    service = build_service()
    server: Server = Server("wozto-portable-ai-core")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(**spec) for spec in tool_definitions()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        payload = await dispatch(name, arguments, service=service, principal=principal)
        return [types.TextContent(type="text", text=render(payload))]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run() -> None:  # pragma: no cover - console script shim
    import asyncio

    asyncio.run(main())
