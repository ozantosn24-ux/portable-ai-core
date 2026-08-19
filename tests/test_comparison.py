"""Ağırlık taramalı karşılaştırma harness'ı — SONUCU ÖNCEDEN BİLİNEN kurgularla.

Neden sentetik: harness'ın kendisi doğru olmadan gerçek veriyle çıkan sayı hiçbir şey
ifade etmez. Burada sahte bir arama sağlayıcısı kullanılır; hangi ağırlıkta neyin
döneceğini test BELİRLER, dolayısıyla hükmün doğru olup olmadığı KESİN bilinir.
"""

from __future__ import annotations

import asyncio

import pytest

from wozto_ai_reference.comparison import DENSE_ONLY, LEXICAL_ONLY, sweep
from wozto_ai_reference.domain import Document, Principal, RetrievalHit
from wozto_ai_reference.evaluation import EvalCase

TENANT = "t1"
PRINCIPAL = Principal(tenant_id=TENANT, user_id="u", roles=frozenset())


def _doc(doc_id: str) -> Document:
    return Document(
        tenant_id=TENANT,
        document_id=doc_id,
        version="v1",
        title=doc_id,
        section="Main",
        source_uri=f"memory://{doc_id}",
        content=f"content of {doc_id}",
        content_hash=f"sha256:{doc_id}",
        acl_roles=frozenset(),
    )


class _ScriptedSearch:
    """ÖNCEDEN YAZILMIŞ sıralamayı döndürür; sorgu başına farklı olabilir."""

    def __init__(self, ranking: list[str] | dict[str, list[str]]) -> None:
        self._ranking = ranking

    async def search(self, *, principal, query, limit):
        ranking = (
            self._ranking if isinstance(self._ranking, list) else self._ranking.get(query, [])
        )
        return [
            RetrievalHit(document=_doc(doc_id), score=1.0 - index / 10)
            for index, doc_id in enumerate(ranking[:limit])
        ]


def _cases(layer: str | None = None) -> tuple[EvalCase, ...]:
    return (
        EvalCase(
            case_id="c1",
            principal=PRINCIPAL,
            query="q",
            relevant_document_ids=frozenset({"hedef"}),
            layer=layer,
        ),
    )


def test_hybrid_that_beats_both_legs_is_reported_as_such():
    """Uc degerlerde hedef 2. sirada, ortada 1. sirada => hibrit KAZANIR."""

    def factory(weight: float):
        ranking = ["hedef", "x"] if 0.0 < weight < 1.0 else ["x", "hedef"]
        return _ScriptedSearch(ranking)

    result = asyncio.run(sweep(factory=factory, cases=_cases(), weights=(0.0, 0.5, 1.0), top_k=2, primary_weight=0.5))
    mrr = next(v for v in result.verdicts if v.metric == "mrr")
    assert mrr.beats_both_legs
    assert mrr.winning_weights == (0.5,)
    assert mrr.best_value == pytest.approx(1.0)
    assert "iki bacagi da gecti" in result.claim_sentence()


def test_no_win_is_reported_honestly_not_hidden():
    """Hibrit kazanmiyorsa cumle bunu ACIKCA soylemeli.

    "Olcmedik" ile "olctuk ve kazanmadi" AYRI seylerdir; ikincisi de bir sonuctur ve
    gizlenirse rapor yanlis okunur."""

    def factory(weight: float):
        # Her agirlikta AYNI: hibritin ustunlugu YOK
        return _ScriptedSearch(["hedef", "x"])

    result = asyncio.run(sweep(factory=factory, cases=_cases(), weights=(0.0, 0.5, 1.0), top_k=2, primary_weight=0.5))
    assert all(not v.beats_both_legs for v in result.verdicts)
    assert "GECMEDI" in result.claim_sentence()


def test_tie_is_not_a_win():
    """Berabere kalmak 'yendi' DEGILDIR — siki esitsizlik araniyor."""

    def factory(weight: float):
        return _ScriptedSearch(["hedef", "x"] if weight in (0.0, 0.5) else ["x", "hedef"])

    result = asyncio.run(sweep(factory=factory, cases=_cases(), weights=(0.0, 0.5, 1.0), top_k=2, primary_weight=0.5))
    mrr = next(v for v in result.verdicts if v.metric == "mrr")
    # w=0.5, lexical-only ile ESIT (ikisi de 1.0) => taban = 1.0, siki gecis YOK
    assert not mrr.beats_both_legs, "beraberlik kazanc sayildi"


def test_sweep_requires_both_pure_legs():
    """Saf bacaklar olmadan 'iki bacagi da gecti' iddiasinin TABANI yoktur."""
    with pytest.raises(ValueError, match="saf bacaklar"):
        asyncio.run(
            sweep(factory=lambda w: _ScriptedSearch(["hedef"]), cases=_cases(), weights=(0.3, 0.7))
        )


