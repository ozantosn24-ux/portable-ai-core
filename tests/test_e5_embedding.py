"""`E5EmbeddingProvider` sözleşmesi — model İNDİRMEDEN sınanır.

Asıl risk şu: e5 asimetriktir (`query: ` / `passage: `) ve yanlış önekle model YİNE
bir vektör üretir, hata VERMEZ; yalnız geri getirme kalitesi düşer. Yani bu, sessiz
kalite kaybı sınıfıdır ve ancak sözleşmeyi doğrudan sınayarak yakalanır.

Sahte bir encoder enjekte edilir ⇒ torch/transformers gerekmez, hızlı suite'te koşar.
"""

from __future__ import annotations

import asyncio

import pytest

from wozto_ai_reference.e5_embedding import DEFAULT_MODEL, E5EmbeddingProvider


class _SpyEncoder:
    """Modeli taklit etmez; yalnız KENDİSİNE NE VERİLDİĞİNİ kaydeder."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.calls: list[dict] = []

    def encode(self, texts, *, max_length: int, batch_size: int):
        self.seen.extend(texts)
        self.calls.append({"max_length": max_length, "batch_size": batch_size})
        return [[0.1] * 384 for _ in texts]


def _provider(encoder: _SpyEncoder, **kwargs) -> E5EmbeddingProvider:
    return E5EmbeddingProvider(encoder=encoder, **kwargs)


def test_query_and_passage_get_different_prefixes():
    """ASIL KAPI: sorgu ve belge AYNI öneki almamalı."""
    spy = _SpyEncoder()
    provider = _provider(spy)
    asyncio.run(provider.embed(["kargo ne zaman çıkar"], kind="query"))
    asyncio.run(provider.embed(["Kargolar her sabah çıkar."], kind="passage"))
    assert spy.seen[0].startswith("query: "), spy.seen[0]
    assert spy.seen[1].startswith("passage: "), spy.seen[1]
    assert spy.seen[0] != spy.seen[1]


def test_default_kind_is_passage():
    """Port varsayılanı `passage`: mevcut çağrı yerleri belge gömüyordu, sessizce
    sorgu önekine kaymamalı."""
    spy = _SpyEncoder()
    asyncio.run(_provider(spy).embed(["metin"]))
    assert spy.seen == ["passage: metin"]


def test_prefix_is_defined_in_exactly_one_place():
    """Önek mantığı tek yerde: iki kopya olursa biri sessizce eskir."""
    assert E5EmbeddingProvider.prefix_for("query") == "query: "
    assert E5EmbeddingProvider.prefix_for("passage") == "passage: "


def test_unknown_kind_is_rejected_loudly():
    """Bilinmeyen `kind` sessizce `passage`a DÜŞMEMELİ — sessiz düşüş, tam da
    önlemeye çalıştığımız kalite kaybını üretir."""
    with pytest.raises(ValueError, match="unknown embedding kind"):
        E5EmbeddingProvider.prefix_for("belge")  # type: ignore[arg-type]


def test_empty_input_does_not_call_the_model():
    spy = _SpyEncoder()
    assert asyncio.run(_provider(spy).embed([])) == []
    assert spy.seen == [], "bos girdide model cagrilmamali"


def test_batch_and_length_settings_reach_the_encoder():
    spy = _SpyEncoder()
    asyncio.run(_provider(spy, max_length=64, batch_size=4).embed(["a", "b"]))
    assert spy.calls == [{"max_length": 64, "batch_size": 4}]


def test_invalid_construction_is_rejected():
    for kwargs in ({"dimensions": 4}, {"max_length": 2}, {"batch_size": 0}):
        with pytest.raises(ValueError):
            E5EmbeddingProvider(encoder=_SpyEncoder(), **kwargs)


def test_missing_optional_dependency_fails_with_actionable_message():
    """torch kurulu değilse: import anında değil, KULLANIMDA ve NE YAPILACAĞINI
    söyleyen bir hata. Aksi hâlde paketi hash-embedding ile kullananlar kırılırdı."""
    with pytest.raises(RuntimeError, match=r"embeddings"):
        _force_missing()


def _force_missing():
    """`_load_encoder`ı bağımlılık YOKMUŞ gibi koştur."""
    import builtins

    from wozto_ai_reference import e5_embedding

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.endswith("_e5_torch"):
            raise ImportError("simulated: torch yok")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import
    try:
        e5_embedding._load_encoder("x")
    finally:
        builtins.__import__ = real_import


def test_model_name_default_is_the_measured_one():
    """Referans ölçüm bu modelle yapıldı; sessizce başka modele kaymasın."""
    assert DEFAULT_MODEL == "intfloat/multilingual-e5-small"
    assert _provider(_SpyEncoder()).model_name == DEFAULT_MODEL


def test_max_length_covers_the_ingest_chunk_size():
    """KESME ASIMETRISI regresyonu.

    `ingest.markdown_chunks` varsayilan 1200 KARAKTER chunk uretir. e5'in `max_length`i
    TOKEN cinsindendir; cok kucuk olursa yogun bacak chunk'in kuyrugunu hic gormez,
    `search_tsv` ise tamamini indeksler => dense/lexical karsilastirmasi TARAFLI olur.
    Kaba ama saglam ust sinir: en kotu durumda ~1 token >= 2 karakter.
    """
    from wozto_ai_reference.e5_embedding import DEFAULT_MAX_LENGTH
    from wozto_ai_reference.ingest import markdown_chunks

    # `inspect.signature`: `max_chars` keyword-only oldugu icin `__defaults__`ta DEGIL
    # `__kwdefaults__`ta durur. Imzadan okumak ikisine de dayaniklidir.
    import inspect

    chunk_chars = inspect.signature(markdown_chunks).parameters["max_chars"].default
    assert DEFAULT_MAX_LENGTH * 2 >= chunk_chars, (
        f"max_length={DEFAULT_MAX_LENGTH} token, chunk={chunk_chars} karakter: "
        "yogun bacak chunk kuyrugunu goremeyebilir (kesme asimetrisi)"
    )


def test_indexed_text_is_defined_once_for_both_legs():
    """GIRDI SIMETRISI regresyonu: iki bacak da AYNI metni gormeli."""
    from wozto_ai_reference.pgvector_store import INPUT_RECIPE, indexed_text

    class _D:
        title, section, content = "Bas", "Bolum", "govde"

    rendered = indexed_text(_D())
    for parca in ("Bas", "Bolum", "govde"):
        assert parca in rendered, f"{parca} gomulen metinde yok"
    assert "title" in INPUT_RECIPE and "content" in INPUT_RECIPE
