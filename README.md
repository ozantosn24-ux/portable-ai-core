# Wozto Portable AI Core — reference implementation

Bu dizin Wozto'nun AI iş mantığını Azure, AWS veya tek bir model sağlayıcısına
kilitlemeden kuran yerel reference implementation'dır. Varsayılan mod ağ çağrısı,
canlı bulut kaynağı, gerçek müşteri verisi veya ücretli model kullanmaz. İsteğe bağlı
`pgvector` backend'i yalnız operatörün yerelde açtığı PostgreSQL'e bağlanır.

## Bu checkpoint neyi kanıtlıyor?

- Model, search, storage, identity ve telemetry için cloud-neutral portlar vardır.
- Yerel deterministic adapter ile uçtan uca `/query` akışı çalışır.
- Retrieval hem adapter içinde hem servis katmanında tenant ve ACL kontrolünden geçer.
- Operatörce yapılandırılan hard query policy, reddedilen isteği retrieval ve modelden önce keser.
- `as_of`, kaynak durumu ve kaynak otoritesi koşulları belge metadata'sıyla fail-closed eşleşir.
- Opt-in scope resolver, doğal dil işaretlerini yalnız önceden incelenmiş daraltıcı
  kurallara çevirir; resolver ile açık istek koşulu çelişirse search başlamaz.
- Opt-in evidence critic, model cevabını citation üretmeden önce kontrol eder; exact
  baseline yalnız birebir extractive desteği kabul eder ve semantik judge iddiası taşımaz.
- Yetkili kaynak bulunmazsa sistem cevap uydurmak yerine abstain eder.
- Model yalnız yetki kontrolünden geçmiş context'i görür.
- Güvensiz local header identity varsayılan olarak kapalıdır.
- Manifest dışında kalan belge okunmaz; path escape ve symlink kaynakları reddedilir.
- Chunk kimliği, belge sürümü ve content hash deterministiktir.
- PostgreSQL sorgusu tenant ve ACL filtresini retrieval öncesinde uygular; bu yol
  Windows Docker Desktop üzerinde gerçek pgvector container'ına karşı da doğrulandı.
- Gold set kapısı Recall@K, MRR, yetkisiz sonuç ve yinelenen vaka kimliğini ölçer.

Bu checkpoint production identity veya Azure deneyimi iddia etmez. Yerel hash
embedding yalnız boru hattını deterministik test etmek içindir; semantic kalite
iddiası veya production embedding modeli değildir.

## Mimari

```text
FastAPI
  └─ QueryService
      ├─ IdentityProvider ─ local headers / Entra ID / Keycloak
      ├─ QueryPolicy      ─ operator-configured hard deny / future policy engine
      ├─ QueryScopeResolver─ configured phrases / calibrated classifier
      ├─ SearchProvider   ─ in-memory / pgvector / Azure AI Search
      ├─ EmbeddingProvider─ deterministic hash / local model / cloud embedding
      ├─ ModelProvider    ─ deterministic / Foundry / OpenAI / local model
      ├─ EvidenceSupportCritic─ exact extractive / calibrated entailment
      ├─ DocumentStore    ─ memory / Blob / S3-MinIO
      └─ TelemetryProvider─ memory / OpenTelemetry / Azure Monitor
```

Ayrıntılı güvenlik ve adapter sınırları için [architecture.md](docs/architecture.md)
dosyasına bakın.

## Yerel çalıştırma

```powershell
cd portable-ai-core
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
$env:WOZTO_REFERENCE_ALLOW_INSECURE_HEADERS="1"
uvicorn wozto_ai_reference.api:app --reload --port 8080
```

Yalnızca yerel demo için örnek sorgu:

```powershell
curl.exe -X POST http://127.0.0.1:8080/query `
  -H "Content-Type: application/json" `
  -H "X-Tenant-ID: tenant-demo" `
  -H "X-User-ID: local-operator" `
  -H "X-Roles: employee" `
  -d '{"query":"refund policy","top_k":5}'
