# V4 sistem onarımı — inceleme raporu

## Entegrasyon ve güvenlik

#77 ve #78 master'da. #80 (`30ae420`) master'a taşınmış kalıcı yayın çözümüdür; kendi CI koşusu [#477 başarılıdır](https://github.com/donozoyout-ux/Bet-app213/actions/runs/38913603147). Bu çalışma #80 üzerine ayrı branch/PR olarak kurulur: kalıcı tabloları, revision algoritmasını, günlük kuponu veya #77 fiyat filtresini yeniden yazmaz. Ayrı PR gerekçesi V4'ün yeni ortak arayüz ve teşhis/performance kapsamıdır. #80 önce değerlendirilmelidir.

Üretimde yalnızca GET ve salt okunur SQL kullanıldı. Deploy, merge, Telegram/NVIDIA isteği, üretimde EXPLAIN ANALYZE veya veri onarımı yapılmadı. Fiyat kaynakları, 900 saniye, kupon %70/%80/%80 tabanı, bağımsızlık ve model eşikleri değişmedi.

## Doğrulanmış canlı bulgular — 16 Ekim 2026, yaklaşık 14:28–14:35 UTC

* `/api/predictions?limit=8`: 504 / 5.110,3 ms; `/api/predictions/best?limit=20`: 504 / 4.995,6 ms. İki istekte hata oranı %100; bu küçük örneklem genel trafik hata oranı değildir. HTTP'de gösterilebilir maç: 0. [Ham, gizli değer içermeyen ölçüm](live-http-before.json).
* PostgreSQL'de 11 etkin ligin panoları dolu; her biri yaklaşık 16–179 KB. 122 yaklaşan planlı maç, o anda taze panolarda 67 kaliteyi geçen maç / 132 seçim vardı. Bunlar farklı zamanlarda alınan hareketli görüntülerdir; tek atomik census olarak toplanamaz.
* Bugün yalnızca 6 yaklaşan planlı maç vardı. Bugünkü kayıtlı adaylar içinde kaliteyi geçenler: korner 4, çifte şans 3, ilk yarı gol 3, gol 2, ikinci yarı gol 3, sarı kart 2. Aynı maçta birden çok seçim olabilir; toplanarak kupon boyutu çıkarılamaz.
* 1X2 kaynak satırları 366; **alanın kendi** `latest_home` gözlem zamanıyla kontrol edilen 153 taze fiyat / 54 maç bulundu. Bu sayı korner/gol çizgisiyle eşleşen uygun kupon seçim sayısı değildir. Genel `updated_at` tek başına fiyat tazeliği kanıtı değildir. BTTS/çifte şans gibi kaynak fiyatı desteklenmeyen marketler oran üretilmeden istatistik olarak kalır.
* Sonraki salt okunur, bugünkü kaliteli adayların maç/market/periyot/çizgi karşılaştırmasında **17 seçimden hiçbirinin güncel kaynak çizgisi eşleşmedi**: korner 4/0, ilk yarı gol 3/0, gol 2/0 (kaliteli seçim/eşleşen çizgi). Çifte şans 3, ikinci yarı gol 3 ve sarı kart 2 için mevcut doğrulanmış fiyat adaptörü yok. Bu sonuçta son fiyatın alan çizgisi ve zamanı daha da daraltmadan önce bile uygun aday sayısı sıfırdır. Taze 1X2 satırlarını bu marketlere aktarmak hatalı olur; kaynak çizgileri uydurularak kupon doldurulmadı. Bu bulgu bugünkü `no_eligible_selections` durumunu açıklıyor; kaynakta doğru çizgi belirdiğinde mevcut worker yeniden değerlendirecek.
* Canlı şemada #80'in `board_editions`, `board_heads`, `daily_coupon_publications` tabloları yok ve `/api/coupons/daily` **404**. Dolayısıyla #80'in CI'ı geçmesi üretimde günlük yayının kurulduğu anlamına gelmez. Bu PR deploy etmez; birleştirme/yayın kararı sonrasında normal uygulama şema başlangıcı ve worker ilk yayını ayrıca doğrulanmalıdır.
* Bağlantı örnekleminde 5 idle, 1 idle-in-transaction ve inceleme bağlantısının 1 active oturumu görüldü; lock wait yakalanmadı. Bu tek görüntü aralıklı kilit/havuz sorunlarını dışlamaz. Uygulama web havuzu 5, scraper havuzu 2 bağlantıyla ve overflow=0 ile sınırlı; havuz boyutu veya timeout artırılmadı.
* Canlı loglarda SELECT 700,9 / 789,5 / 803,0 ms görüldü; eski logger sadece `SELECT` yazdığı için kesin SQL veya kilit kök nedeni bunlardan atanamaz. #80'in faz/sorgu hash telemetrisi henüz deploy edilmemiştir. Kaynakta HTTP okuyucusunun JSON sütunundan ayrı ayrı alanlar çıkardığı doğrulandı; tekrarlayan ziyaretçi okumasını kaldıran değişiklik test PostgreSQL'de ölçülür. Üretim 504'lerinin tamamen çözüldüğü iddia edilmez.
* `pg_extension` salt okunur kontrolünde `pg_stat_statements` kurulu değil; geçmiş SQL sürelerini bu uzantıdan doğrulama olanağı yok. Üretime uzantı kurulmadı veya ayar yazılmadı. Yayın sonrası faz/sorgu hash logları ile gerçek sorgu, havuz beklemesi ve CPU/JSON çözümleme süreleri ayrılmalıdır.

