# BetApp213 kritik düzeltme denetimi — 11 Ekim 2026

## Tabana ve üretime ilişkin doğrulama

Çalışma `origin/master` 0c7e8de üzerinden ayrı `codex/critical-data-coupon-fixes` dalındadır. PR #28 sorgulandığında zaten birleştirilmişti; bu çalışmada merge yapılmadı. Açık kaynak denetimi PR #16 bulundu. Render master commitlerini otomatik dağıtıyor; bu nedenle master'a push veya merge yapılmaz. Dağıtım kullanıcı onayını bekler.

## İki başarısız testin kök nedenleri

Actions 38168620032 kaydı ve yerel tekrar aynı iki hatayı gösterdi:

- `test_daily_balanced_four_and_no_forced_filler`: eski test girdileri .70–.75 kalite/güvenilirlik puanıyla dört seçim bekliyordu; üretim .80 şartını uyguluyordu. Üretim eşiği korunur. Testin sıralama girdileri .80–.85 yapılır; .79 kalite, .69 olasılık, NaN ve geçersiz fiyatların reddedildiği yeni doğrulamalar eklenir. Eşik düşürülmez ve test atlanmaz.
- `test_chromium_coupon_add_copy_clear_and_settlement`: arayüz her maçın iki alternatifini tek satıra indirerek dört adaydan ikisini gizliyordu. İki güçlü alternatif tekrar gösterilir; seçim kontrolü aynı maçtan yalnızca birinin kupona alınmasını sürdürür. Gerçek fiyatlı seçenekler üstte, kendi gruplarında tarihe göre sıralıdır; fiyatı doğrulanmamış olanlar varsayılan olarak gizlenir.

## Veri zincirindeki düzeltmeler

- Tarihsel backfill, 15 dakikalık güncel fiyat şartını tek başına karşılamıyordu. Advisory lock sahibi mevcut worker'ın öncelikli döngüsüne yaklaşan maçların ilgili gerçek marketlerini yenileyen görev eklendi. Tam maç 1X2/AH/OU, ilk yarı OU ve korner kaynakları bağımsız toplanır. Başarılı kaynaklar beş dakika sonra, hatalı kaynaklar kalıcı artan beklemeyle yeniden kontrol edilir. Skor kaynağının hatası oran görevini durdurmaz. HTTP isteklerinde scraper çalıştırılmaz.
- Tahmin seçimi oran eşleştirmesinden önce yapılıyordu. Aynı marketteki güçlü, tam eşleşen ve güncel fiyatlı alternatif dışarıda kalabiliyordu. Şimdi bütün model adayları eşleştirilir; kalite ve bağımsızlık kontrollerinden geçen güncel fiyatlı adaylar öncelik kazanır. En fazla iki bağımsız tahmin kuralı korunur. Cache model sürümü değiştirilir; eski pano yenilenmeden kullanılamaz.
- Market/çizgi/periyot/bookmaker eşleşmesi, alan bazlı gözlem zamanı ve açılış/güncel/kapanış ayrımı korunur. Eksik fiyat için `no_source_rows`, `line_mismatch`, `source_market_unavailable`, `missing_stage_price`, `observed_after_cutoff`, `unsupported_bookmaker`; mevcut fiyat için bayat/bilinmeyen durumları ölçülür. `/api/odds-coverage` gerçek veritabanı fiyatlarını yeniden eşleştirerek raporlar.
- Günlük otomatik kupon yalnızca Türkiye takvim gününden, başlamamış maçlardan, en fazla dört ayrı maçtan oluşur. %70 olasılık, %80 güvenilirlik, %80 kalite ve 15 dakikalık tam fiyat şartları korunur. İlk fiyatlı aday katı otomatik eşiğe takılırsa aynı maçın ikinci güçlü fiyatlı adayı değerlendirilir. Boş sonuçta analiz, gün, kalite veya fiyat sebebi ayrı belirtilir. Yedi günlük manuel liste korunur.
- Manuel taslak ve kaydedilmiş sürüm tarayıcıda kalıcı tutulur; sayfa yenilemesi kullanıcı düzenlemesini kaybettirmez. Önceki güne ait değiştirilmemiş otomatik kupon yeni günde yeniden oluşturulur. Ağ isteği sürerken taslak temizleme/düzenleme eski yanıtın yeni seçimleri ezmesini engeller.
- Telegram teslimat rezervasyonu ağ isteğinden önce commit edilmeye devam eder. Yeni gözlem zamanı/fiyat nedeniyle yeni kupon sürümü oluşması aynı seçimleri ikinci kez gönderme yetkisi vermez. Aynı gün daha önce denenmiş aynı maç/tahmin kümesi, belirsiz teslimat dahil, tekrar gönderilmez. Farklı ve yeniden doğrulanmış kupon akşam penceresinde gönderilebilir. Günde en fazla iki otomatik rezervasyon, boş kupon yasağı ve 12:00/18:00 Türkiye saatleri korunur.

## Gerçek oran kapsaması

Üretimden salt okunur alınmış kayıtlar yerel yeni eşleştiriciyle 11 Ekim 22:55 Türkiye saatinde değerlendirildi. Bu, dağıtım sonrası sonuç değildir.