def test_layers_are_reported_separately():
    """Kume kompozisyonu kazanani onceden secebilir => katmanlar AYRI raporlanmali."""
    cases = (
        EvalCase(
            case_id="a",
            principal=PRINCIPAL,
            query="q",
            relevant_document_ids=frozenset({"hedef"}),
            layer="overlap",
        ),
        EvalCase(
            case_id="b",
            principal=PRINCIPAL,
            query="q",
            relevant_document_ids=frozenset({"hedef"}),
            layer="paraphrase",
        ),
    )
    result = asyncio.run(
        sweep(factory=lambda w: _ScriptedSearch(["hedef"]), cases=cases, weights=(0.0, 0.5, 1.0), top_k=1, primary_weight=0.5)
    )
    for point in result.points:
        assert {lr.layer for lr in point.layers} == {"overlap", "paraphrase"}


def test_layer_inconsistency_is_surfaced():
    """Bir katmanda KAYBEDIYORSA 'tutarli' DENMEMELI — ortalama bunu gizler.

    ⚠️ Bu test ilk yazimda TOTOLOJIKTI: govdesi `if mrr.beats_both_legs:` icindeydi ve
    kurgu kazanc uretmedigi icin HIC KOSMUYORDU (mutasyon testi yakaladi, 2026-08-17).
    Kurgu artik ELDE HESAPLANDI ve kazanc GARANTI:
      3 x overlap + 1 x paraphrase, top_k=2
      w=0.0 (leksik) : overlap rank2 (RR .5 x3), paraphrase yok      -> MRR 1.5/4 = 0.375
      w=1.0 (yogun)  : overlap yok, paraphrase rank1 (RR 1)          -> MRR 1.0/4 = 0.250
      w=0.5 (hibrit) : overlap rank1 (RR 1 x3), paraphrase yok       -> MRR 3.0/4 = 0.750
      taban = max(0.375, 0.250) = 0.375  =>  0.750 > 0.375  => KAZANIR
      paraphrase katmani: hibrit 0.0, yogun 1.0 => o katmanda KAYBEDER => TUTARSIZ
    """
    cases = tuple(
        EvalCase(
            case_id=f"o{i}",
            principal=PRINCIPAL,
            query=f"overlap-{i}",
            relevant_document_ids=frozenset({f"hedef{i}"}),
            layer="overlap",
        )
        for i in range(3)
    ) + (
        EvalCase(
            case_id="p0",
            principal=PRINCIPAL,
            query="paraphrase-0",
            relevant_document_ids=frozenset({"digeri"}),
            layer="paraphrase",
        ),
    )

    def factory(weight: float):
        if weight == LEXICAL_ONLY:
            return _ScriptedSearch({f"overlap-{i}": ["x", f"hedef{i}"] for i in range(3)})
        if weight == DENSE_ONLY:
            return _ScriptedSearch({"paraphrase-0": ["digeri", "x"]})
        return _ScriptedSearch({f"overlap-{i}": [f"hedef{i}", "x"] for i in range(3)})

    result = asyncio.run(sweep(factory=factory, cases=cases, weights=(0.0, 0.5, 1.0), top_k=2, primary_weight=0.5, min_layer_cases=1))
    mrr = next(v for v in result.verdicts if v.metric == "mrr")
    assert mrr.beats_both_legs, (
        "on kosul: kurgu geregi hibrit KAZANMALIYDI "
        f"(lexical={mrr.lexical_only}, dense={mrr.dense_only}, best={mrr.best_value})"
    )
    assert mrr.consistent_across_layers is False, (
        "paraphrase katmaninda kaybettigi halde 'tutarli' dendi"
    )
    assert "TUTARSIZ" in result.claim_sentence()


def test_pure_leg_constants_are_the_endpoints():
    assert (LEXICAL_ONLY, DENSE_ONLY) == (0.0, 1.0)


# --- Fable incelemesi 2026-08-17 sonrasi eklenen kapilar ---------------------


def test_primary_weight_must_be_in_the_grid():
    """ONCEDEN secilen nokta taramada YOKSA hukum verilemez."""
    with pytest.raises(ValueError, match="primary_weight"):
        asyncio.run(
            sweep(
                factory=lambda w: _ScriptedSearch(["hedef"]),
                cases=_cases(),
                weights=(0.0, 0.5, 1.0),
                primary_weight=0.7,
            )
        )


