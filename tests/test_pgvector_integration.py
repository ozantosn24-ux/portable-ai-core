"""Opt-in local database test; skipped unless the operator provides a test URL."""

import os
from datetime import date
from uuid import uuid4

import pytest

from wozto_ai_reference.asyncio_compat import run as run_async
from wozto_ai_reference.domain import Document, Principal
from wozto_ai_reference.embedding import HashEmbeddingProvider
from wozto_ai_reference.pgvector_store import EmbeddingSpaceMismatch, PgVectorStore

DATABASE_URL = os.getenv("WOZTO_REFERENCE_TEST_DATABASE_URL")


@pytest.mark.skipif(not DATABASE_URL, reason="WOZTO_REFERENCE_TEST_DATABASE_URL is not set")
def test_pgvector_round_trip_enforces_tenant_and_acl() -> None:
    async def scenario() -> None:
        tenant = f"test-{uuid4().hex}"
        store = PgVectorStore(
            database_url=DATABASE_URL or "",
            embeddings=HashEmbeddingProvider(dimensions=64),
        )
        await store.initialize()
        documents = [
            Document(
                tenant_id=tenant,
                document_id="policy::0001",
                version="v1",
                title="Approval Policy",
                section="Main",
                source_uri="memory://integration/policy#1",
                content="A human operator approves every payment.",
                content_hash="sha256:integration",
                acl_roles=frozenset({"finance"}),
                source_status="historical",
                source_authority="authoritative",
                valid_from=date(2026, 1, 1),
                valid_through=date(2026, 8, 24),
            )
        ]
        await store.replace_source(
            tenant_id=tenant,
            source_document_id="policy",
            documents=documents,
        )
        unauthorized = await store.search(
            principal=Principal(tenant_id=tenant, user_id="employee", roles=frozenset({"employee"})),
            query="payment approval",
            limit=3,
        )
        authorized = await store.search(
            principal=Principal(tenant_id=tenant, user_id="finance", roles=frozenset({"finance"})),
            query="payment approval",
            limit=3,
        )
        assert unauthorized == []
        assert [hit.document.document_id for hit in authorized] == ["policy::0001"]
        assert authorized[0].document.source_status == "historical"
        assert authorized[0].document.source_authority == "authoritative"
        assert authorized[0].document.valid_from == date(2026, 1, 1)
        assert authorized[0].document.valid_through == date(2026, 8, 24)
        await store.delete(tenant_id=tenant, document_id="policy::0001", version="v1")

    run_async(scenario())


