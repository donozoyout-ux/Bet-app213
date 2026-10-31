# Ücretsiz PostgreSQL'e güvenli dönüş — hazırlık kapıları (31 Ekim 2026)

## Sahada doğrulanan durum

- Site: https://bet-app213.onrender.com Render web service canlı, son deploy `master` PR #160. Render loglarında /health ve pek çok okuma API'si 200; fakat taze maç/tahmin kaydının çalıştığı kanıtlanmış değil. En son maç güncellemesi 30 Ekim 2026 15:21 UTC.
- Kaynak: `Kuponanaliziivv755`, Render Free PostgreSQL 18, ~766 MB uygulama verisi, kapasite tükenmiş; 26 Kasım 2026 son kullanma. **Kaynak üzerinde DELETE, VACUUM FULL, TRUNCATE, DROP, restart, plan değişikliği veya credential değişikliği yapma.**
- Güvenli kurtarma: özel `donozoyout-ux/kuponanalizii-data-archive` Release (tag `escue-vv755-20261031-e`), asset `pg-recovery-vv755-owner-rescue-20261031-e.zip` 268,638,973 byte, GitHub SHA256 `566e475ab72d3b4a6b05b88bbca9811c72f43d6bf7fea62927dff5d4fe626ae4`. Windows'ta ayrı anahtar yedeği gerekir. Yerel restore PG18'e başarılı: 37 tablo, 324,783 satır; **tam kaynak satır parity KANITLANMADI**.
- En büyük kaynak `board_editions`: 4,601 kayıt, toplam 518 MB, JSON TOAST ~517 MB. Bunlardan `board_heads` ve `board_read_publications` üzerinden referanslanan yalnızca baş/okuma FK'lerinde 15 aktif sürüm (~1.35 MB); ayrıca `prediction_publications.board_version` ve `published_match_analyses.board_version` ile korunan toplam **1.491 sürüm (~167 MB)**. Yalnız `board_editions` dışındaki ilişkiler ~248 MB. Bu *ölçüm*, gerçek yeni DB boyutu veya yazma alanının kesin garantisi değildir.
- `board_editions` FK ilişkileri: `board_heads.version` ve `board_read_publications.version`. İlk salt-okunur kontrolde yayın+analiz referansları 1.491 sürüme çıkardı; başka JSON/metin içerikli eski board sürüm referansları hâlâ mevcut olabilir; eski audit ve yayın tarihçesi arşivden okunmadan tam site parity iddia edilmez.

## Bu PR'de mevcut kod: riskli kaynak değil, izole prova

`src/archive/free_hot_cutover.py` kaynak URL kabul etmez; yalnızca kullanıcıya ait etiketlenmiş `postgres:18` Docker konteyneri, tam `127.0.0.1:5433` loopback port ve `postgres` veritabanı ispatını arar. Uzak Render/Neon adresi ve beklenmedik FK bağımlılığı varsa durur. `plan_local_hot_seed` sadece okur.

Açıkça `--apply-local` ile çalıştırıldığında **yalnızca izole prova kopyasından** aktif referansların dışında kalan edition kayıtlarını ayıklar, korunan kayıtların içerik SHA256'larını karşılaştırır ve **yalnız bu geçici kopyada** VACUUM FULL çalıştırır. Tam kaynağı, özel GitHub ZIP'i veya üretim web servisini değiştirmez. Bu bir gerçek, yeniden kurulabilir hot seed ZIP'i ve yeni DB'ye restore aracı DEĞİLDİR.

Bu operasyonun `archive_read_through_verified=false`, `eviction_authorized=false` olması bilinçlidir.

## Yeni ücretsiz PostgreSQL oluşturmadan önce kapılar