def test_non_contiguous_winners_are_flagged_as_fragile():
    """Bitisik OLMAYAN kazanan kumesi ({0.25, 0.75} ama 0.5 degil) gurultu imzasidir."""

    def factory(weight: float):
        # yalniz 0.25 ve 0.75 hedefi 1. siraya koyar; 0.5 koymaz
        return _ScriptedSearch(["hedef", "x"] if weight in (0.25, 0.75) else ["x", "hedef"])

    result = asyncio.run(
        sweep(
            factory=factory,
            cases=_cases(),
            weights=(0.0, 0.25, 0.5, 0.75, 1.0),
            top_k=2,
            primary_weight=0.5,
        )
    )
    mrr = next(v for v in result.verdicts if v.metric == "mrr")
    assert mrr.winning_weights == (0.25, 0.75)
    assert mrr.contiguous is False, "bitisik olmayan kazanc 'bitisik' raporlandi"
    assert "KIRILGAN" in result.claim_sentence()


def test_contiguous_winners_are_not_flagged():
    """Ikizi: bitisik kazanc KIRILGAN diye etiketlenmemeli (yanlis pozitif kontrolu)."""

    def factory(weight: float):
        return _ScriptedSearch(["hedef", "x"] if weight in (0.25, 0.5) else ["x", "hedef"])

    result = asyncio.run(
        sweep(factory=factory, cases=_cases(), weights=(0.0, 0.25, 0.5, 0.75, 1.0),
              top_k=2, primary_weight=0.5)
    )
    mrr = next(v for v in result.verdicts if v.metric == "mrr")
    assert mrr.winning_weights == (0.25, 0.5)
    assert mrr.contiguous is True
    assert "KIRILGAN" not in result.claim_sentence()


def test_bootstrap_ci_rejects_a_noise_sized_difference():
    """Kucuk ve TUTARSIZ bir fark, GA sifiri dislamadigi icin 'anlamli' SAYILMAMALI.

    10 sorgu: hibrit yalnizca BIRINDE ustun, digerlerinde esit => nokta-tahmin
    'kazandi' der ama eslestirilmis GA sifiri kapsar.
    """
    cases = tuple(
        EvalCase(case_id=f"c{i}", principal=PRINCIPAL, query=f"q{i}",
                 relevant_document_ids=frozenset({f"h{i}"}))
        for i in range(10)
    )

    def factory(weight: float):
        if 0.0 < weight < 1.0:
            # yalniz c0'da 1. sira; digerlerinde uc degerlerle AYNI
            return _ScriptedSearch({f"q{i}": ([f"h{i}", "x"] if i == 0 else ["x", f"h{i}"])
                                    for i in range(10)})
        return _ScriptedSearch({f"q{i}": ["x", f"h{i}"] for i in range(10)})

    result = asyncio.run(
        sweep(factory=factory, cases=cases, weights=(0.0, 0.5, 1.0), top_k=2, primary_weight=0.5)
    )
    mrr = next(v for v in result.verdicts if v.metric == "mrr")
    assert mrr.beats_both_legs, "on kosul: nokta-tahmin duzeyinde kazanmali"
    assert mrr.ci_vs_lexical is not None and mrr.ci_vs_dense is not None
    assert mrr.significant is False, (
        f"tek sorgudan gelen fark 'anlamli' sayildi: GA={mrr.ci_vs_lexical}"
    )
    assert "ayirt edilemiyor" in result.claim_sentence()


def test_bootstrap_ci_accepts_a_consistent_difference():
    """Ikizi: HER sorguda ustunse GA sifiri dislamali."""
    cases = tuple(
        EvalCase(case_id=f"c{i}", principal=PRINCIPAL, query=f"q{i}",
                 relevant_document_ids=frozenset({f"h{i}"}))
        for i in range(10)
    )

    def factory(weight: float):
        if 0.0 < weight < 1.0:
            return _ScriptedSearch({f"q{i}": [f"h{i}", "x"] for i in range(10)})
        return _ScriptedSearch({f"q{i}": ["x", f"h{i}"] for i in range(10)})

    result = asyncio.run(
        sweep(factory=factory, cases=cases, weights=(0.0, 0.5, 1.0), top_k=2, primary_weight=0.5)
    )
    mrr = next(v for v in result.verdicts if v.metric == "mrr")
    assert mrr.significant is True, f"tutarli fark 'anlamsiz' sayildi: GA={mrr.ci_vs_lexical}"
    assert mrr.ci_vs_lexical[0] > 0 and mrr.ci_vs_dense[0] > 0


def test_claim_sentence_carries_n_and_k():
    """'Kazandi' cumlesi kac sorgu ve hangi k olmadan YANILTICIDIR."""
    result = asyncio.run(
        sweep(factory=lambda w: _ScriptedSearch(["hedef"]), cases=_cases(),
              weights=(0.0, 0.5, 1.0), top_k=3, primary_weight=0.5)
    )
    sentence = result.claim_sentence()
    assert "N=1" in sentence and "k=3" in sentence