@pytest.mark.skipif(not DATABASE_URL, reason="WOZTO_REFERENCE_TEST_DATABASE_URL is not set")
def test_hybrid_legs_and_weights_reach_postgres() -> None:
    """Hibrit aramanın İKİ BACAĞI ve ağırlık tesisatı GERÇEK Postgres'te sınanır.

    Bugüne kadar `search()` yalnız tek bir round-trip'te koşmuştu; leksik bacağın
    (`search_tsv` generated column + `ts_rank_cd` + `plainto_tsquery`) gerçekten
    çalıştığı ve `vector_weight`in SQL'e ulaştığı HİÇ doğrulanmamıştı.

    ⚠️ SINIR — bilinçli olarak yapılMAYAN iddia: `HashEmbeddingProvider` token-hash
    tabanlıdır (kendi docstring'i: *"never a quality claim"*). Sorgu ile belge ortak
    token taşımıyorsa kosinüs de ~0 olur ⇒ **yoğun bacağın ANLAMSAL değeri bu kurulumda
    GÖSTERİLEMEZ.** Burada kanıtlanan şey TESİSAT: leksik bacak canlı, ağırlıklar SQL'e
    geçiyor, sıralama ağırlıkla değişiyor. Anlamsal kalite iddiası için gerçek bir
    embedding adaptörü gerekir — o ayrı bir iş.
    """

    async def scenario() -> None:
        tenant = f"test-{uuid4().hex}"
        principal = Principal(tenant_id=tenant, user_id="u", roles=frozenset())

        def store(vector_weight: float) -> PgVectorStore:
            return PgVectorStore(
                database_url=DATABASE_URL or "",
                embeddings=HashEmbeddingProvider(dimensions=64),
                vector_weight=vector_weight,
            )

        def doc(doc_id: str, title: str, content: str) -> Document:
            return Document(
                tenant_id=tenant,
                document_id=doc_id,
                version="v1",
                title=title,
                section="Main",
                source_uri=f"memory://hybrid/{doc_id}",
                content=content,
                content_hash=f"sha256:{doc_id}",
                acl_roles=frozenset(),
            )

        base = store(0.7)
        await base.initialize()
        await base.replace_source(
            tenant_id=tenant,
            source_document_id="hybrid",
            documents=[
                # ⚠️ GUNCELLEME 2026-08-17 (Fable hakemligi): store artik AND DEGIL OR
                # kullaniyor (`pgvector_store.py`, `replace(...'&','|')`). Asagidaki
                # kurgu gerekcesi AND rejiminden kalmadir ve ARTIK ZORUNLU DEGILDIR;
                # assert'ler OR altinda da gecer (gercek PG16'da dogrulandi, run
                # 32045047538). Kurgu yine de bilerek korunuyor: deterministik ve iki
                # rejimde de ayni sonucu vermesi onu daha guclu bir tanik yapar.
                # ⚠️ `:151` civarindaki "iki belge 0.0 alir" aciklamasinin SEBEBI de
                # degisti: 0.0 artik "leksik eslesme yok"tan degil, min-max
                # normalizasyonunun MINIMUM'undan geliyor.
                # ⚠️ TARIHSEL KAYIT (silinmesin — bu yorum bir bulgunun kanitidir):
                # `plainto_tsquery` terimleri AND'liyordu ve bu fixture, leksik bacak
                # ateslensin diye IKI KEZ yeniden kurgulanmisti. Bacagin ateslemesi icin
                # belgenin elle kurgulanmak zorunda olmasi, bacagin SAHADA hic
                # ateslenmedigini gosteren isaretti: 150 XQuAD-tr sorgusunun 0'inda
                # sorgunun tum token'larini iceren belge vardi.
                # Ucu de "refund" paylastigi icin
                # hepsi VEKTOR bacaginda >0 alir ve `WHERE vector>0 OR lexical>0`
                # filtresinden GECER — normalizasyonun anlamli olmasi icin en az iki
                # satir sart (tek satirda min=max, her agirlikta 1.0).
                # ⚠️ UCUNDE DE "refund" KELIMESI AYNEN gecmeli. Hash gommesi TAM TOKEN
                # esler (koke indirgeme YOK): "Refunds" ile "refund" FARKLI token'dir.
                # Ilk kurguda digerlerinde "Refunds" yaziyordu => vektor puani da 0 cikti,
                # `WHERE vector>0 OR lexical>0` ikisini de eledi ve aramadan TEK belge
                # dondu; tek satirda min=max oldugu icin normalizasyon her agirlikta 1.0
                # verdi. (Olculdu: run 32036624961.)
                doc("hybrid::both", "Refund window", "The refund window closes after fourteen days."),
                doc("hybrid::refund-only", "Refund policy", "A refund is issued to the original payment method."),
                doc("hybrid::receipt", "Refund receipts", "Every refund requires the original paper receipt."),
            ],
        )

        # (a) LEKSIK BACAK CANLI: saf leksik (vector_weight=0) sorguda, terimleri
        #     taşıyan belge gelmeli; hiç ortak terimi olmayan belge ÜSTTE olmamalı.
        lexical_only = await store(0.0).search(principal=principal, query="refund window", limit=5)
        assert lexical_only, "saf leksik arama HİÇBİR ŞEY döndürmedi - ts_rank_cd yolu ölü"
        assert lexical_only[0].document.document_id == "hybrid::both", (
            f"leksik bacak yanlış belgeyi üste koydu: {[h.document.document_id for h in lexical_only]}"
        )

        # (b) AĞIRLIK SQL'E ULAŞIYOR.
        #     ⚠️ 2026-08-17: bu assert bir kez YANLIŞ kuruldu. "En üstteki belgenin skoru
        #     ağırlıkla değişmeli" deniyordu; SORGU-BAŞI MIN-MAX normalizasyonu eklenince
        #     bu varsayım çöktü — normalizasyon en yüksek belgeyi HER İKİ bacakta da 1.0'a
        #     sabitler, dolayısıyla `w*1 + (1-w)*1 = 1` her ağırlıkta AYNIDIR. Yani test
        #     kırmızı verdi ama davranış DOĞRUYDU; kusur testin varsayımındaydı.
        #     Normalizasyona dayanıklı doğru ölçüt: SKOR HARİTASININ TAMAMI değişiyor mu.
        pure_lexical = {h.document.document_id: h.score for h in lexical_only}
        pure_vector = {
            h.document.document_id: h.score
            for h in await store(1.0).search(principal=principal, query="refund window", limit=5)
        }
        assert pure_vector, "saf vektör araması boş döndü"
        # w=0'da leksik eslesmeyen IKI belge de tam olarak 0.0 alir; w=1'de vektor
        # normalizasyonunda yalnizca MIN 0.0 olabilir => iki harita ayni OLAMAZ.
        assert pure_lexical != pure_vector, (
            "w=0 ve w=1 AYNI skor haritasını üretti - ağırlık SQL'e ulaşmıyor "
            f"(lexical={pure_lexical}, vector={pure_vector})"
        )

        for doc_id in ("hybrid::both", "hybrid::refund-only", "hybrid::receipt"):
            await base.delete(tenant_id=tenant, document_id=doc_id, version="v1")

    run_async(scenario())


