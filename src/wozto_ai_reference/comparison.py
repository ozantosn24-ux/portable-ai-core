"""Hibrit arama gerçekten iki bacağı da yeniyor mu — ağırlık taramalı karşılaştırma.

Cevaplamaya çalıştığı tek soru: *"hibrit, dense-only ve lexical-only'yi ÖLÇÜLEBİLİR
biçimde yeniyor mu?"* Bugüne kadar yalnız "hibrit arama VAR" denebiliyordu.

## Neden tek bir ağırlıkta ölçmek yetmez

Fable incelemesi (2026-08-17): tek bir `vector_weight` değerinde çıkan üstünlük,
o değerin şanslı seçilmiş olmasından gelebilir. İddia ancak **bir ARALIKTA** geçerliyse
anlamlıdır. O yüzden burada w taranır ve hibritin kazandığı aralık raporlanır.

## Neden katman katman raporlanır

Küme kompozisyonu kazananı önceden seçer: tamamı-paraphrase bir set yoğun bacağı,
tamamı-kelime-örtüşmeli bir set leksik bacağı **garanti** yener. Tek bir ortalama sayı
bunu gizler. `EvalCase.layer` etiketleri ayrı ayrı raporlanır ve hüküm, farkın
**üç katmanda da aynı yönde** olmasını arar.

## Ne söylemez

Bu modül bir **deney** koşturur, kapı DEĞİLDİR. Sonucu; tek model, tek korpus, tek
dil çifti ve tek lexical yapılandırması için geçerlidir. Gecikme/ölçek hakkında hiçbir
şey söylemez (HNSW bu sorgu kalıbında devrede değil — `pgvector_store.search` skoru tüm
yetkili satırlar için hesaplar).
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .evaluation import CaseResult, EvalCase, EvalReport, aggregate, run_cases
from .ports import SearchProvider

# Saf bacaklar: uc degerler HER ZAMAN taranir, aksi halde "hibrit kazandi" demek icin
# karsilastirilacak taban kalmaz.
LEXICAL_ONLY = 0.0
DENSE_ONLY = 1.0

# Bir katmanin hukme KATILMASI icin gereken en az sorgu sayisi.
# ⚠️ 2026-08-17'de olculdu: XQuAD-tr'de katman dagilimi 148 overlap / 1 morphology /
# 1 paraphrase cikti. Sebep yapisal -- SQuAD sorulari PARAGRAFA BAKILARAK yazilmis,
# yani leksik ortusme insa geregi var. Tek sorgulu bir katmanda "tutarli/tutarsiz"
# hukmu vermek gurultuyu bilgi gibi sunmaktir. Esigin altindaki katmanlar hukumden
# CIKARILIR ve raporda ADIYLA gorunur.
MIN_LAYER_CASES = 10

ProviderFactory = Callable[[float], SearchProvider | Awaitable[SearchProvider]]


class LayerReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    layer: str
    report: EvalReport


class WeightPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    vector_weight: float = Field(ge=0.0, le=1.0)
    overall: EvalReport
    layers: tuple[LayerReport, ...] = ()
    # Bootstrap ESLESTIRILMIS fark ister => sorgu-basi sonuclar tasinir.
    case_results: tuple[CaseResult, ...] = ()

    def metric(self, name: str) -> float:
        if name == "recall":
            return self.overall.recall_at_k
        if name == "mrr":
            return self.overall.mean_reciprocal_rank
        raise ValueError(f"unknown metric: {name!r}")


class Verdict(BaseModel):
    """Hibritin kazandigi ARALIK ve hangi kosulla kazandigi."""

    model_config = ConfigDict(frozen=True)

    metric: str
    lexical_only: float
    dense_only: float
    winning_weights: tuple[float, ...]
    best_weight: float | None
    best_value: float | None
    beats_both_legs: bool
    # Katman katman ayni yonde mi? Fable: fark UC katmanda da ayni yonde olmali.
    consistent_across_layers: bool | None
    # ⚠️ ONCEDEN SECILEN w (winner's curse'e karsi). 11 noktadan en iyisini POST-HOC
    # secip orada zafer ilan etmek istatistiksel olarak sakattir; birincil karsilastirma
    # uretim varsayilaninda yapilir, tarama SAGLAMLIK katmanidir.
    primary_weight: float | None = None
    primary_value: float | None = None
    primary_beats_both_legs: bool = False
    # Eslestirilmis bootstrap %95 GA (hibrit - bacak). Sifiri DISLAMIYORSA ustunluk
    # iddiasi kucuk N'de gurultuden ayirt edilemez.
    ci_vs_lexical: tuple[float, float] | None = None
    ci_vs_dense: tuple[float, float] | None = None
    significant: bool | None = None
    # Kazanan w'ler taranan gridde TEK ARALIK mi? Bitisik olmayan kazanc (or. {0.3, 0.7}
    # ama 0.5 degil) tipik GURULTU imzasidir; tavan cumlesi hak edilmez.
    contiguous: bool = True
    # Orneklem yetersizligi yuzunden hukme KATILMAYAN katmanlar (adiyla + n ile).
    # Sessizce dislamak, "tum katmanlarda tutarli" izlenimi verirdi.
    skipped_layers: tuple[str, ...] = ()


class SweepResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    top_k: int
    points: tuple[WeightPoint, ...]
    verdicts: tuple[Verdict, ...]

    def claim_sentence(self) -> str:
        """Sonucun HAK ETTIRDIGI cumle -- fazlasi degil.

        Kazanmadiysa bunu ACIKCA yazar; "olcmedik" ile "olctuk ve kazanmadi" ayri
        seylerdir ve ikincisi de bir sonuctur.
        N ve k CUMLENIN ICINDE tasinir: "kazandi" cumlesi kac sorgu ve hangi k olmadan
        YANILTICIDIR (Fable bulgusu 2026-08-17).
        """
        head = self.points[0]
        sizes = ", ".join(f"{lr.layer}={lr.report.cases}" for lr in head.layers) or "katman yok"
        parts: list[str] = [f"N={head.overall.cases}, k={self.top_k} ({sizes})"]
        for verdict in self.verdicts:
            if not verdict.beats_both_legs:
                parts.append(
                    f"{verdict.metric}: hibrit iki bacagi da GECMEDI "
                    f"(lexical-only={verdict.lexical_only:.3f}, "
                    f"dense-only={verdict.dense_only:.3f})"
                )
                continue
            weights = ", ".join(f"{w:g}" for w in verdict.winning_weights)
            notes = [
                "katmanlarda tutarli"
                if verdict.consistent_across_layers
                else "katmanlarda TUTARSIZ"
                if verdict.consistent_across_layers is False
                else "katman etiketi yok"
            ]
            if verdict.skipped_layers:
                notes.append(
                    "hukme KATILMAYAN katmanlar (ornek yetersiz): "
                    + ", ".join(verdict.skipped_layers)
                )
            if not verdict.contiguous:
                notes.append("KIRILGAN: kazanan w'ler bitisik DEGIL (gurultu imzasi)")
            if verdict.significant is True:
                notes.append(
                    f"paired bootstrap %95 GA sifiri disliyor "
                    f"(vs lexical {verdict.ci_vs_lexical[0]:+.3f}..{verdict.ci_vs_lexical[1]:+.3f}, "
                    f"vs dense {verdict.ci_vs_dense[0]:+.3f}..{verdict.ci_vs_dense[1]:+.3f})"
                )
            elif verdict.significant is False:
                notes.append("GA sifiri DISLAMIYOR => ustunluk gurultuden ayirt edilemiyor")
            if verdict.primary_weight is not None:
                notes.append(
                    f"birincil w={verdict.primary_weight:g}: "
                    + ("gecti" if verdict.primary_beats_both_legs else "GECMEDI")
                )
            parts.append(
                f"{verdict.metric}: hibrit w={{{weights}}} icin iki bacagi da gecti "
                f"(en iyi w={verdict.best_weight:g} -> {verdict.best_value:.3f}; "
                f"lexical-only={verdict.lexical_only:.3f}, "
                f"dense-only={verdict.dense_only:.3f}; " + "; ".join(notes) + ")"
            )
        return " | ".join(parts)


def _layers_of(cases: Sequence[EvalCase]) -> tuple[str, ...]:
    return tuple(sorted({case.layer for case in cases if case.layer}))


async def _resolve(factory: ProviderFactory, weight: float) -> SearchProvider:
    provider = factory(weight)
    if isinstance(provider, Awaitable):
        return await provider
    return provider


def _metric_of(result: CaseResult, metric: str) -> float:
    return result.recall if metric == "recall" else result.reciprocal_rank


def _paired_bootstrap_ci(
    treatment: Sequence[CaseResult],
    baseline: Sequence[CaseResult],
    metric: str,
    *,
    iterations: int = 2000,
    seed: int = 20260817,
) -> tuple[float, float]:
    """Sorgu-basi farklar uzerinden ESLESTIRILMIS bootstrap %95 GA.

    Ayni sorgular uc sistemde de kostugu icin fark eslestirilmistir; eslestirilmis
    bootstrap kucuk N'de nokta-tahminden cok daha durust bir cevap verir.
    ⚠️ `seed` SABIT: sonuc repoya commit'lenecek, tekrarlanabilir olmali.
    Kutuphane YOK (stdlib `random`) — cekirdek hafif kalir.
    """
    by_id = {r.case_id: r for r in baseline}
    diffs = [
        _metric_of(t, metric) - _metric_of(by_id[t.case_id], metric)
        for t in treatment
        if t.case_id in by_id
    ]
    if not diffs:
        raise ValueError("bootstrap icin eslesen sorgu yok")
    rng = random.Random(seed)
    size = len(diffs)
    means = []
    for _ in range(iterations):
        means.append(sum(rng.choice(diffs) for _ in range(size)) / size)
    means.sort()
    lower = means[int(0.025 * iterations)]
    upper = means[min(int(0.975 * iterations), iterations - 1)]
    return (lower, upper)


def _is_contiguous(winners: Sequence[float], grid: Sequence[float]) -> bool:
    """Kazanan w'ler taranan gridde TEK ARALIK mi?"""
    if len(winners) <= 1:
        return True
    positions = [grid.index(w) for w in winners]
    return positions == list(range(min(positions), max(positions) + 1))


