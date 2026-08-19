"""XQuAD-tr'den DONDURULMUŞ bir değerlendirme alt kümesi üretir.

Neden vendorluyoruz (repoya donduruyoruz):
  · CI'da ağ bağımlılığı OLMASIN — indirme her koşuda flake riskidir
  · Sonuç TEKRARLANABİLİR olsun: aynı korpus, aynı sorgular, aynı etiketler
  · Bu repo kind/kubectl/postgres imajını pinliyor; veri de pinlenir (aynı konvansiyon)

⚖️ LİSANS: XQuAD **CC BY-SA 4.0** (doğrulandı 2026-08-17, repo README'sinden).
   SHARE-ALIKE'tır: burada üretilen alt küme bir TÜREV ÇALIŞMADIR ve aynı lisansla
   dağıtılır. Veri, kodun lisansından AYRI durur — bkz. `data/xquad-tr/LICENSE`.

## Katman etiketleri MEKANİK atanır (elle değil)

Fable uyarısı: etiketleri yazan kişi farkında olmadan sonucu belirler. Burada etiket
`terms()` ile ÖLÇÜLEN örtüşmeden doğar:
  · exact  = |terms(soru) ∩ terms(hedef paragraf)|
  · "overlap"    -> exact > 0            (kullanıcı belgenin kelimesini kullanmış)
  · "morphology" -> exact == 0 AMA bir terim diğerinin ÖN EKİ (Türkçe eklemeli yapı:
                    "iade" ↔ "iadesi"; en az 4 karakter, rastgele kısa eşleşme olmasın)
  · "paraphrase" -> exact == 0 ve ön-ek eşleşmesi de yok (yalnız anlamsal eşleşme)

⚠️ Bu bir DİLBİLİM sınıflandırması değil, ÖLÇÜLEBİLİR bir yakınsamadır. Amaç kümenin
kompozisyonunu görünür kılmak: tamamı-paraphrase bir set yoğun bacağı, tamamı-örtüşmeli
bir set leksik bacağı GARANTİ yener; katmanlar ayrı raporlanmazsa ortalama bunu gizler.

Kullanım (çıktı repoya commit'lenir, koşu tekrarlanmaz):
    python scripts/build_xquad_subset.py --source xquad.tr.json --out data/xquad-tr
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wozto_ai_reference.embedding import terms  # noqa: E402

# Türkçe soru kalıbı kelimeleri: her soruda geçtikleri için örtüşme ölçümünü
# yapay olarak şişirirler ve HER soruyu "overlap" gösterirlerdi.
STOPWORDS = frozenset(
    """
    ve veya ile ama fakat ancak da de ki mi mı mu mü ne neden nasıl hangi kaç kim
    kimin nerede nereye niçin için gibi kadar daha en çok az bir bu şu o bunlar
    şunlar onlar olarak olan olduğu oldu olur var yok değil ise ise de mıdır midir
    nedir kimdir hangisi hangisidir yılında yılı zaman
    """.split()
)
MIN_STEM = 4


def content_terms(text: str) -> set[str]:
    return {t for t in terms(text) if t not in STOPWORDS and len(t) >= 3}


def classify(question: str, paragraph: str) -> str:
    q = content_terms(question)
    p = content_terms(paragraph)
    if q & p:
        return "overlap"
    for qt in q:
        if len(qt) < MIN_STEM:
            continue
        for pt in p:
            if len(pt) < MIN_STEM:
                continue
            if qt.startswith(pt[:MIN_STEM]) and (qt.startswith(pt) or pt.startswith(qt)):
                return "morphology"
    return "paraphrase"


def build(source: Path, out_dir: Path, *, sample: int, seed: int) -> dict:
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    documents: list[dict] = []
    candidates: list[dict] = []

    for article in raw["data"]:
        for index, paragraph in enumerate(article["paragraphs"]):
            doc_id = f"xquad::{hashlib.sha256(paragraph['context'].encode()).hexdigest()[:12]}"
            documents.append(
                {
                    "document_id": doc_id,
                    "title": article.get("title", ""),
                    "section": f"p{index}",
                    "content": paragraph["context"].strip(),
                }
            )
            for qa in paragraph["qas"]:
                candidates.append(
                    {
                        "case_id": qa["id"],
                        "query": qa["question"].strip(),
                        "relevant_document_ids": [doc_id],
                        "layer": classify(qa["question"], paragraph["context"]),
                    }
                )

    # Deterministik ornekleme: seed SABIT, sonuc commit'lenecek.
    rng = random.Random(seed)
    cases = sorted(rng.sample(candidates, min(sample, len(candidates))), key=lambda c: c["case_id"])

    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = out_dir / "corpus.json"
    cases_path = out_dir / "cases.json"
    corpus_path.write_text(
        json.dumps({"documents": documents}, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    cases_path.write_text(
        json.dumps({"cases": cases}, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    layers: dict[str, int] = {}
    for case in cases:
        layers[case["layer"]] = layers.get(case["layer"], 0) + 1
    manifest = {
        "source": "https://github.com/google-deepmind/xquad — xquad.tr.json",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "license": "CC BY-SA 4.0 (share-alike; turev calisma ayni lisansla dagitilir)",
        "documents": len(documents),
        "cases": len(cases),
        "sample_seed": seed,
        "layers": dict(sorted(layers.items())),
        "corpus_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--sample", type=int, default=150)
    parser.add_argument("--seed", type=int, default=20260817)
    args = parser.parse_args()
    manifest = build(args.source, args.out, sample=args.sample, seed=args.seed)
    print(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
