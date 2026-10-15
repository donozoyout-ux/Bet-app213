# Dersler arayüzü — Faz 3

`#lessons` mevcut hash yönlendirmesine eklenir. Masaüstü menüsü ve mobil alt menü aynı sayfayı açar; tarayıcı geri/ileri ve başlık odağı mevcut yönlendiriciyle korunur. Küçük ekranlarda alt menü yatay kaydırılabilir; yedi bağlantı da klavyeyle erişilebilir.

Arayüz yalnızca kayıtlı `/api/learning/summary`, `/coverage`, `/statistics`, `/lessons`, `/reports` yanıtlarını okur. Özetin `report_id` değeri kapsam, istatistik ve ders sayfalarına sabitlenir. Her sunucu sayfasında kimlik kontrol edilir; farklı raporlar birleştirilmez. Özet 30 saniyelik mevcut sayfa yenilemesine katılır; aynı raporun dersleri tekrar indirilmez. Yenile düğmesi veya rapor seçimi önceki isteği iptal eder.

Mevcut API kapsam/tarih filtrelerini sunmadığı için rapor kayıtları 100 kayıtlık sunucu sayfalarıyla eksiksiz alınır, tarihe göre sıralanır ve 10 kartlık arayüz sayfalarına ayrılır. Kapsam ve ilgili kayıt filtreleri bu sabit rapor üzerinde uygulanır. Brier, log loss, oranlar, güven sınırları ve kanıtlar sunucudaki kayıtlı değerlerdir; tarayıcı bunları yeniden hesaplamaz. API sayfalama sınırını aşan veya eksik kalan rapor hata durumuna geçer; kısmi veri tam rapor gibi gösterilmez. Rapor arşivi 100 kayıtlık sayfalarla genişletilir.

`insufficient_sample` kayıtları “Örneklem yetersiz · Gözlem”, `supported_candidate` kayıtları “Kanıt destekli ders adayı”, `inconclusive` kayıtları “Kesinleşmemiş gözlem” olarak sunulur. Hiçbiri otomatik model değişikliği yapmaz. Güven aralığı dersin **gerçekleşen eksi beklenen başarı farkına** aittir; başarı oranı aralığı olarak sunulmaz. Metodun ham kanıt metni de ayrıntılarda korunur.

Yükleniyor, ilk rapor hazırlanıyor (`warming`), boş filtre/rapor, API hatası, eski rapor (`stale`) ve arşiv raporu ayrı belirtilir. Başarısız yeni rapor yüklemesinde önceki metrikler temizlenir. Kullanıcı tekrar deneyebilir. API metinleri `textContent` ve yerel DOM elemanlarıyla oluşturulur; HTML yorumlanmaz.

## Doğrulama

`tests/test_learning_browser.py`, gerçek ASGI endpoint'leri ve PostgreSQL ile aynı model şemasına sahip kalıcı test raporlarını kullanır. Beş genişlik (320, 390, 768, 1024, 1536), menü/geri/ileri, klavyeyle kanıt açma, tüm kapsam filtreleri, 55 kayıt, sayfalama, XSS metni, warming/boş/hata/yeniden deneme, arşiv seçimi ve 160 kayıtlık çok sayfalı rapor test edilir. Son testte indirme sırasında yeni rapor yayımlanır; devam eden sayfalar ilk rapora sabit kalır. İstatistik motoru, PostgreSQL hesaplamaları, scraper, kupon seçimi ve Telegram kodu değiştirilmez.
