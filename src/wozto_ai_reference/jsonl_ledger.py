"""Append-only JSONL primitives shared by every decision ledger in this package.

Two ledgers now exist for two different questions — `llm_gateway.AttemptLedger` ("what
did the router try, and what came back") and `identity.authz.DecisionLedger` ("who was
allowed to touch which mailbox") — and both need the same three properties. They are
here rather than duplicated because each of them is a lesson that costs something to
relearn:

1. **`newline=""`.** Python's text mode rewrites ``"\\n"`` into ``"\\r\\n"`` on Windows, so
   the *same* ledger written on two machines carries different bytes. Any tool that
   compares two ledgers, hashes a line, or counts line length sees the difference. The
   line terminator is written here, deliberately, not by the operating system.
2. **Re-open per record.** Holding a handle open would let a crash lose records the
   buffer had not flushed, and would keep the file locked against rotation. One
   ``open()`` per row is cheap next to whatever produced the row.
3. **`sort_keys=True`, `ensure_ascii=False`.** Stable field order makes two rows
   diffable; keeping non-ASCII text as itself keeps Turkish reason strings readable in
   the file instead of as ``\\u`` escapes.

⛔ This module holds no schema. Each ledger owns its own record model and validates its
own rows; sharing the *writing* must not become sharing the *meaning*, or one ledger's
schema change would silently reshape the other's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSON object as a single line. Never rewrites an existing line."""

    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(f"{line}\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read every row in write order. A missing file is an empty ledger, not an error."""

    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
