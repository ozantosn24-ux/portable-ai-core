"""SDK adapters — exercised WITHOUT installing `anthropic` or `openai`.

Two things are proven here:

1. The optional dependency is genuinely optional. `sys.modules[...] = None` makes the
   import fail exactly as an uninstalled package would, and the adapter must answer
   with a message that names the extra instead of an `ImportError` traceback.
2. The exception mapping is exercised against a *stand-in* SDK namespace whose class
   tree matches the real one (`APITimeoutError` under `APIConnectionError`, status
   errors under `APIStatusError`). The class names and that hierarchy were read off
   installed `anthropic==1.4.0` / `openai==3.8.0` in a throwaway environment; the
   fake mirrors them so the core suite keeps running with the base dependency set.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from wozto_ai_reference.llm_gateway import (
    AmbiguousServerError,
    AmbiguousTimeoutError,
    AuthError,
    BadRequestError,
    ChatMessage,
    ChatRequest,
    ContentPolicyError,
    ProviderConnectionError,
    ProviderDependencyMissing,
    RateLimitError,
    RetryableError,
    ServerError,
)
from wozto_ai_reference.llm_gateway.providers.anthropic_adapter import AnthropicChatProvider
from wozto_ai_reference.llm_gateway.providers.openai_adapter import OpenAIChatProvider

# ------------------------------------------------------------------- stand-in SDK


class FakeAnthropicError(Exception):
    """`anthropic.AnthropicError` — ağacın kökü (1.4.0'da ölçüldü)."""


class FakeAPIError(FakeAnthropicError):
    pass


class FakeAPIConnectionError(FakeAPIError):
    pass


class FakeAPITimeoutError(FakeAPIConnectionError):
    """Gerçek SDK'larda da `APIConnectionError`'ın ALT SINIFI — sıra bu yüzden önemli."""


class FakeSDKRetryableError(FakeAnthropicError):
    """`anthropic.RetryableError`.

    ⭐ `APIError`'ın DEĞİL, doğrudan `AnthropicError`'ın altındadır (1.4.0 wheel'i
    açılıp `_exceptions.py` okunarak doğrulandı) ⇒ adaptörün tanıdığı iki daldan
    (`APIConnectionError`, `APIStatusError`) hiçbirine düşmez. Hiyerarşi burada yanlış
    kurulursa test, adaptörün gerçekte karşılaşacağı durumu sınamamış olur.
    """


class FakeAPIStatusError(FakeAPIError):
    def __init__(
        self,
        status_code: int,
        *,
        retry_after: str | None = None,
        retry_after_ms: str | None = None,
    ) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        headers: dict[str, str] = {}
        if retry_after is not None:
            headers["retry-after"] = retry_after
        if retry_after_ms is not None:
            headers["retry-after-ms"] = retry_after_ms
        self.response = SimpleNamespace(headers=headers)


FAKE_SDK = SimpleNamespace(
    AnthropicError=FakeAnthropicError,
    APIError=FakeAPIError,
    APIConnectionError=FakeAPIConnectionError,
    APITimeoutError=FakeAPITimeoutError,
    APIStatusError=FakeAPIStatusError,
    RetryableError=FakeSDKRetryableError,
)


class FakeCalls:
    """Records the kwargs an adapter actually sent."""

    def __init__(self, *, response=None, error=None) -> None:
        self.kwargs: list[dict] = []
        self._response = response
        self._error = error

    async def create(self, **kwargs):
        self.kwargs.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def anthropic_client(**kwargs):
    calls = FakeCalls(**kwargs)
    return SimpleNamespace(messages=calls), calls


def openai_client(**kwargs):
    calls = FakeCalls(**kwargs)
    return SimpleNamespace(chat=SimpleNamespace(completions=calls)), calls


def anthropic_response(*, text="cevap", stop_reason="end_turn", category=None, model="claude-opus-5"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model=model,
        stop_reason=stop_reason,
        stop_details=None if category is None else SimpleNamespace(category=category),
        usage=SimpleNamespace(input_tokens=12, output_tokens=7),
    )


def openai_response(*, text="cevap", finish_reason="stop", model="test-model"):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=text))],
        model=model,
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
    )


def request(**overrides) -> ChatRequest:
    payload = {
        "messages": (ChatMessage(role="user", content="merhaba"),),
        "idempotency_key": "idem-a1",
    }
    payload.update(overrides)
    return ChatRequest(**payload)


# ------------------------------------------------------------------- lazy import