@pytest.mark.skipif(not DATABASE_URL, reason="WOZTO_REFERENCE_TEST_DATABASE_URL is not set")
def test_embedding_space_mismatch_raises_instead_of_returning_empty() -> None:
    """Saglayici degisince ESKI satirlar sessizce YOK sayilmamali, HATA vermeli.

    Fable bulgusu (2026-08-17): kalicilastirilmis vektorlerde gomme uzayinin kimligi
    yoktu. hash->e5 ya da model revizyonu kaydiginda eski satirlar eski uzayda kalir;
    BOYUT AYNIYSA kosinus sessizce ANLAMSIZ benzerlik doner. Kimlik eklendi ve arama
    yalnizca ayni uzaydaki satirlarla karsilastiriyor.

    Ama filtrelemek TEK BASINA yetmez: bu sefer sonuc sessizce BOSALIR ve "eslesme yok"
    diye okunur. Bu test o ikinci sessizligi de kapatir.
    """

    async def scenario() -> None:
        tenant = f"test-{uuid4().hex}"
        principal = Principal(tenant_id=tenant, user_id="u", roles=frozenset())

        # Ayni BOYUT, FARKLI uzay: gercek tehlike tam olarak bu (boyut farkliysa zaten patlar).
        eski = PgVectorStore(
            database_url=DATABASE_URL or "",
            embeddings=HashEmbeddingProvider(dimensions=64),
        )
        await eski.initialize()
        await eski.replace_source(
            tenant_id=tenant,
            source_document_id="space",
            documents=[
                Document(
                    tenant_id=tenant,
                    document_id="space::0001",
                    version="v1",
                    title="Refund window",
                    section="Main",
                    source_uri="memory://space/1",
                    content="Refunds are issued within fourteen days.",
                    content_hash="sha256:space",
                    acl_roles=frozenset(),
                )
            ],
        )
        # Ayni uzayda arama CALISIYOR (on kosul: veri gercekten orada)
        assert await eski.search(principal=principal, query="refund", limit=5)

        # Simdi BASKA bir uzay kimligi tasiyan saglayiciyla ayni tenant'ta ara
        class _BaskaUzay(HashEmbeddingProvider):
            @property
            def model_id(self) -> str:
                return "some-other-embedding-space/64"

        yeni = PgVectorStore(
            database_url=DATABASE_URL or "",
            embeddings=_BaskaUzay(dimensions=64),
        )
        with pytest.raises(EmbeddingSpaceMismatch, match="reindex"):
            await yeni.search(principal=principal, query="refund", limit=5)

        await eski.delete(tenant_id=tenant, document_id="space::0001", version="v1")

    run_async(scenario())
