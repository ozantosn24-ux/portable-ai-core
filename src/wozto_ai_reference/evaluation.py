"""Retrieval evaluation with recall, reciprocal rank, and authorization checks."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .adapters import is_authorized
from .domain import Principal
from .embedding import HashEmbeddingProvider, InMemoryHybridSearchProvider
from .ingest import build_plan
from .ports import SearchProvider


class EvalCase(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    case_id: str = Field(min_length=1)
    principal: Principal
    query: str = Field(min_length=1)
    relevant_document_ids: frozenset[str] = Field(min_length=1)
    # Sorgu KATMANI. Fable bulgusu (2026-08-17): kume kompozisyonu kazanani ONCEDEN
    # secer -- tamami-paraphrase bir set yogun bacagi, tamami-kelime-ortusmeli bir set
    # leksik bacagi GARANTI yener. Bu yuzden katmanlar AYRI AYRI raporlanmali.
    #   "overlap"    -> sorgu, hedef belgenin kelimelerini tasiyor (gercekci)
    #   "paraphrase" -> icerik-kelimesi ortusmesi SIFIR (yalniz anlamsal eslesme)
    #   "morphology" -> ekli/cekimli bicimler (refunds<->refund, iade<->iadesi)
    # None = etiketsiz (eski gold set'ler bozulmasin diye).
    # ⚠️ Literal: serbest metin olsaydi "paraphrse" yazim hatasi SESSIZCE dorduncu bir
    # katman yaratir ve tutarlilik kontrolunu seyreltirdi (Fable bulgusu 2026-08-17).
    layer: Literal["overlap", "paraphrase", "morphology"] | None = None


class EvalReport(BaseModel):
    cases: int
    recall_at_k: float
    mean_reciprocal_rank: float
    unauthorized_hits: int
    duplicate_case_ids: int

    @model_validator(mode="after")
    def validate_ranges(self) -> EvalReport:
        if not 0.0 <= self.recall_at_k <= 1.0:
            raise ValueError("recall_at_k must be between 0 and 1")
        if not 0.0 <= self.mean_reciprocal_rank <= 1.0:
            raise ValueError("mean_reciprocal_rank must be between 0 and 1")
        return self

    def passes(self, *, minimum_recall: float, minimum_mrr: float) -> bool:
        return (
            self.cases > 0
            and self.recall_at_k >= minimum_recall
            and self.mean_reciprocal_rank >= minimum_mrr
            and self.unauthorized_hits == 0
            and self.duplicate_case_ids == 0
        )


def load_cases(path: Path) -> tuple[EvalCase, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = tuple(EvalCase.model_validate(item) for item in raw.get("cases", []))
    if not cases:
        raise ValueError("gold set must contain at least one case")
    return cases


class CaseResult(BaseModel):
    """TEK sorgunun sonucu. Agregat DEGIL — bilincli olarak sorgu basina tutulur.

    Iki sebep (Fable 2026-08-17):
    1. `sweep` eskiden once genel, sonra katman basina `evaluate` cagiriyordu => AYNI
       sorgular her agirlikta IKI kez kosuyordu (11 agirlikta ~22N arama, N yeterken).
       Artik bir kez kosulur, genel ve katman agregatlari AYNI sonuclardan turetilir.
    2. Eslestirilmis (paired) bootstrap sorgu-basi farklara ihtiyac duyar; agregattan
       guven araligi cikarilamaz.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    layer: str | None
    recall: float = Field(ge=0.0, le=1.0)
    reciprocal_rank: float = Field(ge=0.0, le=1.0)
    unauthorized_hits: int = Field(ge=0)


async def run_cases(
    *, search: SearchProvider, cases: tuple[EvalCase, ...], top_k: int = 5
) -> tuple[CaseResult, ...]:
    if top_k < 1 or top_k > 100:
        raise ValueError("top_k must be between 1 and 100")
    results: list[CaseResult] = []
    for case in cases:
        hits = await search.search(principal=case.principal, query=case.query, limit=top_k)
        unauthorized = sum(not is_authorized(case.principal, hit.document) for hit in hits)
        # Ayni doc_id'nin birden fazla VERSIYONU listede yer alip k kotasini yiyebilir
        # (Fable 4-f). Sirayi koruyarak tekillestir: sıralama bilgisi kaybolmaz.
        retrieved: list[str] = []
        for hit in hits:
            if hit.document.document_id not in retrieved:
                retrieved.append(hit.document.document_id)
        matched = case.relevant_document_ids.intersection(retrieved)
        first_rank = next(
            (rank for rank, doc_id in enumerate(retrieved, start=1) if doc_id in case.relevant_document_ids),
            None,
        )
        results.append(
            CaseResult(
                case_id=case.case_id,
                layer=case.layer,
                recall=len(matched) / len(case.relevant_document_ids),
                reciprocal_rank=0.0 if first_rank is None else 1.0 / first_rank,
                unauthorized_hits=unauthorized,
            )
        )
    return tuple(results)


def aggregate(results: Sequence[CaseResult]) -> EvalReport:
    """Sorgu-basi sonuclardan agregat. Cagiran taraf ayni sonuclari yeniden kullanabilir."""
    if not results:
        raise ValueError("aggregate requires at least one case result")
    seen: set[str] = set()
    duplicates = 0
    for result in results:
        if result.case_id in seen:
            duplicates += 1
        seen.add(result.case_id)
    count = len(results)
    return EvalReport(
        cases=count,
        recall_at_k=sum(r.recall for r in results) / count,
        mean_reciprocal_rank=sum(r.reciprocal_rank for r in results) / count,
        unauthorized_hits=sum(r.unauthorized_hits for r in results),
        duplicate_case_ids=duplicates,
    )


async def evaluate(*, search: SearchProvider, cases: tuple[EvalCase, ...], top_k: int = 5) -> EvalReport:
    """Geriye uyumlu sarmalayici: kosur + agregatlar."""
    return aggregate(await run_cases(search=search, cases=cases, top_k=top_k))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the offline retrieval quality gate")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--gold-set", required=True, type=Path)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--minimum-recall", type=float, default=0.8)
    parser.add_argument("--minimum-mrr", type=float, default=0.8)
    return parser


async def _run(args: argparse.Namespace) -> int:
    plan = build_plan(source_root=args.source_root, manifest_path=args.manifest)
    cases = load_cases(args.gold_set)
    search = InMemoryHybridSearchProvider(
        plan.documents,
        embeddings=HashEmbeddingProvider(dimensions=256),
    )
    report = await evaluate(search=search, cases=cases, top_k=args.top_k)
    passed = report.passes(minimum_recall=args.minimum_recall, minimum_mrr=args.minimum_mrr)
    print(json.dumps({"passed": passed, **report.model_dump()}, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 2


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
