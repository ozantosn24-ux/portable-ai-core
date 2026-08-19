"""`PgVectorStore.initialize()` SQL'ini VERİTABANI OLMADAN sına.

NEDEN AYRI DOSYA (2026-08-17):
`test_pgvector_integration.py` gerçek Postgres ister ve `WOZTO_REFERENCE_TEST_DATABASE_URL`
yoksa **atlanır**. Uzun süre öyleydi: env değişkeni repoda hiçbir yerde set edilmiyordu, test
hiç koşmadı ve `initialize()` içindeki bir hata (`DEFAULT '{}'` → psycopg `SQL.format()` için
KONUMSAL YER TUTUCU → `IndexError`) fark edilmeden durdu. CI yeşildi.

Bu dosya o boşluğu kapatır: sahte bir bağlantı fabrikası enjekte edip üretilen SQL'i
okur. Postgres GEREKMEZ ⇒ her push'ta koşan hızlı suite'te de çalışır, haftalık
Linux job'ını beklemez.
"""

from __future__ import annotations

import asyncio
import pathlib
from contextlib import asynccontextmanager

from wozto_ai_reference.embedding import HashEmbeddingProvider
from wozto_ai_reference.pgvector_store import PgVectorStore


class _FakeConnection:
    """Yürütülen ifadeleri KAYDEDER, hiçbir şey çalıştırmaz."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, statement) -> None:
        # `psycopg.sql.Composed`/`SQL` nesneleri burada dizeye çevrilir; çevirinin
        # KENDİSİ testin bir parçası: kaçırılmamış `{}` bu adımda patlar.
        self.statements.append(
            statement if isinstance(statement, str) else statement.as_string(None)
        )


def _render() -> list[str]:
    connection = _FakeConnection()

    @asynccontextmanager
    async def factory():
        yield connection

    store = PgVectorStore(
        database_url="postgresql://yok/yok",
        embeddings=HashEmbeddingProvider(dimensions=64),
        connection_factory=factory,
    )
    asyncio.run(store.initialize())
    return connection.statements


def test_initialize_builds_sql_without_placeholder_error():
    """REGRESYON: `DEFAULT '{}'` kaçırılmazsa `SQL.format` IndexError verir.

    Sahada tam olarak bu oldu ve tek kanıtlayıcı test atlandığı için görülmedi."""
    statements = _render()
    assert statements, "initialize() hiç ifade üretmedi"


def test_schema_has_empty_array_default_and_vector_dimensions():
    """Kaçış DOĞRU çözülmeli: SQL'de `{}` görünmeli, `{{}}` DEĞİL."""
    create = next(s for s in _render() if "CREATE TABLE" in s)
    assert "DEFAULT '{}'" in create, f"boş dizi varsayılanı bozuk:\n{create}"
    assert "{{}}" not in create, "kaçış çözülmemiş - SQL'e çift parantez sızmış"
    assert "vector(64)" in create, "embedding boyutu SQL'e geçmemiş"


def test_extension_and_indexes_are_created():
    """`initialize()` şemanın TAMAMINI kurmalı — eklenti + üç indeks."""
    joined = "\n".join(_render())
    for beklenen in (
        "CREATE EXTENSION IF NOT EXISTS vector",
        "rag_documents_tenant_source_idx",
        "USING gin (search_tsv)",
        "USING hnsw (embedding vector_cosine_ops)",
    ):
        assert beklenen in joined, f"eksik: {beklenen}"


def test_schema_records_the_embedding_space():
    """Vektorun HANGI gomme uzayinda uretildigi kalicilastirilmali.

    Kolon yoksa saglayici degisince (hash->e5, revizyon kaymasi) eski satirlar eski
    uzayda kalir ve BOYUT AYNIYSA sessizce anlamsiz benzerlik doner."""
    create = next(s for s in _render() if "CREATE TABLE" in s)
    assert "embedding_model" in create, f"gomme-uzayi kimligi semada YOK:\n{create}"


def test_model_id_is_exposed_by_both_providers():
    """Port sozlesmesi: her saglayici uzayini adlandirabilmeli."""
    from wozto_ai_reference.e5_embedding import E5EmbeddingProvider

    assert "nonsemantic" in HashEmbeddingProvider(dimensions=64).model_id, (
        "hash saglayicisi anlamsal OLMADIGINI kimliginde soylemeli"
    )
    pinned = E5EmbeddingProvider(encoder=object(), revision="abc123").model_id
    # ⚠️ 2026-08-17: varsayilan artik PINLI (Fable KARAR 1) ⇒ pinsiz kimligi gormek icin
    # `revision=None` ACIKCA verilmeli. Once burada varsayilan kullaniliyordu ve
    # varsayilan pinlenince test kirmizi verdi — davranis DOGRUYDU, test varsayimi eskiydi.
    unpinned = E5EmbeddingProvider(encoder=object(), revision=None).model_id
    assert pinned.endswith("@abc123")
    assert unpinned.endswith("@unpinned"), "pinsizlik kimlikte GORUNUR olmali"
    assert pinned != unpinned, "revizyon degisince uzay kimligi de degismeli"


def test_default_revision_is_pinned():
    """⛔ Varsayilan PINSIZ birakilamaz (Fable KARAR 1, 2026-08-17).

    Pin yokken farkli tarihlerde farkli gercek snapshot'la gomulmus satirlar AYNI uzay
    kimligini tasir ⇒ `EmbeddingSpaceMismatch` yapisal olarak ATESLENEMEZ. Yani pinsizlik,
    sessiz-kalite-kaybini yakalayan mekanizmayi devre disi birakir.
    """
    from wozto_ai_reference.e5_embedding import DEFAULT_REVISION, E5EmbeddingProvider

    assert DEFAULT_REVISION, "varsayilan revizyon PINLI olmali"
    assert not E5EmbeddingProvider(encoder=object()).model_id.endswith("@unpinned")


def test_search_sql_has_no_stray_percent():
    """psycopg yer-tutucu tuzagi: SQL dizesinde `%s` DISINDA yuzde isareti olamaz.

    psycopg dizenin TAMAMINI yer tutucu icin tarar — SQL YORUMLARI DAHIL. Yuzde isareti
    + rakam ikilisi `ProgrammingError: only '%s','%b','%t' are allowed as placeholders`
    firlatir ve sorgu HIC kosmaz.

    2026-08-17'de bu bir yorumdaki "yuzde 3" yazimindan dogdu; uc CI kosusu bosa gitti
    ve log okunamadigi icin iki kez YANLIS teshis konuldu. Kural artik mekanik.
    """
    import re

    from wozto_ai_reference import pgvector_store

    source = pathlib.Path(pgvector_store.__file__).read_text(encoding="utf-8")
    start = source.index('        sql = """')
    end = source.index('"""', start + 16)
    sql = source[start:end]
    stray = [m.group(0) for m in re.finditer(r"%(?!s)(.{0,20})", sql)]
    assert not stray, (
        "SQL dizesinde `%s` disinda yuzde isareti var; psycopg bunu yer tutucu sanip "
        f"sorguyu calistirmadan patlatir: {stray}"
    )
