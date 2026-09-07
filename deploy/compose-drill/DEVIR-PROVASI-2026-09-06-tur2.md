# Devir Provası — Tur 2 (compose-drill, README-only)

**Repo:** ozantosn24-ux/portable-ai-core @ `93d5e97f4` (commit message: "handover drill round 1 findings — initdb aborted silently ... up.sh fail fast, Keycloak stale-JDBC restart, README truth")
**Ortam:** GitHub Codespace `deploy-lab-q7w9xvr7rwqwh64wv` (Ubuntu 24.04, kernel 6.8.0-1052-azure, Docker 29.7.2, Compose v2.40.3, 2 vCPU / 7.8 GiB), erişim yalnız `gh codespace ssh`. Çalışma dizini `/workspaces/handover2` (bu tur için sıfırdan klonlandı; `/workspaces/drill`, `/workspaces/handover`, `/workspaces/wozto-ops` dokunulmadı). Ön-var `wozto-control-plane` konteyneri baştan sona `RestartCount=0`, `Up`.
**Yöntem:** yalnız `deploy/compose-drill/README.md` okundu; "Run it" bloğu satır satır, sırayla koşuldu (idp profili + tam cleanup dahil). Başka hiçbir dosya okunmadı — hiçbir adım başarısız olmadı ve README kendi başına yetti.

## Adım tablosu

