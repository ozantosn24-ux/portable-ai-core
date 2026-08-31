import asyncio
import json
import sys
from pathlib import Path

import pytest

from wozto_ai_reference.ingest import (
    PlanHashMismatch,
    _parser,
    _run,
    apply_plan,
    build_plan,
    main,
    markdown_chunks,
    verify_plan_hash,
)


class RecordingStore:
    def __init__(self) -> None:
        self.calls = []

    async def replace_source(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _write_manifest(path: Path, *, documents: list[dict]) -> None:
    path.write_text(json.dumps({"tenant_id": "tenant-a", "documents": documents}), encoding="utf-8")


def test_build_plan_reads_only_allowlisted_markdown(tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    (source / "allowed.md").write_text("# Allowed\n\n## Rule\n\nOnly this policy is indexed.", encoding="utf-8")
    (source / "not-listed.md").write_text("# Secret\n\nMust not be indexed.", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(
        manifest,
        documents=[
            {
                "path": "allowed.md",
                "document_id": "allowed-policy",
                "acl_roles": ["ops"],
                "source_status": "historical",
                "source_authority": "authoritative",
                "valid_from": "2026-01-01",
                "valid_through": "2026-08-24",
            }
        ],
    )

    plan = build_plan(source_root=source, manifest_path=manifest)

    assert plan.source_files == 1
    assert len(plan.documents) == 1
    assert plan.documents[0].document_id == "allowed-policy::0001"
    assert plan.documents[0].acl_roles == frozenset({"ops"})
    assert plan.documents[0].source_status == "historical"
    assert plan.documents[0].source_authority == "authoritative"
    assert plan.documents[0].valid_from.isoformat() == "2026-01-01"
    assert plan.documents[0].valid_through.isoformat() == "2026-08-24"
    assert "Secret" not in plan.documents[0].content


def test_manifest_path_escape_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nDo not read.", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, documents=[{"path": "../outside.md", "document_id": "outside"}])

    with pytest.raises(ValueError, match="escapes"):
        build_plan(source_root=source, manifest_path=manifest)


def test_symlink_source_is_rejected_when_supported(tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    target = source / "target.md"
    target.write_text("# Target\n\nDo not follow links.", encoding="utf-8")
    link = source / "link.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows configuration")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, documents=[{"path": "link.md", "document_id": "linked"}])

    with pytest.raises(ValueError, match="symbolic links"):
        build_plan(source_root=source, manifest_path=manifest)


def test_chunk_ids_and_versions_are_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    document = source / "policy.md"
    document.write_text("# Policy\n\n## Main\n\n" + ("policy sentence " * 80), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, documents=[{"path": "policy.md", "document_id": "policy"}])

    first = build_plan(source_root=source, manifest_path=manifest, max_chars=300, overlap_chars=50)
    second = build_plan(source_root=source, manifest_path=manifest, max_chars=300, overlap_chars=50)

    assert [item.document_id for item in first.documents] == [item.document_id for item in second.documents]
    assert [item.version for item in first.documents] == [item.version for item in second.documents]
    assert first.plan_hash == second.plan_hash
    assert first.plan_hash.startswith("sha256:")
    assert len(first.plan_hash) == 71
    assert len(first.documents) > 1


def test_plan_hash_changes_when_source_content_changes(tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    document = source / "policy.md"
    document.write_text("# Policy\n\nFirst reviewed version.", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, documents=[{"path": "policy.md", "document_id": "policy"}])
    reviewed = build_plan(source_root=source, manifest_path=manifest)

    document.write_text("# Policy\n\nChanged after dry-run.", encoding="utf-8")
    current = build_plan(source_root=source, manifest_path=manifest)

    assert current.plan_hash != reviewed.plan_hash


def test_plan_hash_changes_when_authorization_changes(tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    (source / "policy.md").write_text("# Policy\n\nStable content.", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(
        manifest,
        documents=[{"path": "policy.md", "document_id": "policy", "acl_roles": ["ops"]}],
    )
    ops_plan = build_plan(source_root=source, manifest_path=manifest)

    _write_manifest(
        manifest,
        documents=[{"path": "policy.md", "document_id": "policy", "acl_roles": ["finance"]}],
    )
    finance_plan = build_plan(source_root=source, manifest_path=manifest)

    assert finance_plan.plan_hash != ops_plan.plan_hash


def test_plan_hash_changes_when_source_coverage_metadata_changes(tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    (source / "policy.md").write_text("# Policy\n\nStable content.", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    base = {
        "path": "policy.md",
        "document_id": "policy",
        "source_status": "historical",
        "source_authority": "authoritative",
        "valid_through": "2026-08-24",
    }
    _write_manifest(manifest, documents=[base])
    historical_plan = build_plan(source_root=source, manifest_path=manifest)

    _write_manifest(manifest, documents=[{**base, "valid_through": "2026-08-25"}])
    changed_plan = build_plan(source_root=source, manifest_path=manifest)

    assert changed_plan.plan_hash != historical_plan.plan_hash


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_status", "live-ish", "source_status"),
        ("source_authority", "trusted-ish", "source_authority"),
        ("valid_from", "24-08-2026", "YYYY-MM-DD"),
    ],
)
def test_manifest_rejects_invalid_source_coverage_metadata(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    (source / "policy.md").write_text("# Policy\n\nStable content.", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(
        manifest,
        documents=[{"path": "policy.md", "document_id": "policy", field: value}],
    )

    with pytest.raises(ValueError, match=message):
        build_plan(source_root=source, manifest_path=manifest)


def test_markdown_chunk_limits_are_validated() -> None:
    with pytest.raises(ValueError, match="max_chars"):
        markdown_chunks("text", max_chars=100)
    with pytest.raises(ValueError, match="overlap_chars"):
        markdown_chunks("text", max_chars=200, overlap_chars=100)


def test_apply_replaces_each_source_as_one_batch(tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    (source / "policy.md").write_text("# Policy\n\n## Main\n\nVersioned content.", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, documents=[{"path": "policy.md", "document_id": "policy"}])
    plan = build_plan(source_root=source, manifest_path=manifest)

    store = RecordingStore()
    applied = asyncio.run(
        apply_plan(
            plan,
            store=store,
            expected_plan_hash=plan.plan_hash,
        )
    )

    assert applied == 1
    assert store.calls[0]["source_document_id"] == "policy"
    assert store.calls[0]["documents"] == plan.documents


def test_apply_rejects_a_changed_plan_before_any_store_call(tmp_path: Path) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    document = source / "policy.md"
    document.write_text("# Policy\n\nReviewed content.", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, documents=[{"path": "policy.md", "document_id": "policy"}])
    reviewed_hash = build_plan(source_root=source, manifest_path=manifest).plan_hash

    document.write_text("# Policy\n\nUnreviewed replacement.", encoding="utf-8")
    current_plan = build_plan(source_root=source, manifest_path=manifest)
    store = RecordingStore()

    with pytest.raises(PlanHashMismatch, match="changed after dry-run"):
        asyncio.run(
            apply_plan(
                current_plan,
                store=store,
                expected_plan_hash=reviewed_hash,
            )
        )
    assert store.calls == []


def test_plan_hash_format_is_fail_closed() -> None:
    class EmptyPlan:
        plan_hash = "sha256:" + "a" * 64

    for malformed in ("", "a" * 64, "sha256:" + "A" * 64, "sha256:not-a-hash"):
        with pytest.raises(PlanHashMismatch, match="format"):
            verify_plan_hash(EmptyPlan(), expected_plan_hash=malformed)


def test_dry_run_prints_reviewable_plan_hash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    (source / "policy.md").write_text("# Policy\n\nReviewed content.", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, documents=[{"path": "policy.md", "document_id": "policy"}])
    args = _parser().parse_args(["--source-root", str(source), "--manifest", str(manifest)])

    assert asyncio.run(_run(args)) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry_run"
    assert output["source_files"] == 1
    assert output["chunks"] == 1
    assert output["total_bytes"] > 0
    assert output["plan_hash"].startswith("sha256:")


def test_apply_flag_requires_the_reviewed_plan_hash() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["--source-root", "documents", "--manifest", "manifest.json", "--apply"])


def test_cli_reports_plan_mismatch_without_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "documents"
    source.mkdir()
    (source / "policy.md").write_text("# Policy\n\nReviewed content.", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, documents=[{"path": "policy.md", "document_id": "policy"}])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "wozto-rag-ingest",
            "--source-root",
            str(source),
            "--manifest",
            str(manifest),
            "--apply",
            "sha256:" + "0" * 64,
        ],
    )
    monkeypatch.delenv("WOZTO_REFERENCE_DATABASE_URL", raising=False)

    assert main() == 2

    captured = capsys.readouterr()
    assert "apply refused" in captured.err
    assert "Traceback" not in captured.err


def test_readme_apply_example_forwards_the_reviewed_plan_hash() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "--apply $plan.plan_hash" in readme
    assert "Hash verilmeden apply çalışmaz" in readme
