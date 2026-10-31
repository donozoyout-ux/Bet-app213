# GitHub CLI olmadan ücretsiz tam yedek — tarayıcı üzerinden tek ZIP yükleme

**Taslak ve geliştirme dalı:** PR #169 temelinden ayrıldı. Kod üretim Render uygulamasına dağıtılmaz. Kaynak PostgreSQL üzerinde yalnızca REPEATABLE READ READ ONLY snapshot ve pg_dump; hiçbir silme/migration yok.

## Ön koşul ve güvenlik sınırları

- Windows PowerShell, Docker Desktop (Linux engine açık), Python + requirements. GitHub CLI/gh login veya PAT **gerekmez**.
- Etkin veritabanı `Kuponanaliziivv755` için Render **External Database URL** sadece gizli yerel girişte kullanılmalı. Chat, GitHub issue, ekran görüntüsü, log ve repoya yazılmamalı.
- Şifreleme anahtarını kullanıcı Windows makinesinde yeni, özel bir klasöre CSPRNG ile üretir; ayrıca bağımsız offline kopyasını saklar. Kaybolursa şifreli yedek çözülemez.
- En az 8 GiB Windows çalışma alanı ve yeterli Docker disk alanı. Kaynak 2 GiB dump, ZIP tek Release asset için 2 GB sınırını aşarsa çıkış **fail closed**; bölünmüş tarayıcı yüklemesi ayrı inceleme gerektirir.
- Yerel doğrulama sırası: kaynak snapshot + tüm tablo satır SHA-256 multiset + pg_dump + şifreli chunk/manifest/receipt + yerelde şifre çözülmüş dosyadan değil, depoya yazılmış **şifreli** byte'ların geri okunması + izole boş PG18 restore + tablo/sequence/row hash paritesi. Sonrasında **şifreli ZIP** hazırlanır. Çalışma sırasında plaintext dump ve hash spool yalnızca kullanıcıya özel geçici klasörde bulunur; olağandışı crash sonrası makinedeki kalıntılar gizli tutulmalı.

## Çalıştırma (PR branch yerelde güncellendikten ve CI yeşile döndükten sonra)

```powershell
# From existing C:\Users\PC\KuponRecovery-169 git checkout:
git fetch origin codex/168-browser-upload-fallback
git switch --track origin/codex/168-browser-upload-fallback

powershell.exe -NoProfile -File .\scripts\owner-full-recovery.ps1 -Mode PreflightLocal
```

`PREFLIGHT requiredReady: True` gerekir. `githubAuthenticated: False` **beklenebilir ve önemsizdir**.

Anahtar mevcutsa `InitKey` çalıştırıp eskisini değiştirmeyin. İlk defa yeni key oluşturulacaksa:

```powershell
powershell.exe -NoProfile -File .\scripts\owner-full-recovery.ps1 -Mode InitKey -KeyFile "$env:LOCALAPPDATA\KuponRecoveryKeys-20261031\recovery.key"
```

Anahtarın ayrı, çevrimdışı kopyasını güvenle aldıktan sonra yalnız **kullanıcının kendi bilgisayarında**:

```powershell
powershell.exe -NoProfile -File .\scripts\owner-full-recovery.ps1 -Mode LocalBackup -BackupId "vv755-local-owner-20261031-a" -KeyFile "$env:LOCALAPPDATA\KuponRecoveryKeys-20261031\recovery.key" -KeyRetainedOffline
```

İlk seferde `BackupId` yeni/benzersiz olmalı. Windows, aktif Render external URL'yi **gizli** ister. GitHub CLI giriş kontrolü ve GitHub API çağrısı yok. Süreç sonunda yalnız kaynağı doğrulanmış, **yerel şifreli ZIP**, ZIP SHA-256 ve `offsite_verified=false` döner. ZIP dosyası `%LOCALAPPDATA%\KuponRecovery\run-<ID>\pg-recovery-<ID>.zip` konumundadır. Dosya başarısızlıkta yoksa asla yedek tamam sayılmaz.

## GitHub'a tarayıcıyla aktarma (manuel)

