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
| `QueryScopeResolver` | opt-in `ConfiguredPhraseScopeResolver` | insan-kalibreli zaman/otorite sınıflandırıcısı |
| `EvidenceSupportCritic` | opt-in exact + threshold semantic structured claim critic'leri | insan-kalibreli production judge |
| `TextPairScorer` | opsiyonel pinned Transformers sequence classifier | yerel ONNX/OpenVINO, cloud classifier |
| `TelemetryProvider` | `MemoryTelemetry` | OpenTelemetry, Azure Monitor/Application Insights |
| `ChatProvider` (llm_gateway) | `ScriptedProvider` + opsiyonel Anthropic/OpenAI adapter'ları | başka satıcı SDK'sı, self-hosted vLLM/Ollama |

## LLM gateway (`llm_gateway`)

`ModelProvider` grounded cevap portudur (retrieval hit'leri alır, citation sözleşmesine
bağlıdır); `ChatProvider` onun altındaki ham sohbet katmanıdır ve retrieval'dan haberi
yoktur — bu yüzden ayrı bir porttur, `ModelProvider`'ı genişletmek her RAG adapter'ına
işine yaramayan sohbet parametreleri eklerdi. `FailoverRouter` bu katmanda
birincil/yedek sağlayıcı arasında retry → circuit breaker → failover uygular ve üç sınırı
aynı fail-closed mantıkla kurar: (a) sağlayıcı SDK'ları **opsiyonel extra**dır, yalnız
adapter kurulurken import edilir, çekirdek satıcı-nötr kalır; (b) yan etkisi olan bir
istek (`idempotent=False`), ilk denemenin işlenip işlenmediği kanıtlanamıyorsa **ikinci
kez gönderilmez** — ne aynı sağlayıcıya ne yedeğe; belirsizlik çağırana yükseltilir,
çünkü onu uzlaştırabilecek tek taraf odur; (c) akışta failover yalnız istek sınırındadır
ve `StreamEnd` her zaman **tek bir sağlayıcının tam metnini** taşır, iki sağlayıcının
çıktısı asla birleştirilmez. Her deneme append-only bir `AttemptLedger` satırı üretir;
bu defter `TelemetryProvider`'dan ayrıdır çünkü örneklenemez, gruplanamaz ve yeniden
kurgulanabilir olmak zorundadır. Defterin kapsama kuralının istisnası yoktur:
**sağlayıcı çağrıldıysa satırı vardır.** Bu yüzden taksonomi dışı bir adaptör hatası
(`UnclassifiedProviderError`; belirsiz sayılır, çünkü sınıflandırılamayan bir istisna
isteğin ulaşmadığının kanıtı değildir) ve tüketicinin akıştan çekilmesi
(`outcome="abandoned"`) de yazılır — ikisi de daha önce hiç iz bırakmadan geçebiliyordu.
Devre kesici yalnız **sağlayıcı** arızasını sayar: `BadRequestError` /
`ContentPolicyError` isteğin kusurudur ve sağlıklı bir sağlayıcıyı çitlememelidir.

## Güven sınırları

1. İstemcinin tenant ve rol beyanı production'da güvenilir değildir. Local header
   adapter yalnız geliştirme/test içindir ve varsayılan olarak kapalıdır.
2. Retrieval veya tool çıktısı veri kabul edilir; uygulamaya talimat veya yetki vermez.
3. Search adapter tenant/ACL filtresi uygular. `QueryService` aynı kontrolü ikinci kez
   yapar; hatalı veya ele geçirilmiş adapter'ın cross-tenant context'i modele taşımasını
   engeller.
4. Operatörce yapılandırılan hard query policy reddederse search ve model hiç çağrılmaz.
5. Opt-in scope resolver yalnız daraltıcı constraint üretebilir. Birden fazla kural veya
   açık request constraint'iyle çelişirse search başlamadan abstain edilir.
6. Etkin `as_of`, `source_status` ve `source_authority` koşulları servis katmanında
   kaynak metadata'sına karşı tekrar doğrulanır. Doğal dil otomatik yetki sayılmaz.
7. Yetkili, kapsam-içi ve eşik üstü kaynak yoksa model çağrılmaz; cevap abstain olur.
8. Opt-in evidence critic modelden sonra, citation'dan önce çalışır. Reddettiği cevap
   kullanıcıya sızdırılmaz. Destek id'leri retrieved/authorized kümenin dışındaysa servis
   critic'e güvenmez ve abstain eder.
9. Citation yalnız critic'in desteklediği yetkili ve kapsam-içi hit'lerden türetilir;
   critic yoksa önceki davranışla tüm yetkili hit'leri taşır.
10. Structured answer'daki her atomik claim exact belge sürümü ve content hash'e bağlanır.
    Exact structured critic, claim dışı cevap metnini ve retrieved/authorized kümede olmayan
    evidence referansını reddeder; query relevance veya semantic entailment iddiası taşımaz.
11. Semantic structured critic aynı mekanik kapıları model çağrısından önce uygular. Ardından
    her claim için query relevance ve her atıf için entailment eşiği ister. Scorer exception,
    non-finite/out-of-range skor, eksik skor veya cited evidence'lardan herhangi birinin eşik
    altı kalması fail-closed abstain olur. Eşik ve model revision için varsayılan yoktur.
12. Secret, `.env`, credential, session veya token dosyaları document ingestion kapsamına
    alınmaz.
13. Ingest yalnız harici manifest'te açıkça listelenen relative Markdown yollarını okur;
    root escape, symlink, binary içerik ve boyut sınırı ihlali fail-closed reddedilir.
14. PostgreSQL tenant ve rol filtresini ranking'den önce uygular. Servis katmanı sonucu
    tekrar kontrol ederek defense-in-depth sağlar.
15. Kaynak güncellemesi eski chunk'ları silip yeni sürümü aynı transaction'da yazar.

## Production'a geçmeden önce açık kapılar

- JWT doğrulayan identity adapter ve tenant'ın doğrulanmış claim'den türetilmesi,
- parser sürümü/provenance ve silinen manifest kaynakları için reconciliation,
- gerçek kullanıcı sorularından en az 30–50 vakalık lexical/vector/hybrid retrieval eval,
- semantic critic için insan-onaylı paraphrase/negation/query-relevance seti, pinned model
  revision'ları ve false-accept=0 doğrulanmış eşikler,
- doğal dilden zaman/otorite koşulu çıkaran bileşen için insan-etiketli calibration;
  bu bileşen hard policy'nin veya kaynak metadata doğrulamasının yerine geçmez,
- prompt injection ve cross-tenant negatif testleri,
- OpenTelemetry trace, latency, token ve cost-per-task ölçümü,
- queue/DLQ ve restore tatbikatı; `llm_gateway` `AllProvidersUnavailable(queue_hint=True)`
  ile kuyruğa alınması gerektiğini SÖYLER ama kuyruğun kendisi bu pakette YOKTUR ve
  gateway hiçbir canlı sağlayıcı kesintisine karşı ölçülmedi (SDK eşlemesi kurulu
  paketten okunarak doğrulandı, üretimde tekrar edilmedi),
- Azure adapter'ı için Managed Identity/Key Vault/RBAC ve private-network kararı.