def _verdict(
    points: Sequence[WeightPoint],
    metric: str,
    layers: Sequence[str],
    primary_weight: float | None,
    min_layer_cases: int = MIN_LAYER_CASES,
) -> Verdict:
    by_weight = {point.vector_weight: point for point in points}
    grid = [p.vector_weight for p in points]
    lexical = by_weight[LEXICAL_ONLY].metric(metric)
    dense = by_weight[DENSE_ONLY].metric(metric)
    floor = max(lexical, dense)

    # SIKI esitsizlik: berabere kalmak "yendi" DEGILDIR.
    winners = tuple(
        point.vector_weight
        for point in points
        if LEXICAL_ONLY < point.vector_weight < DENSE_ONLY and point.metric(metric) > floor
    )
    best_point = max(
        (p for p in points if p.vector_weight in winners), key=lambda p: p.metric(metric), default=None
    )

    def layer_value(weight: float, layer: str) -> float:
        point = by_weight[weight]
        match = next((lr for lr in point.layers if lr.layer == layer), None)
        if match is None:
            # ⚠️ Sessiz 0.0 DONMEZ (Fable bulgusu): eksik katman tabani 0'a cekip
            # SAHTE "tutarli" uretirdi -- yani hatayi kendi lehine cevirirdi.
            raise ValueError(f"w={weight} icin '{layer}' katman raporu YOK")
        return match.report.recall_at_k if metric == "recall" else match.report.mean_reciprocal_rank

    # Tutarlilik KAZANAN ARALIGIN TAMAMINDA aranir (yalniz best_weight'te degil) ve
    # genel hukumle ayni SIKI esitsizligi kullanir.
    # Esik alti katmanlar hukumden cikarilir (bkz. MIN_LAYER_CASES).
    def layer_size(layer: str) -> int:
        point = by_weight[LEXICAL_ONLY]
        match = next((lr for lr in point.layers if lr.layer == layer), None)
        return match.report.cases if match else 0

    judged_layers = tuple(l for l in layers if layer_size(l) >= min_layer_cases)
    skipped_layers = tuple(
        f"{l}(n={layer_size(l)})" for l in layers if layer_size(l) < min_layer_cases
    )

    consistent: bool | None = None
    if winners and judged_layers:
        consistent = all(
            layer_value(w, layer) > max(layer_value(LEXICAL_ONLY, layer), layer_value(DENSE_ONLY, layer))
            for w in winners
            for layer in judged_layers
        )

    # BIRINCIL nokta (onceden secilmis) — winner's curse'e karsi
    primary_value = None
    primary_wins = False
    ci_lex = ci_dense = None
    significant = None
    if primary_weight is not None and primary_weight in by_weight:
        primary_point = by_weight[primary_weight]
        primary_value = primary_point.metric(metric)
        primary_wins = primary_value > floor
        if primary_point.case_results:
            ci_lex = _paired_bootstrap_ci(
                primary_point.case_results, by_weight[LEXICAL_ONLY].case_results, metric
            )
            ci_dense = _paired_bootstrap_ci(
                primary_point.case_results, by_weight[DENSE_ONLY].case_results, metric
            )
            significant = ci_lex[0] > 0 and ci_dense[0] > 0

    return Verdict(
        metric=metric,
        lexical_only=lexical,
        dense_only=dense,
        winning_weights=winners,
        best_weight=best_point.vector_weight if best_point else None,
        best_value=best_point.metric(metric) if best_point else None,
        beats_both_legs=bool(winners),
        consistent_across_layers=consistent,
        primary_weight=primary_weight,
        primary_value=primary_value,
        primary_beats_both_legs=primary_wins,
        ci_vs_lexical=ci_lex,
        ci_vs_dense=ci_dense,
        significant=significant,
        contiguous=_is_contiguous(winners, grid),
        skipped_layers=skipped_layers,
    )


