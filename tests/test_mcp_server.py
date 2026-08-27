"""Contracts for the MCP surface — the boundary, not the plumbing.

These tests do not start a stdio server. What matters is not that the SDK can be
wired up (it can) but that the authorization guarantee survives translation into a
protocol whose arguments are attacker-influenceable.

Async style follows `test_service.py`: plain `asyncio.run`, no async plugin.
"""

from __future__ import annotations

import asyncio

import pytest

from wozto_ai_reference.domain import Principal
from wozto_ai_reference.mcp_server import (
    IDENTITY_ARGUMENT_NAMES,
    ROLES_ENV,
    TENANT_ENV,
    TOOL_ANSWER,
    TOOL_WHOAMI,
    USER_ENV,
    IdentityNotConfigured,
    build_service,
    dispatch,
    reject_identity_arguments,
    resolve_principal,
    tool_definitions,
)


def demo_principal(roles: frozenset[str] = frozenset()) -> Principal:
    return Principal(tenant_id="tenant-demo", user_id="u1", roles=roles)


def call(name: str, arguments: dict | None, *, roles: frozenset[str] = frozenset()) -> dict:
    return asyncio.run(
        dispatch(name, arguments, service=build_service(), principal=demo_principal(roles))
    )


# --- startup identity ------------------------------------------------------

def test_principal_comes_from_environment():
    p = resolve_principal({TENANT_ENV: "tenant-demo", USER_ENV: "u1", ROLES_ENV: "finance, ops"})
    assert p.tenant_id == "tenant-demo"
    assert p.roles == frozenset({"finance", "ops"})


@pytest.mark.parametrize(
    "env",
    [
        {},
        {TENANT_ENV: "tenant-demo"},
        {USER_ENV: "u1"},
        {TENANT_ENV: "  ", USER_ENV: "u1"},
    ],
)
def test_missing_identity_FAILS_CLOSED(env):
    """A server with no identity must refuse to start rather than invent one."""
    with pytest.raises(IdentityNotConfigured):
        resolve_principal(env)


# --- the boundary ----------------------------------------------------------

def test_tool_schema_EXPOSES_NO_identity_parameter():
    """The schema is part of the security argument: a reviewer can read the boundary."""
    for spec in tool_definitions():
        props = set(spec["inputSchema"].get("properties", {}))
        assert not (props & IDENTITY_ARGUMENT_NAMES), f"{spec['name']} leaks identity params"
        # additionalProperties must be closed, or the schema promises nothing.
        assert spec["inputSchema"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "smuggled",
    ["tenant_id", "tenant", "user_id", "roles", "acl_roles", "principal", "identity",
     "TENANT_ID", "tenant-id"],
)
def test_identity_argument_is_REFUSED_not_ignored(smuggled):
    """Silently dropping it would make an injection attempt look like normal traffic."""
    out = call(TOOL_ANSWER, {"query": "refund", smuggled: "tenant-other"})
    assert out["ok"] is False and out["refused"] is True
    # The message echoes the key AS THE CALLER WROTE IT, not a normalised form:
    # when you are told which argument was refused, you want your own spelling back.
    assert smuggled.lower() in out["message"].lower()


def test_other_tenant_document_NEVER_returned():
    """The corpus carries a foreign-tenant document on purpose (positive control):
    if the boundary broke, this test would be the one that noticed."""
    out = call(TOOL_ANSWER, {"query": "roadmap secret rival"})
    assert out["ok"] is True
    leaked = [c for c in out["citations"] if "tenant-other" in c["source_uri"]]
    assert not leaked, "foreign tenant document crossed the boundary"


def test_acl_role_gates_restricted_document():
    """Same query, two principals: the role is the only thing that changes."""
    without = call(TOOL_ANSWER, {"query": "finance approval limits"})
    with_role = call(TOOL_ANSWER, {"query": "finance approval limits"},
                     roles=frozenset({"finance"}))
    ids_without = {c["document_id"] for c in without["citations"]}
    ids_with = {c["document_id"] for c in with_role["citations"]}
    assert "finance-policy" not in ids_without
    assert "finance-policy" in ids_with


# --- abstention ------------------------------------------------------------

def test_abstention_is_a_SUCCESSFUL_result():
    """'No authorized source' is the authorization path working, not a fault.

    Marking it as an error would invite clients to retry it like a transient failure.
    """
    out = call(TOOL_ANSWER, {"query": "zzzz nothing matches this at all"})
    assert out["ok"] is True
    assert out["abstained"] is True
    assert out["citations"] == []


def test_answer_carries_verifiable_provenance():
    out = call(TOOL_ANSWER, {"query": "refund requests"})
    assert out["abstained"] is False and out["citations"]
    c = out["citations"][0]
    for field in ("document_id", "version", "content_hash", "source_uri"):
        assert c[field], f"citation missing {field}"


# --- argument validation ---------------------------------------------------

@pytest.mark.parametrize("bad", [{}, {"query": ""}, {"query": "   "}, {"query": 5}])
def test_bad_query_is_rejected_with_a_reason(bad):
    out = call(TOOL_ANSWER, bad)
    assert out["ok"] is False and "query" in out["message"]


@pytest.mark.parametrize("limit", [0, 21, -1, "5", True, 2.5])
def test_bad_limit_is_rejected(limit):
    """`True` is deliberate: bool subclasses int in Python and would otherwise sail
    through a naive isinstance check."""
    out = call(TOOL_ANSWER, {"query": "refund", "limit": limit})
    assert out["ok"] is False and "limit" in out["message"]


def test_unknown_tool_named_in_error():
    out = call("definitely_not_a_tool", {})
    assert out["ok"] is False and "definitely_not_a_tool" in out["message"]


def test_whoami_reports_but_cannot_change_identity():
    out = call(TOOL_WHOAMI, {}, roles=frozenset({"finance"}))
    assert out["tenant_id"] == "tenant-demo" and out["roles"] == ["finance"]
    smuggled = call(TOOL_WHOAMI, {"tenant_id": "tenant-other"})
    assert smuggled.get("refused") is True


def test_refusal_helper_is_case_and_dash_insensitive():
    assert reject_identity_arguments({"Tenant-Id": "x"}) is not None
    assert reject_identity_arguments({"query": "x"}) is None
