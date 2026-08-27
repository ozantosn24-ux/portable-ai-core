"""End-to-end proof: spawn the MCP server and talk to it with a real MCP client.

The unit tests in `tests/test_mcp_server.py` exercise the boundary without a client,
on purpose -- they must run wherever the core runs, SDK installed or not. This script
answers the different question those tests cannot: **does the thing actually speak the
protocol?** It starts the server as a subprocess over stdio and drives it with the
MCP SDK's own client.

Requires the optional extra:  pip install -e ".[mcp]"
Run:                          python scripts/mcp_smoke.py
Exit code 0 only if every check below holds.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


def text_of(result) -> str:
    return "".join(block.text for block in result.content
                   if getattr(block, "type", "") == "text")


def payload_of(result) -> dict:
    """Tool results arrive as content blocks; the server encodes JSON in text.

    Not every result is ours: a schema violation is rejected by the protocol layer
    and comes back as a plain error string. Returning `{}` for that case keeps the
    caller honest -- it has to look at `isError` rather than assume JSON.
    """
    text = text_of(result)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


async def main() -> int:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        print("mcp SDK not installed. Install with:  pip install -e \".[mcp]\"")
        return 2

    env = dict(os.environ)
    env.update(
        {
            "WOZTO_MCP_TENANT_ID": "tenant-demo",
            "WOZTO_MCP_USER_ID": "smoke-user",
            "WOZTO_MCP_ROLES": "finance",
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from wozto_ai_reference.mcp_server import run; run()"],
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            check("server initializes", bool(init.serverInfo.name),
                  f"name={init.serverInfo.name}")

            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            check("tools advertised", names == {"answer_from_authorized_sources",
                                                "describe_identity"}, str(sorted(names)))

            # The schema is the boundary a reviewer can read.
            answer_tool = next(t for t in listed.tools if t.name == "answer_from_authorized_sources")
            props = set((answer_tool.inputSchema or {}).get("properties", {}))
            check("schema exposes NO identity parameter",
                  not (props & {"tenant_id", "user_id", "roles", "principal"}),
                  f"properties={sorted(props)}")

            grounded = payload_of(await session.call_tool(
                "answer_from_authorized_sources", {"query": "refund requests"}))
            check("grounded answer with citations",
                  grounded["ok"] and not grounded["abstained"] and grounded["citations"],
                  f"{len(grounded['citations'])} citation(s)")

            # Role-gated document: this principal HAS the finance role.
            fin = payload_of(await session.call_tool(
                "answer_from_authorized_sources", {"query": "finance approval limits"}))
            check("role-gated document reachable with the role",
                  any(c["document_id"] == "finance-policy" for c in fin["citations"]))

            abstain = payload_of(await session.call_tool(
                "answer_from_authorized_sources", {"query": "zzzz nothing matches"}))
            check("abstains instead of guessing",
                  abstain["ok"] and abstain["abstained"] and not abstain["citations"])

            # The one that matters: try to cross the tenant boundary through an argument.
            # ⭐ MEASURED 2026-08-27: this is refused TWICE, by two independent layers.
            #    (1) protocol -- `additionalProperties: false` makes a validating peer
            #        reject the call before any handler runs;
            #    (2) application -- `reject_identity_arguments` in dispatch().
            #    A compliant client never reaches layer 2, so the check accepts either
            #    and REPORTS WHICH ONE fired. Relying on (1) alone would be wrong: not
            #    every peer validates, and the guarantee must not depend on politeness.
            raw = await session.call_tool(
                "answer_from_authorized_sources",
                {"query": "rival roadmap", "tenant_id": "tenant-other"})
            smuggle = payload_of(raw)
            protocol_refused = bool(getattr(raw, "isError", False))
            app_refused = smuggle.get("refused") is True
            check("identity smuggling REFUSED (not silently ignored)",
                  protocol_refused or app_refused,
                  ("protocol layer (schema)" if protocol_refused else "application layer")
                  + f" -- {text_of(raw)[:60]}")

            # And the foreign tenant's document must not appear even on a plain query.
            plain = payload_of(await session.call_tool(
                "answer_from_authorized_sources", {"query": "roadmap secret rival"}))
            check("foreign tenant document never cited",
                  not any("tenant-other" in c["source_uri"] for c in plain["citations"]))

            who = payload_of(await session.call_tool("describe_identity", {}))
            check("identity is the one configured at startup",
                  who["tenant_id"] == "tenant-demo" and who["roles"] == ["finance"],
                  f"{who['tenant_id']} / {who['roles']}")

    failed = [name for name, ok, _ in CHECKS if not ok]
    print()
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("MCP server verified against a real client over stdio.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
