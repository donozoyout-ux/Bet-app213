# Disk doluluğunda READ-ONLY kurtarma: önce pg_dump, sonra AES-GCM ZIP

**Kapsam:** Ücretsiz Render PostgreSQL dolu olduğunda mevcut tam parity akışı
`readonly_snapshot_started` aşamasında SQL geçici dosyası yazamadığı için
durabilir. Yeni `LocalRescue` **güvenli bir ara yedek** üretmeyi dener:
kaynak DB’de veri satırlarının hashlenmesini **çalıştırmaz**; doğrudan
salt-okunur repeatable-read snapshot altında tam `pg_dump --format=custom`
çalıştırır. Yalnız küçük sistem kataloglarından tablo/sequence isimleri
ve large-object sayısı alınır. Çıktı kullanıcıya özel yerel geçici alana
yazılır; aynı makinede AES-256-GCM korumalı 16 MiB chunk'lara bölünür;
diske yazılmış ciphertext'ten geri okunup çözülür, bağımsız boş PostgreSQL 18'e
geri yüklenir, gerçek restore edilmiş kayıtların **yerel** SHA-256 hashleri
hesaplanır. Kaynak ve restore nesne isimleri karşılaştırılır.

**Güvence ayrımı:** `restore_verified=true` yalnızca **indirilen yerel
şifreli byte'lardan restore başarıyla tamamlandı** demektir.
`source_row_content_verified=false`,
`row_content_verified=false`, `offsite_verified=false`,
`application_read_through_verified=false`.
Bu mod **eski verileri silme/sıfırlama izni vermez**. GitHub'a otomatik
yüklemez, Render planını değiştirmez ve source üzerindeki read-only oturum
haricinde hiçbir işlem yapmaz.

Kurtarma da `No space left on device` ile başarısız olabilir. Render Free
PostgreSQL üzerinde fiziksel disk tamamen doluysa `pg_dump` da çalışamayabilir.
Böyle durumda aynı sorguları tekrar tekrar çalıştırmayın; **kaynak DB'yi
silmeden** disk büyütme/ücretli plan için ayrı onay veya Render desteği gerekir.

## Çalıştırma

En güncel CI'nin (Windows PowerShell 5.1 parser, Python/Postgres18 restore) yeşil
olduğunu teyit edin; indirilmiş eski ZIP kodu kullanılmamalı.

```powershell
# Aşağıdaki dalın tam yeni ZIP'i tarayıcıdan çekilip ayrı klasöre açılır.
# Ardından o klasörde:
powershell.exe -NoProfile -File .\scripts\owner-full-recovery.ps1 -Mode PreflightLocal

# Önceden oluşturulup bağımsız offline kopyası saklanmış anahtarı YENİDEN ÜRETMEYİN.
powershell.exe -NoProfile -File .\scripts\owner-full-recovery.ps1 -Mode LocalRescue -BackupId "vv755-owner-rescue-20261031-e" -KeyFile "$env:LOCALAPPDATA\KuponRecoveryKeys-20261031\recovery.key" -KeyRetainedOffline
```

PowerShell yalnızca Render **External Database URL** ister ve girişi gizler.
Hassas DSN, key, token, PostgreSQL dump içeriği GitHub PR, Issue, chat ve loglara
konulmamalı. Çalışma klasörleri `%LOCALAPPDATA%\KuponRecovery\run-<BackupId>`
altındadır. Var olan başarısız/başarılı run klasörleri **silinmez** ve aynı ID
tekrar kullanılmaz.

Başarılı yerel kurtarma işareti:

`RESCUE_LOCAL_DUMP_RESTORED_SOURCE_ROW_PARITY_NOT_VERIFIED`

Bu mesaj yalnızca **yerel doğrulamayı** gösterir. ZIP dosyası özel GitHub
Release asset olarak **tarayıcıdan** yüklenir; aynı dosya bağımsız olarak
GitHub'dan geri indirilir ve SHA-256 aynı mı diye kontrol edilir. Hash eşleşse
dahi gerçek kaynak satır parity tam doğrulanana, site geçmiş veriyi arşivden
okuyana ve sahibi sonrasında açıkça onay verene kadar DB'den kayıt silinmez.

Durum raporu `RESCUE_INCOMPLETE` veya `OWNER_RECOVERY_INCOMPLETE stage=...`
olduğunda kaynak DB'ye müdahale etmeyin. `pg_dump` denemesi diski daha da
zorluyorsa ve tekrar hata verirse durun.

## Önkoşullar ve sınırlar

- Kaynak adresi ve database ismi uygulamada pinlenmiş: yanlış Render/Neon DB
  yedeklenemez. İzin verilen SSL modları only require/verify-*; plaintext yok.
- `pg_dump` Docker PostgreSQL 18 imajından, salt-okunur snapshot ile başlar;
  kaynakta DDL/DML/delete/retention yok.
- Verifier yalnız rastgele kimlikli, o run için etiketlenmiş, ayrı
  `127.0.0.1:5433` PostgreSQL 18 Docker konteyneridir. Başka konteyner,
  volume, veritabanı değiştirilmez.
- Yerel ciphertext ZIP 2 GB tek Release asset sınırını aşarsa kapalı hata.
- Anahtar 32 byte ve offline saklanmalıdır; ZIP anahtarı içermez. ZIP içinde
  `*.enc` ve hassas olmayan `summary.json` dışında dosya yok.
- Aşırı düşük Render diskinde bile PostgreSQL kendi temp/WAL ihtiyacı nedeniyle
  `pg_dump` başarısız olabilir. Bu mod garantili kurtarma değildir.
