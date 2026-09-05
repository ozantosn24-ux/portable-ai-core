"""`ChatProvider` adapter for the official `openai` SDK (Chat Completions).

Not a core dependency — install with `pip install -e ".[llm]"`. Same lazy-import
contract as the Anthropic adapter: importing this module never imports the SDK.

Measured against `openai==3.8.0`:

* `chat.completions.create` still accepts `temperature`, unlike the Anthropic API — so
  this adapter forwards `ChatRequest.temperature` when the caller set it.
* `max_completion_tokens` is the parameter used here; `max_tokens` still exists as a
  legacy alias but is rejected by newer reasoning models, so sending it would make the
  adapter fail on exactly the models people are moving to.
* Streamed usage requires `stream_options={"include_usage": True}`. Without it the
  final chunk carries no usage and every streamed answer would be an estimate.
* A content filter arrives as `finish_reason == "content_filter"` on an otherwise
  successful response — not as an exception.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..errors import BadRequestError, ContentPolicyError, GatewayError
from ..types import ChatRequest, Completion, TextDelta, Usage
from ._sdk_common import classify_status, classify_transport, import_sdk, retry_after_seconds


class OpenAIChatProvider:
    """Thin wrapper around `AsyncOpenAI().chat.completions`.

    ⚠️ `model` has **no default**, unlike the Anthropic adapter. A default here would
    be a model id this package cannot verify, and a stale or invented id fails at the
    worst moment — on the failover path, during the primary's outage. Pin one you have
    checked against the provider's model list.
    """

    def __init__(
        self,
        *,
        model: str,
        provider_id: str = "openai",
        client: Any | None = None,
        sdk: Any | None = None,
        api_key: str | None = None,
        **client_options: Any,
    ) -> None:
        """`client` and `sdk` are injection points for tests; production passes neither.

        `max_retries=0` for the same reason as the Anthropic adapter: retry belongs to
        `RetryPolicy` alone, or the ledger and the circuit breaker both undercount.
        """

        self._sdk = sdk if sdk is not None else import_sdk("openai")
        self._provider_id = provider_id
        self._model = model
        self._last_usage: Usage | None = None
        if client is not None:
            self._client = client
        else:
            options: dict[str, Any] = {"max_retries": 0, **client_options}
            if api_key is not None:
                options["api_key"] = api_key
            self._client = self._sdk.AsyncOpenAI(**options)

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def model(self) -> str:
        return self._model

    def last_stream_usage(self) -> Usage | None:
        return self._last_usage

    # ------------------------------------------------------------------ requests

    async def complete(self, request: ChatRequest) -> Completion:
        kwargs = self._request_kwargs(request)
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised as a taxonomy error below
            raise self._classify(exc) from exc
        choices = list(getattr(response, "choices", ()) or ())
        for choice in choices:
            self._raise_on_content_filter(getattr(choice, "finish_reason", None))
        text = "".join(getattr(choice.message, "content", None) or "" for choice in choices)
        return Completion(
            text=text,
            provider_id=self._provider_id,
            model=getattr(response, "model", kwargs["model"]),
            usage=_usage_of(getattr(response, "usage", None)),
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[TextDelta]:
        kwargs = self._request_kwargs(request)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        self._last_usage = None
        captured: Usage | None = None
        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    captured = _usage_of(usage)
                for choice in getattr(chunk, "choices", ()) or ():
                    self._raise_on_content_filter(getattr(choice, "finish_reason", None))
                    text = getattr(getattr(choice, "delta", None), "content", None)
                    if text:
                        yield TextDelta(provider_id=self._provider_id, text=text)
        except GatewayError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised as a taxonomy error below
            raise self._classify(exc) from exc
        self._last_usage = captured

    # ----------------------------------------------------------------- internals

    def _request_kwargs(self, request: ChatRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model or self._model,
            "max_completion_tokens": request.max_tokens,
            "messages": [{"role": message.role, "content": message.content} for message in request.messages],
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        return kwargs

    def _raise_on_content_filter(self, finish_reason: str | None) -> None:
        if finish_reason != "content_filter":
            return
        raise ContentPolicyError(
            "provider declined the request on content policy",
            provider_id=self._provider_id,
            category="content_filter",
        )

    def _classify(self, error: Exception) -> GatewayError:
        sdk = self._sdk
        if isinstance(error, sdk.APIConnectionError):
            return classify_transport(sdk, error, provider_id=self._provider_id)
        if isinstance(error, sdk.APIStatusError):
            return classify_status(
                getattr(error, "status_code", None),
                provider_id=self._provider_id,
                message=str(error),
                retry_after_s=retry_after_seconds(error),
            )
        return BadRequestError(str(error), provider_id=self._provider_id)


def _usage_of(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        exact=True,
    )
