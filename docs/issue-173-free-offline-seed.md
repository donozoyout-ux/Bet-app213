# PR #173: Şifreli kurtarma ZIP'inden küçük ücretsiz PostgreSQL seed (sadece yerel)

**Durum:** Draft. Eski veritabanına hiçbir SQL çalıştırmaz, yeni Render PostgreSQL
oluşturmaz, web servisine deploy yapmaz veya mevcut `DATABASE_URL` değiştirmez.
CI yeşili tek başına gerçek kullanıcı yedeğinin restore edildiğini kanıtlamaz.

## Sabit kaynak

- ZIP: `%LOCALAPPDATA%\KuponRecovery\run-vv755-owner-rescue-20261031-e\pg-recovery-vv755-owner-rescue-20261031-e.zip`
- SHA256: `566e475ab72d3b4a6b05b88bbca9811c72f43d6bf7fea62927dff5d4fe626ae4`
- Restored source dump SHA256: `b66fd7b17d40ea59d0abdfd66b821fd36fb2ada0e22780883754f3e565747082`.
- Anahtar: kullanıcıda, asla GitHub'a veya sohbete gönderilmez.
- Old source: `Kuponanaliziivv755` (~766 MB; salt okunur araştırma yapılmıştır).
- Sadece 15 mevcut yayın sürümü `board_heads` / `board_read_publications`
  FK'si ile tutuluyor; 4.586 adet diğer değişmez yayın sürümü yeni hot seed'de
  **yer almayacak**, ama tam orijinal ZIP'te kalacaktır.
- 37 tablo ve 324783 original restored satır yalnızca eski ZIP'in yerel
  restore kanıtıdır. Yeni seed için ayrı gerçek rapor gerekir.

## İzole aşamalar

1. ZIP boyut/sha, manifest AES-GCM AAD, imzalı chunk checksum'ları, encrypted
   receipt ve eski restore flag'leri doğrulanır. Böyle bir doğrulama başarısızsa
   seed oluşturulmaz.
2. Kaynak tam `pg_dump` sadece yereldeki geçici özel dizine açılır.
3. `postgres:18` local loopback `127.0.0.1:5433` ayrı etiketli konteyner
   açılır; tam dump geri yüklenir, bütün tabloların satır hash'leri hesaplanır.
4. Yalnız bu **kopyada** aktif FK bağlı `board_editions` sürümleri korunarak
   diğer eski editions kaldırılır, korunan sürümlerin birebir hash'i kontrol
   edilir, yerel `VACUUM FULL` uygulanır. Başka tabloların hash'i değişemez.
5. Küçük `pg_dump -Fc -Z6` oluşturulur; AES-256-GCM ile yeni yerel ZIP içine
   konur. ZIP'deki şifreli byte'lar geri açılır, ilk plaintext dump değil bu
   kopya ayrı `127.0.0.1:5434` PostgreSQL 18'e geri yüklenir; bütün tabloların
   tam satır-hash envanteri (ve sekans/large object metadata) karşılaştırılır.
6. Başarıda yerel yol ve SHA256, 37 tablo, korunmuş satır sayısı raporlanır;
   yerel **ayrı** ZIP korunur, tam eski ZIP'e dokunulmaz. Başarısızlıkta
   `HOT_SEED_INCOMPLETE`, üretim tarafında hiç işlem yapılmamış demektir.

## Yalnız yetkili yerel deneme — şimdi çalıştırılması istenmiyor

Üç kapı tamamlandıktan sonra (PR CI yeşil, Docker Desktop Linux, yedek anahtarının
bağımsız kopyası) sahibin kendi Windows bilgisayarında taslak branch'in güncel
ZIP'i ayrı klasöre açılarak aşağıdaki komut *yalnız yerel prova* için kullanılır:

```powershell
python -m src.archive.free_seed_builder `
  --zip "$env:LOCALAPPDATA\KuponRecovery\run-vv755-owner-rescue-20261031-e\pg-recovery-vv755-owner-rescue-20261031-e.zip" `
  --key-file "$env:LOCALAPPDATA\KuponRecoveryKeys-20261031\recovery.key" `
  --backup-id "vv755-hotseed-20261031-a" `
  --output-root "$env:LOCALAPPDATA\KuponRecovery\hot-seed-vv755-20261031-a"
```

Bu komut yerel disk alanını ve Docker konteynerlerini kullanır. **Kaynak
Render PostgreSQL'in bağlantı URL'sini istemez.** Port 5433 veya 5434 meşgulse
durur, başka konteyneri kapatmaz. Output root zaten varsa üzerine yazmaz.

Şifreleme anahtarı yalnızca yerel dosyadan okunur. ZIP plaintext dump ve gizli
loglar hiçbir GitHub işlemine eklenmez. `pg_dump` şifrelenmiş yeni ZIP'in
oluşturulması, orijinal GitHub arşivini tüketmez veya kaynak DB'den alan boşaltmaz.

## Henüz çözülmeyen ürün gereksinimleri

- Arşivdeki eski board sürümlerini siteden okuyacak **gerçek, doğrulanmış
  API+frontend** işlevi hazırlanmalı; arşiv monolitik ZIP, doğrudan sorgulanabilir
  indeks değil. Tarihsel maçlar / yayınlar / tahmin revizyonları sahte olarak
  tamamlanmış gösterilmez.
- Yeni hot seed yalnızca gerçek envanter/iki restore kanıtından sonra serbest
  bırakılır. Sonrasında **ayrı** kullanıcı onayıyla yeni Free Render PG18 açılır,
  boş hedef kontrol edilir, dump restore edilir, site read/API yazma smoke yapılır
  ve rollback planı ile DATABASE_URL çevrilir.
- Yeni günlük kayıtlar için bugünkü + önümüzdeki 3 gün sınırı ve sonuç kesinleşene
  kadar kalacak maçlar; 1 gün sonra GitHub'a şifreli doğrulanmış archive; büyüme
  guard'ı, kalite eşikleri ve otomatik kuponları ayrıca test etmek gerekir.
- Render Free PostgreSQL 30 günlük süre sınırına sahiptir; bu yol sonsuz ücretsiz
  sabit depolama sağlamaz.
- Mevcut source `Kuponanaliziivv755` asla silinmez veya temizlenmez; owner
  tarafından sonradan ayrıca explicit onay gerekir.

`source_row_content_verified=false`, `eviction_authorized=false`,
`historical_read_through_verified=false` yeni başlangıç ZIP'inin temel
kimlikleridir; bunlar tamamlanmadan prod kesintisiz geçmiş erişimi iddia edilmez.
