"""Deneyin GERÇEKTEN koştuğunu doğrular — sonucun NE OLDUĞUNA bakmadan.

⚠️ Bu bir KALİTE kapısı DEĞİL. "Hibrit kazandı mı" diye sormaz; kaybetmek de bir
sonuçtur ve gizlenmemelidir. Burada yalnızca koşunun sahiciliği sınanır.

En olası SESSİZ hata, yanlışlıkla `HashEmbeddingProvider` ile koşup sonucu anlamsal
sanmaktır — hash gömmesi kendi docstring'inde "never a quality claim" der ama çıktı
JSON'u aynı şekli taşır. Provenance'taki gömme-uzayı kimliği bunu görünür kılar.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def check(path: Path) -> list[str]:
    if not path.is_file() or not path.stat().st_size:
        return [f"sonuc dosyasi YOK ya da bos: {path}"]
    data = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    prov = data.get("provenance") or {}

    space = prov.get("embedding_space", "")
    if "nonsemantic" in space:
        problems.append(f"HASH gommesiyle kosulmus - sonuc anlamsal DEGIL (uzay: {space})")
    if not space or space == "bilinmiyor":
        problems.append("gomme uzayi kimligi YOK - neyin sonucu oldugu okunamaz")
    if space.endswith("@unpinned"):
        # ⛔ HATA, uyari DEGIL (Fable KARAR 1, 2026-08-17). Headless bir kapida "uyari",
        # kimsenin okumadigi satirdir; bu repo'nun alarm-yorgunlugu dersi bunu soyluyor.
        # Pinsiz kosulan sonuc "tekrarlanabilir" diye ATIFLANAMAZ.
        problems.append(
            f"model revizyonu PINSIZ ({space}) - sonuc yeniden uretilebilir DEGIL; "
            "`DEFAULT_REVISION` pinlenmeden atif yapilamaz"
        )
    if prov.get("provider") != "pgvector":
        problems.append(f"gercek SQL yolu kullanilmamis (provider: {prov.get('provider')!r})")
    if not prov.get("text_search_config"):
        # Leksik bacagin KIMLIGI olmadan "lexical-only 0.78" sayisi okunamaz: hangi
        # config'le (simple/turkish) uretildigi sonucun anlamini degistirir.
        problems.append("leksik config provenance'ta YOK - 'lexical-only' sayisi okunamaz")
    if prov.get("git_sha") in (None, "", "bilinmiyor"):
        problems.append("git SHA yok - kosu bir koda baglanamiyor")
    if not data.get("points"):
        problems.append("hicbir agirlik noktasi uretilmemis")
    if not data.get("claim_sentence"):
        problems.append("hak edilen cumle uretilmemis")
    problems.extend(_dead_lexical_leg(data.get("points") or []))
    return problems


def _dead_lexical_leg(points: list[dict]) -> list[str]:
    """POZITIF KONTROL: leksik bacak hic ateslenmemisse sonuc GECERSIZDIR.

    ⚠️ NEDEN VAR (Fable hakemligi, 2026-08-17 — olculdu):
    `plainto_tsquery` terimleri AND'liyordu ve `simple` config stopword atmadigi
    icin soru kelimeleri de sorguya giriyordu. 150 XQuAD-tr sorgusunun 0'inda
    sorgunun tum token'larini iceren belge vardi ⇒ `ts_rank_cd` her cift icin 0.
    Bu durumda kosu YESIL biter, `points` dolu gelir, `claim_sentence` uretilir --
    yani buradaki eski kontrollerin HEPSI gecerdi -- ama olculen sey "hibrit"
    degil dense-only'dir. Tam olarak bu repo'nun adini koydugu sinif: sistemin
    kendi raporunu kanit saymak.

    IMZA: leksik bacak hicbir sey katmiyorsa w>0 olan TUM noktalar dense-only ile
    ayni siralamayi uretir ⇒ metrikleri BIREBIR ayni cikar. 10 nokta boyunca tam
    esitlik tesadufle aciklanamaz.

    ⛔ Bu kontrol sonuca BAKMAZ (hibrit kaybedebilir, o da bir sonuctur); yalnizca
    "olculen duzenekte leksik bacak CANLI miydi" sorusunu sorar.
    """
    active = [p for p in points if (p.get("vector_weight") or 0) > 0]
    if len(active) < 2:
        return []
    signatures = {
        (
            round(float((p.get("overall") or {}).get("recall_at_k", -1)), 12),
            round(float((p.get("overall") or {}).get("mean_reciprocal_rank", -1)), 12),
        )
        for p in active
    }
    if len(signatures) != 1:
        return []
    # ⚠️ Ayni imza IKI AYRI arizadan gelebilir (Fable 2. tur, 2026-08-17). Ikisi de
    # sonucu gecersiz kilar ama TESHISLERI farklidir; tek metin yanlis yere baktirirdi.
    if signatures == {(1.0, 1.0)}:
        return [
            f"TAVAN REJIMI: w>0 olan {len(active)} agirligin hepsi recall=1.0, MRR=1.0 "
            "verdi => korpus/gold-set AYIRIM GUCU tasimiyor (leksik bacak olu olmayabilir). "
            "Bu kume uzerinde hicbir siralama iddiasi ayirt edilemez."
        ]
    return [
        "LEKSIK BACAK OLU: w>0 olan tum agirliklar BIREBIR ayni metrigi verdi "
        f"({len(active)} nokta) => karisim hicbir sey degistirmiyor, olculen sey "
        "dense-only. Sonuc hibrit hakkinda GECERSIZ."
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    problems = check(args.path)
    if problems:
        for problem in problems:
            print(f"KIRMIZI: {problem}", file=sys.stderr)
        return 1
    data = json.loads(args.path.read_text(encoding="utf-8"))
    prov = data["provenance"]
    print(f"gomme uzayi : {prov['embedding_space']}")
    print(f"git sha     : {prov['git_sha'][:12]}")
    print(f"N / k       : {prov['cases']} / {prov['top_k']}")
    print("\nHAK EDILEN CUMLE:")
    print(data["claim_sentence"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
