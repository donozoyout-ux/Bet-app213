# Mevcut PostgreSQL: salt okunur alan ve arşiv planı

Bu PR yalnız ölçüm ve plan içerir. Yeni PostgreSQL, canlı şema değişikliği,
silme, TRUNCATE, tetikleyici devre dışı bırakma veya bakım komutu çalıştırmaz.
Kaynak: 31 Ekim 2026 Render salt okunur SQL sonuçları; ham toplu ölçümler
`storage-observation-2026-10-31.json` dosyasında. Ölçümler ayrı bağlantılardan
alındı; eşzamanlı yazıcılar nedeniyle tek snapshot değildir. Yeniden ölçüm için
`scripts/storage_readonly_report.sql` tek REPEATABLE READ READ ONLY işlem,
30 saniye sorgu ve 2 saniye kilit sınırı kullanır; sonunda ROLLBACK yapar.
Katalog satır tahminleri kesin sayım değildir. Fiziksel disk kotası, WAL,
diğer veritabanları ve boş disk alanı bu ölçümle bilinmiyor.

## Ölçüm ve tahmin

Toplam veritabanı: **802.936.511 bayt / 765,74 MiB**.

| Tablo | Toplam MiB | İndeks MiB | TOAST MiB |
|---|---:|---:|---:|
| board_editions | 518,20 | 0,64 | 516,86 |
| match_events | 87,36 | 32,72 | 0,01 |
| market_odds | 40,59 | 7,48 | 0,01 |
| odds_1x2 | 22,27 | 2,15 | 0,01 |
| asian_totals | 21,87 | 2,16 | 0,01 |
| asian_handicap | 21,70 | 2,18 | 0,01 |
| match_statistics | 19,35 | 0,33 | 0,01 |

TOAST kendi indeksleri dahil ölçülür; ana tablo indeksleri ayrıca sayılır.
En büyük indeks `match_events_match_id_source_key_key`: 24,55 MiB.
`idx_scan=0` olması kaldırma gerekçesi değildir: benzersizlik korumasıdır,
istatistikler de sıfırlanabilir. Hiçbir indeks kaldırılması önerilmiyor.

Kesin toplu sayımlar: 12.390 maç; 9.766 geçmiş tamamlanmış maç;
bugün + 3 gün içinde 89 maç (88 scheduled, 1 postponed).
8 geçmiş scheduled ve 1 abandoned kayıt otomatik arşivleme dışıdır.
2.526 daha ileri tarihli scheduled maç da korunur.

4.601 board sürümünün 1.491'i bilinen dört referans kaynağında kullanılıyor.
3.110 aday sürümün depolanmış payload toplamı **345.043.022 bayt / 329,06 MiB**.
Bu değer boş disk vaadi veya bloat ölçümü değildir; JSON içindeki ek referanslar
incelenmeden bu sürümler bile silinebilir kabul edilmez.

| Senaryo | Tahmini veritabanı | Garanti edilen fiziksel kazanç |
|---|---:|---:|
| Bu PR sonrası (yalnız okuma) | 765,74 MiB; doğal büyüme hariç | 0 |
| İleride onaylı mantıksal temizlik, yeniden yazma yok | Yaklaşık mevcut boyut | 0 |
| Yalnız aday board payload alanı tamamen geri kazanılabilirse | 436,68 MiB | Garanti yok |

Son satır `802936511 - 345043022` aritmetik senaryosudur; satır/indeks
yerleşimi, TOAST parçaları, devam eden yazılar ve WAL etkisini modellemez.
Diğer tablolar için satır oranını bayt oranı sayıp sahte kesinlik üretilmez.
Gerçek sonraki boyut ancak onaylı bakım sonrası aynı sorgularla ölçülebilir.

## Dört günlük aktif pencere ve bağımlılıklar

Europe/Istanbul takvimine göre [bugün 00:00, bugün+4 gün 00:00).
31 Ekim çalışması için UTC sınırları 30 Ekim 21:00 ve 3 Kasım 21:00.
Pencerenin tamamı, daha ileri gelecek maçlar, tarihi bilinmeyenler,
sonuçlanmamış ve sonuç kanıtı eksik maçlar PostgreSQL'de kalır.
Yalnız geçmiş finished kayıtlar inceleme adayıdır; aday olmak izin değildir.

Maç arşivi ilişkisel bir kapanış olmalı: matches, teams/leagues/bookmakers
kimlikleri; odds_1x2, asian_handicap, asian_totals, market_odds, odds_snapshots;
match_events ve match_statistics; prediction_snapshots/publications,
prediction_settlements, published_match_analyses, kuponlar ve öğrenme kanıtları.
scraper_jobs ve scraper_job_items bağlantıları da hesaba katılmalı.
Kaynak/kanıt JSON alanları ve revizyonlar kayıpsız tutulmalı.

