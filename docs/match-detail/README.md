# Bağımsız maç analizi

PR #141, üst menü PR #140 üzerine kuruludur. Maçlar `/match/{internal_match_id}` kalıcı adresinde açılır. Ana Sayfa, Canlı Skor, Tahminler, İstatistikler ve Kuponum bağlantıları yerel HTML bağlantılarıdır: `target="_blank" rel="noopener noreferrer"`. Tarayıcının klavye, bağlam menüsü ve bağlantı kopyalama davranışları korunur.

## Veri sözleşmesi

- `GET /api/matches/{id}/dossier`: maç bilgileri, kaynak fiyatları, son 5/10 özetleri, saha ayrımları, H2H ve dondurulmuş tahmin kayıtları. Hiçbir model, veri toplama veya sonuçlandırma işi başlatmaz.
- `GET /api/matches/{id}/history?scope=home&offset=0&limit=10`: kapsam `home`, `away`, `home_venue`, `away_venue`, `h2h`; limit 1–20, offset 0–1000. SQL sorgusu `limit+1` kayıt okur; toplam sayım yapmaz. Kronolojik sıra tarih ve ID ile sabittir.
- PostgreSQL üzerinde 20 saniyelik, 128 anahtarla sınırlı ve aynı anahtarda eşzamanlı hesaplamayı birleştiren mevcut ReadCache kullanılır. SQLite testleri yazıları hemen görür.
- Geçmiş yalnızca aktif veritabanından okunur. CSV arşivi ve hibrit geçmiş ayarı değiştirilmez. Kapsam sınırı sayfada açıkça yazılır; boş geçmiş uydurulmaz.
- Fiyat grafiklerinin sütunları kayıtlı açılış/son/kapanış gözlemleridir. Kesintisiz zaman serisi veya aradaki fiyatlar üretilmez. Alan bazlı gözlem zamanı olmayan fiyat grafik dışı bırakılır ve tabloda doğrulanmamış işaretlenir. Son fiyat kapanış yerine kullanılmaz. Güncellik, oynanabilir fiyat doğrulaması olarak sunulmaz.
- Kart/korner ortalamaları yalnızca kesinleşmiş gözlemlerden, metrik başına ayrı örneklemle hesaplanır. Eksikler sıfır değildir. Gol geçmişi maç tarihine göre kesilir; mevcut kaynak kayıtlarının betimleyici özeti, tarihsel model girdisi iddiası değildir.
- Tahminler yalnızca `PredictionSnapshot` içindeki, başlama saatinden önce kaydedilmiş seçimlerdir. Eksik marketler açıkça belirtilir. Bitmiş maçlara yeni tahmin eklenmez. Sonuç ancak mevcut skor/istatistik ve değişmez sonuçlandırma kaydı birlikte doğrulanırsa kesin gösterilir; uyuşmazlık ayrıca işaretlenir.
- Eski modal ve yerinde analiz çağrıları geçmiş URL uyumluluğu için korunur. Bunların regresyon testleri programatik çağrılarını sınar; yeni bağlantı davranışı ayrı Chromium testleriyle sınanır.

## Görsel karşılaştırma

Tüm görüntüler izole test veritabanından alınmıştır; üretim verisi veya gerçek tahmin örneği değildir.

| Görünüm | 390px | 1536px |
| --- | --- | --- |
| Önce: aynı sayfada modal (PR #140) | [Mobil](../top-navigation/screenshots/after-dialog-390.png) | [Masaüstü](../top-navigation/screenshots/after-dialog-1536.png) |
| Sonra: bağımsız genel bakış | [Mobil](screenshots/overview-390.png) | [Masaüstü](screenshots/overview-1536.png) |
| Oranlar | [Mobil](screenshots/odds-390.png) | [Masaüstü](screenshots/odds-1536.png) |
| Takımlar / H2H | [Mobil](screenshots/teams-390.png) | [Masaüstü](screenshots/teams-1536.png) |
| Korner / kart | [Mobil](screenshots/counts-390.png) | [Masaüstü](screenshots/counts-1536.png) |
| Kayıtlı tahminler | [Mobil](screenshots/predictions-390.png) | [Masaüstü](screenshots/predictions-1536.png) |

Tekrar üretim: `MATCH_SCREENSHOT_DIR=docs/match-detail/screenshots python -m pytest tests/test_match_page.py -q` (PowerShell'de ortam değişkenini `$env:MATCH_SCREENSHOT_DIR` ile ayarlayın).

Testler gerçek FastAPI/SQLite yanıtlarını Chromium'a yönlendirir; dış ağa izin vermez. Yeni sekme, `window.opener`, beş giriş bölümü, beş analiz sekmesi, doğrudan URL/yenileme, klavye, azaltılmış hareket, yatay taşma, sayfalama ve kayıt kanıtı kontrollerini içerir. Tam CI sonucu PR açıklamasında raporlanır. Merge veya Render deploy yapılmaz.
