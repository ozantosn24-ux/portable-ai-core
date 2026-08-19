"""Deney kosucusunun ingest'i `PgVectorStore` SOZLESMESINE bagli mi?

⚠️ NEDEN VAR (2026-08-17, sahada olculdu):
`hybrid-experiment` job'unun ILK gercek kosusu ingest'te dustu --
`ValueError: all chunks must match the tenant and source document`. Kosucu sabit
`source_document_id="experiment"` geciyordu, korpus id'leri ise `xquad::<hash>`.

Ariza DB'siz smoke'ta GORUNMEDI cunku smoke `InMemoryHybridSearchProvider` ile
kosuyordu ve o saglayicida bu sozlesme YOK ⇒ smoke baska bir sistemi olcuyordu.
Bu dosya o bosluktur: kosucunun grupladigi cikti, GERCEK store'un `replace_source`
kontratina (sahte baglanti uzerinden) sokulur. Boylece ayni ariza haftalik/dispatch
kosusunu ve model indirmesini BEKLEMEDEN hizli suite'te kirmizi verir.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from wozto_ai_reference.domain import Document
from wozto_ai_reference.embedding import HashEmbeddingProvider
from wozto_ai_reference.pgvector_store import PgVectorStore

from test_pgvector_store import FakeConnection, FakeConnectionContext

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_hybrid_experiment import (  # noqa: E402
    TENANT,
    group_by_source,
    ingest_corpus,
    load_corpus,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "xquad-tr"


def _document(document_id: str) -> Document:
    return Document(
        tenant_id=TENANT,
        document_id=document_id,
        version="v1",
        title="T",
        section="S",
        source_uri=f"memory://{document_id}",
        content="icerik",
        content_hash="sha256:test",
        acl_roles=frozenset(),
    )


def _ingest_with_fake_connection(documents: list[Document]) -> FakeConnection:
    """Kosucunun ingest yolunu sahte baglantiyla kosar; sozlesme ihlali PATLAR."""
    connection = FakeConnection()
    store = PgVectorStore(
        database_url="postgresql://local/test",
        embeddings=HashEmbeddingProvider(dimensions=16),
        connection_factory=lambda: FakeConnectionContext(connection),
    )
    # ⚠️ URETIM YOLUNUN KENDISI cagrilir (dongu burada KOPYALANMAZ) — Fable
    # hakemligi 2026-08-17: kopyalanmis dongu cagri yerini test etmiyordu.
    asyncio.run(ingest_corpus(store, documents))
    return connection


def test_runner_grouping_satisfies_replace_source_contract() -> None:
    """Asil regresyon: bu, 2026-08-17'de CI'da dusen tam ayni cagridir."""
    documents = [_document("xquad::aaa"), _document("xquad::bbb")]

    connection = _ingest_with_fake_connection(documents)

    # Silme, kaynak onegiyle yapilmali (sabit "experiment" ile DEGIL).
    assert "DELETE FROM rag_documents" in str(connection.calls[0][0])
    assert connection.calls[0][1] == (TENANT, "xquad")
    # Iki chunk da yazilmali.
    inserts = [c for c in connection.calls if "INSERT INTO rag_documents" in str(c[0])]
    assert len(inserts) == 2


def test_multiple_source_documents_are_ingested_as_separate_sources() -> None:
    documents = [_document("a::1"), _document("b::1"), _document("a::2")]

    groups = group_by_source(documents)

    assert {k: len(v) for k, v in groups.items()} == {"a": 2, "b": 1}
    _ingest_with_fake_connection(documents)  # sozlesme ihlali olsa patlardi


def test_ids_without_separator_fail_loudly_instead_of_silently() -> None:
    with pytest.raises(SystemExit, match="konvansiyona uymuyor"):
        group_by_source([_document("ayrac-yok")])


def test_shipped_xquad_corpus_ids_are_preserved_verbatim() -> None:
    """⛔ Kimlik korunmali: gold set (`cases.json`) CIPLAK id'lere bakar.

    Id'leri oneklemek `ValueError`i susturur ama recall'u SESSIZCE 0'a dusururdu.
    Bu test o "duzeltmeyi" imkansiz kilar.
    """
    documents = load_corpus(DATA_DIR)
    groups = group_by_source(documents)

    assert sum(len(v) for v in groups.values()) == len(documents)
    grouped_ids = {d.document_id for chunks in groups.values() for d in chunks}
    assert grouped_ids == {d.document_id for d in documents}
    for source_id, chunks in groups.items():
        assert all(d.document_id.startswith(f"{source_id}::") for d in chunks)
