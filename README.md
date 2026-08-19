# Wozto Portable AI Core — reference implementation

Bu dizin Wozto'nun AI iş mantığını Azure, AWS veya tek bir model sağlayıcısına
kilitlemeden kuran yerel reference implementation'dır. Varsayılan mod ağ çağrısı,
canlı bulut kaynağı, gerçek müşteri verisi veya ücretli model kullanmaz. İsteğe bağlı
`pgvector` backend'i yalnız operatörün yerelde açtığı PostgreSQL'e bağlanır.

## Bu checkpoint neyi kanıtlıyor?

- Model, search, storage, identity ve telemetry için cloud-neutral portlar vardır.
- Yerel deterministic adapter ile uçtan uca `/query` akışı çalışır.
- Retrieval hem adapter içinde hem servis katmanında tenant ve ACL kontrolünden geçer.
- Yetkili kaynak bulunmazsa sistem cevap uydurmak yerine abstain eder.
- Model yalnız yetki kontrolünden geçmiş context'i görür.
- Güvensiz local header identity varsayılan olarak kapalıdır.
- Manifest dışında kalan belge okunmaz; path escape ve symlink kaynakları reddedilir.
- Chunk kimliği, belge sürümü ve content hash deterministiktir.
- PostgreSQL sorgusu tenant ve ACL filtresini retrieval öncesinde uygular.
- Gold set kapısı Recall@K, MRR, yetkisiz sonuç ve yinelenen vaka kimliğini ölçer.

Bu checkpoint production identity veya Azure deneyimi iddia etmez. Yerel hash
embedding yalnız boru hattını deterministik test etmek içindir; semantic kalite
iddiası veya production embedding modeli değildir.

## Mimari

```text
FastAPI
  └─ QueryService
      ├─ IdentityProvider ─ local headers / Entra ID / Keycloak
      ├─ SearchProvider   ─ in-memory / pgvector / Azure AI Search
      ├─ EmbeddingProvider─ deterministic hash / local model / cloud embedding
      ├─ ModelProvider    ─ deterministic / Foundry / OpenAI / local model
      ├─ DocumentStore    ─ memory / Blob / S3-MinIO
      └─ TelemetryProvider─ memory / OpenTelemetry / Azure Monitor
```

Ayrıntılı güvenlik ve adapter sınırları için [architecture.md](docs/architecture.md)
dosyasına bakın.

## Yerel çalıştırma

```powershell
cd reference-implementations/portable-ai-core
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

## Güvenli ingest ve retrieval ölçümü

Örnek korpus gerçek Vault veya müşteri verisi içermez. Manifest, okunmasına izin
verilen dosyaları tek tek listeler; CLI klasörü recursive taramaz.

```powershell
cd reference-implementations/portable-ai-core
$env:PYTHONPATH="src"

# Yalnız plan üretir; veritabanına yazmaz.
python -m wozto_ai_reference.ingest `
  --source-root sample-corpus/documents `
  --manifest sample-corpus/manifest.json

# Küçük sentetik başlangıç kapısı; gerçek kalite için 30–50 insan-yazımı soru gerekir.
python -m wozto_ai_reference.evaluation `
  --source-root sample-corpus/documents `
  --manifest sample-corpus/manifest.json `
  --gold-set sample-corpus/gold-set.json
```

Gerçek Vault pilotunda ayrı bir kaynak klasörü ve repo dışında tutulan manifest
kullanılmalı; `.env`, credential/session depoları ve müşteri PII dosyaları manifest'e
eklenmemelidir. İlk `--apply` öncesi dry-run çıktısındaki dosya/chunk sayısı operatörce
kontrol edilmelidir.

## İsteğe bağlı PostgreSQL + pgvector

Compose tanımı resmi `pgvector/pgvector` image'ını sabit sürüm etiketiyle kullanır ve
yalnız `127.0.0.1:55432` üzerinde yayınlar. Bu makinede Docker henüz kurulu olmadığı
için gerçek container entegrasyonu bu checkpoint'te çalıştırılmadı; SQL ve transaction
davranışı birim testlidir.

```powershell
# Değeri terminalde yerel olarak belirle; dosyaya/commit'e yazma.
$env:WOZTO_RAG_DB_PASSWORD="<yerel-guclu-parola>"
docker compose up -d

$env:WOZTO_REFERENCE_DATABASE_URL="postgresql://wozto:<url-encoded-parola>@127.0.0.1:55432/wozto_rag"
$env:WOZTO_REFERENCE_BACKEND="pgvector"
$env:WOZTO_REFERENCE_ALLOW_INSECURE_HEADERS="1"

python -m wozto_ai_reference.ingest `
  --source-root sample-corpus/documents `
  --manifest sample-corpus/manifest.json `
  --apply

uvicorn wozto_ai_reference.api:app --port 8080
```

`WOZTO_REFERENCE_BACKEND=pgvector` seçilmişken database URL yoksa uygulama açılmaz.
Ingest her kaynak belgeyi transaction içinde tamamen değiştirir; eski sürümden kalan
chunk'lar böylece sorgulanmaya devam etmez.

## Doğrulama

```powershell
cd reference-implementations/portable-ai-core
python -m pytest -q
ruff check .
```

## Sonraki checkpoint

1. Operatörce seçilmiş güvenli Vault alt kümesiyle 30–50 soruluk gerçek gold set.
2. Yerel production adayı embedding modeli ve keyword/vector/hybrid karşılaştırması.
3. Groundedness critic ve cevap düzeyi hallucination ölçümü.
4. Docker kurulduğunda gerçek pgvector entegrasyon testi ve latency ölçümü.
5. Sonuç kanıtı oluşunca Azure AI Search ve Foundry adapter'ları.
6. Ücretli Azure resource açılmadan önce maliyet, bölge ve cleanup planı için
   eyleme özel operatör onayı.
