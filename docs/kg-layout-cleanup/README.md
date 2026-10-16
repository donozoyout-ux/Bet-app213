# KG bağlantıları ve kompakt yerleşim

Başlangıç: master `f65a3c8` (#70–73 dahil). Branch: `codex/kg-layout-cleanup`.

## Davranış

- Mevcut KG modeli, market filtreleri, kupon seçim kimlikleri ve settlement kullanılıyor. Model yeniden yazılmadı; kalite eşiği düşürülmedi. Sekiz benzersiz maç sınırı ve tek seçili maç/dört ayrıntı bloğu korundu.
- Maç penceresine KG sekmesi eklendi: Var/Yok olasılıkları, gerçek son 5/10 ve ilgili saha örnekleri, gol ortalamaları, tarihli H2H ve KG oranı. Yetersiz modelde takım/saha/yarışma sayıları ve gerekli minimumlar gösterilir. Model hazırken seçim yoksa kalite eşiği nedeni ayrıca belirtilir.
- KG fiyatı ancak maç, market, seçim, çizgisiz tam maç periyodu ve 900 saniyelik doğrulanmış kanıt eşleşirse gösterilir. 1X2 veya toplam gol fiyatı KG yerine kullanılmaz. Mevcut kaynak KG fiyatı sağlamadığından fiyat üretilmedi.
- Masaüstü içerik genişletildi, hero ve analiz kartları küçültüldü. Mobil tek sütun, kaydırılabilir sekmeler/şerit ve sidebar korundu. Kaynak takım logosu olmadığı için takım isimleri ve baş harflerinden metin fallback kullanılır.
- Analiz API 504, eski/eşleşmeyen kanıt ve bağlantı hatası ayrı mesajlar gösterir. Eski detayın geç yanıttan geri gelmesini engelleyen generation/abort denetimleri korunur.
- Performans: Toplam, Sonuçlanan, Kazandı, Kaybetti, Bekliyor, Void ve Başarı ayrı. Bekliyor = maç bekleyen + sonuç verisi bekleyen. Toplam = Kazandı + Kaybetti + Bekliyor + Void; Sonuçlanan = Kazandı + Kaybetti. Başarı = Kazandı / Sonuçlanan; void/pending paydaya girmez. Aynı maç/market/seçim tekrarları raporda tek sayılır, ilk kayıt esas alınır. Tahmin hafızası değiştirilmez.
- Derslerde tekrarlanan hazır olmayan AI kartları yerine tek özet açıklama var. Hazır kayıtlı anlatım ve kanıt/örneklem uyarıları korunur. KG kapsamı mevcut rapor kayıtlarını kullanır; boş KG geçmişinden ders üretilmez.

## Salt okunur canlı veri teşhisi — 16 Ekim 2026

Deniz's workspace üzerinde Render PostgreSQL aracı salt okunur transaction kullandı. Ham sorgu planları ve sonuçlar [postgres-evidence.json](postgres-evidence.json) içinde.

- `market_odds`: 5.704 korner/tam maç ve 11.072 ilk yarı gol/ilk yarı satırı. KG satırı yok. Mevcut tablo over/under sütunları taşır; yes/no KG fiyatının bu sütunlardan tahmin edilmesi güvenli değildir. Kaynak/scraper onarımı bu PR kapsamına alınmadı.
- Güncel öğrenme raporunda 65 ders/gözlem kaydı var; KG kaydı yok ([kanıt](learning-evidence.json)). Bu nedenle Dersler ekranında KG sonucu zorla oluşturulmaz.
- Kaydedilmiş 286 seçim, 286 benzersiz maç/market/seçim: canlı kayıtta tekrar yok. KG snapshot seçimi yok. En son kaydedilmiş kararlar: 100 won, 30 lost, 1 void. Bu karar tablosu sayısı tek başına toplam kayıt sayısı değildir; bekleyen/veri bekleyen ve henüz dondurulmamış değerlendirmeler ayrıca hesaplanır. Canlı `/api/market-performance` bunu tam doğruladı: **286 = 100 kazandı + 30 kaybetti + 155 bekliyor + 1 void**; sonuçlanan 130, başarı **%76,923**. [API kanıtı](api-evidence.json). KG performansı 0 kayıt dönerken `/predictions/best?market=btts&statistical_all=true` 172 istatistiksel aday döndürür; tüm adaylar ile ileriye dönük kaydedilmiş güçlü seçimler aynı küme değildir. Fark kayıp kayıt olarak yorumlanmaz.
- Pano HTTP okuyucusu yaklaşık 9.731.771 bayt tam JSON yüklerken normal ekran için gereken items + availability yalnızca 1.068.154 bayt. JSON alanlarını SQL'de projekte ederek normal tahmin isteğinde kupon bağlamı ve motor teşhisi aktarımı/deserialization kaldırıldı (~%89 daha az JSON). İstatistiksel tüm seçimler ve odds-coverage ayrıntıları gerektiğinde alınır. Kupon okuyucusu değişmedi.
- Ayrıntı cache kontrolü bütün bahis tablolarını tarıyordu. `revision(session, match_id)` yalnızca ilgili maçın dört fiyat tablosunu kontrol eder; genel maç/istatistik sürümü UEFA/lig geçmişinin invalidation güvencesini korur. Worker'ın genel sürüm denetimi değişmedi. Canlı EXPLAIN ANALYZE örneği 776,538 ms → 195,463 ms. Bunlar tekil, farklı cache/IO koşullarındaki SQL ölçümleridir; uçtan uca hız garantisi veya bütün 504'lerin kök nedeninin kanıtı değildir. Timeout artırılmadı.

## Doğrulama ve görseller

Chromium: 320, 390, 768, 1024, 1536 px; KG sekmesi, tüm sekmeler, sekiz benzersiz maç, 0/1/4/8 ekranları, geciken yanıt/stale analiz ve 504. Python/JavaScript/SQL regresyon sonuçları PR açıklamasında tamamlanır.

Görseller `test_kg_capture.py` ile gerçek, salt okunur API yanıtlarından alınır. Baseline master `f65a3c8`; aynı yakalamadaki yanıtlar before/after için tekrar kullanılır. Kaynak fiyat ve maç eklenmez. Otomatik kupon oluşturma, scraper, Telegram ve NVIDIA yolları yakalamada engellenir; bu nedenle canlı kupon paneli bir bağlantı/erişim durumu gösterebilir. Görseller backend değişikliğinin deploy edildiği anlamına gelmez.

- [Önce masaüstü](before-1536.png) / [Sonra masaüstü](after-1536.png)
- [Önce mobil](before-390.png) / [Sonra mobil](after-390.png)
- [Önce ana sayfa](before-home-1536.png) / [Sonra ana sayfa](after-home-1536.png)
- [Mobil ana sayfa](after-home-390.png)
- [KG masaüstü](after-detail-1536.png) / [KG mobil](after-detail-390.png)
- [Yakalama kaynakları ve durum kodları](capture-evidence.json)

Master'a merge, Render deploy, gerçek Telegram veya NVIDIA isteği yapılmadı.
