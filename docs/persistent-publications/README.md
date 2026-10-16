# Kalıcı pano ve günlük kupon yayını

Branch: `codex/persistent-board-daily-coupon`. Taban açık PR #78 (`8ef4535`); bu PR onun üzerine açılır. #77'nin `coupon.py` fiyat satırı daraltması tekrar yazılmadı. Mevcut PR'lar değiştirilmedi. Master'a geçiş, bağımlılık merge edildikten sonra yapılabilir; otomatik merge/deploy yok.

## Kök neden ve sınırlar

Mevcut `prediction_boards`, `prediction_snapshots` ve `saved_coupons` zaten kalıcıydı. F5 model çalıştırmıyordu; fakat hem ana sayfa hem Kuponum açılışı POST `/coupons/automatic` ile tekrar aday taraması, fiyat doğrulaması, kaynak kapsamı raporu ve portföy hesabı yapıyordu. Kaydedilmiş kuponu önce okuyan bir yol yoktu. Pano ile gizli gelişmiş tablo da aynı anda iki büyük tahmin isteği gönderiyordu.

Diğer somut hata: `read_board`, herhangi bir ligin süresi dolunca tüm panoyu stale işaretliyordu. Arayüz böylece diğer liglerin hâlâ geçerli seçimlerini de boşaltıyordu. Şimdi eski/missing ligler ayrı tutulur; yalnızca geçerli ligler sunulur (`partial`), hiçbir geçerli lig yoksa stale kalır. Eski lig seçimleri hiç sunulmaz.

[Salt okunur üretim log örnekleri](render-observations.json): 16 Ekim 12:30:39 UTC SELECT 2.960,6 ms, ardından `/predictions/best` ve `/predictions` bounded timeout; aynı pencerede worker pano yayını yaklaşık 9 saniye sürüyor. Eski logger yalnızca SELECT/UPDATE adı yazdığından bu kayıtlar tek başına belirli SQL'in veya kilidin kök neden olduğunu kanıtlamaz. Sonraki salt okunur `pg_stat_activity` örneğinde 7 idle/ClientRead bağlantısı vardı; kilit beklemesi yakalanmadı. [İki eşzamanlı GET ölçümü](api-before.json) yaklaşık 5,4–5,7 saniyede 504 döndü. Üretimde POST kupon oluşturma, EXPLAIN veya veri onarımı çalıştırılmadı.

Yeni `[PROFILE]` ve `[PHASE]` logları toplam süre, SQL sayısı, SQL süresi ve board_read/current_quotes/input_revision/model_calculation/daily_read/daily_worker aşamalarını verir. Yavaş SQL için yalnızca statement hash yazılır; SQL, bind değerleri, takım/kupon kimlikleri ve gizli bilgiler yazılmaz. Bu kod deploy edilmedi; yeni aşama logları üretimde ölçülmüş gibi sunulmaz.

## Kalıcılık ve veri akışı

- `board_editions`: içerik hash'iyle model sürüm geçmişi; DB UPDATE/DELETE koruması vardır. Model içeriği değişmedikçe yeni satır eklenmez. Kaynak fiyatlar model olasılığından ayrı tutulur; versiyon hash'inde fiyat önbelleği ve değişmeyen verinin kontrol zamanları yer almaz.
- `board_heads`: etkin sürüm ve gerçek worker kontrol zamanı. Worker yeni kaynak revision'ında veya geçerlilik dolmadan yeniden değerlendirir. Son kontrol zamanına göre lig sırası adildir; aynı içeriğin eski oluşturulma tarihi bir ligi sürekli öne geçirmez. Global giriş revision'ı aynı worker işlemi içinde tekrar taranmaz.
- `prediction_boards`: mevcut hızlı okuma tablosu korunur. Aynı modelde ilk generation/sıra korunur; tamamlanmış doğrulama yalnızca expiry'yi yeniler. API expiry'yi generation + TTL varsayımıyla hesaplamaz; gerçek kayıt sınırını kullanır. Başlamış/rescheduled maç kontrolleri ve 900 saniye kaynak fiyat doğrulaması korunur.
- `daily_coupon_publications`: gün/strateji PK'si, giriş revision'ı, içerik sürümü, kontrol zamanı ve kalıcı gösterim kaydı. Boş sonuçlar da sebebiyle saklanır. Dünkü kayıt değişmez. Ekonomik içerik aynıysa mevcut `saved_coupons` kimliği tekrar kullanılır; yeni fiyat gözlemi güncel yayında yer alır, eski arşiv payload'ı değiştirilmez.
- GET `/api/coupons/daily`: yalnızca bugünkü kaydı okur; model/scraper/portföy çalıştırmaz ve kayıt oluşturmaz. Yayın henüz yoksa bugünkü mevcut eski otomatik kuponu da salt okunur inceleyebilir. Maç durumu/kickoff, pano süresi ve fiyat kanıtı tekrar kontrol edilir. Hiç kayıt yoksa arka plan yayını beklendiği söylenir.
- POST `/api/coupons/automatic`: açık kullanıcı yeniden değerlendirmesi. Giriş revision'ı değişmediyse pahalı tarama tekrarlanmaz. Worker varsayılan dengeli yayını da aynı mekanizmayla hazırlar. Günlük ekran **en fazla 4** bağımsız, kaliteli seçim alır; daha az uygun maç varsa liste doldurulmaz. Genel/Telegram portföy fonksiyonunun varsayılan politikası değiştirilmez.