1. Tarayıcıdan özel [kuponanalizii-data-archive Releases](https://github.com/donozoyout-ux/kuponanalizii-data-archive/releases) sayfasına giriş yapın.
2. **Draft a new release** seçin. Benzersiz `postgres-recovery-v1-<BackupId>` tag'ini oluşturun. ZIP'i **release asset** olarak ekleyin; **normal Git commit'i veya public repo kullanmayın**.
3. ZIP yüklemesinin tamamlandığını doğrulayın. `summary.json` içindeki `offsite_verified=false` bu aşamada otomatik olarak `true` olmaz: kullanıcı tarafından upload başarılı olsa bile **GitHub'dan geri indirme** ve SHA-256 eşitlik kontrolü gerekir.
4. Yüklenen ZIP'i tarayıcıdan yeni, ayrı bir klasöre indirin. PowerShell'de iki ZIP'in `Get-FileHash -Algorithm SHA256` sonuçlarını karşılaştırın; beklenen hash ayrıca local final summary'de bulunur. Eşleşmezse hiçbir temizlik/taşıma yapılmaz.
5. Daha yüksek güvence için ZIP'i indirilen kopyadan ayrık PG18'e restore eden tekrar test modu ayrıca gereklidir. **Bu PR, remote restore'ı tamamlanmış saymaz.** Hem GitHub offsite eşitliği hem gerçek site archive read-through ve owner onaylı migration olmadan source temizliği YASAK.

## Sınırlamalar

- Tarayıcıdan yükleme bir dış arşiv eylemidir; program otomatik upload yapmaz. Repo **private** kalmalı.
- ZIP yalnız şifreli `*.enc` dosyaları ve teknik `summary.json` içerir; plaintext dump/key/token eklenmez. Gerçek DB bağlantısı/TLS ve kaynak izinleri ayrıca yerel Backup esnasında doğrulanır.
- Bozuk ZIP, kayıp anahtar, hatalı kaynak kimliği, non-empty verifier ve parity hatası fail-closed.
- GitHub'da yayınlanmış Release + aynı hash ve gerçek site archive-backed read-through olmadan PostgreSQL sıfırlanmaz. Render Free veritabanı süre bitimi temizleme ile uzamaz.

## Hata kodları: yalnız sabit aşama adı, gizli bilgi gösterilmez

Windows çalıştırıcısı herhangi bir arıza durumunda yalnızca `OWNER_RECOVERY_INCOMPLETE stage=<sabit_kod>` döndürür. Bağlantı URL'sini, şifreyi, token veya istisna içeriğini hiçbir yere yazdırmaz. Kaynakta değişiklik yapmaz. Eski ID ile klasör oluşmuşsa aynı ID'yi tekrar kullanmak yerine benzersiz yeni bir BackupId seçin.

- `source_identity_validation`: Girilen URL'nin **aktif** `Kuponanaliziivv755` PostgreSQL veritabanının Render **External Database URL** alanından olduğunu kontrol edin. Suspended eski DB, dahili URL veya Neon URL kabul edilmez. Bağlantıyı mesaja yapıştırmayın.
- `source_readonly_tls_connectivity`: URL'nin biçimi doğru ama TLS/şifre/ağ bağlantısı kurulamadı; aktif DB'nin erişimini Render Dashboard ve yerel ağdan kontrol edin. Sadece `SELECT` denemesi, DDL/DML yok.
- `local_private_workspace`: Gerekli klasör zaten varsa yeni BackupId seçin veya yerel izinleri inceleyin; mevcut klasörü silmeyin.
- `isolated_postgres_container_start` / `isolated_postgres_container_ready`: Docker ve localhost 5433, başka mevcut container/volume silmeden incelenmeli.
- `readonly_export_local_restore`: Gerçek `pg_dump` / şifreli ZIP / bağımsız PG18 restore sırasında işlem durdu; yalnız gizli bilgi içermeyen `RECOVERY_INCOMPLETE: <kod>` aşamalarını paylaşın. Kaynaktan veri silmeyin.

Aşama kodları doğrudan kullanıcının girdiği metinden türetilmez; sabit, önceden tanımlanmış dizelerdir. 
