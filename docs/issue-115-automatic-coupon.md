# Issue #115 — Üç saatlik otomatik kupon

## Davranış

Türkiye saati 00/03/06/09/12/15/18/21 pencereleri kullanılır. Çalışan uygulama dakikada bir kontrol eder; mevcut GitHub uyandırma işinin zamanlaması da bu saatlere uyarlanır. Ücretsiz Render ve GitHub zamanlayıcısı tam saatinde çalışmayı garanti etmez. Uyanınca yalnızca güncel pencere değerlendirilir; kaçırılan pencereler topluca gönderilmez.

Telegram ve ekran aynı günlük yayını kullanır. Sabit dört maç sınırı kaldırılmıştır; bağımsızlık ve risk bütçesi seçim sayısını belirler. Olasılık >=0.70, güvenilirlik >=0.80, kalite >=0.80 ve 900 saniyeden genç doğrulanmış fiyat şartları korunur. Önceki günün kuponu kullanılmaz. Tek uygun seçim varsa sayı açıkça bir olarak gösterilir; zayıf seçim eklenmez.

Her pencerede en fazla bir ağ gönderimi denenir. Aynı gün aynı seçimler, yalnızca fiyat veya gözlem zamanı değişti diye tekrar gönderilmez. Gönderim yapılmış pencerede oluşan yeni seçimler sonraki üç saatlik pencerede değerlendirilir. Boş, eskimiş veya tekrar eden seçimler beş dakika sonra yeniden kontrol edilir; işlenmiş aynı sürüm sonraki pencereyi bekler. Belirsiz Telegram yanıtlarında otomatik tekrar yoktur. PostgreSQL işlem kilidi ve kalıcı teslimat kayıtları eşzamanlı işçileri korur.

Mesaj gerçek maç, başlama saati, seçim, bookmaker, fiyat/gözlem zamanı, kalite, toplam oran ve gerekçe içerir. Karma bookmaker çarpımı tek bookmaker teklifi olarak sunulmaz. Yalnız gözlem zamanının yenilenmesi yeni arşiv kopyası üretmez.

## Salt okunur üretim gözlemi

21 Ekim 2026 15:25 UTC civarında günlük kupon ve oran kapsamı GET uçları HTTP 200 döndürdü. Günlük kapsamda 10 incelenmiş/güncel analizli maç, 5 kaliteli maç, 0 uygun seçim vardı. 222 aday seçimin 189'u kalite, 33'ü fiyat kontrolünde elendi. Çizgi eşleşmezliği, eskimiş oran ve kaynakta bulunmayan marketler görüldü. Bu gözlem önceki 36 güçlü tahmin sayısını yeniden üretmez; kapsam ve zaman farklıdır. Ayrıntılar `issue-115-readonly-observation.json` dosyasındadır.

Yeni GET `/api/coupons/diagnostics` güncel aday ve ret sayılarını yayın durumuyla birlikte salt okunur döndürür. PostgreSQL'de 20 saniyelik önbellek ve dört saniyelik süre sınırı vardır. Arayüz yalnız kullanıcı tanı düğmesine bastığında ister.

Render servisinin free planı salt okunur doğrulandı. Altyapı veya ortam değişkeni değiştirilmedi. NVIDIA kod varsayılanı kapalıdır; üretim ortam değişkenleri bu görevde okunmadı. Gerçek Telegram/NVIDIA çağrısı, backfill, üretim veri değişikliği, merge veya deploy yapılmadı.

## Doğrulama

Testler sekiz saat penceresini, gün değişimini, 899/900/901 saniyelik fiyat sınırlarını, aynı seçim/farklı fiyat bastırmasını, değişen seçimleri, son anda değişen fiyatı, salt okunur tanıyı ve PostgreSQL eşzamanlı otomatik üretim/gönderimini kapsar. Telegram yalnız MockTransport üzerinden sınanır. 390 ve 1536 piksel Chromium testleri dinamik beş seçim, tanı düğmesi, sekiz zaman penceresi ve yatay taşmayı kontrol eder. Ekran görüntüleri sentetik test verisidir; gerçek maç/başarı kanıtı değildir.