@pytest.mark.parametrize(
    ("module_name", "factory"),
    [("anthropic", AnthropicChatProvider), ("openai", lambda: OpenAIChatProvider(model="pinned-model"))],
)
def test_missing_sdk_names_the_extra_instead_of_leaking_an_import_error(monkeypatch, module_name, factory):
    """`sys.modules[name] = None` kurulu paketi de görünmez yapar — kurulu olsa da olmasa
    da aynı yolu sınar."""
    monkeypatch.setitem(sys.modules, module_name, None)

    with pytest.raises(ProviderDependencyMissing) as info:
        factory()

    message = str(info.value)
    assert module_name in message
    assert '.[llm]' in message, "mesaj hangi extra'nın kurulacağını SÖYLEMELİ"


def test_importing_the_adapter_modules_does_not_import_any_sdk():
    """Modülü içe aktarmak SDK'yı yüklememeli; yalnız kurmak yükler."""
    assert "anthropic" not in sys.modules
    assert "openai" not in sys.modules


# --------------------------------------------------------------- status mapping


@pytest.mark.parametrize(
    ("status", "expected", "pre_send"),
    [
        (429, RateLimitError, True),
        (401, AuthError, None),
        (403, AuthError, None),
        (400, BadRequestError, None),
        (422, BadRequestError, None),
        (404, BadRequestError, None),
        (503, ServerError, True),
        (529, ServerError, True),
        (500, AmbiguousServerError, None),
        (502, AmbiguousServerError, None),
        (504, AmbiguousServerError, None),
    ],
)
def test_anthropic_status_mapping(status, expected, pre_send):
    client, _ = anthropic_client(error=FakeAPIStatusError(status))
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    with pytest.raises(expected) as info:
        asyncio.run(provider.complete(request()))

    assert info.value.provider_id == "anthropic"
    if pre_send is not None:
        assert info.value.pre_send is pre_send


def test_500_is_ambiguous_not_merely_retryable():
    """⭐ 500 'işlenmiş OLABİLİR'dir. Düz retryable sayılırsa yan etkili bir istek
    ikinci kez gönderilir ve bunu hiçbir kapı yakalamaz."""
    client, _ = anthropic_client(error=FakeAPIStatusError(500))
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    with pytest.raises(AmbiguousServerError):
        asyncio.run(provider.complete(request()))


def test_retry_after_header_is_read_off_the_status_error():
    client, _ = anthropic_client(error=FakeAPIStatusError(429, retry_after="4"))
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    with pytest.raises(RateLimitError) as info:
        asyncio.run(provider.complete(request()))

    assert info.value.retry_after_s == 4.0


def test_retry_after_ms_header_is_read_as_milliseconds():
    """`retry-after` tam SANİYEDİR; 250 ms isteyen sağlayıcı onu 0'a ya da 1'e
    yuvarlamak zorunda kalır. `retry-after-ms` bu yüzden var ve okunmalı."""
    client, _ = anthropic_client(error=FakeAPIStatusError(429, retry_after_ms="250"))
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    with pytest.raises(RateLimitError) as info:
        asyncio.run(provider.complete(request()))

    assert info.value.retry_after_s == 0.25


def test_retry_after_ms_wins_over_the_coarser_retry_after_seconds():
    """İki başlık birden gelirse hassas olan kazanır — SDK'nın kendi ayrıştırıcısı da
    (`_base_client._parse_retry_after_header`, 1.4.0'da okundu) önce `-ms`'e bakar."""
    client, _ = anthropic_client(error=FakeAPIStatusError(429, retry_after="1", retry_after_ms="250"))
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    with pytest.raises(RateLimitError) as info:
        asyncio.run(provider.complete(request()))

    assert info.value.retry_after_s == 0.25, "saniye başlığı 1.0 derdi; 4 kat fazla beklerdik"


def test_unparseable_retry_after_ms_falls_back_to_the_seconds_header():
    """Bozuk bir `-ms` başlığı, geçerli saniye başlığını DÜŞÜRMEZ."""
    client, _ = anthropic_client(error=FakeAPIStatusError(429, retry_after="3", retry_after_ms="soon"))
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    with pytest.raises(RateLimitError) as info:
        asyncio.run(provider.complete(request()))

    assert info.value.retry_after_s == 3.0


def test_sdk_middleware_retryable_error_is_not_treated_as_a_bad_request():
    """`anthropic.RetryableError` iki tanıdık dala da düşmez ve `BadRequestError`
    dalına yuvarlanıyordu: yeniden denemeye DEĞER bir arıza, çağırana
    'istek kusurlu, failover yok' diye yükseliyordu."""
    client, _ = anthropic_client(error=FakeSDKRetryableError("middleware asked for a retry"))
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    with pytest.raises(RetryableError) as info:
        asyncio.run(provider.complete(request()))

    assert info.value.provider_id == "anthropic"
    assert info.value.pre_send is False, (
        "SDK 'middleware' diyor ve middleware CEVABI da görebilir ⇒ 'gönderilmedi' kanıtı YOK"
    )


