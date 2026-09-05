"""Scripted provider: hermetic tests without a network, a clock, or an SDK.

The script is a queue of steps consumed one per call, so a test states the failure
sequence it wants ("429, then 429, then success") as data instead of as mock wiring.
When the script runs out the provider raises rather than repeating its last step — a
provider that silently keeps answering would let a test assert a passing behaviour that
the router never actually produced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from ..errors import GatewayError
from ..types import ChatRequest, Completion, TextDelta, Usage


class ScriptExhausted(RuntimeError):
    """The provider was called more times than the script describes."""


@dataclass(frozen=True)
class Answer:
    """Succeed. In `stream` the text is emitted as a single delta."""

    text: str
    usage: Usage | None = None


@dataclass(frozen=True)
class Fail:
    """Raise before producing anything."""

    error: GatewayError


@dataclass(frozen=True)
class PartialThenFail:
    """Emit `chunks` and then raise — the case the no-concatenation rule exists for.

    In `complete()` there is no partial state to expose, so this behaves as a plain
    failure: the chunks are dropped and the error is raised.
    """

    chunks: tuple[str, ...]
    error: GatewayError


type ScriptStep = Answer | Fail | PartialThenFail


@dataclass
class ScriptedProvider:
    """A `ChatProvider` whose behaviour is a list."""

    provider_id: str
    script: Sequence[ScriptStep]
    model: str = "scripted-model"
    complete_calls: list[ChatRequest] = field(default_factory=list)
    stream_calls: list[ChatRequest] = field(default_factory=list)
    _cursor: int = field(default=0, init=False, repr=False)
    _last_usage: Usage | None = field(default=None, init=False, repr=False)

    @property
    def calls(self) -> int:
        """Total calls of any kind — how a test proves an open breaker really fenced."""

        return len(self.complete_calls) + len(self.stream_calls)

    def _next_step(self) -> ScriptStep:
        if self._cursor >= len(self.script):
            raise ScriptExhausted(f"{self.provider_id}: script has {len(self.script)} steps, call {self._cursor + 1}")
        step = self.script[self._cursor]
        self._cursor += 1
        return step

    def last_stream_usage(self) -> Usage | None:
        return self._last_usage

    async def complete(self, request: ChatRequest) -> Completion:
        self.complete_calls.append(request)
        step = self._next_step()
        if isinstance(step, Fail):
            raise step.error
        if isinstance(step, PartialThenFail):
            raise step.error
        return Completion(
            text=step.text,
            provider_id=self.provider_id,
            model=request.model or self.model,
            usage=step.usage if step.usage is not None else Usage(),
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[TextDelta]:
        self.stream_calls.append(request)
        self._last_usage = None
        step = self._next_step()
        if isinstance(step, Fail):
            raise step.error
        if isinstance(step, PartialThenFail):
            for chunk in step.chunks:
                yield TextDelta(provider_id=self.provider_id, text=chunk)
            raise step.error
        self._last_usage = step.usage
        yield TextDelta(provider_id=self.provider_id, text=step.text)
