"""Small asyncio runner for psycopg-compatible Windows event loops."""

from __future__ import annotations

import asyncio
import selectors
import sys
from collections.abc import Coroutine
from typing import Any


def _new_windows_selector_loop() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine with a selector loop when psycopg requires it on Windows."""

    if sys.platform == "win32":
        return asyncio.run(coroutine, loop_factory=_new_windows_selector_loop)
    return asyncio.run(coroutine)
