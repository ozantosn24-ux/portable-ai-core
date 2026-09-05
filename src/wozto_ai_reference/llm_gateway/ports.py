"""Ports a chat provider adapter implements.

Kept in the subpackage rather than added to the top-level `ports.py`: the repo's
`ModelProvider` port is a *grounded answer* port — it takes retrieval hits and a trace
id and returns a `ModelOutput` bound to the RAG contract. A chat provider is a lower
layer with no notion of retrieval or citations. Widening `ModelProvider` to cover both
would force every RAG adapter to grow chat-shaped parameters it has no use for.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .types import ChatRequest, Completion, TextDelta, Usage


class ChatProvider(Protocol):
    """One upstream chat model, already wrapped so it speaks the gateway taxonomy."""

    @property
    def provider_id(self) -> str:
        """Stable, human-readable id. It lands in ledger rows, `Completion.provider_id`
        and every stream event, so it must not change between deployments."""
        ...

    @property
    def model(self) -> str:
        """The model this adapter uses when the request pins none.

        Required because a streamed answer has no response object to read the model
        from: `stream` yields text and nothing else, so without this the router would
        have to write an empty `Completion.model` into the ledger for every streamed
        request. For `complete()` the adapter still reports what the server echoed —
        that is the stronger source and the router prefers it.
        """
        ...

    async def complete(self, request: ChatRequest) -> Completion: ...

    def stream(self, request: ChatRequest) -> AsyncIterator[TextDelta]:
        """Yield text fragments until the response is complete.

        ⚠️ Bildirim bilerek `async def` DEĞİL. Uygulama tarafı `async def ... yield`
        (async generator) yazar; böyle bir fonksiyonu ÇAĞIRMAK coroutine değil,
        doğrudan yineleyici döndürür — yani `await provider.stream(req)` hata verir,
        doğru kullanım `async for delta in provider.stream(req)`. Protokolü
        `async def` yazsaydık imza, çalışan tek uygulama biçimiyle çelişirdi.
        """
        ...


@runtime_checkable
class StreamUsageReporter(Protocol):
    """Optional add-on: exact token counts for the stream that just finished.

    The `ChatProvider.stream` port yields text only, so the router cannot see token
    counts and would have to publish `Usage(0, 0, exact=False)` for every streamed
    answer. Both real SDKs *do* deliver usage in the terminal stream event, so an
    adapter that captured it implements this and the router uses it verbatim.

    ⛔ Optional on purpose — a provider that cannot report usage must be able to stay
    silent rather than invent a number. Silence yields an inexact `Usage`; a guess
    would yield a wrong one wearing `exact=True`.
    """

    def last_stream_usage(self) -> Usage | None: ...