def test_unparseable_retry_after_falls_back_to_local_backoff():
    """HTTP-date biçimi yanlış ayrıştırılırsa saçma bir gecikme doğar — None daha güvenli."""
    client, _ = anthropic_client(error=FakeAPIStatusError(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"))
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    with pytest.raises(RateLimitError) as info:
        asyncio.run(provider.complete(request()))

    assert info.value.retry_after_s is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeAPITimeoutError("timed out"), AmbiguousTimeoutError),
        (FakeAPIConnectionError("connection reset"), ProviderConnectionError),
    ],
)
def test_transport_errors_are_classified_by_what_we_can_prove(error, expected):
    client, _ = anthropic_client(error=error)
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    with pytest.raises(expected):
        asyncio.run(provider.complete(request()))


# ------------------------------------------------------------ anthropic specifics


def test_anthropic_refusal_is_a_content_policy_error_not_an_empty_success():
    """Reddetme HTTP 200'dür (ölçüldü). Yalnız exception eşleyen bir adaptör bunu
    'boş ama başarılı' diye geçirirdi."""
    client, _ = anthropic_client(response=anthropic_response(text="", stop_reason="refusal", category="cyber"))
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    with pytest.raises(ContentPolicyError) as info:
        asyncio.run(provider.complete(request()))

    assert info.value.category == "cyber"


def test_anthropic_does_not_send_temperature_by_default():
    """`anthropic==1.4.0`'ın `messages.create` imzasında `temperature` YOK (ölçüldü);
    göndermek güncel modellerde 400 üretir."""
    client, calls = anthropic_client(response=anthropic_response())
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    asyncio.run(provider.complete(request(temperature=0.7)))

    assert "temperature" not in calls.kwargs[0]


def test_anthropic_sends_temperature_only_when_explicitly_enabled():
    client, calls = anthropic_client(response=anthropic_response())
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client, send_temperature=True)

    asyncio.run(provider.complete(request(temperature=0.7)))

    assert calls.kwargs[0]["temperature"] == 0.7


def test_anthropic_hoists_leading_system_turns_into_the_system_parameter():
    client, calls = anthropic_client(response=anthropic_response())
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)
    messages = (
        ChatMessage(role="system", content="Kısa cevap ver."),
        ChatMessage(role="user", content="merhaba"),
    )

    asyncio.run(provider.complete(request(messages=messages)))

    sent = calls.kwargs[0]
    assert sent["system"] == "Kısa cevap ver."
    assert [message["role"] for message in sent["messages"]] == ["user"]


def test_anthropic_rejects_a_mid_conversation_system_turn_instead_of_moving_it():
    """Başa taşımak istemi sessizce DEĞİŞTİRİRDİ; bu bir model-kapılı özelliktir."""
    client, _ = anthropic_client(response=anthropic_response())
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)
    messages = (
        ChatMessage(role="user", content="merhaba"),
        ChatMessage(role="system", content="Artık kısa konuş."),
    )

    with pytest.raises(BadRequestError, match="model-gated"):
        asyncio.run(provider.complete(request(messages=messages)))


def test_anthropic_usage_is_marked_exact():
    client, _ = anthropic_client(response=anthropic_response())
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    result = asyncio.run(provider.complete(request()))

    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 7
    assert result.usage.exact is True, "sağlayıcı saydıysa kesin"
    assert result.model == "claude-opus-5"


# --------------------------------------------------------------- openai specifics


def test_openai_sends_max_completion_tokens_and_forwards_temperature():
    client, calls = openai_client(response=openai_response())
    provider = OpenAIChatProvider(model="pinned-model", sdk=FAKE_SDK, client=client)

    asyncio.run(provider.complete(request(temperature=0.3)))

    sent = calls.kwargs[0]
    assert sent["max_completion_tokens"] == 1024
    assert "max_tokens" not in sent, "eski alan yeni akıl yürütme modellerinde reddediliyor"
    assert sent["temperature"] == 0.3
    assert sent["model"] == "pinned-model"


def test_openai_content_filter_is_a_content_policy_error():
    client, _ = openai_client(response=openai_response(finish_reason="content_filter"))
    provider = OpenAIChatProvider(model="pinned-model", sdk=FAKE_SDK, client=client)

    with pytest.raises(ContentPolicyError):
        asyncio.run(provider.complete(request()))