## Ekran güncellemeleri

GET `/api/dashboard/revision` yalnızca etkin pano başlıkları, yaklaşan maç kimlik/zamanları ve bu maçlara ait fiyat satırlarının küçük timestamp projeksiyonlarını okur. MAX timestamp yerine satır başına revision, daha yeni başka bir satırın altında kalan düzeltmeleri de yakalar. Günlük yayın başlığı ve geçerlilik özeti eklenir. PostgreSQL'de üç saniyelik single-flight cache aynı anda gelen kontrolleri birleştirir. Büyük pano JSON'u veya tüm geçmiş taranmaz.

30 saniyede bir yalnızca bu küçük kontrol yapılır. Aynı revision'da büyük istek ve kart DOM yenilemesi yoktur. Aynı içerik yeniden doğrulanmışsa model/kupon expiry bilgisi eşleşen sürüme küçük yanıtla aktarılır; fiyatın kendi 900 saniyelik sınırı uzatılmaz. Değişen bileşen yenilenir; seçilen maç, açık bloklar, gelişmiş filtreler ve manuel kupon taslağı korunur. Gizli gelişmiş tahmin tablosu yalnızca açılınca yüklenir. Geçici hata yeniden denenebilir; henüz geçerli veri silinmez. Model/fiyat/gün/kickoff süresi dolan veri açık sayfada da güncel seçim olarak tutulmaz.

## Ölçüm ve doğrulama

`tests/test_publication_postgres.py`, izole PostgreSQL şemasında eski otomatik değerlendirme iş yükü ile yeni günlük kayıt okumasının ms/SQL sayılarını karşılaştırır; 2.000 arşiv günü üzerinde EXPLAIN ANALYZE/BUFFERS çıktısını CI loguna `PUBLICATION_BENCHMARK` olarak yazar. Gün/strateji primary key'i sorguyu karşılar; fiyat tablolarının mevcut match_id indeksleri kullanılır, #77 ile aynı indeks/filtre işi eklenmez. Üretimde değişiklik veya indeks kurulumu yapılmadı.

Testler: ayrı DB bağlantısından yeniden okuma, aynı model/metadata tekrarında tek edition, yeni kaynakta TTL beklemeden worker yayını, aynı fiyatta tek kupon kimliği, gün değişimi/arşiv, başlamış maç ve 900 saniye expiry, 504'te geçerli kayıt korunması, partial lig, revision düzeltmesi, F5, 320/390/768/1024/1536 Chromium, değişmeyen DOM/filtre/açık blok ve değişen revision'da yalnızca ilgili istek. Son Python/PostgreSQL/JS/Chromium sonuçları ve benchmark sayıları PR açıklamasında yer alır.

Üretim öncesi/sonrası aynı ortam ölçümü yapılamaz: istenen şekilde deploy yapılmadı. İzole test benchmark'ı, üretimde 504'lerin tümünün bittiği garantisi olarak sunulmaz. Model eşikleri, fiyat doğrulama, kupon bağımsızlığı, tahmin hafızası ve gerçek Telegram/NVIDIA istek yasağı korunur.