| Adım | Saniye | İlk denemede | Takıldı mı? | Açan şey | README hükmü |
|---|---|---|---|---|---|
| clone + `git checkout 93d5e97f4` | 1 | evet | hayır | — | (hazırlık, README dışı) |
| `chmod +x scripts/*.sh` | 0 | evet | hayır | — | **kısmen yanlış**: dosyalar 666 (`-rw-rw-rw-`) geldi, "644" değil |
| `scripts/gen_secrets.sh` | 0 | evet | hayır | — | covered |
| `scripts/up.sh` | 38 | evet | hayır | — | covered (37.8s ALL_HEALTHY; "n8n-runner was replaced by Compose; re-measuring" kendi kendine çözüldü, exit 0) |
| `scripts/seed.sh` | 47 | evet | hayır | — | covered (webhook mid-run n8n restart README'de zaten yazılıydı) |
| `scripts/verify_tls.sh` | 1 | evet | hayır | — | covered, ca.crt yazıldı |
| `scripts/backup.sh` | 7 | evet | hayır | — | covered, manifest.json birebir |
| `scripts/restore_drill.sh` | 35 | evet | hayır | — | covered, tüm doğrulamalar (satır sayısı/digest/sahiplik/workflow/hazır olma/decrypt) geçti |
| Port iddiası doğrulaması (2 komut, `up.sh` sonrası / idp'den önce) | <1 | evet | hayır | — | covered — çıktı README'nin iddiasıyla birebir aynı |
| `scripts/idp_up.sh` | 92 | evet | hayır | — | covered ("postgres"/"app" replaced-and-remeasure — README yalnız Keycloak vakasını anlatıyor, bu farklı servisler) |
| `scripts/idp_check.sh` | 2 | evet | hayır | — | **kapsam dar**: yalnız alice test edildi, bkz. "vaat edilip görülmeyen" |
| `docker compose -p drill down -v` | 3 | evet | hayır | — | covered, ama idp kaynaklarını da (keycloak + authz-ledger volume) sildi |
| `docker compose -p drill --profile idp down -v` | 0 | evet | hayır | — | **no-op**: "Warning: No resource found to remove for project drill" |
| `rm -f secrets/*.txt ca.crt` | 0 | evet | hayır | — | covered |
| `/etc/hosts` yeniden yazma | 1 | evet | hayır | — | covered, "Device or resource busy" hiç görülmedi |
| `docker rmi drill-app:local drill-app-auth:local` | 0 | evet | hayır | — | covered, 336MB/361MB README'deki ölçümle birebir |

Temizlik sonrası doğrulama: `docker ps -a` → yalnız `wozto-control-plane`. `df -h /` → `overlay 32G, 11G used, 20G avail, 35%`. `/etc/hosts` → `drill.internal` satırı yok (grep boş döndü).

## README fixes I would make (öneri — UYGULANMADI)

1. Alıntı: `chmod +x scripts/*.sh           # the tree ships them mode 644; a fresh clone needs this`
   Öneri: "the tree ships them non-executable; the exact mode after clone depends on your umask (measured `666`/`-rw-rw-rw-` in a fresh Codespace clone, not 644) — run this regardless of what `ls -l` shows."

2. Alıntı (cleanup bloğu, iki `down -v` satırı art arda):
   Öneri: birinci satırın altına ekle — "the plain `down -v` above already removes every container and volume in the project, idp profile included — Compose does not scope `down` by profile. The `--profile idp down -v` line is a no-op if you already ran the base one; expect `Warning: No resource found to remove for project "drill"`, not an error."

3. Alıntı: `scripts/idp_check.sh               # end-to-end logins: alice / bob / mia, cross-user 403s, ledger rows`
   Öneri: script iki bağımsız koşuda da (bkz. aşağı) yalnız alice'i test etti, 16 satırlık çıktının tamamı `== login as alice ==` ile başlayıp `ALL CHECKS PASSED for alice` ile bitiyor — bob/mia'ya ait tek satır yok. Ya iddiayı "logs in as alice end-to-end (cross-tenant 403 against sales-b), logout" olarak daralt, ya da bob/mia'yı tetikleyen bayrağı/adımı ekle.

4. Cleanup bloğu `backups/` ve `key-backups/` dizinlerinden hiç bahsetmiyor; `scripts/backup.sh` bunları oluşturuyor ve README'nin temizlik adımlarının hiçbiri silmiyor.
   Öneri: `rm -f secrets/*.txt ca.crt` satırının altına `rm -rf backups/ key-backups/  # scripts/backup.sh artifacts; not removed above` eklensin.

5. Port iddiası doğrulama komutları "Requirements" paragrafında, "Run it" bloğundan kopuk duruyor; ne zaman koşulacağı (idp'den önce mi sonra mı, cleanup'tan önce mi) yazmıyor.
   Öneri: iki komutu "Run it" bloğuna, `scripts/up.sh`'tan hemen sonra, şu notla taşı: "# run this now, before the optional idp profile publishes two more ports — otherwise the base-stack claim above is untestable."

## Promised but not observed

- `idp_check.sh` için "alice / bob / mia" — iki bağımsız koşuda da (biri normal, biri `/tmp` dosyasına yönlendirilmiş tam çıktı) yalnız alice'in login→authz→logout akışı görüldü; bob ve mia hiç oturum açmadı, "ledger rows" adlı ayrı bir kontrol satırı da yazdırılmadı.

## Observed but undocumented

- `up.sh` sırasında "n8n-runner was replaced by Compose; re-measuring", `idp_up.sh` sırasında "postgres was replaced by Compose; re-measuring" + "app was replaced by Compose; re-measuring" — README yalnız idp profilinde zaten çalışan Keycloak'ın yeniden başlatılması senaryosunu anlatıyor; bu üç farklı servis/senaryo hiç yazılı değil (zararsız, exit 0, kendi kendine çözülüyor).
- `seed.sh` ve `backup.sh`'ın her n8n CLI çağrısında üç satır "Warning: The file specified by ..._FILE contained leading or trailing whitespace; the value was trimmed." basılıyor — kozmetik, ama README hiç bahsetmiyor.
- `docker compose -p drill --profile idp down -v`, birinci `down -v`'den sonra çalıştırılınca stderr'e "Warning: No resource found to remove for project \"drill\"" yazıp exit 0 dönüyor — bir yeni gelen bunu "temizlik eksik kaldı" diye okuyabilir.
- `scripts/backup.sh` sonrası `backups/<UTC-timestamp>/` ve `key-backups/<UTC-timestamp>/` diskte kalıyor; cleanup bloğu bunlara hiç değinmiyor.

**Toplam duvar-saati (16 zamanlanmış README adımı): 227 saniye (~3dk 47sn). Takılma sayısı: 0.**