async def sweep(
    *,
    factory: ProviderFactory,
    cases: Sequence[EvalCase],
    weights: Sequence[float] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    top_k: int = 5,
    primary_weight: float | None = 0.7,
    min_layer_cases: int = MIN_LAYER_CASES,
) -> SweepResult:
    """Agirlik taramasi. `primary_weight` ONCEDEN secilir (varsayilan: uretim degeri 0.7).

    ⚠️ Birincil noktayi taramadan SONRA secmek (en iyisini alip orada zafer ilan etmek)
    winner's curse'tur. Tarama SAGLAMLIK katmanidir; hukum birincil noktada verilir.
    """
    if not cases:
        raise ValueError("sweep requires at least one case")
    ordered = sorted({round(float(w), 4) for w in weights})
    for endpoint in (LEXICAL_ONLY, DENSE_ONLY):
        if endpoint not in ordered:
            raise ValueError(
                f"agirlik taramasi {endpoint} icermeli: saf bacaklar olmadan "
                "'hibrit iki bacagi da gecti' iddiasinin TABANI yoktur"
            )
    if primary_weight is not None and round(float(primary_weight), 4) not in ordered:
        raise ValueError(f"primary_weight={primary_weight} taramada yok")
    layers = _layers_of(cases)
    case_tuple = tuple(cases)

    points: list[WeightPoint] = []
    for weight in ordered:
        provider = await _resolve(factory, weight)
        # ⚠️ TEK kosu. Eskiden genel + her katman icin AYRI `evaluate` cagriliyordu =>
        # ayni sorgular agirlik basina 2 kez, 11 agirlikta ~22N arama (N yeterken).
        # Gercek e5 + Postgres'le bu kalite degil KOTA meselesiydi (Fable bulgusu).
        results = await run_cases(search=provider, cases=case_tuple, top_k=top_k)
        layer_reports = tuple(
            LayerReport(
                layer=layer,
                report=aggregate([r for r in results if r.layer == layer]),
            )
            for layer in layers
        )
        points.append(
            WeightPoint(
                vector_weight=weight,
                overall=aggregate(results),
                layers=layer_reports,
                case_results=results,
            )
        )

    verdicts = tuple(
        _verdict(points, metric, layers, primary_weight, min_layer_cases)
        for metric in ("recall", "mrr")
    )
    return SweepResult(top_k=top_k, points=tuple(points), verdicts=verdicts)
