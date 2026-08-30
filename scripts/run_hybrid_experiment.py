"""Hibrit arama deneyi: dense-only ⟂ lexical-only ⟂ hibrit, ağırlık taramalı.

⚠️ Bu bir **deney koşucusu**, KAPI DEĞİL. `evaluation.py` eşikli bir kalite kapısıdır
(`passes()`); ikisini tek CLI'ye sıkıştırmak ikisini de bozar — kapı deneyin gürültüsüyle
kırmızıya döner, deney kapının eşiğine uydurulmaya çalışılır.

## Ne üretir

`SweepResult` + **provenance** (model + revizyon, korpus/gold-set sha256, git SHA).
Provenance olmadan "sonuç repoda" ama **neyin** sonucu olduğu dosyadan okunamaz —
gömme-uzayı kimliği bulgusunun (2026-08-17) doğrudan devamı.

## Nerede koşar

Fable kararı (2026-08-17): **yalnız `workflow_dispatch`**, schedule DEĞİL. Bu bir deney
sonucudur; korpus/gold-set/model/kod pinliyken haftalık tekrar yeni bilgi üretmez, sadece
kota yakar. Tekrarlanabilirlik schedule'dan değil **pin'den** gelir.

## Maliyet notu

Korpus **BİR KEZ** gömülür; ağırlık taraması aynı veriyi kullanır. Aksi hâlde 11 ağırlık
× N belge yeniden gömme olurdu (gerçek e5 ile bu kalite değil kota meselesi).

Kullanım:
    python scripts/run_hybrid_experiment.py --data data/xquad-tr --embeddings hash --provider memory
    python scripts/run_hybrid_experiment.py --data data/xquad-tr --embeddings e5 \\
        --provider pgvector --database-url "host=... passfile=..." --out results/xquad-tr.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wozto_ai_reference.asyncio_compat import run as run_async  # noqa: E402
from wozto_ai_reference.comparison import sweep  # noqa: E402
from wozto_ai_reference.domain import Document, Principal  # noqa: E402
from wozto_ai_reference.embedding import (  # noqa: E402
    HashEmbeddingProvider,
    InMemoryHybridSearchProvider,
)
from wozto_ai_reference.evaluation import EvalCase  # noqa: E402

TENANT = "experiment"


def load_corpus(data_dir: Path) -> list[Document]:
    raw = json.loads((data_dir / "corpus.json").read_text(encoding="utf-8"))
    return [
        Document(
            tenant_id=TENANT,
            document_id=item["document_id"],
            version="v1",
            title=item["title"],
            section=item["section"],
            source_uri=f"memory://{item['document_id']}",
            content=item["content"],
            content_hash="sha256:" + hashlib.sha256(item["content"].encode()).hexdigest(),
            acl_roles=frozenset(),
        )
        for item in raw["documents"]
    ]


def load_experiment_cases(data_dir: Path) -> tuple[EvalCase, ...]:
    raw = json.loads((data_dir / "cases.json").read_text(encoding="utf-8"))
    principal = Principal(tenant_id=TENANT, user_id="evaluator", roles=frozenset())
    return tuple(
        EvalCase(
            case_id=item["case_id"],
            principal=principal,
            query=item["query"],
            relevant_document_ids=frozenset(item["relevant_document_ids"]),
            layer=item.get("layer"),
        )
        for item in raw["cases"]
    )


def build_embeddings(kind: str):
    if kind == "hash":
        # ⚠️ ANLAMSAL DEGILDIR. Yalnizca tesisati sinamak icin; bu saglayiciyla cikan
        # sayidan KALITE iddiasi TURETILMEZ (saglayicinin kendi docstring'i de boyle der).
        return HashEmbeddingProvider(dimensions=256)
    if kind == "e5":
        from wozto_ai_reference.e5_embedding import E5EmbeddingProvider

        return E5EmbeddingProvider()
    raise SystemExit(f"bilinmeyen embeddings turu: {kind!r}")


def group_by_source(documents: Sequence[Document]) -> dict[str, list[Document]]:
    """Chunk'lari KAYNAK BELGE onegine gore gruplar. Kimlikleri DEGISTIRMEZ.

    ⚠️ NEDEN VAR (2026-08-17, sahada olculdu — tahmin degil):
    `PgVectorStore.replace_source` her chunk'in `"{source_document_id}::"` ile
    baslamasini sart kosar (`pgvector_store.py:190-195`); `upsert` de ayni `::`
    konvansiyonunu kullanir (satir 164). Bu kosucu ise sabit `"experiment"`
    geciyordu, korpus id'leri ise `xquad::<hash>` ⇒ ilk gercek CI kosusu
    `ValueError: all chunks must match the tenant and source document` ile dustu.

    ⛔ AKLA GELEN ILK "DUZELTME" YANLISTI: id'leri `experiment::` ile oneklemek
    hatayi SUSTURUR ama gold set (`cases.json`) ciplak `xquad::` id'lerine bakar
    ⇒ recall her bacakta SESSIZCE 0 olurdu ve cikti "olctuk, kimse kazanmadi"
    gibi gorunurdu. O yuzden id'ler KORUNUR, kaynak onegi id'den TURETILIR.

    ⚠️ Bu arizanin DB'siz smoke'ta yakalanmamasinin sebebi:
    `InMemoryHybridSearchProvider`de bu sozlesme YOK ⇒ smoke baska bir sistemi
    olcuyordu. Bu yuzden asagidaki kontrat testi DB'siz suite'e baglandi.
    """
    groups: dict[str, list[Document]] = {}
    for document in documents:
        source_id, separator, _ = document.document_id.partition("::")
        if not separator or not source_id:
            # Sessiz fallback YOK: konvansiyona uymayan korpus, olcumu bozacak
            # bicimde "calisiyormus gibi" gecmemeli.
            raise SystemExit(
                f"korpus konvansiyona uymuyor: document_id={document.document_id!r} "
                "'<kaynak>::<chunk>' bicimde degil; kaynak onegi turetilemez"
            )
        groups.setdefault(source_id, []).append(document)
    return groups


async def ingest_corpus(store, documents: Sequence[Document]) -> None:
    """Korpusu kaynak-belge gruplari halinde store'a yazar.

    ⚠️ AYRI FONKSIYON OLMASININ SEBEBI (Fable hakemligi, 2026-08-17):
    Bu dongu once `_pgvector_factory` icinde gomuluydu ve testler yalnizca
    `group_by_source`'u cagiriyordu ⇒ CAGRI YERI test edilmiyordu. Yardimci dogru
    olsa bile cagri yeri yarin sabit `source_document_id="experiment"`e geri donse
    suite YESIL kalirdi. "Mutasyon 4/4 yakaladi" iddiasi da yalnizca yardimci icin
    gecerliydi. Artik uretim yolu ve test AYNI fonksiyonu cagirir.
    """
    for source_id, chunks in group_by_source(documents).items():
        await store.replace_source(
            tenant_id=TENANT, source_document_id=source_id, documents=chunks
        )


async def _pgvector_factory(args, documents, embeddings):
    from wozto_ai_reference.pgvector_store import PgVectorStore

    if not args.database_url:
        raise SystemExit("--provider pgvector icin --database-url zorunlu")
    # Korpus BIR KEZ gomulur; agirlik taramasi ayni veriyi kullanir.
    ingest_store = PgVectorStore(
        database_url=args.database_url,
        embeddings=embeddings,
        text_search_config=args.text_search_config,
    )
    await ingest_store.initialize()
    await ingest_corpus(ingest_store, documents)

    def factory(weight: float):
        return PgVectorStore(
            database_url=args.database_url,
            embeddings=embeddings,
            vector_weight=weight,
            text_search_config=args.text_search_config,
        )

    return factory


def _memory_factory(documents, embeddings):
    # ⚠️ SINIR: bu yol GERCEK SQL yolunu (ts_rank_cd + <=>) sinamaz; ayri bir sistemdir.
    # Yalnizca kosucunun kendisini DB'siz denemek icindir, sonuc RAPORLANMAZ.
    def factory(weight: float):
        return InMemoryHybridSearchProvider(documents, embeddings=embeddings, vector_weight=weight)

    return factory


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "bilinmiyor"


async def _run(args: argparse.Namespace) -> int:
    data_dir = args.data
    documents = load_corpus(data_dir)
    cases = load_experiment_cases(data_dir)
    embeddings = build_embeddings(args.embeddings)

    if args.provider == "pgvector":
        factory = await _pgvector_factory(args, documents, embeddings)
    else:
        factory = _memory_factory(documents, embeddings)

    weights = [round(i / 10, 1) for i in range(11)]
    result = await sweep(
        factory=factory,
        cases=cases,
        weights=weights,
        top_k=args.top_k,
        primary_weight=args.primary_weight,
    )

    manifest_path = data_dir / "manifest.json"
    payload = {
        "provenance": {
            "git_sha": git_sha(),
            "provider": args.provider,
            "embeddings": args.embeddings,
            "embedding_space": getattr(embeddings, "model_id", "bilinmiyor"),
            # ⚠️ LEKSIK BACAGIN KIMLIGI (Fable KARAR 2). Bu yazilmadan sonuc
            # dosyasindan "lexical-only 0.78" NEYIN 0.78'i oldugu okunamaz;
            # `INPUT_RECIPE`in leksik analogudur.
            "text_search_config": args.text_search_config,
            "data_dir": str(data_dir),
            "data_manifest": json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.is_file()
            else None,
            "documents": len(documents),
            "cases": len(cases),
            "top_k": args.top_k,
            "primary_weight": args.primary_weight,
            "weights": weights,
        },
        "claim_sentence": result.claim_sentence(),
        "verdicts": [v.model_dump() for v in result.verdicts],
        "points": [
            {
                "vector_weight": p.vector_weight,
                "overall": p.overall.model_dump(),
                "layers": [
                    {"layer": lr.layer, **lr.report.model_dump()} for lr in p.layers
                ],
            }
            for p in result.points
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(result.claim_sentence())
    if not args.out:
        print(text)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path, help="corpus.json + cases.json dizini")
    parser.add_argument("--provider", choices=("pgvector", "memory"), default="pgvector")
    parser.add_argument("--embeddings", choices=("hash", "e5"), default="e5")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--primary-weight", type=float, default=0.7)
    parser.add_argument(
        "--text-search-config",
        default="simple",
        choices=("simple", "turkish", "english"),
        help="leksik bacagin PG text-search config'i; Turkce urun hatti icin `turkish`",
    )
    parser.add_argument("--out", type=Path, default=None)
    return parser


def main() -> int:
    return run_async(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
