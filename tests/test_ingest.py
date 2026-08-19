import asyncio
import json
from pathlib import Path

import pytest

from wozto_ai_reference.ingest import apply_plan, build_plan, markdown_chunks


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
        documents=[{"path": "allowed.md", "document_id": "allowed-policy", "acl_roles": ["ops"]}],
    )

    plan = build_plan(source_root=source, manifest_path=manifest)

    assert plan.source_files == 1
    assert len(plan.documents) == 1
    assert plan.documents[0].document_id == "allowed-policy::0001"
    assert plan.documents[0].acl_roles == frozenset({"ops"})
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
    assert len(first.documents) > 1


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

    class RecordingStore:
        def __init__(self) -> None:
            self.calls = []

        async def replace_source(self, **kwargs) -> None:
            self.calls.append(kwargs)

    store = RecordingStore()
    applied = asyncio.run(apply_plan(plan, store=store))

    assert applied == 1
    assert store.calls[0]["source_document_id"] == "policy"
    assert store.calls[0]["documents"] == plan.documents