```

`WOZTO_REFERENCE_ALLOW_INSECURE_HEADERS=1` production authentication değildir.
Bu bayrak kapalıyken `/query` fail-closed olarak `503` döner.

İsteğe bağlı kapsam koşulları çağrı gövdesinde açıkça taşınabilir:

```json
{
  "query": "25 Ağustos 2026 tarihinde hangi politika geçerliydi?",
  "top_k": 5,
  "as_of": "2026-08-25",
  "source_status": "current",
  "source_authority": "authoritative"
}
```

Çekirdek varsayılan olarak doğal dildeki tarihi veya "güncel/onaylı" niyetini
kendi kendine doğru kabul etmez. Opt-in `ConfiguredPhraseScopeResolver`, yalnız
operatörün açıkça tanımladığı phrase → constraint kurallarını birleştirir; çelişen
kurallar veya resolver ile açık request alanı çatışması fail-closed abstain olur.
Bu baseline normalize edilmiş token-sequence eşleşmesidir, genel amaçlı doğal dil
anlayışı değildir.
`as_of` verildiğinde hiç validity metadata'sı olmayan kaynak fail-closed elenir.
`DenyPhraseQueryPolicy` ise credential/PII gibi operatörün belirlediği ifadeleri search
ve model çağrısından önce reddeden opt-in bir hard-policy adapter'ıdır; varsayılan gizli
kelime listesi yoktur.

Model üretiminden sonra opt-in `ExactEvidenceSupportCritic`, cevabın normalize edilmiş
halini yetkili evidence ile birebir karşılaştırır. Destek yoksa üretilen metni ve
citation'ları kullanıcıya göstermeden abstain eder; destek varsa yalnız destekleyen
document id'lerinin citation'larını döndürür. Bu deterministik baseline paraphrase veya
entailment değerlendirmez; production groundedness critic yerine geçmez.

Resolver ve critic retrieval'dan ayrı frozen vakalarla ölçülür:

```powershell
$env:PYTHONPATH="src"
python -m wozto_ai_reference.quality_evaluation --minimum-accuracy 1 scope `
  --rules sample-corpus/scope-eval.json `
  --cases sample-corpus/scope-eval.json

python -m wozto_ai_reference.quality_evaluation --minimum-accuracy 1 critic `
  --cases sample-corpus/critic-eval.json `
  --allowed-prefix "Grounded answer:"
```

Her iki kapı da false allow/accept, false refusal/reject, constraint/support mismatch ve
duplicate vaka kimliklerini ayrıca raporlar. Örnekler sentetik mekanizma testidir; insan
etiketli domain calibration veya production kalite iddiası değildir.

## Güvenli ingest ve retrieval ölçümü

Örnek korpus gerçek Vault veya müşteri verisi içermez. Manifest, okunmasına izin
verilen dosyaları tek tek listeler; CLI klasörü recursive taramaz.

```powershell
cd reference-implementations/portable-ai-core
$env:PYTHONPATH="src"

# Yalnız plan üretir; veritabanına yazmaz. Çıktıdaki sayıları ve plan_hash'i incele.
$plan = python -m wozto_ai_reference.ingest `
  --source-root sample-corpus/documents `
  --manifest sample-corpus/manifest.json | ConvertFrom-Json
$plan | Format-List mode, source_files, chunks, total_bytes, plan_hash

# Küçük sentetik başlangıç kapısı; gerçek kalite için 30–50 insan-yazımı soru gerekir.
python -m wozto_ai_reference.evaluation `
  --source-root sample-corpus/documents `
  --manifest sample-corpus/manifest.json `
  --gold-set sample-corpus/gold-set.json
```

Gold set pozitif vakalarda ya tam chunk kimliklerini (`relevant_document_ids`) ya da
kaynak-belge kimliklerini (`relevant_source_document_ids`) kullanır. Korpusta cevabı
olmaması gereken ve yetki nedeniyle reddedilmesi gereken sorular
`"expected_abstain": true` ile açıkça etiketlenir; rapor bunları retrieval recall'a
karıştırmadan `abstain_accuracy` ve `unexpected_answers` olarak ölçer.

`--minimum-score` serving katmanındaki aynı score kapısını yeniden üretir;
`--minimum-abstain-accuracy` varsayılan olarak `1.0`dır. Arama skorları sağlayıcıya,
modele ve korpusa bağlıdır. Özellikle sorgu-başı normalize edilmiş hibrit pgvector
skorunda en iyi ilgisiz aday da `1.0` olabilir; ayrı bir negatif doğrulama kümesi olmadan
eşik seçmek veya bu eşiği başka bir modele taşımak kalite iddiası değildir.