def test_sweep_runs_each_case_once_per_weight():
    """KOTA kapisi: katman raporlari AYNI kosudan turetilmeli, sorgular tekrarlanmamali."""
    calls: list[str] = []

    class _Counting(_ScriptedSearch):
        async def search(self, *, principal, query, limit):
            calls.append(query)
            return await super().search(principal=principal, query=query, limit=limit)

    cases = (
        EvalCase(case_id="a", principal=PRINCIPAL, query="q1",
                 relevant_document_ids=frozenset({"hedef"}), layer="overlap"),
        EvalCase(case_id="b", principal=PRINCIPAL, query="q2",
                 relevant_document_ids=frozenset({"hedef"}), layer="paraphrase"),
    )
    asyncio.run(sweep(factory=lambda w: _Counting(["hedef"]), cases=cases,
                      weights=(0.0, 0.5, 1.0), top_k=1, primary_weight=0.5))
    # 3 agirlik x 2 sorgu = 6. Katman basina yeniden kosulsaydi 12 olurdu.
    assert len(calls) == 6, f"sorgular tekrar kosuldu: {len(calls)} cagri (beklenen 6)"


def test_missing_layer_report_raises_instead_of_scoring_zero():
    """Savunma dali DOGRUDAN sinanir — bugun ulasilamaz oldugu icin mutasyon testi
    kacirmisti (2026-08-17).

    Onemi: sessiz `0.0` donseydi eksik katman TABANI 0'a ceker ve SAHTE "tutarli"
    uretirdi — yani hata kendi lehine calisirdi. Fail-closed olmali.
    """
    from wozto_ai_reference.comparison import LayerReport, WeightPoint, _verdict
    from wozto_ai_reference.evaluation import EvalReport

    def report(mrr: float) -> EvalReport:
        return EvalReport(
            cases=1, recall_at_k=mrr, mean_reciprocal_rank=mrr,
            unauthorized_hits=0, duplicate_case_ids=0,
        )

    def point(weight: float, mrr: float, *, with_layer: bool) -> WeightPoint:
        return WeightPoint(
            vector_weight=weight,
            overall=report(mrr),
            layers=(LayerReport(layer="overlap", report=report(mrr)),) if with_layer else (),
        )

    points = [
        point(0.0, 0.2, with_layer=True),
        point(0.5, 0.9, with_layer=False),  # KAZANAN ama katman raporu EKSIK
        point(1.0, 0.3, with_layer=True),
    ]
    with pytest.raises(ValueError, match="katman raporu YOK"):
        _verdict(points, "mrr", ("overlap",), 0.5, 1)


def test_undersized_layers_are_excluded_and_named():
    """Ornek yetersiz katman hukme KATILMAZ ve raporda ADIYLA gorunur.

    ⚠️ 2026-08-17'de olculdu: XQuAD-tr'de dagilim 148 overlap / 1 morphology /
    1 paraphrase. Sebep yapisal — SQuAD sorulari PARAGRAFA BAKILARAK yazilmis, leksik
    ortusme insa geregi var. Tek sorgulu katmanda "tutarli" demek gurultuyu bilgi gibi
    sunmaktir. Sessizce dislamak da olmaz: "tum katmanlarda tutarli" izlenimi verirdi.
    """
    cases = tuple(
        EvalCase(case_id=f"o{i}", principal=PRINCIPAL, query=f"q{i}",
                 relevant_document_ids=frozenset({f"h{i}"}), layer="overlap")
        for i in range(12)
    ) + (
        EvalCase(case_id="p0", principal=PRINCIPAL, query="qp",
                 relevant_document_ids=frozenset({"hp"}), layer="paraphrase"),
    )

    def factory(weight: float):
        if 0.0 < weight < 1.0:
            return _ScriptedSearch({f"q{i}": [f"h{i}", "x"] for i in range(12)})
        return _ScriptedSearch({f"q{i}": ["x", f"h{i}"] for i in range(12)})

    result = asyncio.run(
        sweep(factory=factory, cases=cases, weights=(0.0, 0.5, 1.0), top_k=2,
              primary_weight=0.5, min_layer_cases=10)
    )
    mrr = next(v for v in result.verdicts if v.metric == "mrr")
    assert mrr.beats_both_legs, "on kosul: hibrit kazanmali"
    assert mrr.skipped_layers == ("paraphrase(n=1)",), mrr.skipped_layers
    # ⚠️ Esigin ETKISI de sinaniyor, yalniz raporlamasi degil: tek-sorgulu paraphrase
    # katmani hukme girseydi (o katmanda hibrit kazanmiyor) `consistent` FALSE olurdu.
    # Ilk yazimda bu assert yoktu ve "esigi kaldir" mutasyonu KACIRILMISTI.
    assert mrr.consistent_across_layers is True, (
        "esik alti katman hukme sizmis olmali degil"
    )
    sentence = result.claim_sentence()
    assert "hukme KATILMAYAN" in sentence and "paraphrase(n=1)" in sentence
