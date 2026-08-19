"""PostgreSQL/pgvector document store with tenant and ACL filtering in SQL."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL, Literal

from .domain import Document, Principal, RetrievalHit
from .ports import EmbeddingProvider


class AsyncConnectionContext(Protocol):
    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


ConnectionFactory = Callable[[], AsyncConnectionContext]


def vector_literal(values: Sequence[float], *, dimensions: int) -> str:
    if len(values) != dimensions:
        raise ValueError(f"expected {dimensions} embedding dimensions, got {len(values)}")
    normalized: list[str] = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("embedding values must be finite")
        normalized.append(format(number, ".12g"))
    return f"[{','.join(normalized)}]"


# ⚠️ BLOKER DUZELTMESI (Fable, 2026-08-17) — GIRDI SIMETRISI.
# Leksik bacak `to_tsvector('simple', title || section || content)` indeksliyordu, yogun
# bacak ise YALNIZ `content` gomuyordu. Basliktaki anahtar kelime lexical'e BEDAVA
# geliyordu ⇒ dense/lexical karsilastirmasi yapisal olarak taraflıydi. Iki bacak artik
# AYNI metni gorur.
def indexed_text(document: Document) -> str:
    """Her iki bacagin da gordugu metin. TEK yerde tanimli: iki kopya olursa
    asimetri sessizce geri doner ve karsilastirma yine taraflı olur."""
    return f"{document.title} {document.section} {document.content}"


# Girdi tarifi de GOMME UZAYININ parcasidir: ayni model, farkli girdi => farkli vektorler.
# `model_id` yalniz modeli adlandirir; tarif degisirse eski satirlar sessizce uyumsuz
# kalirdi. Bu yuzden kalicilastirilan kimlik ikisinin BILESIMIDIR.
INPUT_RECIPE = "title+section+content/v1"

# ⚠️ LEKSIK CONFIG (Fable karari 2026-08-17, KARAR 2). Cekirdek varsayilani `simple`
# kalir — bu referans paket DIL-NOTRDUR. Turkce urun hatti (Wozto: WhatsApp/mail
# sorulari uzerinde RAG) `turkish` gecer: Turkce aglutinatiftir, `simple`da
# "iade" != "iadeler" != "iadesi" ⇒ leksik bacagin isinin yarisi STEM'dir; ayrica
# `simple` stopword ATMAZ ve soru kelimeleri gurultu uretir.
# ⛔ `turkish` bile bu bacagi BM25 YAPMAZ: stok PG FTS'te IDF YOKTUR.
# 🔑 TEK KAYNAK: hem `search_tsv` generated column'u hem sorgu tarafi BU degerden
#    beslenir. Ikisini ayri yerde tanimlamak, kapatilan asimetri hatasini geri getirir.
DEFAULT_TEXT_SEARCH_CONFIG = "simple"
ALLOWED_TEXT_SEARCH_CONFIGS = frozenset({"simple", "turkish", "english"})


class EmbeddingSpaceMismatch(RuntimeError):
    """Kayitli vektorler aktif saglayicinin uzayinda DEGIL.

    Sessizce bos sonuc donmek yerine bunu yukseltiyoruz: bos sonuc "eslesme yok" diye
    okunur ve gercek sebep (reindex gerekiyor) aylarca gorunmez kalir.
    """


class PgVectorStore:
    """pgvector destekli, tenant ve ACL farkindalikli hibrit depo."""


    """Implements both DocumentStore and SearchProvider against one local database."""

    def __init__(
        self,
        *,
        database_url: str,
        embeddings: EmbeddingProvider,
        connection_factory: ConnectionFactory | None = None,
        vector_weight: float = 0.7,
        text_search_config: str = DEFAULT_TEXT_SEARCH_CONFIG,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url must not be empty")
        if not 0.0 <= vector_weight <= 1.0:
            raise ValueError("vector_weight must be between 0 and 1")
        if text_search_config not in ALLOWED_TEXT_SEARCH_CONFIGS:
            # ⚠️ BEYAZ LISTE SART: bu deger DDL'e (generated column) LITERAL olarak
            # giriyor, yer tutucuyla gecemez ⇒ dogrulanmazsa enjeksiyon yuzeyi olur.
            raise ValueError(
                f"text_search_config must be one of {sorted(ALLOWED_TEXT_SEARCH_CONFIGS)}"
            )
        self._database_url = database_url
        self._embeddings = embeddings
        self._connection_factory = connection_factory or self._connect
        self._vector_weight = vector_weight
        self._text_search_config = text_search_config

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        connection = await psycopg.AsyncConnection.connect(self._database_url, row_factory=dict_row)
        try:
            yield connection
        finally:
            await connection.close()

    @property
    def _space_id(self) -> str:
        """Kalicilastirilan GOMME UZAYI kimligi = model + girdi tarifi.

        Ikisi de uzayi belirler: ayni modelle farkli metin gomersen vektorler
        karsilastirilamaz. `model_id` tek basina yetseydi, tarif degistiginde eski
        satirlar SESSIZCE uyumsuz kalirdi -- kapatmaya calistigimiz arizanin aynisi."""
        # ⚠️ LEKSIK CONFIG DE KIMLIGE GIRER (Fable KARAR 2, 2026-08-17): `search_tsv`
        # GENERATED bir kolondur ve config'e baglidir. Config degisince ESKI satirlarin
        # tsv'si ESKI config'le uretilmis kalir (`CREATE TABLE IF NOT EXISTS` onlari
        # yeniden yazmaz) ⇒ sorgu `turkish`, veri `simple` olur ve leksik bacak sessizce
        # yanlis calisir. Kimlige koymak bu uyusmazligi `EmbeddingSpaceMismatch` ile
        # GORUNUR kilar; koymamak, kapatmaya calistigimiz sessizligin leksik ikizidir.
        return f"{self._embeddings.model_id}|{INPUT_RECIPE}|tsc={self._text_search_config}"

    async def initialize(self) -> None:
        dimensions = self._embeddings.dimensions
        if dimensions < 1 or dimensions > 16_000:
            raise ValueError("embedding dimensions must be between 1 and 16000")
        create_table = SQL(
            """
            CREATE TABLE IF NOT EXISTS rag_documents (
                tenant_id text NOT NULL,
                source_document_id text NOT NULL,
                document_id text NOT NULL,
                version text NOT NULL,
                title text NOT NULL,
                section text NOT NULL,
                source_uri text NOT NULL,
                content text NOT NULL,
                content_hash text NOT NULL,
                -- '{{}}' = PostgreSQL BOŞ DİZİ literali. Süslü parantezler ÇİFTLENMEK
                -- ZORUNDA: bu dize `psycopg.sql.SQL(...).format(...)` içinden geçiyor ve
                -- orada tek süslü parantez çifti KONUMSAL YER TUTUCUDUR (str.format
                -- ile aynı kural). ⚠️ Bu yorumun KENDİSİ de aynı kurala tabidir:
                -- ilk yazımında buraya kaçırılmamış bir parantez çifti koyup aynı
                -- hatayı YENİDEN ürettim; testler yakaladı (2026-08-17).
                -- Kaçırılmazsa `IndexError: tuple index out of range` ile patlar —
                -- 2026-08-17'de sahada böyleydi ve HİÇ FARK EDİLMEMİŞTİ, çünkü bu kodu
                -- çalıştıran tek test `WOZTO_REFERENCE_TEST_DATABASE_URL` yokluğunda
                -- ATLANIYORDU. Yani `initialize()` bugüne kadar hiç koşmamıştı.
                acl_roles text[] NOT NULL DEFAULT '{{}}',
                embedding vector({dimensions}) NOT NULL,
                -- Vektorun HANGI gomme uzayinda uretildigi. Fable bulgusu 2026-08-17:
                -- saglayici degisince (hash->e5, e5-small->base, model revizyonu kayinca)
                -- eski satirlar eski uzayda kalir. Boyut FARKLIYSA gurultuyle patlar; ama
                -- BOYUT AYNIYSA sessizce anlamsiz benzerlik doner -- projenin adini koydugu
                -- "sessiz kalite kaybi" sinifi. Kimlik yazilmadan bu tespit EDILEMEZ.
                embedding_model text NOT NULL DEFAULT 'unknown',
                search_tsv tsvector GENERATED ALWAYS AS (
                    to_tsvector({text_config}, title || ' ' || section || ' ' || content)
                ) STORED,
                PRIMARY KEY (tenant_id, document_id, version)
            )
            """
        ).format(
            dimensions=Literal(dimensions),
            # DDL'e literal olarak girer; deger `__init__`te beyaz listeden gecmistir.
            text_config=Literal(self._text_search_config),
        )
        async with self._connection_factory() as connection:
            async with connection.transaction():
                await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
                await connection.execute(create_table)
                await connection.execute(
                    "CREATE INDEX IF NOT EXISTS rag_documents_tenant_source_idx "
                    "ON rag_documents (tenant_id, source_document_id)"
                )
                await connection.execute(
                    "CREATE INDEX IF NOT EXISTS rag_documents_search_idx "
                    "ON rag_documents USING gin (search_tsv)"
                )
                await connection.execute(
                    "CREATE INDEX IF NOT EXISTS rag_documents_embedding_hnsw_idx "
                    "ON rag_documents USING hnsw (embedding vector_cosine_ops)"
                )

    async def upsert(self, document: Document) -> None:
        vectors = await self._embeddings.embed([indexed_text(document)])
        source_document_id = document.document_id.split("::", 1)[0]
        async with self._connection_factory() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM rag_documents WHERE tenant_id = %s AND document_id = %s",
                    (document.tenant_id, document.document_id),
                )
                await self._insert(connection, document, source_document_id=source_document_id, vector=vectors[0])

    async def delete(self, *, tenant_id: str, document_id: str, version: str) -> None:
        async with self._connection_factory() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM rag_documents WHERE tenant_id = %s AND document_id = %s AND version = %s",
                    (tenant_id, document_id, version),
                )

    async def replace_source(
        self,
        *,
        tenant_id: str,
        source_document_id: str,
        documents: Sequence[Document],
    ) -> None:
        if not documents:
            raise ValueError("replace_source requires at least one chunk")
        expected_prefix = f"{source_document_id}::"
        if any(
            document.tenant_id != tenant_id or not document.document_id.startswith(expected_prefix)
            for document in documents
        ):
            raise ValueError("all chunks must match the tenant and source document")
        if len({document.document_id for document in documents}) != len(documents):
            raise ValueError("chunk document_ids must be unique")
        vectors = await self._embeddings.embed(
            [indexed_text(document) for document in documents]
        )
        if len(vectors) != len(documents):
            raise ValueError("embedding provider returned an unexpected vector count")

        async with self._connection_factory() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM rag_documents WHERE tenant_id = %s AND source_document_id = %s",
                    (tenant_id, source_document_id),
                )
                for document, vector in zip(documents, vectors, strict=True):
                    await self._insert(
                        connection,
                        document,
                        source_document_id=source_document_id,
                        vector=vector,
                    )

    async def _insert(
        self,
        connection: Any,
        document: Document,
        *,
        source_document_id: str,
        vector: Sequence[float],
    ) -> None:
        await connection.execute(
            """
            INSERT INTO rag_documents (
                tenant_id, source_document_id, document_id, version, title, section,
                source_uri, content, content_hash, acl_roles, embedding, embedding_model
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
            """,
            (
                document.tenant_id,
                source_document_id,
                document.document_id,
                document.version,
                document.title,
                document.section,
                document.source_uri,
                document.content,
                document.content_hash,
                sorted(document.acl_roles),
                vector_literal(vector, dimensions=self._embeddings.dimensions),
                self._space_id,
            ),
        )

    async def search(self, *, principal: Principal, query: str, limit: int) -> Sequence[RetrievalHit]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        vectors = await self._embeddings.embed([query], kind="query")
        embedding = vector_literal(vectors[0], dimensions=self._embeddings.dimensions)
        lexical_weight = 1.0 - self._vector_weight
        sql = """
            WITH authorized AS (
                SELECT *,
                    GREATEST(0.0, 1.0 - (embedding <=> %s::vector)) AS vector_score,
                    -- ⚠️ BLOKER DUZELTMESI (Fable hakemligi, 2026-08-17) — LEKSIK BACAK OLUYDU.
                    -- `plainto_tsquery` terimleri AND'ler. `simple` config stopword ATMAZ,
                    -- yani soru kelimeleri de ("hangi", "nedir", "kac") sorguya girer ve
                    -- paragrafta GECMEZ ⇒ cover bulunamaz ⇒ `ts_rank_cd` 0 doner.
                    -- OLCULDU (150 XQuAD-tr sorgusu, 240 paragraf): sorgunun TUM
                    -- token'larini iceren belge olan sorgu sayisi = 0/150. Yani leksik
                    -- bacak hicbir cift icin ateslenmiyordu; w>0'in tamami dense-only ile
                    -- AYNI sonucu veriyor, w=0 ise alfabetik tie-break piyangosu oluyordu.
                    -- Kosu YESIL biter ve "hibrit iki bacagi da gecmedi" diye GECERSIZ bir
                    -- olcum yayinlanirdi.
                    -- ⛔ Bu bir DENEY sorunu DEGIL, URUN sorunu: gercek bir kullanici sorusu
                    -- ("kac gunde teslim ediyorsunuz") da hicbir paragrafla AND eslesmez ⇒
                    -- paket "hibrit" diyip fiilen dense-only calisiyordu.
                    -- ⛔ AND'in bilincli bir karar OLMADIGI kayitli: `test_pgvector_integration.py:106-117`
                    -- yorumu AND'i savunmuyor, ona UYUYOR — fixture leksik bacak ateslensin
                    -- diye IKI KEZ yeniden kurgulanmis. Bacagin atesle(n)mesi icin belge
                    -- elle kurgulanmak zorundaysa, sahada hic ateslenmiyor demektir.
                    -- COZUM: postgres'in KENDI ayristirmasini kullanip AND'i OR'a cevir.
                    -- Sorguyu elde string olarak kurmuyoruz (enjeksiyon/kacis riski);
                    -- `plainto_tsquery` lexeme'leri uretir, aralarina yalniz `&` koyar
                    -- (phrase operatoru uretmez), o yuzden `&`->`|` degisimi guvenlidir.
                    -- Bos sorguda bos tsquery kalir ve skor 0 doner (davranis korunur).
                    -- ⚠️ SINIR (Fable 2. tur, 2026-08-17): "guvenli" KOSULLUDUR. Default
                    -- parser'in `url`/`url_path` token tipleri query-string'li bir URL'yi
                    -- TEK lexeme yapar; boyle bir lexeme'in ICINDE `&` bulunabilir ve
                    -- `replace` onu da cevirir. Cast PATLAMAZ, o tek terim sessizce bozulur
                    -- (zarif bozulma). Bu veri setinde olculdu: 150 sorgunun 1'inde `&` var
                    -- ("McKinsey & Company"), orada `&` bagimsiz sembol => lexeme'e girmez;
                    -- URL/email-benzeri sorgu 0/150. Yani BU dondurulmus kume icin guvenli;
                    -- URL tasiyan sorgu bekleniyorsa tsquery elde kurulmalidir.
                    -- ⚠️ BU DIZEDE YUZDE ISARETI YOK — ustteki `scaled` blogundaki kural
                    -- burada da gecerli (psycopg tum dizeyi yer tutucu icin tarar).
                    -- ⚠️ CONFIG YER TUTUCUYLA GECER (`::regconfig`) ve DDL'deki
                    -- `to_tsvector` ile AYNI degerden beslenir — ikisi ayrisirsa sorgu
                    -- bir dilde, veri baska dilde analiz edilir ve bacak sessizce bozulur.
                    ts_rank_cd(
                        search_tsv,
                        replace(plainto_tsquery(%s::regconfig, %s)::text, '&', '|')::tsquery
                    ) AS lexical_score
                FROM rag_documents
                WHERE tenant_id = %s
                  AND (cardinality(acl_roles) = 0 OR acl_roles && %s::text[])
                  -- Farkli gomme uzayindaki satirlar KARSILASTIRILMAZ. Filtrelemek,
                  -- "sessizce yanlis sonuc"u "gorunur bicimde eksik sonuc"a cevirir;
                  -- ikincisi asagida ayrica tespit edilip HATA olarak yukseltilir.
                  AND embedding_model = %s
            )
            , scaled AS (
                -- ⚠️ BLOKER DUZELTMESI (Fable, 2026-08-17) — SKOR OLCEKLERI UYUMSUZ.
                -- `ts_rank_cd` NORMALIZE DEGILDIR (tipik 0,01-0,1), kosinus 0-1 arasi.
                -- Ham toplamda `0.7*vector + 0.3*lexical` yazsan bile leksik bacagin
                -- EFEKTIF katkisi yuzde 3 civarinda kalir; "hibrit lexical'i yendi" sonucu
                -- o zaman
                -- aramanin degil KARISIMIN hilesidir.
                -- BU DIZEDE YUZDE ISARETI KULLANMA -- YORUMDA BILE. psycopg tum dizeyi
                --    yer tutucu icin tarar; yuzde-isareti + rakam ikilisi
                --    ProgrammingError ile PATLAR ve sorgu HIC kosmaz. 2026-08-17'de tam
                --    olarak bu oldu; uyariyi yazarken de AYNI hatayi bir kez daha yaptim
                --    (uyari metninde yuzde isareti vardi). Kural teste baglandi:
                --    `test_search_sql_has_no_stray_percent`.
                -- Her bacak SORGU BASINA kendi
                -- araligina gore 0-1'e olceklenir; agirlik ancak boyle anlam tasir.
                -- Tek satir varsa (max=min) olcek tanimsizdir => 1.0 kabul edilir.
                SELECT *,
                    CASE WHEN MAX(vector_score) OVER () = MIN(vector_score) OVER ()
                         THEN CASE WHEN vector_score > 0 THEN 1.0 ELSE 0.0 END
                         ELSE (vector_score - MIN(vector_score) OVER ())
                              / NULLIF(MAX(vector_score) OVER () - MIN(vector_score) OVER (), 0)
                    END AS vector_norm,
                    CASE WHEN MAX(lexical_score) OVER () = MIN(lexical_score) OVER ()
                         THEN CASE WHEN lexical_score > 0 THEN 1.0 ELSE 0.0 END
                         ELSE (lexical_score - MIN(lexical_score) OVER ())
                              / NULLIF(MAX(lexical_score) OVER () - MIN(lexical_score) OVER (), 0)
                    END AS lexical_norm
                FROM authorized
                WHERE vector_score > 0 OR lexical_score > 0
            )
            SELECT tenant_id, document_id, version, title, section, source_uri,
                   content, content_hash, acl_roles,
                   (%s * vector_norm + %s * lexical_norm) AS score
            FROM scaled
            ORDER BY score DESC, document_id, version
            LIMIT %s
        """
        async with self._connection_factory() as connection:
            cursor = await connection.execute(
                sql,
                (
                    embedding,
                    self._text_search_config,
                    query,
                    principal.tenant_id,
                    sorted(principal.roles),
                    self._space_id,
                    self._vector_weight,
                    lexical_weight,
                    limit,
                ),
            )
            rows = await cursor.fetchall()
            if not rows:
                # Bos sonuc iki AYRI seyden gelebilir: (a) gercekten eslesme yok
                # (b) belgeler BASKA bir gomme uzayinda duruyor ve yukaridaki filtre
                # hepsini disarida biraktı. (b) sessiz kalirsa "arama calismiyor" diye
                # aylarca yanlis yerde aranir. Ayirt et ve (b)'yi HATA'ya yukselt.
                stale = await connection.execute(
                    "SELECT DISTINCT embedding_model FROM rag_documents "
                    "WHERE tenant_id = %s AND embedding_model <> %s LIMIT 5",
                    (principal.tenant_id, self._space_id),
                )
                others = [row["embedding_model"] for row in await stale.fetchall()]
                if others:
                    raise EmbeddingSpaceMismatch(
                        f"tenant {principal.tenant_id!r} icin kayitli vektorler baska "
                        f"gomme uzay(lar)inda: {others!r}; aktif saglayici: "
                        f"{self._space_id!r}. Yeniden gomme (reindex) gerekiyor."
                    )
        return [
            RetrievalHit(
                document=Document(
                    tenant_id=row["tenant_id"],
                    document_id=row["document_id"],
                    version=row["version"],
                    title=row["title"],
                    section=row["section"],
                    source_uri=row["source_uri"],
                    content=row["content"],
                    content_hash=row["content_hash"],
                    acl_roles=frozenset(row["acl_roles"]),
                ),
                score=max(0.0, float(row["score"])),
            )
            for row in rows
        ]