Gerçek Vault pilotunda ayrı bir kaynak klasörü ve repo dışında tutulan manifest
kullanılmalı; `.env`, credential/session depoları ve müşteri PII dosyaları manifest'e
eklenmemelidir. İlk `--apply` öncesi dry-run çıktısındaki dosya/chunk/byte sayısı ve
manifest kapsamı operatörce kontrol edilmelidir. `plan_hash`; tenant, ACL, chunk kimliği,
`source_status`, `source_authority`, validity penceresi, sürüm, kaynak URI'si ve yazılacak
bütün içerik alanlarının hash'inden deterministik
üretilir. Hash verilmeden apply çalışmaz; kaynak veya yetki dry-run'dan sonra değişirse
veritabanına bağlanmadan önce reddedilir.

Manifest belge girdileri isteğe bağlı olarak şunları taşır:

- `source_status`: `unspecified`, `current`, `historical` veya `reference`;
- `source_authority`: `unspecified`, `advisory` veya `authoritative`;
- `valid_from` / `valid_through`: ISO `YYYY-MM-DD` sınırları.

Metadata değişikliği de plan hash'ini değiştirir ve yeniden operatör incelemesi ister.

## İsteğe bağlı PostgreSQL + pgvector

Compose tanımı resmi `pgvector/pgvector` image'ını sabit sürüm etiketiyle kullanır ve
yalnız `127.0.0.1:55432` üzerinde yayınlar. PostgreSQL parolası container environment'ına
konmaz; Compose, repo dışındaki parola-dosyasını `/run/secrets/postgres_password` olarak
mount eder. Uygulama bağlantısı da parolayı URL'ye gömmek yerine libpq `passfile` kullanır.

```powershell
# İki dosyayı repo dışında, yalnız kendi kullanıcının okuyabildiği bir konumda oluştur:
# 1) postgres-password.txt -> yalnız güçlü parola
# 2) pgpass.conf -> 127.0.0.1:55432:wozto_rag:wozto:<aynı-parola>
$env:WOZTO_RAG_DB_PASSWORD_FILE="C:\guvenli\postgres-password.txt"
docker compose up -d --wait

$env:WOZTO_REFERENCE_DATABASE_URL="host=127.0.0.1 port=55432 dbname=wozto_rag user=wozto passfile=C:/guvenli/pgpass.conf"
$env:WOZTO_REFERENCE_BACKEND="pgvector"
$env:WOZTO_REFERENCE_ALLOW_INSECURE_HEADERS="1"

# Apply öncesinde planı bu kaynak durumuyla yeniden üret ve incele.
$plan = python -m wozto_ai_reference.ingest `
  --source-root sample-corpus/documents `
  --manifest sample-corpus/manifest.json | ConvertFrom-Json
$plan | Format-List mode, source_files, chunks, total_bytes, plan_hash

# Yalnız yukarıda incelenen plan birebir aynıysa yazar.
python -m wozto_ai_reference.ingest `
  --source-root sample-corpus/documents `
  --manifest sample-corpus/manifest.json `
  --apply $plan.plan_hash