## Değişiklikler

1. **Model JSON okuması:** küçük pano metadata'sı ve maç durumları her GET'te yeniden okunur. JSON, yayın kimliğiyle sınırlı 4 girişlik process cache üzerinden tek sefer yüklenir; eşzamanlı okuyucular tek yüklemeyi paylaşır. Pano JSON'u bir kez çözülür; aynı yayında tekrar TOAST/JSON okuması yapılmaz. Cache fiyat veya uygunluk kararı saklamaz. Soğuk okumada metadata/payload arasındaki yayın yarışında eşleşmeyen içerik sunulmaz. Yeni yayın/cache kaybı DB'den tekrar okunur. Süre dolunca eski yayına fallback yapılmaz.
2. **Görünür tahmin:** ana sayfanın `priced_only=true` filtresi kaldırıldı. Kaynak fiyatı eksik ama geçerli istatistiksel tahmin artık kaybolmaz. Kupona uygunluk ve fiyat ayrı doğrulanır. Planlı, istatistiksel, kupon kalite tabanını geçen ve doğrulanmış oranlı maç sayıları API metadata'sında ayrıdır; arayüzde planlı/kaliteli/oranlı sayılar görünür. Sayılar gösterim limiti uygulanmadan hesaplanır.
3. **Kupon teşhisi:** mevcut maç bazlı kalite/analiz teşhisine seçim bazlı fiyat ret nedenlerinin toplamları eklendi; aynı seçim birden fazla nedenle sayılmaz. Sayılar maç toplamına eklenmez. Ana sayfada incelenen, kaliteli ve taze eşleşen fiyatlı maç sayıları görünür. Günlük kalıcılık, arşiv, GET okuması ve idempotency #80'den korunur.
4. **Dersler:** 100 etkin bağımsız maç eşdeğeri tabanı değişmeden görünür ilerleme eklendi. Eşik tek başına güçlü kanıt sayılmaz; örneklem ve güven aralığı koşulları korunur. AI zorunlu değildir.
5. **Ortak mavi tema:** lacivert `#071426`, panel `#10243D`, vurgu `#2684FF`, camgöbeği `#35C7FF`, metin `#F3F8FF`, sınır `#274665`. Eski yeşil dekorasyonlar ortak CSS'te değiştirildi; başarı yeşili/hata kırmızısı korunur. Fontlar büyütüldü, boş paneller ve hero daraltıldı; gereksiz animasyonlar kaldırıldı. Sidebar, mobil gezinme, manuel kupon ve analiz etkileşimleri korunur.

## Telegram ve öğrenme teşhisi

