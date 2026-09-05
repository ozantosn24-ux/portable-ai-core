"""Provider-neutral value contracts for the LLM gateway.

These types deliberately carry no vendor object. A `Completion` produced by an
Anthropic adapter and one produced by an OpenAI adapter are the same shape, so the
router, the ledger and the caller never branch on which provider answered.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChatRole = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    """One turn handed to a provider."""

    model_config = ConfigDict(frozen=True)

    role: ChatRole
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    """A provider-independent chat request plus the two flags routing depends on.

    `idempotency_key` and `idempotent` are NOT the same knob and must not be merged:

    * `idempotency_key` identifies *this* logical request so a provider that supports
      idempotency keys can collapse a duplicate delivery into one execution.
    * `idempotent` says whether **the caller** can absorb the result arriving twice.
      Pure generation is idempotent. A request whose result the caller has already
      wired to a side effect (an email queued, a row written, money moved) is not,
      even when the provider itself would happily replay it.

    The gateway may retry or fail over a request only while it can prove the first
    attempt never landed; when it cannot prove that and `idempotent` is False, it
    raises instead. Details in `errors.AmbiguousOutcomeError`.
    """

    model_config = ConfigDict(frozen=True)

    messages: tuple[ChatMessage, ...] = Field(min_length=1)
    # Bir İPUCU, emir değil: her sağlayıcının model kimliği farklıdır ve adaptör
    # kendi varsayılanına düşebilir. Hangi modelin GERÇEKTEN koştuğu
    # `Completion.model` alanındadır — istek alanından okunmaz.
    model: str | None = None
    max_tokens: int = Field(default=1024, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    idempotency_key: str = Field(min_length=1)
    idempotent: bool = True
    # Ledger korelasyonu; verilmezse yönlendirici üretir.
    request_id: str | None = None


class Usage(BaseModel):
    """Token accounting that remembers whether anyone actually counted.

    `exact` is True only when a provider returned the counts. Anything the gateway
    derived itself (a character-based estimate, a streamed response whose provider
    reported nothing) stays False, and `__add__` propagates that: a sum containing one
    estimate is an estimate. Without this flag an approximate number silently becomes
    the input to a cost report or a quota decision and looks authoritative.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    exact: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        if not isinstance(other, Usage):  # pragma: no cover - defensive
            return NotImplemented
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            # ⭐ Tek tahmin toplamı da tahmin yapar. `or` yazılırsa kesin olmayan bir
            # sayı "kesin" etiketiyle fatura/kota kararına girer.
            exact=self.exact and other.exact,
        )


class Completion(BaseModel):
    """A finished, non-streamed answer plus who produced it and at what cost."""

    model_config = ConfigDict(frozen=True)

    text: str
    provider_id: str = Field(min_length=1)
    model: str
    usage: Usage = Field(default_factory=Usage)
    # Kaç sağlayıcı denemesi harcandı (yeniden denemeler + failover dahil). 1 = ilk
    # denemede döndü. Bu sayı ledger satır sayısıyla karşılaştırılabilir olmalıdır.
    # 0 GEÇERLİDİR ve tek bir anlamı vardır: hiçbir sağlayıcı ÇAĞRILMADI (tüm devre
    # kesicileri açıkken tasarruf-modu şablonu). `ge=1` yazmak bu durumu "1 deneme
    # yapıldı" diye kaydettirirdi — defterin yalan söylediği tek satır o olurdu.
    attempts: int = Field(default=1, ge=0)
    # Defter korelasyonu. `attempts=3` gören bir çağıran "hangi üç satır?" diye
    # soramıyorsa defterin varlığı ona bir işe yaramaz: anahtar
    # `AttemptRecord.request_id`'dir ve cevabın kendisi onu taşımalıdır.
    # ⚠️ `None` doğru varsayılandır: adaptörler bu id'yi ÜRETMEZ (o yönlendiricinin
    # korelasyon kimliğidir) ve tasarruf-modu şablonu da bilmez. Damgayı yönlendirici
    # vurur — `Completion` yönlendiriciden çıkarken alan DOLUDUR.
    request_id: str | None = None


class TextDelta(BaseModel):
    """One streamed fragment, tagged with the provider that produced it."""

    model_config = ConfigDict(frozen=True)

    provider_id: str = Field(min_length=1)
    text: str


class StreamRestarted(BaseModel):
    """The stream the consumer has been reading is dead; everything so far is void.

    Emitted only when partial deltas already reached the consumer. `discarded_chars`
    is exactly how many characters the consumer must throw away — a consumer that
    resets its buffer on this event ends up holding one provider's complete text.
    """

    model_config = ConfigDict(frozen=True)

    from_provider: str = Field(min_length=1)
    to_provider: str = Field(min_length=1)
    discarded_chars: int = Field(ge=0)


class StreamEnd(BaseModel):
    """Terminal stream event carrying the authoritative completion.

    `completion.text` is always exactly one provider's full output. It is never the
    concatenation of a failed provider's partial text and the replacement's text —
    see `router.FailoverRouter.stream` for why that rule is load-bearing.
    """

    model_config = ConfigDict(frozen=True)

    completion: Completion


type StreamEvent = TextDelta | StreamRestarted | StreamEnd
