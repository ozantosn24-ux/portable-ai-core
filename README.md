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
- `StructuredAnswer`, her atomik claim'i kaynak `document_id + version + content_hash`
  referanslarına bağlar. Opt-in `ExactStructuredClaimSupportCritic`, claim dışı cevap
  metnini, getirilmeyen referansı ve kaynakta birebir bulunmayan claim'i fail-closed reddeder.
- Opt-in `SemanticStructuredClaimSupportCritic`, zorunlu ve açık relevance/entailment
  eşikleriyle her claim'i hem sorguya hem referans verdiği tüm kanıtlara karşı ölçer;
  scorer hatası, geçersiz skor veya eşik altı sonuç abstain olur.
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
      ├─ EvidenceSupportCritic─ exact answer / structured claims / calibrated entailment
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

Yapılandırılmış üretim için model `StructuredAnswer(answer, claims)` döndürebilir. Her
`StructuredClaim`, benzersiz bir `claim_id`, atomik ve **self-contained** claim metni ile en az bir
`EvidenceReference(document_id, version, content_hash)` taşır. Opt-in
`ExactStructuredClaimSupportCritic`, görüntülenen cevabın yalnız claim'lerden oluştuğunu,
referansların retrieved/authorized hit'lerde bulunduğunu ve her claim'in referans verdiği
kaynakta exact extract olduğunu doğrular. Bu sözleşme citation varlığını kanıt saymaz;
query relevance, negation ve paraphrase entailment hâlâ ayrı insan-kalibreli kapılardır.

`SemanticStructuredClaimSupportCritic` bu iki kapıyı ayrı `TextPairScorer` portlarıyla
uygular. Önce answer/claim bütünlüğü, tenant/ACL ve tam evidence referansı mekanik olarak
doğrulanır; sonra her self-contained claim için doğrudan query relevance ve her atıf için
entailment aranır. Claim, öznesini ve koşullarını soruya veya önceki cevap metnine gönderme
yapmadan belirtmelidir; bu şart `StructuredClaim` JSON Schema açıklamasına da işlenmiştir.
Relevance'ı cited chunk üzerinden geçirmek çok-konulu chunk'larda alakasız claim aklama riski
taşıdığı için doğrudan claim kapısının yerine kullanılmaz.
Bir claim birden fazla belgeye atıf yapıyorsa belgelerin **tümü** eşiği geçmelidir; böylece
destekli bir kaynağın yanına ilgisiz citation ekleyerek destek aklama reddedilir. Eşiklerin
varsayılanı yoktur. Model adı, revision, positive-label sözleşmesi ve eşikler frozen,
insan-onaylı calibration setinden açıkça verilmedikçe bu adapter production otoritesi değildir.
Opsiyonel `TransformersTextPairScorer`, yalnız ilk kullanımda `.[embeddings]` bağımlılıklarını
yükler ve model revision'ını `model_id` içine dahil eder.

Resolver ve critic retrieval'dan ayrı frozen vakalarla ölçülür:

```powershell
$env:PYTHONPATH="src"
python -m wozto_ai_reference.quality_evaluation --minimum-accuracy 1 scope `
  --rules sample-corpus/scope-eval.json `
  --cases sample-corpus/scope-eval.json

python -m wozto_ai_reference.quality_evaluation --minimum-accuracy 1 critic `
  --cases sample-corpus/critic-eval.json `
  --allowed-prefix "Grounded answer:"

python -m wozto_ai_reference.quality_evaluation --minimum-accuracy 1 structured-critic `
  --cases sample-corpus/structured-critic-eval.json

# Yalnız insan-onaylı frozen semantic vakalar/eşiklerle çalıştırın:
python -m wozto_ai_reference.quality_evaluation --minimum-accuracy 1 semantic-structured-critic `
  --cases <human-reviewed-semantic-cases.json> `
  --relevance-model <model> --relevance-revision <commit> `
  --entailment-model <model> --entailment-revision <commit> `
  --entailment-positive-label-index <index> `
  --minimum-relevance <reviewed-threshold> `
  --minimum-entailment <reviewed-threshold>