Board kapanışı yalnız FK değildir: board_heads ve board_read_publications FK;
prediction_publications.board_version ve published_match_analyses.board_version
düz metin referanslarıdır. saved_coupons, daily_coupon_publications ve diğer
JSON payload içindeki sürüm/maç bağlantıları ayrıca çıkarılmalı. Bilinmeyen
bağımlılık varsa aday kapsamı daraltılır, temizlik engellenir. Canlı FK'lerin
GitHub kaydına işaret etmesi mümkün değildir; referans veren canlı kayıt varsa
ilgili board/match PostgreSQL'de kalır.

Canlı board_editions üzerinde immutable_history UPDATE/DELETE koruması
doğrulandı. `src/prediction_integrity.py` RAISE EXCEPTION kullanıyor.
Eski kurtarma commit'indeki doğrudan DELETE bu korumayla uyumsuzdur;
tetikleyici atlatmak çözüm değildir. Önceki hatanın SQLSTATE'i bu ölçümde
yakalanmadı; salt okunur analiz sırasında hatayı üretmek için DELETE denenmedi.

## Temizlikten önce zorunlu kanıtlar

1. Aynı tutarlı snapshot'tan tam kurtarma dump'ı, şema/sürüm/envanter,
   tüm tablo kesin sayıları ve satır içerik SHA-256 çoklu-küme özetleri;
   sequence ve large-object içerik/metadata kapsamı. Disk dolu kaynakta
   geçici tablo/sıralama dosyası üretmeden istemciye akış, yerel şifreleme.
2. Orijinal şifreli ZIP değişmez. Yeni benzersiz özel GitHub Releases arşivi;
   AES-GCM doğrulaması, parça ve toplam SHA-256; anahtar GitHub'a yüklenmez.
3. GitHub'dan tekrar indirme, şifre çözme ve bağımsız izole test ortamında
   restore; tablo, satır, içerik, FK, sequence ve kanıt revizyonları eşitliği.
   Bu test ortamı mevcut üretim veritabanının yerine geçmez.
4. Tarih/match_id/board_version ile sürümlü arşiv indeksi; sunucu tarafında
   özel GitHub erişimi, boyut sınırı, checksum, kimlik/şema doğrulaması ve
   sınırlı önbellek. Token/anahtar tarayıcıya verilmez. Ağ veya bozuk arşiv
   durumunda açık 'geçmiş geçici olarak kullanılamıyor'; sahte boş sonuç yok.
5. PostgreSQL'deki geçmiş kayıt testte gizlendiğinde gerçek uygulama geçmiş,
   maç, oran, tahmin, kupon ve sonuç sayfalarının GitHub'dan indirilmiş
   verilerle aynı kimlik, sayı ve içerikleri verdiği doğrulanır. Karışık
   sıcak/soğuk sayfalama, tekrar kayıt, revizyon, eksik parça ve kesinti testleri.
6. Manifest kimliği, ölçüm snapshot'ı, downloader/reader sürümü ve doğrulama
   raporu birbirine bağlanır. Eski veya eksik kanıt temizlik izni değildir.

Bu PR'da bu kapıların hiçbiri tamamlandı diye işaretlenmez. master'daki
`src/archive/schema.py` owned-audit-v2 yalnız seçilmiş denetim kayıtlarını
taşır; tam maç/oran/sonuç arşivi değildir. `restore.py` izole SQLite denetim
kataloğu açar; uygulama geçmişi için yeterli değildir. Tam tarihsel GitHub
okuma adaptörü ayrı uygulama PR'ı gerektirir; bu plan onu etkinleştirmez.

## Dolu disk ve mevcut veritabanını koruma

Doğrudan VACUUM FULL yok: yeni tablo kopyası için ek alan ve özel kilit ister.
CLUSTER, tablo yeniden yazımı, pg_repack ve indeks yeniden oluşturma da boş
alan/WAL bütçesi olmadan kaçış yolu değildir. Salt okunur sorgu timeout'u
olursa yeniden tekrar zorlamak yerine durulur.

İleride ayrı onayla önce büyüme kaynağı durdurulur; arşiv doğrulanınca küçük
işlemlerle yalnız uygun veriler ele alınır. Normal VACUUM alanı çoğunlukla
veritabanının yeniden kullanımına açar; işletim sistemine geri vermeyi garanti
etmez. Boş alan yetersizse aynı instance için geçici kapasite artışı ve bakım
penceresi değerlendirilir; otomatik uygulanmaz. Mevcut instance korunur.
Uzun snapshot'lar ve replication/WAL tutulması ayrıca incelenmelidir.

Kaynak: https://www.postgresql.org/docs/current/routine-vacuuming.html

## Doğrulama ve teslim kapsamı

`python -m pytest -q tests/test_retention_plan.py` tarih sınırlarını,
belirsiz/gelecek maç korumasını, ölçüm bileşenlerini ve kazanç garantisi
verilmediğini sınar. CI ayrıca mevcut tüm testleri çalıştırır.
Ölçümler gerçek salt okunur bağlantıdan alınmıştır; yedek/restore ve uygulama
arşivden geri okuma bu PR'da yapılmış değildir. Merge/deploy gerektirmez.