1. **Orijinalin korunması:** GitHub ZIP SHA256 ve yerel AES anahtarının *çevrimdışı* kopyası teyit edilir. ZIP tam paket olarak GitHub'da kalır. Eski kaynak DB değişmez. Bu ZIP tek başına web sitesi tarafından sorgulanabilir JSON tarihçe değildir.
2. **İzole sıcak DB provası:** kurtarma ZIP'i Windows'ta ayrı PostgreSQL 18 konteynerine açılır. Referanslı edition sürümleri ve bütün diğer tablolar korunarak yalnız kopyada daraltma yapılır; FK/cascade/başlangıç, tablo sayıları ve veri SHA256 denetlenir. Bu PR yalnız yerel daraltma çekirdeğini sağlar.
3. **Küçük seed üretimi:** izole daraltılmış DB'den `pg_dump` özel biçimde alınır; tekrar AES-GCM şifrelenir, **ayrı** bağımsız boş PostgreSQL 18'e restore edilip bütün 37 tablo ve ilgili satır SHA256'sı karşılaştırılır. Bu işlem henüz tamamlanmadı; kullanıcı yeni DB'yi henüz açmamalı.
4. **Geçmiş erişimi:** geçmişteki yayın `board_editions` kayıtlarının tamamı için **doğrulanmış, sürüm başına/partition bazlı, özel GitHub'dan sunucu taraflı, anahtarı browser'a vermeyen** read-through gerekir. Draft PR #156/#157'nin tarihçe sistemi tam/full rescue ZIP'i doğrudan sorgulamıyor. Çakışan IDs, eski tahmin/yayın kanıtı ve bütünlük testleri yapılmadan varsayılan kaynak değişmez.
5. **Sıcak yazma sınırı:** sadece bugün ve sonraki 3 gün fikstürü, gecikmiş/oynanan/sonucu bekleyen mevcut kayıtların güncellemeleri korunur. Tarihsel maçları otomatik backfill etmek kapalı; `AUTO_BACKFILL_ON_EMPTY=false`; kalite eşikleri düşmez. Ücretsiz DB için güvenli kapasite eşiği, max board jurnal boyutu, günlük sayısal sınırlar, 3 saatlik kuponun duplicate guard'ı test edilir. NVIDIA/Telegram gerçek istek gönderilmez.
6. **Günlük özel GitHub aktarımı:** ertesi gün yalnızca *tamamlanmış, kanıtı eksiksiz* kayıtlar, kayıpsız salt-okunur arşive alınır; yeniden indirip gerçek restore ve hash teyidi gerekir. Tamamlanmamış/itirazlı maçlar sıcak DB'de kalır. Cron varsayılan kapalı; sunucu kurulumundan sonra açıkça onaylanır. **Arşivleme tek başına kaynak DB'de yer açmaz.** Daha sonra silme ayrı açık onaya tabidir.
7. **Son aşama — kullanıcı oluşturur:** yukarıdaki kod ve testler ile seed ve arşiv okuması kanıtlandıktan sonra kullanıcı *yeni* bir ücretsiz Render PostgreSQL 18 açar. Ayrı ilk restore ve kıyas, siteyi yeni bağlantıyla ön izleme / Render API smoke; eski `DATABASE_URL` yedekli, rollback belgeli. Ancak bundan sonra çalışan site kesintisiz yeni DB'ye geçirilir. Eski DB silinmez.
8. **Yaşam döngüsü:** Render Free PostgreSQL'in süre sınırı ve yedekleme sınırlaması var. Yeni DB sonsuza dek kalıcı değildir. Özellikle 26 Kasım 2026'ya kadar eski DB'yi saklama riski için ek çevrimdışı/yedek planı gerekir.

## Bu aşamada kesinlikle yapılmayacaklar

- Yeni ücretli kaynak; ücretli plan; mevcut eski Free DB'nin reset/cleanup işlemi.
- Gerçek NVIDIA ya da Telegram gönderimi.
- GitHub şifreli verisinin public edilmesi veya anahtarın GitHub'a yüklenmesi.
- `master` merge, Render prod deploy, DATABASE_URL değişimi.
- `source_row_content_verified=false` iken `full parity` yazısı / eski kaynak temizliği.

Yol: **Önce kod, izole prova ve doğrulanmış yeni hot seed; en son yeni Free PostgreSQL açılışı.**