uvicorn wozto_ai_reference.api:app --port 8080
```

`WOZTO_REFERENCE_BACKEND=pgvector` seçilmişken database URL yoksa uygulama açılmaz.
Ingest her kaynak belgeyi transaction içinde tamamen değiştirir; eski sürümden kalan
chunk'lar böylece sorgulanmaya devam etmez.

`tests/fixtures/` altındaki parola ve passfile yalnız herkese açık, geçici entegrasyon
fixture'ıdır; kalıcı volume veya gerçek veriyle kullanılmaz. 30 Ağustos 2026'da Windows
Docker Desktop 4.88.1 / Engine 29.7.2 ve `pgvector/pgvector:0.8.6-pg17-trixie` üzerinde:

- container healthcheck geçti;
- gerçek PostgreSQL entegrasyon takımı `3 passed` verdi;
- XQuAD-TR'nin 240 belgesi ve 150 sorgusunda, 20 warm-up sonrası hash embedding + yerel
  hibrit SQL + sonuç eşleme gecikmesi p50 `31,179 ms`, p95 `90,010 ms` ölçüldü;
- boş sonuç `0/150`; bu ölçüm E5/model latency'si, semantic kalite veya production ölçeği
  iddiası değildir.

Tekrarlanabilir latency probu:

```powershell
$env:WOZTO_REFERENCE_DATABASE_URL="host=127.0.0.1 port=55432 dbname=wozto_rag user=wozto passfile=C:/guvenli/pgpass.conf"
python scripts/benchmark_pgvector_latency.py --data data/xquad-tr
```

### Exact search / HNSW / IVFFlat kararı

30 Ağustos 2026'da aynı deterministic 64-boyutlu 10.000 vektör ve her filtre diliminde
60 sabit sorgu üzerinde exact top-k ground truth üretildi. ANN sorgularının gerçekten
beklenen index planını kullandığı `EXPLAIN (FORMAT JSON)` ile kapılandı. Değerler
`p50 / p95 ms · ortalama top-5 overlap` biçimindedir:

| Yetkili aday dilimi | Exact | HNSW | IVFFlat |
| --- | ---: | ---: | ---: |
| 10.000 satır (%100) | `3,098 / 3,779 · 1,0000` | `1,945 / 2,509 · 0,9933` | `1,802 / 2,162 · 0,7333` |
| 1.000 satır (%10) | `2,184 / 2,958 · 1,0000` | `2,310 / 3,550 · 0,9567` | `1,873 / 2,497 · 0,7200` |
| 100 satır (%1) | `1,911 / 2,547 · 1,0000` | `4,862 / 6,582 · 0,9767` | `2,022 / 2,674 · 0,6467` |

HNSW build süresi `2,753 saniye`, index boyutu `5.701.632 byte`; IVFFlat build süresi
`0,183 saniye`, index boyutu `2.883.584 byte` ölçüldü. Shared-buffer residency ayrıca
raporlanır; bu değer peak build memory değildir. Tam makine-okunur kanıt
[`results/pgvector-index-benchmark-2026-08-30.json`](results/pgvector-index-benchmark-2026-08-30.json)
içindedir.

Karar: mevcut pilot 20–240 belge ve seçici tenant/ACL filtreleriyle çalıştığı için
exact search varsayılandır; şema otomatik ANN index oluşturmaz. HNSW ancak gerçek
korpus büyüyüp bu benchmarkta kabul edilen recall ile ölçülmüş bir darboğaz gösterirse
eklenir. IVFFlat bu ayarlarda recall kaybı nedeniyle seçilmedi. Bu benchmark semantic
arama kalitesi iddiası değildir; yalnız vektör index mekaniğini ölçer. Mevcut hibrit
SQL de yetkili aday kümesinin tamamında skor normalizasyonu yaptığı için ANN indexi
doğrudan kullanmaz; ANN'e geçiş iki aşamalı candidate/fusion tasarımı gerektirir.

Tekrarlanabilir karşılaştırma (geçici tablo koşu sonunda otomatik silinir):

```powershell
$env:WOZTO_REFERENCE_DATABASE_URL="host=127.0.0.1 port=55432 dbname=wozto_rag user=wozto passfile=C:/guvenli/pgpass.conf"
python scripts/benchmark_pgvector_indexes.py `
  --rows 10000 --queries 60 --warmup 10 --top-k 5 `
  --output results/pgvector-index-benchmark.json
