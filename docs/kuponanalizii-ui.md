# Kuponanalizii arayüzü

BetApp213 uygulamasının kullanıcıya görünen markası Kuponanalizii'dir. Teknik
servis kimliği, API yolları/yanıtları, tarayıcıdaki mevcut taslak ve sahiplik
anahtarları aynı kalır. Güncel master `c5e3ac42` temel alınmıştır.

## Görünüm ve gezinme

Kullanıcının sağladığı şeffaf PNG `src/api/assets/kuponanalizii-logo.png` içinde
aynen korunur. Header ve hero amblemi CSS ile çerçevelenir; aynı dosya favicon
ve Apple touch icon olarak kullanılır. Sabit dosya yolu bir günlük tarayıcı
önbelleğiyle servis edilir. Harici font, görsel sunucusu, framework veya yeni
bağımlılık yoktur. Lacivert/cyan tema, mevcut bileşenlerin yerleşimi ve
erişilebilirlik sözleşmeleri üzerinde çalışır. Önceki birikmiş yeşil tema
katmanları kaldırılmıştır.

Ana Sayfa, Canlı Skor (mevcut canlı/bugün/yaklaşan/geçmiş sekmeleri), Kupon
Analizi, Tahminler, İstatistikler ve Performans aynı hash yollarını kullanır.
Sayfa başlıkları gezinmeyle güncellenir; tarayıcı geri/ileri, klavye ile maç
analizi, filtreler, global arama ve mobil alt menü korunur. Maç tablosundaki
kaynak düğmeleri mevcut Crown/Bet365/Sbobet davranışını kullanır. Canlı işareti
yalnız API'nin canlı statüleri için gösterilir. Ekranda AI, Premium, hesap,
uydurma kazanç veya statik maç/oran verisi bulunmaz.

## Kupon ve Telegram

Ana sayfadaki kupon bağlantısı mevcut otomatik kupon akışını açar. Hedef
varsayılan dört, strateji dengeli; desteklenen hedef aralığı 1–4'tür. Günlük
kuponun API'deki tarihi ayrı gösterilir. Otomatik kupon, manuel taslak, geçmiş,
oluşturma/kaydetme/kopyalama ve paylaşım kontrolleri korunur. Kupon oranı API'nin
doğruladığı seçim fiyatlarından gelir; toplam oran üst sınırı yoktur.

%70 olasılık, %80 güvenilirlik, %80 kalite, farklı maç zorunluluğu ve 900 saniye
fiyat tazeliği değişmez. Yetersiz fiyat/analiz durumu gerçek ret açıklamalarıyla
gösterilir. Veri üretimi, scraper/worker, veritabanı şeması, Telegram gönderim
kodu, 12:00/18:00 programı, günlük iki otomatik gönderim sınırı ve tekrar
engelleme değiştirilmemiştir. Yönetim erişim anahtarı girişinin tipi password
olarak kalır; bot tokenı veya grup kimliği istemciye eklenmez. Bu çalışma gerçek
Telegram mesajı göndermez, workflow tetiklemez ve Render'a deploy yapmaz.

## Doğrulama

Mevcut Python/Chromium ve dört JavaScript runtime paketi çalıştırılır.
`tests/test_kuponanalizii_browser.py` ayrıca 1536, 1024, 768, 390 ve 320 px
genişliklerde gerçek FastAPI ASGI uygulamasına bağlanır. Test veritabanındaki
fixture kayıtlarıyla dört maçlık kupon oluşturma, gerçek fiyat çarpımı,
manuel düzenleme, menüler, başlıklar, logo yüklenmesi, taşma olmaması, mobil
arama ve konsol hataları kontrol edilir. Canlı skor, kaynak seçimi ve normal /
azaltılmış hareket modları ayrıca test edilir. Fixture ekran görüntüleri
üretim ölçümü değildir; isteğe bağlı `UI_SCREENSHOT_DIR` ile dışarı alınabilir.

```sh
python -m pytest -q -rP
node tests/dashboard_runtime.cjs
node tests/predictions_runtime.cjs
node tests/market_runtime.cjs
node tests/coupon_runtime.cjs
```

PostgreSQL'e özel testler için `TEST_POSTGRES_URL` gerekir. Mevcut GitHub Actions
işi PostgreSQL 16 servisini ve Chromium'u hazırlar. Testlerde gerçek Telegram
anahtarları temizlenir ve gönderimler MockTransport ile sınanır.
