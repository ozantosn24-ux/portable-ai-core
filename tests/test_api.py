import pytest
from fastapi.testclient import TestClient

from wozto_ai_reference.api import create_app


def test_health_is_available_but_readiness_fails_closed_without_identity() -> None:
    client = TestClient(create_app(allow_insecure_identity=False))

    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "reason": "identity_disabled"}

    query = client.post(
        "/query",
        headers={"X-Tenant-ID": "tenant-demo", "X-User-ID": "local-operator"},
        json={"query": "refund policy"},
    )
    assert query.status_code == 503


def test_local_query_requires_explicit_identity_and_returns_authorized_citation() -> None:
    client = TestClient(create_app(allow_insecure_identity=True))

    response = client.post(
        "/query",
        headers={
            "X-Tenant-ID": "tenant-demo",
            "X-User-ID": "local-operator",
            "X-Roles": "employee",
        },
        json={"query": "refund policy", "top_k": 5},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["abstained"] is False
    assert [citation["document_id"] for citation in body["citations"]] == ["refund-policy"]
    assert body["trace_id"]


def test_local_query_rejects_missing_identity_headers() -> None:
    client = TestClient(create_app(allow_insecure_identity=True))

    response = client.post("/query", json={"query": "refund policy"})

    assert response.status_code == 400
    assert response.json()["detail"] == "X-Tenant-ID and X-User-ID are required"


def test_unknown_backend_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("WOZTO_REFERENCE_BACKEND", "unknown")

    with pytest.raises(RuntimeError, match="Unsupported"):
        create_app(allow_insecure_identity=True)


def test_pgvector_backend_requires_database_url(monkeypatch) -> None:
    monkeypatch.setenv("WOZTO_REFERENCE_BACKEND", "pgvector")
    monkeypatch.delenv("WOZTO_REFERENCE_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        create_app(allow_insecure_identity=True)