- Korner: 42 tahmin; 38'inde kaynak satırı yok, 4'ünde çizgi farklı.
- İlk yarı gol: 15 tahmin; 15'inde çizgi farklı.
- Tam maç gol: 3 tahmin; 3'ünde çizgi farklı.
- 1X2: 2 tahmin; 2 gerçek fiyat eşleşiyor, ikisi de bayat.
- İkinci yarı gol: 30 tahmin; doğrulanmış kaynak marketi yok.
- Sarı kart: 10 tahmin; doğrulanmış kaynak marketi yok.

Toplam 102 seçimde eşleşen fiyat 2 (%1.96), güncel kullanılabilir fiyat 0. O saatte günün başlamamış adayları kalmamıştı. Yeni kod dağıtılmadan kapsama artışı iddia edilmez.

Kaynak kontrolü: yaklaşan Goaloo maç 2993805 için ilk yarı çizgisi 1.25, Bet365/Sbobet/Crown fiyatları gerçekten alındı; Bet365 korner çizgisi 8.5 ve fiyatları gerçekten alındı. 1.25 fiyatı 1.5 tahminine, 8.5 fiyatı 9.5 tahminine aktarılmaz. İlk yarı çeyrek çizgilerin model tarafından desteklenmediği ve Goaloo'nun doğrulanmış ikinci yarı/kart endpointi bulunmadığı durumlar açık kalır. Kornerde Sbobet fiyatı uydurulmaz.

## Önceden kaydedilmiş gerçek tahminlerin değerlendirmesi

Üretimdeki 102 değişmez maç öncesi snapshot, 222 seçim içeriyordu. Yalnızca `captured_at < kickoff_at` kayıtları alınır; tahmin ve kalite değerleri yeniden hesaplanmaz. Sonuç için yayınlanmış maç/final istatistikleri kullanılır. Bildirilen 12 tahminin kimlikleri verilmediği için bu 12'li gruba özel sonuç çıkarılamaz.

- 1X2: 13 sonuç; 9 kazanma, 4 kaybetme; %69.23; Brier 0.2112.
- Tam maç gol: 43 sonuç; 38 kazanma, 5 kaybetme; %88.37; Brier 0.1105.
- Çifte şans: 28 sonuç; 21 kazanma, 7 kaybetme; %75; Brier 0.1879.
- Asya handikapı: 2 sonuç; 1 kazanma, 1 kaybetme; %50; Brier 0.2272.
- Korner: 5 sonuç; 4 kazanma, 1 kaybetme; %80; Brier 0.1916. Örneklem küçüktür.
- Sarı kart: 3 sonuç, 3 kazanma; Brier 0.0182. Örneklem küçüktür.
- İlk ve ikinci yarı gollerde henüz sonuçlanmış kayıt yok.

Eşik karşılaştırması: bütün kayıtlar 76/94 (%80.85, Brier 0.1513); yalnız .70 olasılık 70/84 (%83.33, 0.1409); yalnız .80 güvenilirlik 76/93 (%81.72, 0.1444); yalnız .80 kalite 73/88 (%82.95, 0.1387); üçü birlikte 67/78 (%85.90, 0.1259). Bu gözlemsel, küçük ve seçilmiş örneklem eşiklerin gelecekte çalışacağını kanıtlamaz. Market bazlı örneklem ve olasılık kalibrasyon dilimleri JSON ölçüm dosyasındadır. Farklı/eski model sürümleri bir aradadır.

Sonuçlanmış seçimlerde kaydedilmiş, o anda güncel gerçek fiyat örneklemi 0; ROI hesaplanamaz. Bugünkü fiyatlar geçmiş tahminlere eklenmez. API Brier, olasılık dilimleri, örneklem ve yalnızca kaydedilmiş güncel maç öncesi fiyatlardan ROI alanlarını döndürür. Başarı veya kazanç garantisi verilmez.

## Telegram ve canlı sistem

Salt okunur kontrolde `/health`, tahmin API, kupon geçmişi ve Telegram durum API HTTP 200 döndürdü. Bot yapılandırması mevcut; bu bilgi grup iznini kanıtlamaz. İki pencere `no_eligible_selections` durumunda, teslimat tablosu boş. Loglarda gönderim öncesi scheduler `TimeoutError` görüldü; teslimat rezervasyonu veya başarılı mesaj kaydı olmadığı için bunu Telegram'a ulaşmış mesaj diye raporlamıyoruz. Günlük üretim artık tüm yedi günün oranlarını sorgulamaz; bugünün adaylarını okur. Gerçek Telegram mesajı gönderilmedi. Tüm ağ gönderim testleri MockTransport kullanır.

## Doğrulama ve dağıtım

Python/Chromium, gerçek PostgreSQL 16 ve JavaScript senaryoları çalıştırılır. Testlerin güncel sonucu, commit SHA ve PR bağlantısı son yanıtta verilir. Yerel PostgreSQL kurulumunda [EDB taşınabilir resmi ikilileri](https://productsdl.enterprisedb.com/download-postgresql-binaries) kullanılır; test verisi üretimden ayrıdır. Windows'ta Gunicorn'un mevcut POSIX testleri Linux CI tarafından doğrulanır. Üretim merge veya dağıtımı yapılmaz; önce başarılı CI ve kullanıcı dağıtım onayı gerekir. Onay sonrası `/health`, tahmin, kupon ve Telegram durumu yeniden doğrulanmalıdır.
