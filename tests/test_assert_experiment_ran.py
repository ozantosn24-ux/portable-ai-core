"""Deney sahiciligi kontrolu — kendisi de olculmeli.

Bu kontrol, "hibrit kazandi mi"ya BAKMAZ (deney bir kapi degildir; kaybetmek de bir
sonuctur). Yalnizca kosunun sahiciligini sinar. En olasi SESSIZ hata, yanlislikla
`HashEmbeddingProvider` ile kosup sonucu anlamsal sanmaktir: cikti JSON'u aynidir,
yalnizca provenance'taki gomme-uzayi kimligi farklidir.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "assert_experiment_ran.py"
_spec = importlib.util.spec_from_file_location("assert_experiment_ran", MODULE)
aer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aer)


def _write(tmp_path: Path, **prov_overrides) -> Path:
    prov = {
        "embedding_space": "intfloat/multilingual-e5-small@614241f6|tsc=turkish",
        "provider": "pgvector",
        "git_sha": "deadbeefcafe1234",
        "cases": 150,
        "top_k": 5,
        "text_search_config": "turkish",
    }
    prov.update(prov_overrides)
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps({"provenance": prov, "points": [{"x": 1}], "claim_sentence": "GECMEDI"}),
        encoding="utf-8",
    )
    return path


def test_valid_result_passes(tmp_path):
    """Yanlis-pozitif kontrolu: gecerli sonuc REDDEDILMEMELI.

    ⚠️ `claim_sentence` bilerek 'GECMEDI' — kontrol sonucun ICERIGINE bakmamali."""
    assert aer.check(_write(tmp_path)) == []


def test_hash_embedding_result_is_rejected(tmp_path):
    """ASIL KAPI: hash gommesi anlamsal DEGILDIR, sonucu rapor edilemez."""
    problems = aer.check(_write(tmp_path, embedding_space="hash-blake2b-nonsemantic/256"))
    assert any("anlamsal DEGIL" in p for p in problems), problems


def test_memory_provider_is_rejected(tmp_path):
    """`memory` yolu GERCEK SQL yolunu (ts_rank_cd + <=>) sinamaz; ayri bir sistemdir."""
    problems = aer.check(_write(tmp_path, provider="memory"))
    assert any("gercek SQL yolu" in p for p in problems), problems


@pytest.mark.parametrize("bozuk", [{"git_sha": "bilinmiyor"}, {"embedding_space": "bilinmiyor"}])
def test_missing_provenance_is_rejected(tmp_path, bozuk):
    """Provenance eksikse sonuc bir koda/uzaya BAGLANAMAZ => rapor edilemez."""
    assert aer.check(_write(tmp_path, **bozuk))


def test_missing_file_is_rejected(tmp_path):
    assert aer.check(tmp_path / "yok.json")


def test_empty_points_is_rejected(tmp_path):
    path = _write(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["points"] = []
    path.write_text(json.dumps(data), encoding="utf-8")
    assert aer.check(path)


def _point(weight: float, recall: float, mrr: float = 0.4) -> dict:
    return {
        "vector_weight": weight,
        "overall": {"recall_at_k": recall, "mean_reciprocal_rank": mrr},
    }


def _write_points(tmp_path: Path, points: list[dict]) -> Path:
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(
            {
                "provenance": {
                    "embedding_space": "intfloat/multilingual-e5-small@614241f6|tsc=turkish",
                    "provider": "pgvector",
                    "git_sha": "deadbeefcafe1234",
                    "cases": 150,
                    "top_k": 5,
                    "text_search_config": "turkish",
                },
                "points": points,
                "claim_sentence": "GECMEDI",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_dead_lexical_leg_is_rejected(tmp_path):
    """⚠️ 2026-08-17 regresyonu: bu sonuc ESKI kapidan YESIL gecerdi.

    `plainto_tsquery` AND'ledigi icin leksik bacak 0/150 sorguda atesleniyordu;
    w>0'in tamami dense-only ile ayni siralamayi uretiyordu. `points` dolu,
    `claim_sentence` var, provenance tam ⇒ eski kontrollerin hepsi geciyordu ve
    "olctuk, hibrit kazanmadi" diye GECERSIZ bir olcum yayinlanacakti.
    """
    olu = [_point(w / 10, 0.6) for w in range(1, 11)]

    problems = aer.check(_write_points(tmp_path, olu))

    assert any("LEKSIK BACAK OLU" in p for p in problems), problems


def test_live_lexical_leg_passes(tmp_path):
    """Yanlis-pozitif kontrolu: agirlik metrigi degistiriyorsa bacak CANLIDIR."""
    canli = [_point(w / 10, 0.6 + w * 0.001) for w in range(1, 11)]

    assert aer.check(_write_points(tmp_path, canli)) == []


def test_single_weight_point_is_not_flagged(tmp_path):
    """Tek nokta varsa 'hepsi ayni' anlamsizdir - kontrol susmali."""
    assert aer.check(_write_points(tmp_path, [_point(0.7, 0.6)])) == []


def test_ceiling_regime_gets_its_own_diagnosis(tmp_path):
    """Ayni imza (hepsi ayni), FARKLI ariza: korpus ayirim gucu yoksa teshis 'olu bacak'
    degildir. Tek metin operatoru yanlis yere baktirirdi (Fable 2. tur)."""
    tavan = [_point(w / 10, 1.0, 1.0) for w in range(1, 11)]

    problems = aer.check(_write_points(tmp_path, tavan))

    assert any("TAVAN REJIMI" in p for p in problems), problems
    assert not any("LEKSIK BACAK OLU" in p for p in problems), problems


def test_unpinned_model_revision_is_rejected(tmp_path):
    """⛔ Pinsiz kosu ATIFLANAMAZ (Fable KARAR 1, 2026-08-17).

    Pin yokken farkli tarihlerde farkli snapshot AYNI kimligi tasir ⇒ uzay uyusmazligi
    yapisal olarak tespit EDILEMEZ. Kapi bunu uyari degil HATA sayar: headless bir
    koshuda uyari, kimsenin okumadigi satirdir.
    """
    path = _write(tmp_path, embedding_space="intfloat/multilingual-e5-small@unpinned")

    problems = aer.check(path)

    assert any("PINSIZ" in p for p in problems), problems


def test_missing_text_search_config_is_rejected(tmp_path):
    """Leksik bacagin KIMLIGI provenance'ta olmadan sonuc okunamaz (Fable KARAR 2).

    'lexical-only = 0.780' sayisi, hangi config'le uretildigi bilinmeden anlamsizdir:
    `simple` (stopword'suz TF) ile `turkish` (stem + stopword) AYNI SEY DEGILDIR.
    """
    path = _write(tmp_path)
    import json as _json

    data = _json.loads(path.read_text(encoding="utf-8"))
    del data["provenance"]["text_search_config"]
    path.write_text(_json.dumps(data), encoding="utf-8")

    assert any("leksik config" in p for p in aer.check(path)), aer.check(path)
