# Portable AI Core — architecture and trust boundaries

## Amaç

Wozto'nun ortak iş kuralları model, arama motoru, bulut veya UI seçiminden bağımsız
kalır. Provider adapter'ları altyapı ayrıntısını taşır; `QueryService` ise provider
sonucuna körü körüne güvenmeden tenant, ACL, abstention ve citation kurallarını uygular.

## Portlar

| Port | Bugünkü adapter | Sonraki adapter örnekleri |
|---|---|---|
| `ModelProvider` | `DeterministicGroundedModel` | Microsoft Foundry, OpenAI/Anthropic gateway, Ollama/vLLM |
| `SearchProvider` | memory hybrid + `PgVectorStore` | Azure AI Search |
| `EmbeddingProvider` | deterministic `HashEmbeddingProvider` | local embedding, cloud embedding |
| `DocumentStore` | transaction'lı `PgVectorStore` | Azure Blob, S3/MinIO |
| `IdentityProvider` | açıkça etkinleştirilen local header adapter | Entra ID, Keycloak/Authentik |
| `QueryPolicy` | opt-in `DenyPhraseQueryPolicy` | merkezi policy engine / sınıflandırıcı + insan kuyruğu |
| `TelemetryProvider` | `MemoryTelemetry` | OpenTelemetry, Azure Monitor/Application Insights |

## Güven sınırları

1. İstemcinin tenant ve rol beyanı production'da güvenilir değildir. Local header
   adapter yalnız geliştirme/test içindir ve varsayılan olarak kapalıdır.
2. Retrieval veya tool çıktısı veri kabul edilir; uygulamaya talimat veya yetki vermez.
3. Search adapter tenant/ACL filtresi uygular. `QueryService` aynı kontrolü ikinci kez
   yapar; hatalı veya ele geçirilmiş adapter'ın cross-tenant context'i modele taşımasını
   engeller.
4. Operatörce yapılandırılan hard query policy reddederse search ve model hiç çağrılmaz.
5. Açık `as_of`, `source_status` ve `source_authority` koşulları servis katmanında
   kaynak metadata'sına karşı tekrar doğrulanır. Doğal dil otomatik yetki sayılmaz.
6. Yetkili, kapsam-içi ve eşik üstü kaynak yoksa model çağrılmaz; cevap abstain olur.
7. Citation yalnız modele verilen yetkili ve kapsam-içi hit'lerden türetilir; validity ve
   authority metadata'sını da taşır.
8. Secret, `.env`, credential, session veya token dosyaları document ingestion kapsamına
   alınmaz.
9. Ingest yalnız harici manifest'te açıkça listelenen relative Markdown yollarını okur;
   root escape, symlink, binary içerik ve boyut sınırı ihlali fail-closed reddedilir.
10. PostgreSQL tenant ve rol filtresini ranking'den önce uygular. Servis katmanı sonucu
   tekrar kontrol ederek defense-in-depth sağlar.
11. Kaynak güncellemesi eski chunk'ları silip yeni sürümü aynı transaction'da yazar.

## Production'a geçmeden önce açık kapılar

- JWT doğrulayan identity adapter ve tenant'ın doğrulanmış claim'den türetilmesi,
- parser sürümü/provenance ve silinen manifest kaynakları için reconciliation,
- gerçek kullanıcı sorularından en az 30–50 vakalık lexical/vector/hybrid retrieval eval,
- cevap üretiminden bağımsız groundedness critic,
- doğal dilden zaman/otorite koşulu çıkaran bileşen için insan-etiketli calibration;
  bu bileşen hard policy'nin veya kaynak metadata doğrulamasının yerine geçmez,
- prompt injection ve cross-tenant negatif testleri,
- OpenTelemetry trace, latency, token ve cost-per-task ölçümü,
- retry/idempotency, queue/DLQ ve restore tatbikatı,
- Azure adapter'ı için Managed Identity/Key Vault/RBAC ve private-network kararı.
