# Issue #143 — Grafik kapsamı ve bugünkü sonuçlar

## Canlı ortamda salt okunur bulgular

25 Ekim 2026, 19:54–19:57 İstanbul saati. HTTP GET ve Render'ın salt okunur PostgreSQL sorguları kullanıldı; üretimde hiçbir kayıt, ayar veya iş değiştirilmedi.

| Kanıt | Bulgular |
| --- | --- |
| `/api/performance-trends` | HTTP 200; `today_excluded=true`, `truncated=false`; önceki 7 ve 30 günde sıfır kayıtlı seçim. |
| `/api/chart-diagnostics` | 12.390 maç, 118 maç öncesi snapshot, 233 benzersiz seçim; 6 doğrulanmış sonuç: 4 kazandı / 2 kaybetti. 233 seçimin tümü bugün veya gelecekteki maçlara ait. |
| `/api/market-performance?limit=5` | Aynı genel toplam ve 6 doğrulanmış sonuç. Sayfalama, genel sonuç toplamını düşürmüyor. |
| Settlement heartbeat | `ok`, başarısızlık 0; 6 kalıcı karar zaten mevcut. `written=0`, sonuç yok anlamına gelmiyor. |
| Snapshot/maç SQL birleşimi | Maç öncesi kaydı olan en eski maç günü 25 Ekim. Önceki tamamlanmış İstanbul günlerinde kayıp/filtrelenmiş sonuç bulunmadı. |
| Bitmiş maçların kanıtları | Son kontrolde 16 bitmiş maç, 31 seçim; tam maç skorları mevcut, ilk yarı skorları yok. 25 ilk/ikinci yarı gol seçimi için gereken ilk yarı kanıtı eksik. 5 toplam gol + 1 maç sonucu seçimi, 6 sonuçlandırılmış seçimle uyumlu. |

Kontroller sırasında bir maç daha tamamlandığından eksik sonuç sayısı 23'ten 25'e yükseldi. Bu bir zaman aralığı gözlemidir; ekran görüntülerindeki sayılar üretim verisi değildir.

Kök neden: grafikler yalnızca tamamlanmış günleri çiziyor, bugünkü sonuçları dışarıda bırakıyordu. Sıfır sonuç durumu ayrıca açıklamasız yatay çizgiye benziyordu. Sonuçlandırma motorunda düzeltme gerektiren kanıt bulunmadı; eksik ilk yarı skoru uydurulmadı.

## Değişiklik

- Mevcut 60 günlük geçmiş, 7/30 günlük karşılaştırmalar ve İstanbul gün sınırları korunur.
- `/api/performance-trends` güncel referans tarihinde ayrıca `current_day` döndürür. Aynı lig, market, seçim, güven ve capture-date filtrelerini kullanır. Gelecek günler dahil değildir; geçmiş referans tarihi seçildiğinde bu alan `null` olur.
- Aynı sınırlandırılmış SELECT, bugünün sonuna kadar okur; snapshot limiti ve önbellek korunur. GET hiçbir tahmin veya settlement üretmez. Veri sınırına ulaşılırsa eksik bugünkü toplam gösterilmez.
- Ana Sayfa ve Performans'ta **Bugün** kutusu, kayıtlı/kazanan/kaybeden/bekleyen/eksik/çelişkili/geçersiz seçim sayılarını gösterir. Bu kutu dönem karşılaştırmalarından açıkça ayrıdır.
- Sonuçsuz tamamlanmış dönemlerde başarı çizgisi yerine etiketli seçim sayısı çubukları gösterilir. Gerçek sıfırlar `0`; hesaplanamayan başarı `N/A`/`—` olarak kalır. Bekleyen kayıtlar kazanan veya kaybeden sayılmaz.
- API hatasında önceki bugünkü sayılar kaldırılır; ağ hatası sıfır sonuç gibi gösterilmez.
- Veri toplama, immutable snapshot, sonuçlandırma, Telegram, NVIDIA ve hibrit geçmiş ayarları değiştirilmez.

## Test ve görseller

`tests/test_chart_evidence_143.py`: gerçek FastAPI/SQLite ile SQL'in salt okunurluğu ve sınırı, idempotent okumalar, İstanbul gece yarısı, capture-date/market filtreleri, geçmiş referans tarihi ve Chromium durumları. Tarayıcı çağrıları yalnızca izole ASGI uygulamasına yönlendirilir.

| Durum | 390px | 1536px |
| --- | --- | --- |
| Önce — Ana Sayfa, bugün sonuç var | [Mobil](screenshots/before-home-settled-390.png) | [Masaüstü](screenshots/before-home-settled-1536.png) |
| Sonra — Ana Sayfa, bugün sonuç var | [Mobil](screenshots/after-home-settled-390.png) | [Masaüstü](screenshots/after-home-settled-1536.png) |
| Önce — Performans | [Mobil](screenshots/before-performance-settled-390.png) | [Masaüstü](screenshots/before-performance-settled-1536.png) |
| Sonra — Performans | [Mobil](screenshots/after-performance-settled-390.png) | [Masaüstü](screenshots/after-performance-settled-1536.png) |
| Sıfır kayıt | [Mobil](screenshots/after-home-empty-390.png) | [Masaüstü](screenshots/after-home-empty-1536.png) |
| Bekleyen | [Mobil](screenshots/after-home-pending-390.png) | [Masaüstü](screenshots/after-home-pending-1536.png) |
| Eksik kanıt | [Mobil](screenshots/after-home-awaiting-390.png) | [Masaüstü](screenshots/after-home-awaiting-1536.png) |
| API kesintisi | [Mobil](screenshots/after-home-outage-390.png) | [Masaüstü](screenshots/after-home-outage-1536.png) |

Diğer Performans durumları aynı `screenshots/` klasöründedir. Görsellerde yapışkan uygulama başlığı yalnızca paneli örtmemesi için gizlenmiştir. Önce görüntüsü `0300627` (#140) varlıklarıyla, aynı izole veri üzerinde alınmıştır.

Tekrar üretim (PowerShell):

```powershell
$env:CHART143_SCREENSHOTS='docs/chart-evidence-143/screenshots'
python -m pytest tests/test_chart_evidence_143.py -q
$env:CHART143_BEFORE='1'
python -m pytest tests/test_chart_evidence_143.py -q
```

Tam CI sonucu PR açıklamasında raporlanır. Merge/deploy ve gerçek Telegram/NVIDIA isteği yapılmaz.