```

## MCP sunucusu — yetki sınırını protokole taşımak

`wozto_ai_reference.mcp_server`, aynı tenant+ACL filtreli sorgu servisini **stdio MCP
sunucusu** olarak açar.

**Neden ilginç:** bir arama fonksiyonunu MCP'ye sarmak kolaydır ve bir şey kanıtlamaz. Asıl
problem şu: **MCP'nin kendi yetkilendirme modeli yoktur.** Bir araç çağrısı yalnızca bir ad ve
bir JSON nesnesidir; o nesneyi, güvenilmeyen içerik okumuş olabilecek bir model kurar.
Modelin argümana koymaya ikna edilebildiği her şey fiilen saldırgan kontrolündedir.

Bu yüzden tek kural:

> **Kimlik sunucu açılışında belirlenir. Asla bir araç argümanı değildir.**

HTTP yüzeyiyle aynı duruş: `QueryPayload` bilinçli olarak kimlik taşımaz, principal header'dan
bir `IdentityProvider` ile çözülür. Burada da principal, herhangi bir istemci konuşmadan önce,
**süreç ortamından** bir kez çözülür (`WOZTO_MCP_TENANT_ID`, `WOZTO_MCP_USER_ID`,
`WOZTO_MCP_ROLES`). Kimlik yoksa sunucu **başlamaz** — uydurmaz, sonradan istemciden de almaz.

### Savunulacak iki tasarım kararı

1. **Kimlik biçimli argümanlar reddedilir, sessizce yok sayılmaz.** `tenant_id`'yi sessizce
   düşürmek çağırana "işledi" sandırır ve bir enjeksiyon denemesini normal trafikten ayırt
   edilemez kılar. Ret hem güvenli hem **gözlenebilir**.
2. **Abstain bir HATA değil, başarılı sonuçtur.** "Yetkili kaynak yok" cevabı, yetkilendirme
   yolunun çalıştığının kanıtıdır. `isError` işaretlemek, istemcileri onu geçici arıza gibi
   yeniden denemeye davet ederdi — tam tersi.

### ⭐ Ölçülen sonuç: sınır İKİ katmanda korunuyor

Kaçak `tenant_id` denemesi canlı istemcide **protokol katmanında** reddedildi
(`additionalProperties: false` ⇒ *"Input validation error: Additional properties are not
allowed"*), yani handler'a hiç ulaşmadı. Uygulama katmanındaki reddi ise yedekte durur.
⚠️ **Yalnız (1)'e güvenmek yanlış olurdu:** her istemci/sunucu şema doğrulaması yapmaz ve bir
güvenlik garantisi karşı tarafın nezaketine bağlanamaz. `scripts/mcp_smoke.py` ikisini de kabul
eder ve **hangisinin ateşlediğini raporlar**.

### Çalıştırma ve doğrulama

```bash
pip install -e ".[mcp]"

# 1) Sınır sözleşmeleri (SDK gerekmez -- sunucu SDK'yi yalnız main() icinde import eder)
pytest tests/test_mcp_server.py -q          # 32 test

# 2) Gerçek istemciyle uçtan uca (sunucuyu stdio ile başlatır)
python scripts/mcp_smoke.py                 # 9/9 kontrol

# 3) Bir MCP istemcisine tanıtmak icin
WOZTO_MCP_TENANT_ID=tenant-demo WOZTO_MCP_USER_ID=you WOZTO_MCP_ROLES=finance wozto-rag-mcp
```

Araçlar: `answer_from_authorized_sources` (yalnız yetkili kaynaklardan cevap + provenance'lı
citation, yoksa abstain) · `describe_identity` (hangi tenant/rol olarak davranıldığını bildirir,
**değiştiremez**).

Sunucu **müşteri verisi taşımaz**: varsayılan korpus, HTTP demo'sunun kullandığı sentetik
kümedir ve içine bilinçli olarak **başka bir tenant'a ait bir belge** konmuştur — sınır bozulursa
onu yakalayacak pozitif kontrol budur.

## Doğrulama

```powershell
cd reference-implementations/portable-ai-core
python -m pytest -q
ruff check .
```

## Sonraki checkpoint

1. Operatörce seçilmiş güvenli Vault alt kümesiyle 30–50 soruluk gerçek gold set.
2. Yerel production adayı embedding modeli ve keyword/vector/hybrid karşılaştırması.
3. Exact baseline'ın ötesinde insan-kalibreli semantic entailment critic ve cevap düzeyi
   hallucination ölçümü.
4. Korpus 10.000 satıra yaklaşır veya exact p95 hedefi aşarsa ölçülmüş HNSW ayarını
   gerçek tenant/ACL dağılımında yeniden doğrulama; IVFFlat şimdilik seçilmedi.
5. Sonuç kanıtı oluşunca Azure AI Search ve Foundry adapter'ları.
6. Ücretli Azure resource açılmadan önce maliyet, bölge ve cleanup planı için
   eyleme özel operatör onayı.
