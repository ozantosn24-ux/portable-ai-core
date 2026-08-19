"""Gerçek anlamsal gömme: `intfloat/multilingual-e5-*` ailesi için adaptör.

Çekirdeğin bağımlılıkları BİLİNÇLİ olarak hafif (fastapi/psycopg/pydantic/uvicorn).
`torch` + `transformers` buraya girmez; bu modül **opsiyonel extra**dır:

    pip install -e ".[embeddings]"

Kurulu değilse import anında değil, ilk KULLANIMDA anlaşılır bir hata verir — böylece
paketi yalnız `HashEmbeddingProvider` ile kullananlar etkilenmez.

## Neden ayrı bir adaptör (ve neden `kind` şart)

e5 **asimetrik** bir modeldir: sorgu `"query: "`, belge `"passage: "` önekiyle
kodlanır. Önek yanlış olursa model yine bir vektör üretir — hata vermez — ama geri
getirme kalitesi ölçülebilir biçimde düşer. Bu, sessiz kalite kaybı sınıfıdır; o
yüzden `EmbeddingProvider` portuna `kind` eklendi ve burada ZORUNLU olarak uygulanır.

## Referans

Gömme tekniği, ayrı bir dahili baseline aracıyla birebir aynıdır ve orada donmuş bir
qrel'e karşı ölçülmüştür: **attention-mask mean pooling + L2 normalize**,
`query: ` / `passage: ` önekleri. Kosinüs benzerliği L2-normalize edilmiş vektörlerde
nokta çarpımına indiği için `pgvector`'ın `<=>` operatörüyle tutarlıdır.

⚠️ SINIR: bu modül modeli İNDİRİR (ilk kullanımda ~470 MB, `multilingual-e5-small`).
Ağsız/çevrimdışı çalışması gereken kurulumlarda kullanılmaz — çekirdeğin varsayılanı
hâlâ `HashEmbeddingProvider`'dır.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from .ports import EmbeddingKind

DEFAULT_MODEL = "intfloat/multilingual-e5-small"
# ⚠️ REVIZYON PINI (Fable bulgusu 2026-08-17). `from_pretrained(name)` HF `main`i ceker;
# model repo'su guncellenirse GOMME UZAYI KAYAR ve kalicilastirilmis vektorler sessizce
# baska bir uzayla karsilastirilir. Bu repo kind, kubectl ve postgres imajini PINLIYOR —
# modeli pinsiz birakmak kendi konvansiyonuyla celisiyordu.
# ✅ PINLENDI 2026-08-17. Deger HF API'sinden CANLI alindi (uydurulmadi):
#   https://huggingface.co/api/models/intfloat/multilingual-e5-small -> sha
#   lastModified: 2026-04-02T02:16:05Z
# ⚠️ ASIL GEREKCE "HF degisir mi" OLASILIGI DEGIL: pin yokken farkli tarihlerde farkli
# gercek snapshot'la gomulmus satirlar AYNI uzay kimligini tasir (`_space_id` ->
# `embedding_model` kolonu) ⇒ `EmbeddingSpaceMismatch` yapisal olarak ATESLENEMEZ.
# Yani kolonun yakalamak icin kuruldugu sessiz-kalite-kaybi, tam da pin yoklugunda
# tespit edilemez hale geliyordu.
# ⛔ BUNU GUNDELIK BUMP'LAMA: her degisiklik YENI gomme uzayidir ⇒ kalici dagitimda
#    reindex sart ve eski sonuc serileri karsilastirilamaz hale gelir.
DEFAULT_REVISION: str | None = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
_PREFIX: dict[str, str] = {"query": "query: ", "passage": "passage: "}

# ⚠️ BLOKER DUZELTMESI (Fable, 2026-08-17) — KESME ASIMETRISI. Onceki varsayilan 128
# TOKEN'di; `ingest.markdown_chunks` ise varsayilan **1200 KARAKTER**lik chunk uretiyor
# (~300 token). Yani yogun bacak her chunk'in KUYRUGUNU hic gormuyor, `search_tsv` ise
# metnin tamamini indeksliyordu. Cevabi chunk sonunda olan her vaka dense'i HAKSIZ
# cezalandirirdi ve dense/lexical karsilastirmasi yapisal olarak tarafli olurdu.
# 512, e5 ailesinin destekledigi ust sinirdir ve 1200 karakteri rahat kapsar.
# ⛔ Bunu dusurursen chunk boyutunu da dusur; ikisi AYRI ayarlanirsa asimetri geri doner.
DEFAULT_MAX_LENGTH = 512


class E5EmbeddingProvider:
    """`EmbeddingProvider` portunun asimetrik, gerçek-model uygulaması."""

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_MODEL,
        revision: str | None = DEFAULT_REVISION,
        dimensions: int = 384,
        max_length: int = DEFAULT_MAX_LENGTH,
        batch_size: int = 16,
        encoder: Any | None = None,
    ) -> None:
        """`encoder` yalnız TEST içindir: sözleşmeyi model indirmeden sınamak için
        enjekte edilir. Üretimde `None` bırakılır ve `transformers` tembel yüklenir."""
        if dimensions < 8 or dimensions > 4096:
            raise ValueError("dimensions must be between 8 and 4096")
        if max_length < 8:
            raise ValueError("max_length must be at least 8")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._model_name = model_name
        self._revision = revision
        self._dimensions = dimensions
        self._max_length = max_length
        self._batch_size = batch_size
        self._encoder = encoder

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def revision(self) -> str | None:
        return self._revision

    @property
    def model_id(self) -> str:
        """Kalicilastirilan vektorlerin YANINA yazilan gomme-uzayi kimligi.

        Pin yoksa `@unpinned` yazar: kimlik YINE de kaydedilir ve saglayici degisince
        uyusmazlik yakalanir, ama pinsizligin kendisi de kayitta GORUNUR kalir."""
        return f"{self._model_name}@{self._revision or 'unpinned'}"

    @staticmethod
    def prefix_for(kind: EmbeddingKind) -> str:
        """Öneki tek yerde tut: yanlış önek sessiz kalite kaybıdır, testi de buna bağlı."""
        try:
            return _PREFIX[kind]
        except KeyError:
            raise ValueError(f"unknown embedding kind: {kind!r}") from None

    async def embed(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind = "passage",
    ) -> Sequence[Sequence[float]]:
        prefix = self.prefix_for(kind)
        if not texts:
            return []
        prefixed = [f"{prefix}{text}" for text in texts]
        # ⚠️ SONUC self'e YAZILMALI (Fable hakemligi, 2026-08-17). Onceki hali
        # `encoder = self._encoder or _load_encoder(...)` idi: donen encoder hicbir
        # yere baglanmiyordu ve `_load_encoder` cache'siz ⇒ HER `embed()` cagrisi
        # modeli SIFIRDAN yukluyordu. Agirlik taramasinda 11 agirlik x 150 sorgu =
        # 1650 arama ⇒ 1650 model yuklemesi (+ her seferinde HF hub etag istegi).
        # Ilk kosuda gorunmedi cunku ValueError embed'den ONCE atilmisti; torch hic
        # import edilmemis, model hic inmemisti.
        if self._encoder is None:
            self._encoder = _load_encoder(self._model_name, self._revision)
        encoder = self._encoder
        # Model çağrısı CPU-yoğun ve senkron; olay döngüsünü BLOKLAMAMASI için
        # ayrı bir iş parçacığına alınır (port async sözleşmesi korunur).
        return await asyncio.to_thread(
            encoder.encode,
            prefixed,
            max_length=self._max_length,
            batch_size=self._batch_size,
        )


def _load_encoder(model_name: str, revision: str | None = None) -> Any:  # pragma: no cover
    try:
        from ._e5_torch import TorchE5Encoder
    except ImportError as exc:  # torch/transformers kurulu değil
        raise RuntimeError(
            "E5EmbeddingProvider `torch` ve `transformers` ister. "
            'Kurulum: pip install -e ".[embeddings]" — ya da çevrimdışı kalmak '
            "istiyorsanız HashEmbeddingProvider kullanın (anlamsal DEĞİLDİR)."
        ) from exc
    return TorchE5Encoder(model_name, revision=revision)