`/api/telegram/status` 200: yapılandırma mevcut ve biçimsel olarak geçerli, `connection_verified=false`; gizli değerler okunmadı veya kaydedilmedi. Bugünkü 12:00 slotu `no_eligible_selections`, 18:00 henüz zamanı değil. Dolayısıyla scheduler çalışıyor ve aday yokluğunda mesaj üretmiyor. Ledger'da geçmişten 1 sent, 1 failed (`telegram_400`) var; genel eski hata kodundan chat/bot kök nedeni çıkarılamaz. Gerçek bağlantı isteği yapılmadığından bot yetkisi/hedef erişimi kesinleştirilmedi. 12:00/18:00, iki otomatik slot, durable claim ve aynı seçimleri tekrar göndermeme politikası değiştirilmedi. Mock transport regresyonları çalıştırılır; gerçek gönderim ayrı kullanıcı onayı gerektirir.

Son öğrenme raporu #8: 286 kayıt = 155 maç bekleyen + 131 terminal; terminal 131 = 130 geçerli sonuç + 1 void. Sonuç verisi bekleyen 0. 130 geçerli sonuçtan 100 kazanmış; payda 130'dur. Pazar bazında etkin maçlar: gol 54, çifte şans 33, sonuç 14, ikinci yarı gol 9, korner 7, ilk yarı gol 6, sarı kart 5, handikap 2. Hiçbiri mevcut 100 tabanına ulaşmıyor; “örneklem yetersiz” bu kayıtlarda doğrudur. KG için sonuçlanmış kayıt yoksa ders uydurulmaz. Aynı içeriğin yeni rapor üretmemesi nedeniyle rapor oluşturulma tarihi güncel kaynak kontrolünden eskidir; `learning_heads.source_observed_at` ayrıca gösterilir. Öğrenme loop'u ilk 30 saniyeden sonra 900 saniye aralıkla kontrol eder.

## Kanıtlar ve ölçüm sınırı

Yeni `test_v4_postgres.py`: gerçek PostgreSQL'de kalıcı DB içeriği HTTP'den okunur; 8 eşzamanlı okuyucu tek model JSON okumasını paylaşır; sıcak okumada JSON sorgusu yoktur. Canlıya alınmış maç ve dolmuş analiz cache'e rağmen dışlanır. EXPLAIN ANALYZE yalnızca rastgele adlı izole test şemasında çalışır. `V4_PUBLICATION_BENCHMARK` CI logunda soğuk/sıcak süre, SQL sayısı, HTTP durum/maç sayısı ve planı verir. Bunlar test fixture'larıdır; canlı veri veya üretim hızlanması gibi sunulmaz. #80'in günlük kupon benchmark'ı da korunur (`PUBLICATION_BENCHMARK`).

Chromium 320/390/768/1024/1536 px bütün sekmeleri, mavi renkleri, yatay taşmayı ve API sayımlarını kontrol eder. Önce/sonra görselleri **izole test DB'siyle**, aynı fixture ve API kullanılarak alınmıştır; önceki statik arayüz #80 commitinden, sonraki arayüz bu branch'tendir. Gösterim amaçlı üretim verisi eklenmedi.

| Ekran | Mobil önce / sonra | Masaüstü önce / sonra |
|---|---|---|
| Ana Sayfa | [Önce](screenshots/before-home-390.png) · [Sonra](screenshots/after-home-390.png) | [Önce](screenshots/before-home-1536.png) · [Sonra](screenshots/after-home-1536.png) |
| Tahminler | [Önce](screenshots/before-predictions-390.png) · [Sonra](screenshots/after-predictions-390.png) | [Önce](screenshots/before-predictions-1536.png) · [Sonra](screenshots/after-predictions-1536.png) |
| Kuponum | [Önce](screenshots/before-coupon-390.png) · [Sonra](screenshots/after-coupon-390.png) | [Önce](screenshots/before-coupon-1536.png) · [Sonra](screenshots/after-coupon-1536.png) |

Son CI sonuçları PR açıklamasına eklenir. Üretimde sonradan API p95/hata oranı, publication job yayını, gerçek taze kupon adayı ve bağlantı durumunun doğrulanması hâlâ yayın sonrası kontroldür.