def test_openai_usage_is_read_from_prompt_and_completion_tokens():
    client, _ = openai_client(response=openai_response())
    provider = OpenAIChatProvider(model="pinned-model", sdk=FAKE_SDK, client=client)

    result = asyncio.run(provider.complete(request()))

    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 7)
    assert result.usage.exact is True


def test_openai_status_mapping_uses_the_same_rules():
    client, _ = openai_client(error=FakeAPIStatusError(429, retry_after="2.5"))
    provider = OpenAIChatProvider(model="pinned-model", sdk=FAKE_SDK, client=client)

    with pytest.raises(RateLimitError) as info:
        asyncio.run(provider.complete(request()))

    assert info.value.retry_after_s == 2.5
    assert info.value.provider_id == "openai"


# --------------------------------------------------------------------- streaming


def delta_event(text: str, kind: str = "text_delta"):
    return SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type=kind, text=text))


class FakeAnthropicStream:
    """Mirrors the shape the adapter uses: async context manager + event iteration."""

    def __init__(self, events, final) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def __aiter__(self):
        for event in self._events:
            yield event

    async def get_final_message(self):
        return self._final


def anthropic_streaming_client(events, final):
    calls: list[dict] = []

    def stream(**kwargs):
        calls.append(kwargs)
        return FakeAnthropicStream(events, final)

    return SimpleNamespace(messages=SimpleNamespace(stream=stream)), calls


class FakeOpenAIStream:
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def openai_chunk(text=None, *, usage=None, finish_reason=None):
    choices = []
    if text is not None or finish_reason is not None:
        choices = [SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish_reason)]
    return SimpleNamespace(choices=choices, usage=usage)


async def drain(provider, req):
    return [delta.text async for delta in provider.stream(req)]


def test_anthropic_stream_yields_only_text_deltas():
    """`anthropic==1.4.0`'da `stream.text_stream` YOK (ölçüldü) — metin ham olaydan
    süzülür ve düşünme/başlangıç olayları cevaba SIZMAMALI."""
    events = [
        SimpleNamespace(type="message_start", delta=None),
        delta_event("gizli akıl yürütme", kind="thinking_delta"),
        delta_event("Kargolar "),
        delta_event("09:00'da çıkar."),
    ]
    final = anthropic_response(text="Kargolar 09:00'da çıkar.")
    client, calls = anthropic_streaming_client(events, final)
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    chunks = asyncio.run(drain(provider, request()))

    assert chunks == ["Kargolar ", "09:00'da çıkar."]
    assert calls[0]["model"] == "claude-opus-5"
    assert provider.last_stream_usage().exact is True
    assert provider.last_stream_usage().output_tokens == 7


def test_anthropic_stream_refusal_raises_after_the_deltas():
    final = anthropic_response(text="", stop_reason="refusal", category="bio")
    client, _ = anthropic_streaming_client([delta_event("kısmi")], final)
    provider = AnthropicChatProvider(sdk=FAKE_SDK, client=client)

    with pytest.raises(ContentPolicyError):
        asyncio.run(drain(provider, request()))


def test_openai_stream_requests_usage_and_captures_it():
    chunks = [
        openai_chunk("Kargolar "),
        openai_chunk("09:00'da çıkar."),
        openai_chunk(usage=SimpleNamespace(prompt_tokens=5, completion_tokens=9)),
    ]
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        return FakeOpenAIStream(chunks)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider = OpenAIChatProvider(model="pinned-model", sdk=FAKE_SDK, client=client)

    text = asyncio.run(drain(provider, request()))

    assert text == ["Kargolar ", "09:00'da çıkar."]
    assert calls[0]["stream"] is True
    assert calls[0]["stream_options"] == {"include_usage": True}, (
        "bu olmadan son parça usage taşımaz ve her akış 'tahmin' olur"
    )
    assert provider.last_stream_usage() is not None
    assert (provider.last_stream_usage().input_tokens, provider.last_stream_usage().output_tokens) == (5, 9)
    assert provider.last_stream_usage().exact is True


def test_openai_stream_content_filter_raises():
    chunks = [openai_chunk("kısmi"), openai_chunk(finish_reason="content_filter")]

    async def create(**kwargs):
        return FakeOpenAIStream(chunks)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider = OpenAIChatProvider(model="pinned-model", sdk=FAKE_SDK, client=client)

    with pytest.raises(ContentPolicyError):
        asyncio.run(drain(provider, request()))
