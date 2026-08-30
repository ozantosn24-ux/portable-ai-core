from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compose_mounts_password_file_instead_of_exporting_password() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD:" not in compose
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password" in compose
    assert "WOZTO_RAG_DB_PASSWORD_FILE" in compose


def test_readme_connection_examples_use_passfile() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "postgresql://wozto:" not in readme
    assert "passfile=" in readme
