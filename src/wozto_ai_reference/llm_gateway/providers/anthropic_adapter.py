"""`ChatProvider` adapter for the official `anthropic` SDK.

Not a core dependency — install with `pip install -e ".[llm]"`. The import happens in
`__init__`, so importing this module (and the rest of the gateway) works without the
SDK present; only constructing the provider requires it.

Three things here are measured against `anthropic==1.4.0`, not recalled:

1. `messages.create` has **no `temperature` parameter at all** — sampling controls were
   removed from the current Claude models and sending one is a 400. `ChatRequest.temperature`
   is therefore ignored by this adapter unless `send_temperature=True` is set explicitly
   for an older model.
2. The stream helper has **no `text_stream` attribute** in this version. Text is taken
   from raw events (`content_block_delta` -> `text_delta`), which is what the SDK's own
   internal text iterator does.
3. A content refusal is **HTTP 200** with `stop_reason == "refusal"`, not an exception.
   An adapter that only maps exceptions would hand the caller an empty string and call
   it a success.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..errors import BadRequestError, ContentPolicyError, GatewayError, RetryableError
from ..types import ChatMessage, ChatRequest, Completion, TextDelta, Usage
from ._sdk_common import classify_status, classify_transport, import_sdk, retry_after_seconds

DEFAULT_MODEL = "claude-opus-5"


class AnthropicChatProvider:
    """Thin, stateless-per-request wrapper around `AsyncAnthropic`."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        provider_id: str = "anthropic",
        client: Any | None = None,
        sdk: Any | None = None,
        api_key: str | None = None,
        send_temperature: bool = False,
        **client_options: Any,
    ) -> None:
        """`client` and `sdk` are injection points for tests; production passes neither.

        The SDK's own retry loop is disabled (`max_retries=0`). Two retry layers
        multiply: a 3-attempt gateway policy over a 2-retry client is nine calls, the
        ledger records three, and the circuit breaker counts three failures for nine
        rejections. Retry lives in exactly one place, and that place is `RetryPolicy`.
        """

        self._sdk = sdk if sdk is not None else import_sdk("anthropic")
        self._provider_id = provider_id
        self._model = model
        self._send_temperature = send_temperature
        self._last_usage: Usage | None = None
        if client is not None:
            self._client = client
        else:
            options: dict[str, Any] = {"max_retries": 0, **client_options}
            if api_key is not None:
                options["api_key"] = api_key
            self._client = self._sdk.AsyncAnthropic(**options)

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
            response = await self._client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised as a taxonomy error below
            raise self._classify(exc) from exc
        self._raise_on_refusal(response)
        return Completion(
            text=_text_of(response),
            provider_id=self._provider_id,
            model=getattr(response, "model", kwargs["model"]),
            usage=_usage_of(response),
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[TextDelta]:
        kwargs = self._request_kwargs(request)
        self._last_usage = None
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    # `anthropic` 1.4.0'da `stream.text_stream` YOK (olculdu). Ham
                    # olaydan okumak SDK'nin kendi ic metin yineleyicisiyle ayni sart.
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield TextDelta(provider_id=self._provider_id, text=event.delta.text)
                final = await stream.get_final_message()
        except GatewayError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised as a taxonomy error below
            raise self._classify(exc) from exc
        self._last_usage = _usage_of(final)
        # Reddedilme akisin SONUNDA ortaya cikar: o ana kadar yayilan delta'lar
        # gecerliydi. Yonlendirici bunu NonRetryableError olarak gorur ve failover
        # DENEMEZ — reddi baska saglayicida "denemek" politika yikamasidir.
        self._raise_on_refusal(final)

    # ----------------------------------------------------------------- internals

    def _request_kwargs(self, request: ChatRequest) -> dict[str, Any]:
        system, messages = _split_system(request.messages)
        kwargs: dict[str, Any] = {
            "model": request.model or self._model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
        }
        if system:
            kwargs["system"] = system
        if self._send_temperature and request.temperature is not None:
            kwargs["temperature"] = request.temperature
        return kwargs

    def _raise_on_refusal(self, response: Any) -> None:
        if getattr(response, "stop_reason", None) != "refusal":
            return
        details = getattr(response, "stop_details", None)
        raise ContentPolicyError(
            "provider declined the request on content policy",
            provider_id=self._provider_id,
            category=getattr(details, "category", None),
        )

    def _classify(self, error: Exception) -> GatewayError:
        sdk = self._sdk
        if isinstance(error, sdk.APIConnectionError):
            # APITimeoutError bunun ALT SINIFI — once burasi, sonra ayrim iceride.
            return classify_transport(sdk, error, provider_id=self._provider_id)
        sdk_retryable = getattr(sdk, "RetryableError", None)
        if sdk_retryable is not None and isinstance(error, sdk_retryable):
            # OLCULDU (anthropic==1.4.0 wheel'i acilip okunarak, 2026-09-06):
            # `anthropic.RetryableError` VARDIR ve `AnthropicError`in dogrudan alt
            # sinifidir — `APIError`in DEGIL, yani yukaridaki iki daldan hicbirine
            # dusmez. SDK onu kendisi ATMAZ; `_base_client._should_retry_exception`
            # yalnizca TANIR, atan taraf kullanicinin middleware'idir. Bu satir olmasa
            # boyle bir hata asagidaki `BadRequestError` dalina duser ve yeniden
            # denemeye deger bir ariza `NonRetryableError` olarak cagirana yukseltilirdi.
            # ⚠️ `pre_send` BILINCLI olarak isaretlenmiyor (varsayilan False): SDK'nin
            # kendi docstring'i "middleware" diyor ve middleware istegi de CEVABI da
            # gorebilir ⇒ "gonderilmedi" KANITI yoktur. errors.py'nin kurali acik —
            # (3) ile (4) ayirt edilemiyorsa (4) secilir. `pre_send=True`, yan etkisi
            # olan bir istegin ikinci kez gonderilmesine izin vermek demektir.
            return RetryableError(str(error) or "provider middleware asked for a retry", provider_id=self._provider_id)
        if isinstance(error, sdk.APIStatusError):
            return classify_status(
                getattr(error, "status_code", None),
                provider_id=self._provider_id,
                message=str(error),
                retry_after_s=retry_after_seconds(error),
            )
        return BadRequestError(str(error), provider_id=self._provider_id)


def _split_system(messages: tuple[ChatMessage, ...]) -> tuple[str, list[ChatMessage]]:
    """Hoist leading system turns into the top-level `system` parameter.

    The Messages API takes the system prompt as its own field, not as a message. A
    system message appearing *after* a user or assistant turn is a different feature
    (mid-conversation system messages) that only some models accept, so it is rejected
    here with an explanation rather than silently relocated to the front — moving it
    would change the prompt's meaning without telling anyone.
    """

    system_parts: list[str] = []
    rest: list[ChatMessage] = []
    for message in messages:
        if message.role != "system":
            rest.append(message)
            continue
        if rest:
            raise BadRequestError(
                "a system message after the first non-system turn is model-gated on this API; "
                "put system content first or send it as a user turn"
            )
        system_parts.append(message.content)
    if not rest:
        raise BadRequestError("at least one non-system message is required")
    return "\n\n".join(system_parts), rest


def _text_of(response: Any) -> str:
    return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")


def _usage_of(response: Any) -> Usage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        # Saglayici SAYDI ⇒ kesin. Tek `exact=True` yazan yer burasi olmali.
        exact=True,
    )
