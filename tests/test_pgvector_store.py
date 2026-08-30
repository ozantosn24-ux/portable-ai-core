import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

from wozto_ai_reference.domain import Document, Principal
from wozto_ai_reference.embedding import HashEmbeddingProvider
from wozto_ai_reference.pgvector_store import PgVectorStore, vector_literal


class FakeTransaction(AbstractAsyncContextManager):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class FakeConnection:
    def __init__(self, *, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[object, object]] = []
        self.rows = rows or []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: object, params: object = None) -> FakeCursor:
        self.calls.append((query, params))
        return FakeCursor(self.rows)


class FakeConnectionContext(AbstractAsyncContextManager):
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return None


def _document(document_id: str = "policy::0001") -> Document:
    return Document(
        tenant_id="tenant-a",
        document_id=document_id,
        version="v1",
        title="Policy",
        section="Main",
        source_uri="vault://policy.md#1",
        content="Human approval policy.",
        content_hash="sha256:test",
        acl_roles=frozenset({"ops"}),
    )


def test_vector_literal_rejects_wrong_dimensions_and_non_finite_values() -> None:
    assert vector_literal([0.5, -0.25], dimensions=2) == "[0.5,-0.25]"
    with pytest.raises(ValueError, match="dimensions"):
        vector_literal([1.0], dimensions=2)
    with pytest.raises(ValueError, match="finite"):
        vector_literal([float("nan")], dimensions=1)


def test_database_url_rejects_embedded_password_without_echoing_it() -> None:
    marker = "integration-secret-marker"
    connection_strings = (
        f"postgresql://wozto:{marker}@127.0.0.1:55432/wozto_rag",
        f"host=127.0.0.1 dbname=wozto_rag user=wozto password={marker}",
    )
    for connection_string in connection_strings:
        with pytest.raises(ValueError, match="passfile") as exc_info:
            PgVectorStore(
                database_url=connection_string,
                embeddings=HashEmbeddingProvider(dimensions=16),
            )
        assert marker not in str(exc_info.value)


def test_replace_source_deletes_old_chunks_before_inserting_new_batch() -> None:
    connection = FakeConnection()
    store = PgVectorStore(
        database_url="postgresql://local/test",
        embeddings=HashEmbeddingProvider(dimensions=16),
        connection_factory=lambda: FakeConnectionContext(connection),
    )

    asyncio.run(
        store.replace_source(
            tenant_id="tenant-a",
            source_document_id="policy",
            documents=[_document()],
        )
    )

    assert "DELETE FROM rag_documents" in str(connection.calls[0][0])
    assert connection.calls[0][1] == ("tenant-a", "policy")
    assert "INSERT INTO rag_documents" in str(connection.calls[1][0])


def test_search_pushes_tenant_and_acl_filter_parameters_into_sql() -> None:
    row = {
        "tenant_id": "tenant-a",
        "document_id": "policy::0001",
        "version": "v1",
        "title": "Policy",
        "section": "Main",
        "source_uri": "vault://policy.md#1",
        "content": "Human approval policy.",
        "content_hash": "sha256:test",
        "acl_roles": ["ops"],
        "score": 0.9,
    }
    connection = FakeConnection(rows=[row])
    store = PgVectorStore(
        database_url="postgresql://local/test",
        embeddings=HashEmbeddingProvider(dimensions=16),
        connection_factory=lambda: FakeConnectionContext(connection),
    )
    principal = Principal(tenant_id="tenant-a", user_id="operator", roles=frozenset({"ops"}))

    hits = asyncio.run(store.search(principal=principal, query="approval", limit=3))

    query, params = connection.calls[0]
    assert "tenant_id = %s" in str(query)
    assert "acl_roles && %s::text[]" in str(query)
    # ⚠️ 2026-08-17: leksik config yer tutucuya cikinca sira BIR KAYDI
    # (embedding, text_search_config, query, tenant_id, roles, ...).
    assert params[1] == "simple", "leksik config sorguya PARAMETRE olarak gecmeli"
    assert params[3] == "tenant-a"
    assert params[4] == ["ops"]
    assert hits[0].document.tenant_id == "tenant-a"
