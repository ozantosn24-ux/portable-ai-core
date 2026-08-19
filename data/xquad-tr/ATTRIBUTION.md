# XQuAD-tr — dondurulmuş değerlendirme alt kümesi

## ⚖️ Lisans (KODDAN AYRI)

Bu dizindeki **veri** `CC BY-SA 4.0` altındadır — bu reponun kod lisansı DEĞİL.
Tam metin: `LICENSE` (kaynağın kendi `CC-BY-SA4.0.txt` dosyası, olduğu gibi alındı).

**Share-alike:** `corpus.json` ve `cases.json` özgün veriden türetilmiş **adaptasyondur**
(alt küme + mekanik katman etiketi) ve aynı lisansla dağıtılır. Kodun kendi lisansı bu
dizini kapsamaz; sınır bilinçli olarak dizin düzeyinde çizilmiştir.

## Kaynak

- Depo: <https://github.com/google-deepmind/xquad> · dosya: `xquad.tr.json`
- XQuAD: SQuAD v1.1 geliştirme kümesinden 240 paragraf ve 1.190 soru-cevabın
  **profesyonel çevirisi** (makine çevirisi değil).
- Doğrulanan sayılar (2026-08-17, koşuda ölçüldü): 48 makale, **240 paragraf**,
  **1.190 soru** — depo README'siyle birebir.
- Kaynak dosya sha256'sı ve türetilen dosyaların sha256'ları `manifest.json` içinde.

## Nasıl üretildi

`scripts/build_xquad_subset.py` (repoda). Örnekleme deterministik: `seed=20260817`.
Katman etiketleri **elle değil mekanik** atanır — `terms()` ile ölçülen içerik-kelimesi
örtüşmesinden doğar. Yeniden üretmek için betiği aynı kaynak dosyayla koştur;
`manifest.json` içindeki sha256'lar tutmalı.

## ⚠️ Bu kümenin ÖLÇÜLMÜŞ sınırları

1. **Katman dağılımı çarpık: 148 overlap / 1 morphology / 1 paraphrase** (n=150).
   Sebep **yapısal**: SQuAD soruları paragrafa BAKILARAK yazılmıştır, dolayısıyla
   leksik örtüşme inşa gereği vardır (medyan ~3 ortak içerik kelimesi; 150 sorudan
   yalnız 2'si sıfır örtüşmeli).
   ⇒ Bu kümeyle **katmanlar arası tutarlılık iddiası KURULAMAZ.** `MIN_LAYER_CASES`
   eşiği bu katmanları hükümden çıkarır ve raporda adıyla gösterir.
2. **Leksik bacak lehine çarpık — AMA bu beklenti 2026-08-17'de KOŞULLU HÂLE GELDİ.**
   Örtüşme baskın olduğu için `terms()`-kesişimi anlamındaki leksik eşleşme avantajlıdır.
   ⚠️ **Fakat ölçülen leksik bacak o değildir.** Store `ts_rank_cd` + OR'lu
   `plainto_tsquery` kullanır. **2026-08-17 itibarıyla config `turkish`** (Fable KARAR 2):
   stemming VAR, stopword süzgeci VAR — Türkçe aglütinatiftir, `simple`da
   "iade"≠"iadeler"≠"iadesi" ve soru kelimeleri gürültü üretiyordu.
   ⛔ **Ama `turkish` bile bu bacağı BM25 YAPMAZ: stok PostgreSQL FTS'te IDF YOKTUR**
   (uzunluk normalizasyonu da varsayılanda kapalı). ⇒ *"hibrit, lexical-only'yi geçti"*
   sonucu **bu `ts_rank_cd` + `turkish` yapılandırmasına koşulludur**; *"hibrit, düzgün bir
   leksik aramayı (BM25 sınıfı) yener"* iddiasını **HAK ETTİRMEZ**.
   📌 **Seri kırılması:** `simple` ile koşulmuş run `32045893909` (lexical-only 0.780,
   hibrit 0.993, dense 0.973) bir **arşiv taban çizgisidir**; `turkish` sonucuyla aynı
   seride karşılaştırılamaz. Manşet olarak **yeni** sonuç kullanılır.
   ⚠️ **Sınır — bu korpusta stemming'in asıl faydası ÖLÇÜLEMEZ:** morphology katmanı
   n=1 (§1) ⇒ `turkish`in görünür etkisi ağırlıkla stopword temizliği olacaktır.
   ⇒ *"hibrit, dense-only'yi geçti"* yönü bu değişiklikten **etkilenmez** (yine daha kolay/zayıf).
   📌 Tarihsel not: 2026-08-17 öncesinde bacak AND'liydi ve **hiç ateşlenmiyordu**
   (150 sorgunun 0'ında tüm token'ları içeren belge vardı) ⇒ o dönemde üretilecek her
   "hibrit" sayısı fiilen dense-only olurdu. Bkz. `pgvector_store.py` leksik skor bloğu.
3. **Olası kontaminasyon (DOĞRULANMADI):** `multilingual-e5` ailesinin eğitim
   karışımında SQuAD türevleri bulunabilir. Doğruysa dense bacağın mutlak sayıları
   şişkindir. Yön: *"hibrit > dense"* iddiasını **muhafazakâr**, *"hibrit > lexical"*
   iddiasını **iyimser** yapar.
4. **Alan uyuşmazlığı:** Wikipedia paragrafı ≠ ajans/operasyon dokümanı. Bu küme
   MEKANİZMANIN kanıtıdır; Wozto'nun kendi alanına dair hiçbir şey söylemez.