```

Tüm kapılar false allow/accept, false refusal/reject, constraint/support mismatch ve
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

## LLM gateway (retry → circuit breaker → failover)

`wozto_ai_reference.llm_gateway`, tek bir model sağlayıcısının kesintisini uygulamanın
kesintisi olmaktan çıkarır. Yönlendirici üç karar verir ve başka hiçbir şey yapmaz:
**aynı sağlayıcıda tekrar dene**, **bu sağlayıcıyı çağırmayı bırak**, **bir sonrakine
geç**. İstemi düzenlemez, iki sağlayıcının çıktısını birleştirmez, reddedilen bir
isteği başka sağlayıcıda "denemez".

```powershell
# Adaptörler opsiyoneldir; çekirdek ve testleri bu extra OLMADAN koşar.
pip install -e ".[llm]"
```

### Davranış tablosu

Tamamı `tests/test_llm_gateway_*.py`'den türetilmiştir — buradaki her satırın karşılığı
koşan bir testtir. **Süre/latency rakamı yoktur**: bu paket hiçbir sağlayıcıya karşı
performans ölçmedi, yalnız karar sırasını sınadı.

| Hata | Sınıf | `idempotent=True` | `idempotent=False` |
|---|---|---|---|
| 429 rate limit (+ `Retry-After`) | `RateLimitError` (pre-send) | Retry-After kadar bekler, aynı sağlayıcıda tekrar | **Aynı** — 429 "işe başlamadım" demektir, kanıt vardır |
| 503 / 529 | `ServerError` (pre-send) | Geri çekilmeli tekrar → failover | Aynı |
| 500 / 502 / 504 | `AmbiguousServerError` | Tekrar → failover | **Yükseltilir**; tekrar da failover da YOK |
| Zaman aşımı (gönderim sonrası) | `AmbiguousTimeoutError` | Tekrar → failover | **Yükseltilir** |
| Bağlantı hatası | `ProviderConnectionError` | Tekrar → failover | **Yükseltilir** |
| 401 / 403 | `AuthError` | **Beklemeden** doğrudan failover; aynı sağlayıcıya ikinci deneme yok | Aynı |
| 400 / 422 | `BadRequestError` | Çağırana yükseltilir; failover YOK (ikinci sağlayıcı da aynı 400'ü verir) | Aynı |
| İçerik reddi | `ContentPolicyError` | Çağırana yükseltilir; failover YOK | Aynı |
| Adaptörden sınıflandırılamayan hata (ör. `TypeError`) | `UnclassifiedProviderError` | Failover — ama **aynı sağlayıcıda tekrar YOK** (şüpheli adaptörün kendisi) | **Yükseltilir**; `__cause__` orijinal hatadır |
| N ardışık **sağlayıcı** hatası | devre kesici AÇILIR | Birincil `open_seconds` boyunca **hiç çağrılmaz**; yarı-açıkta tek yoklama | Aynı |
| Her iki sağlayıcı da düştü | `AllProvidersUnavailable(queue_hint=True)` | Tasarruf modu yapılandırıldıysa şablon, yoksa hata | Aynı |

Hangi testin neyi kanıtladığı: **statü → sınıf** eşlemesi `test_llm_gateway_adapters.py`de,
**sınıf → davranış** (her iki sütun da) `test_llm_gateway_router.py` /
`test_llm_gateway_stream.py`de. Belirsiz satırların ikisi de aynı davranış yolunu
(`AmbiguousOutcomeError`) kullanır ve o yol `AmbiguousTimeoutError` üzerinden koşulur.

⛔ **Devre kesici SAĞLAYICIYI çitler, İSTEĞİ değil.** `BadRequestError` /
`ContentPolicyError` sağlayıcının sağlığı hakkında hiçbir şey söylemez ve kesici
sayacına GİRMEZ; girseydi arka arkaya iki reddedilen istem, sapasağlam bir sağlayıcıyı
`open_seconds` boyunca kapatır ve sıradaki ilgisiz çağıranı yedeğe sürerdi.

⭐ **Belirsiz sonuç kuralı.** `idempotent=False`, "sonucun yan etkisi zaten bağlandı"
demektir (gönderilen bir e-posta, yazılan bir satır, tahsil edilen bir tutar). İlk
denemenin işlenip işlenmediği kanıtlanamıyorsa yönlendirici **durur ve belirsizliği
çağırana verir**; onu uzlaştırabilecek tek taraf odur. "Yeniden deneme yok" ile
"failover yok" AYNI korumadır: isteği başkasına göndermek de ikinci kez göndermektir.

### Akış (stream) sözleşmesi

1. **Failover yalnız istek sınırındadır.** Yedek sağlayıcı sıfırdan akar.
2. Birincil **hiç delta üretmeden** düşerse failover şeffaftır — tüketiciye olay gitmez.
3. Kısmi delta **çıktıktan sonra** düşerse `StreamRestarted(discarded_chars=N)` yayınlanır
   ve **yedek sağlayıcının ilk delta'sından ÖNCE** gider.
4. `StreamEnd.completion.text` **her zaman tek bir sağlayıcının tam metnidir** — asla
   birleştirme değildir. Kırık bir akış tüketicide yarım cümle/kapanmamış JSON bırakır;
   üstüne ikinci sağlayıcının metnini eklemek hiçbir modelin yazmadığı, tekrar
   üretilemeyen bir metin doğurur çünkü dikiş yalnızca yönlendiricide vardır.
5. `stream(req, buffered=True)`: delta'lar akış başarıyla bitene kadar tutulur ⇒ tüketici
   kısmi çıktıyı **hiç görmez**, `StreamRestarted` de yayınlanmaz — yalnız deftere yazılır.
6. Buffered modda **yeniden deneme bütçesi korunur**: tüketici hiçbir şey görmediği için
   iç tampon atılıp AYNI sağlayıcı temiz bir istekle tekrar denenebilir. "Metin çıktıktan
   sonra tekrar yok" kuralının gerekçesi tüketicinin görmüş olmasıdır; görmediyse gerekçe
   de yoktur ve birincil ilk mikro kesintide gereksizce terk edilmez.
7. Tüketici akıştan **çekilirse** (istemci koptu, `break`, iptal) yönlendirici o denemeyi
   `outcome="abandoned"` satırıyla deftere yazar — sağlayıcı çağrılmıştı ve faturalanmış
   olabilir. Devre kesiciye DOKUNULMAZ: başarısız olan sağlayıcı değil, giden tüketicidir.

Testler iki tüketiciyi aynı olay akışına karşı koşturur: naif olarak birleştiren tüketici
**yanlış** metin elde eder, `StreamRestarted`'da tamponunu sıfırlayan tüketici tam olarak
`StreamEnd`'deki metni elde eder.

### Defter (`AttemptLedger`)

Her deneme — sağlayıcı, deneme no, sonuç, hata sınıfı, `latency_ms`, usage,
`idempotency_key`, `request_id` — **append-only** bir JSONL satırıdır. Açık devre kesici
yüzünden atlanan sağlayıcı da (`skipped_open_circuit`, `attempt=0`) yazılır: kesicinin
ısırdığını başka hiçbir kayıt göstermez. `Usage.exact` yalnız sağlayıcı saydığında
`True`'dur ve toplama alındığında yayılır — bir tahmin içeren toplam tahmindir.

İki alan ayrı saatlerden gelir ve karıştırılmamalıdır: `ts` **duvar saatinden** yazılan
bir ISO-8601 UTC dizgisidir ("bu istek ne zaman gitti"), `latency_ms` ise monotonik
saatten ölçülen süredir. Her ikisi de ayrı ayrı enjekte edilebilir (`clock`,
`wall_clock`). Çağıran kendi satırlarını `Completion.request_id` ile bulur — cevabın
kendisi defterin anahtarını taşır.

### Bilinen sınırlar (henüz KAPATILMADI)

Bunlar bilinen ve bilinçli açık kalemlerdir; "yok" sanılmasınlar diye burada duruyorlar.

* **Kesilme (truncation) yüzeye çıkmıyor.** `stop_reason == "max_tokens"` (Anthropic) /
  `finish_reason == "length"` (OpenAI), çağırana yarım bir metnin TAM cevap gibi
  dönmesi demektir; adaptörler bunu ne bir alana yazıyor ne de bir hataya çeviriyor.
* **`_last_usage` adaptör başına DEĞİŞKEN durumdur.** Değişmez şart: atama ile okuma
  arasında `await` YOKTUR. Aynı adaptör örneğini eşzamanlı iki akışta kullanmak bu şartı
  bozar ve biri ötekinin token sayısını okur. Bugün korunuyor, ama tip sistemiyle değil
  disiplinle.
* **`ProviderTimeoutError` dışa aktarılıyor ama hiçbir adaptör ÜRETMİYOR.** SDK zaman
  aşımı `AmbiguousTimeoutError`'a eşlenir (gönderim sonrası zaman aşımının ne olduğu
  kanıtlanamaz). Sınıf, pre-send'i kanıtlayabilen bir adaptör için duruyor; tabloda
  satırı yoktur çünkü bugün hiçbir yol oraya çıkmıyor.
* **OpenAI adaptöründeki çok-choice birleştirmesi bugün ERİŞİLEMEZ.** `n` parametresi
  gönderilmediği için cevap tek choice taşır; birleştirme kodu ileriye dönük ve
  sınanmamış bir daldır.
* **Canlı sağlayıcı çağrısı YAPILMADI.** Bütün adaptör testleri sahte SDK istemcileriyle
  koşar; SDK eşlemesi kurulu paketler *okunarak* doğrulandı, ağ üzerinden değil.

⚠️ Bu katman **canlı model çağrısıyla ölçülmedi**. Adaptörlerin SDK eşlemesi kurulu
`anthropic==1.4.0` / `openai==3.8.0` üzerinden *okunarak* doğrulandı (istisna sınıf
adları, sınıf hiyerarşisi, imza parametreleri, `retry-after-ms` başlık önceliği), ama
gerçek bir 429 veya kesinti senaryosu üretimde tekrar edilmedi.

## Identity (OIDC login + kaynak düzeyinde yetkilendirme)

`wozto_ai_reference.identity`, "bu isteği kim yapıyor" ve "bu kişi BU posta kutusuna bunu
yapabilir mi" sorularını **ayrı** üç katmanda cevaplar. Üçü ayrı, çünkü üçü ayrı biçimde
yanlış gidiyor: kimlik doğrulaması imzayla, oturum sürekliliği sunucu tarafı bir kayıtla,
yetki ise bir **yetki tablosuyla** korunur.

```powershell
# Opsiyoneldir; çekirdek ve testleri bu extra OLMADAN koşar.
pip install -e ".[auth]"
```

* **`oidc`** — authorization code + PKCE (S256). ID token'ın imzası JWKS'ten doğrulanır
  (önbellekli; bilinmeyen `kid` görülünce **bir kez** yeniden çekilir, hâlâ yoksa reddedilir),
  `iss`/`aud`/`exp`/`iat`/`nbf` enjekte edilebilir bir saatle ve **sınırlı** kaymayla
  sınanır, `state` ve `nonce` karşılaştırılır. `alg` izin listesi **çağrı yerindedir** ve
  anahtar aranmadan ÖNCE sınanır. Rol/grup claim yolu yapılandırılabilir
  (`realm_access.roles`, `roles`, `groups`…).
* **`session`** — sunucu tarafı oturum deposu (bellek-içi varsayılan + takas edilebilir bir
  Protocol), imzalı `HttpOnly` `SameSite=Lax` çerezde **yalnız opak bir oturum kimliği**.
  Login'de kimlik döndürülür ve eskisi silinir (fixation savunması); çıkış sunucudan siler;
  oturum başına CSRF token'ı.
* **`authz`** — `authorize(principal, resource, action) -> Decision`, `MailboxGrant`
  tablosuyla. **Hedef posta kutusu HER ZAMAN tablodan, principal üzerinden çözülür; istek
  girdisinden ASLA.** Her karar — izin de ret de — tek bir append-only JSONL satırı yazar.

⭐ **Anahtar varsayılan olarak KAPALI**; mevcut uygulama ve `tests/test_api.py` birebir
aynı kalır. ⚠️ Sınırı ÖLÇÜLMÜŞ hâliyle yazalım (2026-09-06): `api.py` anahtarı okuyabilmek
için `identity` alt paketinin **8 modülünü import EDER** — ama `httpx` ve `joserfc`yi
**ETMEZ**. Önemli olan ikincisidir: `auth` extra'sı kurulu olmayan bir kurulum etkilenmez.
Kanıt `test_switch_off_does_not_import_the_auth_extra`.

### Yapılandırma (anahtar açıkken hepsi ZORUNLU)

| Ortam değişkeni | Ne işe yarar |
|---|---|
| `WOZTO_REFERENCE_OIDC_ENABLED` | `1` olmadan hiçbir şey mount edilmez (varsayılan kapalı) |
| `WOZTO_REFERENCE_OIDC_ISSUER` | IdP'nin `iss` olarak yazdığı dizginin BİREBİR kendisi |
| `WOZTO_REFERENCE_OIDC_CLIENT_ID` | `aud` bununla karşılaştırılır |
| `WOZTO_REFERENCE_OIDC_REDIRECT_URI` | IdP'de kayıtlı olanla birebir aynı olmalı |
| `WOZTO_REFERENCE_SESSION_SECRET` / `..._FILE` | çerez imzalama anahtarı (en az 32 bayt) |
| `WOZTO_REFERENCE_AUTHZ_GRANTS_PATH` | yetki tablosu JSON'u; **yoksa başlangıçta hata** |
| `WOZTO_REFERENCE_AUTHZ_LEDGER_PATH` | karar defterinin JSONL yolu; **yoksa başlangıçta hata** |

Opsiyoneller: `..._OIDC_SCOPES`, `..._OIDC_ROLES_CLAIM` (varsayılan `realm_access.roles`),
`..._OIDC_TENANT_CLAIM`, `..._OIDC_TENANT_ID`, `..._OIDC_TOKEN_AUTH_METHOD`,
`..._SESSION_TTL_SECONDS`, `..._SESSION_COOKIE_SECURE` (varsayılan `1`).

🔴 **`AUTHZ_LEDGER_PATH` neden ZORUNLU?** Eskiden yoksa bellek-içi deftere düşülüyordu ve
bu SESSİZ bir denetim kaybıydı: uygulama çalışır görünür, kararlar doğru alınır, hiçbir
kapı ötmez — ama sürecin ömrü boyunca biriken bütün izin/ret satırları yeniden başlatmada
yok olurdu. Kalıcı olmayan bir defter defter değildir
(`test_missing_ledger_path_fails_closed_instead_of_falling_back_to_memory`).

### Davranış tablosu

Tamamı `tests/test_identity_*.py`'den türetilmiştir — buradaki her satırın karşılığı koşan
bir testtir. **Süre/latency rakamı yoktur**; ölçülen tek zaman tatbikatındır
([`S2-DRILL-2026-09-06.md`](deploy/compose-drill/S2-DRILL-2026-09-06.md)).

| Durum | Sonuç | Neden |
|---|---|---|
| Geçerli kod + eşleşen `state` + eşleşen `nonce` | 303 → oturum çerezi → `/me` principal'i gösterir | mutlu yol |
| `state` bu oturumun başlattığı değer değil | 400 `state_mismatch`, **kod HİÇ harcanmaz** | tek kontrol, kodu harcamadan yapılabilir |
| ID token'daki `nonce` farklı | 400 `nonce_mismatch` | token tekrar oynatma |
| `exp` geçmişte (kayma penceresinin dışında) | 400 `token_expired` | pencere İÇİNDE kalan token kabul edilir (pozitif kontrol) |
| `iat` gelecekte | 400 `issued_in_future` | saat kayması sınırlıdır, sınırsız değil |
| `aud` bizim client değil | 400 `audience_mismatch` | başka uygulamanın token'ı |
| `iss` yapılandırılan issuer değil | 400 `issuer_mismatch` | sabit-zamanlı karşılaştırma |
| `alg` izin listesinde değil | 400 `algorithm_not_allowed`, **JWKS'e HİÇ gidilmez** | `alg:none` / HS-RS karışıklığı imza koduna girmeden kapanır |
| Bilinmeyen `kid` | **tam bir** JWKS yenilemesi, sonra 400 `unknown_signing_key` | rotasyon yenilemeyle çözülürse KABUL (pozitif kontrol) |
| Aynı bilinmeyen `kid` tekrar tekrar | ek yenileme YOK (varsayılan 300 sn) | uç nokta IdP'ye karşı yükseltme aracı olmamalı |
| Keşif belgesinin `issuer`'ı yapılandırmayla eşleşmiyor | `discovery_issuer_mismatch` | yanlış kiracıya bakıyor olabiliriz |
| Login | çerez değeri DEĞİŞİR, eski kayıt SİLİNİR | session fixation |
| Çıkıştan sonra eski çerez | 401 | otorite sunucuda, çerezde değil |
| Çerez imzası kurcalanmış | 401, depoya HİÇ bakılmaz | |
| `/auth/logout` ya da `POST .../drafts` CSRF'siz | 403, **deftere karar YAZILMAZ** | değerlendirilmemiş istek karar değildir |
| alice (`sales-a` sahibi) → `sales-b` read/draft/send | deny `no_grant_for_mailbox` + defter satırı | yetki KUTU BAŞINADIR |
| bob (`sales-b` sahibi) → `sales-a` | aynı | |
| Yetkisi olmayan bir kutu adı (başka kutuda sahip olsa bile) | deny `no_grant_for_mailbox` | "bir yerde sahibim" hiçbir yerde yetki değildir |
| mia (`manager_view`) → `view_summary` (iki kutu) | allow | |
| mia → `draft` / `send` | deny `role_forbids_action` | |
| owner / delegate → read·draft·send·view_summary | allow | `manager_view`in ÜST kümesi (aşağıdaki nota bakın) |
| Her izin ve her ret | **tam olarak bir** defter satırı | kapsama kuralının istisnası yok |
| İki ayrı koşu, aynı dosya | ilk koşunun baytları BİREBİR durur | append-only |
| Yetki tablosu değişti | sonraki istekte etkili, yeniden login GEREKMEZ | iptal, en çok gerektiği anda çalışmalı |

⭐ **`owner`/`delegate` `view_summary`yi de taşır.** `manager_view` "YALNIZ özet" demektir,
tersi değil: kendi kutusunun özetini göremeyen bir sahip güvenlik özelliği değil arızadır.
Tablo bir üst-küme ilişkisidir.

⛔ **Yetki, ID token'ın rolünden GELMEZ.** Keycloak rolleri (`realm_access.roles`)
`Principal.roles`a taşınır ama posta kutusu kararını **yetki tablosu** verir. IdP'de bir rol
kazanmak, bir kutuya erişim kazanmak DEĞİLDİR — bu ayrım Entra ID'de de aynen korunur
([`docs/identity-entra-migration.md`](docs/identity-entra-migration.md)).

### Bilinen sınırlar (henüz KAPATILMADI)

Bunlar bilinen ve bilinçli açık kalemlerdir; "yok" sanılmasınlar diye burada duruyorlar.

* **Anahtar açıkken `POST /query` herkese 503 döner.** `OidcIdentityProvider.resolve()`
  başlık kimliğini reddeder (principal imzalı ID token'dan ve sunucu oturumundan doğar),
  `/query` ise hâlâ başlık yolunu kullanır. `/query`i oturuma bağlamak mevcut rotanın
  sözleşmesini değiştirirdi ve bilinçli olarak YAPILMADI. Sınır bir testle sabitlendi
  (`test_query_is_503_under_the_oidc_switch_known_limitation`) — sessizce değişemez.
* **Oturum deposu varsayılanı bellek-içidir.** Tek süreçte doğru, yeniden başlatmada her
  oturumu düşürür, birden çok kopyada çalışmaz. `SessionStore` Protocol'ü Redis/Postgres
  için hazır ama **böyle bir depo bu pakette YOKTUR**.
* **Refresh token YOK, back-channel logout YOK.** Oturum kendi TTL'iyle biter; IdP tarafında
  yapılan bir çıkış buraya ULAŞMAZ. Token yenileme hiç uygulanmadı.
* **Sertifika kimlik bilgisi (`private_key_jwt`) YOK.** `_exchange_code` yalnız `none`,
  `client_secret_post` ve `client_secret_basic` bilir. Entra için bu bir kod değişikliğidir.
* **Çok kiracılılık YOK.** `iss` sabit bir dizgiyle birebir karşılaştırılır ve
  `MailboxGrant.principal_id` yalnız `sub`'dur. Entra'nın `common`/`organizations` yolu ve
  zorunlu `tid` doğrulaması için ikisi de değişmek zorundadır (migration notunda §2/§7).
* **MFA, Conditional Access, device compliance ÖLÇÜLMEDİ.**
* **Canlı Entra ID denemesi YAPILMADI.** Migration notu belgelerin okunmasıdır; hiçbir
  kiracıya bağlanılmadı.
* **JWKS için periyodik arka plan yenilemesi YOK** — yalnız tembel yükleme +
  bilinmeyen-`kid` yenilemesi. Doğruluk için yeterli, ilk isteğin gecikmesi için değil.
* **Tatbikattaki Keycloak `start-dev` modundadır** ve düz HTTP konuşur; bu yüzden orada
  çerez `Secure=0` ile koşar. Varsayılan `Secure=True`dur ve öyle kalır.

## Doğrulama

```powershell
# Depo kökünden (bu dosyanın bulunduğu dizin).
python -m pytest -q
python -m pip install -r requirements-auth.lock --require-hashes   # `auth` extra: identity testleri
python -m pytest -q
ruff check src/wozto_ai_reference tests
```

⚠️ **Bu bloktaki iki şey ölçülerek düzeltildi (2026-09-06), sanılan hâliyle bırakılmadı:**

* Eski ilk satır `cd reference-implementations/portable-ai-core` idi. **Böyle bir dizin
  YOK** — bu depo 2026-08-19'da ayrı bir public repoya taşındı ve kök zaten paketin kendisi.
  Komut olduğu gibi kopyalanınca ilk adımda ölüyordu.
* Eski son satır `ruff check .` idi ve **exit 1 veriyor**: depoda `identity` işinden ÖNCE
  de var olan **6 bulgu** duruyor (`comparison.py`de 2× `E741`, `test_comparison.py`de
  2× `E501`, `test_e5_embedding.py` ve `test_hybrid_experiment_ingest.py`de `I001` —
  ikisi `--fix` ile düzelir). Bunlar bu değişikliğin kapsamı dışındaki dosyalardadır ve
  BİLİNÇLİ olarak düzeltilmedi. ⚠️ **Yukarıdaki kapsamlı komut da aynı 6 bulguyla exit 1 verir**
  (altı dosyanın hepsi `src/wozto_ai_reference` ve `tests` içinde; 2026-09-06'da ölçüldü) — yani
  bu satır bugün yeşil bir kapı DEĞİLDİR. Yeni kod (`identity/`, `llm_gateway/`, testleri) ruff
  temizdir; "ruff yeşil" ancak o 6 bulgu kapatılınca söylenir (`docs/tech-debt.md`).
* ⛔ **CI ruff KOŞMUYOR.** `.github/workflows/ci.yml` yalnız pytest ve pgvector kapılarını
  koşturur; lint tamamen yerel disiplindir. "CI yeşil" bu depoda "lint temiz" DEMEK DEĞİLDİR.

`identity` testleri `auth` extra'sı olmadan **toplanamaz** (import hatası, sessiz skip
değil). Extra'sız koşuda bu beklenen davranıştır ve CI onu ayrı bir pozitif kontrolle
ölçer.

## Sonraki checkpoint

1. Operatörce seçilmiş güvenli Vault alt kümesiyle 30–50 soruluk gerçek gold set.
2. Yerel production adayı embedding modeli ve keyword/vector/hybrid karşılaştırması.
3. Uygulanan semantic critic adayını insan-onaylı paraphrase/negation/relevance setinde
   kalibre etme; cevap düzeyi hallucination ölçümü ve false-accept=0 kapısı.
4. Korpus 10.000 satıra yaklaşır veya exact p95 hedefi aşarsa ölçülmüş HNSW ayarını
   gerçek tenant/ACL dağılımında yeniden doğrulama; IVFFlat şimdilik seçilmedi.
5. Sonuç kanıtı oluşunca Azure AI Search ve Foundry adapter'ları.
6. Ücretli Azure resource açılmadan önce maliyet, bölge ve cleanup planı için
   eyleme özel operatör onayı.
