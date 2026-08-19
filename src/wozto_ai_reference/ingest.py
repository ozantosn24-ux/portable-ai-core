"""Allowlisted Markdown ingestion with deterministic, versioned chunks."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .domain import Document
from .embedding import HashEmbeddingProvider
from .ports import DocumentStore

MAX_SOURCE_BYTES = 1_000_000
_DOCUMENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,119}$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class ManifestEntry:
    path: Path
    document_id: str
    tenant_id: str
    acl_roles: frozenset[str]


@dataclass(frozen=True)
class IngestBatch:
    tenant_id: str
    source_document_id: str
    documents: tuple[Document, ...]


@dataclass(frozen=True)
class IngestPlan:
    batches: tuple[IngestBatch, ...]
    total_bytes: int

    @property
    def documents(self) -> tuple[Document, ...]:
        return tuple(document for batch in self.batches for document in batch.documents)

    @property
    def source_files(self) -> int:
        return len(self.batches)


def _nonempty(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text


def _safe_source(root: Path, relative: object) -> Path:
    raw = Path(_nonempty(relative, field="path"))
    if raw.is_absolute() or raw.suffix.casefold() != ".md":
        raise ValueError("manifest paths must be relative .md files")
    resolved_root = root.resolve(strict=True)
    unresolved = resolved_root / raw
    cursor = resolved_root
    for part in raw.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("symbolic links are not accepted as ingestion sources")
    candidate = unresolved.resolve(strict=True)
    if not candidate.is_relative_to(resolved_root):
        raise ValueError("manifest path escapes the selected source root")
    if not candidate.is_file():
        raise ValueError("manifest source must be a regular file")
    return candidate


def load_manifest(*, source_root: Path, manifest_path: Path) -> tuple[ManifestEntry, ...]:
    root = source_root.resolve(strict=True)
    manifest = manifest_path.resolve(strict=True)
    if manifest.is_relative_to(root):
        raise ValueError("manifest must be outside the selected document root")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    default_tenant = _nonempty(data.get("tenant_id"), field="tenant_id")
    entries: list[ManifestEntry] = []
    seen: set[tuple[str, str]] = set()
    for raw in data.get("documents", []):
        document_id = _nonempty(raw.get("document_id"), field="document_id")
        if not _DOCUMENT_ID.fullmatch(document_id):
            raise ValueError(f"invalid document_id: {document_id}")
        tenant_id = _nonempty(raw.get("tenant_id", default_tenant), field="tenant_id")
        key = (tenant_id, document_id)
        if key in seen:
            raise ValueError(f"duplicate tenant/document_id: {tenant_id}/{document_id}")
        seen.add(key)
        roles = frozenset(_nonempty(role, field="acl_role") for role in raw.get("acl_roles", []))
        entries.append(
            ManifestEntry(
                path=_safe_source(root, raw.get("path")),
                document_id=document_id,
                tenant_id=tenant_id,
                acl_roles=roles,
            )
        )
    if not entries:
        raise ValueError("manifest must allowlist at least one document")
    return tuple(entries)


def _split_window(text: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    clean = " ".join(text.split())
    if not clean:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + max_chars)
        if end < len(clean):
            boundary = clean.rfind(" ", start + max_chars // 2, end)
            if boundary > start:
                end = boundary
        chunks.append(clean[start:end].strip())
        if end >= len(clean):
            break
        start = max(start + 1, end - overlap_chars)
    return chunks


def markdown_chunks(text: str, *, max_chars: int = 1000, overlap_chars: int = 150) -> list[tuple[str, str]]:
    # ⚠️ 1200 -> 1000 (2026-08-17). Sebep KESME ASIMETRISI: yogun bacak e5'in SERT
    # 512-token sinirina tabidir; leksik bacak (`search_tsv`) metnin tamamini indeksler.
    # Chunk kuyrugu 512 token'i asarsa dense onu HIC gormez ve dense/lexical
    # karsilastirmasi yapisal olarak tarafli olur. Kotu-durum orani 1 token >= 2 karakter
    # alinirsa 512*2 = 1024 >= 1000 ✓ (1200'de 1024 < 1200 idi, sinir asiliyordu).
    # ⛔ Bunu buyutursen `e5_embedding.DEFAULT_MAX_LENGTH` ile birlikte dusun; testi var.
    if max_chars < 200:
        raise ValueError("max_chars must be at least 200")
    if overlap_chars < 0 or overlap_chars >= max_chars // 2:
        raise ValueError("overlap_chars must be non-negative and less than half max_chars")
    sections: list[tuple[str, list[str]]] = []
    heading = "main"
    body: list[str] = []
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            if body:
                sections.append((heading, body))
            heading = match.group(2).strip()
            body = []
        else:
            body.append(line)
    if body:
        sections.append((heading, body))

    output: list[tuple[str, str]] = []
    for section, lines in sections:
        for chunk in _split_window("\n".join(lines), max_chars=max_chars, overlap_chars=overlap_chars):
            output.append((section, chunk))
    return output


def build_plan(
    *,
    source_root: Path,
    manifest_path: Path,
    max_chars: int = 1200,
    overlap_chars: int = 150,
) -> IngestPlan:
    entries = load_manifest(source_root=source_root, manifest_path=manifest_path)
    batches: list[IngestBatch] = []
    total_bytes = 0
    root = source_root.resolve(strict=True)
    for entry in entries:
        size = entry.path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            raise ValueError(f"source exceeds {MAX_SOURCE_BYTES} bytes: {entry.path.name}")
        total_bytes += size
        raw = entry.path.read_bytes()
        if b"\x00" in raw:
            raise ValueError(f"binary content rejected: {entry.path.name}")
        text = raw.decode("utf-8")
        source_hash = hashlib.sha256(raw).hexdigest()
        version = source_hash[:16]
        title_match = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else entry.path.stem.replace("-", " ")
        relative_uri = entry.path.relative_to(root).as_posix()
        source_documents: list[Document] = []
        for index, (section, content) in enumerate(
            markdown_chunks(text, max_chars=max_chars, overlap_chars=overlap_chars),
            start=1,
        ):
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            source_documents.append(
                Document(
                    tenant_id=entry.tenant_id,
                    document_id=f"{entry.document_id}::{index:04d}",
                    version=version,
                    title=title,
                    section=section,
                    source_uri=f"vault://{relative_uri}#{index}",
                    content=content,
                    content_hash=f"sha256:{content_hash}",
                    acl_roles=entry.acl_roles,
                )
            )
        if not source_documents:
            raise ValueError(f"source produced no text chunks: {entry.path.name}")
        batches.append(
            IngestBatch(
                tenant_id=entry.tenant_id,
                source_document_id=entry.document_id,
                documents=tuple(source_documents),
            )
        )
    if not batches:
        raise ValueError("allowlisted sources produced no text chunks")
    return IngestPlan(batches=tuple(batches), total_bytes=total_bytes)


async def apply_plan(plan: IngestPlan, *, store: DocumentStore) -> int:
    for batch in plan.batches:
        await store.replace_source(
            tenant_id=batch.tenant_id,
            source_document_id=batch.source_document_id,
            documents=batch.documents,
        )
    return len(plan.documents)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan or apply allowlisted Markdown ingestion")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument("--overlap-chars", type=int, default=150)
    parser.add_argument("--apply", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    plan = build_plan(
        source_root=args.source_root,
        manifest_path=args.manifest,
        max_chars=args.max_chars,
        overlap_chars=args.overlap_chars,
    )
    if not args.apply:
        print(
            json.dumps(
                {"mode": "dry_run", "source_files": plan.source_files, "chunks": len(plan.documents)},
                ensure_ascii=False,
            )
        )
        return 0

    database_url = os.getenv("WOZTO_REFERENCE_DATABASE_URL")
    if not database_url:
        raise RuntimeError("WOZTO_REFERENCE_DATABASE_URL is required for --apply")
    from .pgvector_store import PgVectorStore

    store = PgVectorStore(database_url=database_url, embeddings=HashEmbeddingProvider())
    await store.initialize()
    applied = await apply_plan(plan, store=store)
    print(json.dumps({"mode": "apply", "chunks": applied}, ensure_ascii=False))
    return 0


def main() -> int:
    args = _parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
