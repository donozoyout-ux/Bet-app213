# Issue #39: son fikstür ve gerçek oran doğrulaması

Başlangıç: güncel `origin/master`, `5224a01` (PR #38 dahil). Branch:
`codex/issue-39-fixture-odds`. Ölçümler 12 Ekim 2026, 20:36–20:49
Europe/Istanbul saatlerinde alındı. Üretimde yalnız GET ve Render'ın salt okunur
PostgreSQL sorguları kullanıldı. Üretime yazılmadı, merge/deploy yapılmadı;
gerçek Telegram mesajı gönderilmedi.

## Fikstür: kaynak IDs / gerçek DB IDs

Üretim PostgreSQL'de toplam **10.161** benzersiz maç var: aktif sekiz ligde
**8.051**, devre dışı 3/5/29 liglerinde **2.110**. Kayıtlar 2027'ye uzanıyor.
Kaynak karşılaştırması 2024-2025, 2025-2026 ve 2026-2027 sezonlarının tamamını
kapsar. Toplam maç sayısı ile aktif lig kapsamı birbirine karıştırılmadı.

Lig başına `DB / eski parser / düzeltilmiş parser` benzersiz ID sayıları:

- 36 Premier League: 1.140 / 1.140 / 1.140; kaynak hatası 0 → 0.
- 31 La Liga: 1.140 / 1.139 / 1.140; kaynak hatası 1 → 0.
- 34 Serie A: 1.140 / 1.140 / 1.140; kaynak hatası 0 → 0.
- 8 Bundesliga: 918 / 918 / 918; kaynak hatası 0 → 0.
- 11 Ligue 1: 918 / 918 / 918; kaynak hatası 0 → 0.
- 30 Süper Lig: 954 / 954 / 954; kaynak hatası 0 → 0.
- 16 Eredivisie: 924 / 924 / 924; kaynak hatası 0 → 0.
- 23 Liga Portugal: 917 / 917 / 918; kaynak hatası 1 → 0.

Eski parser toplam **8.050** geçerli kaynak ID okuyordu. Düzeltilmiş parser
**8.052** okuyor; DB'de eksik tek kaynak ID **3023443**. **3013719** zaten DB'de
mevcut; bunu yeni maç saymak yanlış olur.

Gerçek satırlarda `3013719` için `-12` statüsü ve skor alanında
`腰斩|腰斬|Interupt`, `3023443` için `-14` ve `推迟|推遲|Delay` var. Bunlar
sırasıyla terk edilen ve ertelenen maç açıklamaları. Parser yalnız bu doğrulanmış
statü/token çiftlerini skorsuz kabul eder. Aynı token bitmiş bir maçta gelirse
satır hata olarak izole edilir. Skor sıfır olarak üretilmez. Lig kimliği yanlış
olan bir satır da komşu geçerli satırların işlemini iptal etmez.

Üretim GET kayıtları ve aynı kaynak yanıtları, **izole PostgreSQL şemasında**
iki kez uygulandı. İlk uygulama: **8.051 → 8.052 unique**, **8.052 keşfedilen**,
**1 eklenen**, **1 güncellenen** (lig 30), **8.050 değişmeyen**, **0 parse-failed**.
Tekrar: **0 eklenen**, **0 güncellenen**, **8.052 değişmeyen**, **0 hata**.
Bu sayılar üretimde uygulanmış bir değişiklik iddiası değildir.

Her sezon/tur için keşif checkpoint'i artık job üzerinde kalıcıdır:
`discovered/added/updated/unchanged/errors`. Aynı checkpoint yeniden yazılır;
sayacı tekrar tekrar artırmaz. `/api/diagnostics` lig başına son tamamlanan
keşfi ve hatasız son keşif zamanını verir. Eski şemalar additive ve idempotent
migration ile genişler. Market backfill'in mevcut boş-iş ve retry TTL kuralları
korundu.

## Oranlar: ölçülmüş kapsam

Üretim PostgreSQL, tüm saklanmış maçlar (devre dışı ligler dahil):

- FT 1X2, AH ve OU: her market için **29.682 kaynak satırı**,
  Crown/Bet365/Sbobet başına 9.894.
- İlk yarı OU: **2.825 satır**; Bet365 935, Crown 945, Sbobet 945.
- Korner OU: **1.754 satır**; Bet365 884, Crown 870. Sbobet satırı yok.
- FT market başına temsilî latest alanında **395 doğrulanmış alan gözlemi**,
  **12 taze maç öncesi kaynak satırı** var.
- İlk yarı latest_over: **877 alan gözlemi**, **39 taze kaynak satırı**.
- Korner latest_over: **464 alan gözlemi**, **3 taze kaynak satırı**.

Her bookmaker için priced/observed/fresh ayrımı
`issue39-production-source-counts.json` içinde. Bunlar result/AH için
`latest_home`, OU için `latest_over` alanlarını ölçer. Kaynak satırının taze olması,
model çizgisinin eşleştiğini veya kupon kalite koşullarını geçtiğini göstermez.
Yeni `/api/odds-coverage.source_rows` çıktısı her çizgi/periyot/bookmaker için
her latest fiyat alanının priced/observed/fresh sayısını ayrıca verir.

**Aynı üretim PostgreSQL anlık görüntüsünde** master ve yeni matcher çalıştırıldı
(20:49:33 Europe/Istanbul). Önce → sonra:

- Öneri sayısı: **102 → 102**.
- Gerçek eşleşen fiyat: **4 → 4**; taze fiyat: **4 → 4**.
- .70/.80/.80 koşullarını ve taze fiyat koşulunu geçen seçim: **2 → 2**.
- Bugün bağımsız kupona uygun maç: **0 → 0**; günlük kupon maç sayısı: **0 → 0**.
- 1X2: 2/2 fiyatlı; ilk yarı: 2/13 fiyatlı, 11 çizgi uyuşmazlığı.
- FT gol: 0/2 fiyatlı, 2 çizgi uyuşmazlığı.
- Korner: 0/42 fiyatlı; 40 kaynak satırı yok, 2 çizgi uyuşmazlığı.
- İkinci yarı: 0/32; sarı kart: 0/11, kaynak market bulunamadı.

Kapsamın yükseldiği iddia edilmiyor. Model 8.5 çizgisinin fiyatını 9.5 kaynaktan
almak yasak. Alternatif çizgi olasılığı doğrulanmadığı için model çizgisi
değiştirilmez. İkinci yarı, kart, BTTS ve çifte şans için doğrulanmış kaynak
olmadığında fiyat boş kalır.

Gerçek `2996166` maçında Goaloo yanıtları yeniden alındı:

- `type=14,t=1,h=0`: FT 1X2/OU/AH, üç bookmaker. Latest OU 3.25,
  AH ev perspektifinde -1.0.
- `type=14,t=1,h=1`: ilk yarı OU; Bet365/Sbobet 1.25, Crown 1.5.
- `type=14,t=4,h=0,ex=1`: korner OU; Bet365 10.0, Crown 10.5.

Gerçek fiyatlar ve raw source alanları `issue39-real-source-prices.json` içinde.
Bu yanıtlar kaynak doğrulamasıdır; tek başına güncel kupona uygunluk kanıtı
değildir. İlk yarı FT fiyatıyla, korner de gol fiyatıyla eşleştirilmez.

## Çalışan düzeltmeler ve sınırlar

Alan gözlem metadata'sı olmayan eski fiyat artık `updated_at` değerinden zaman
ödünç almaz. Partial scrape eksik alanlara yapay geçmiş gözlem zamanı eklemez.
Gerçek fiyat görüntülenebilir; doğrulanmış alan gözlemi yoksa freshness unknown
ve coupon eligibility false olur. Mevcut gerçek `_field_observed_at` ve
`_field_lines` kayıtları korunur; 900 saniyelik sınır değişmedi.

Goaloo retry denemeleri bittikten sonra aynı URL/parametre/yanıt türü için
60 saniyelik cooldown var. İlk yarı ve FT parametreleri birbirini engellemez.
Collector'ın kalıcı retry/backoff davranışı korunur.

`/api/matches/{id}/odds` ve `include_odds` artık ilk yarı/korner satırlarını da
market, periyot, bookmaker ve alan gözlem metadata'sıyla sunar. Maç kartında
gerçek fiyatın kaynağı/gözlem zamanı veya market bazında eksiklik nedeni görünür.
Günlük en fazla dört bağımsız maç, .70 probability, .80 reliability, .80 score,
15 dakika fiyat sınırı ve Telegram 12:00/18:00 ledger kuralları değiştirilmedi.

## Tekrar çalıştırma ve doğrulama

```powershell
python scripts/issue39_capture.py scratch/issue39-capture
# Yalnızca yerel/izole TEST_POSTGRES_URL ile:
python scripts/issue39_replay.py scratch/issue39-capture scratch/issue39-replay.json
python scripts/issue39_compare_prices.py docs/issue39-exact-price-snapshot.json scratch/prices.json
python -m compileall -q src legacy
python -m pytest -q
node tests/dashboard_runtime.cjs
node tests/predictions_runtime.cjs
node tests/market_runtime.cjs
node tests/coupon_runtime.cjs
```

Kaynak capture salt okunurdur; replay yalnız açıkça verilmiş TEST_POSTGRES_URL
üzerinde benzersiz, işlem sonunda silinen şema kullanır. Yeni canlı capture
zamanla değişebilir; fiyat karşılaştırması commit edilmiş aynı snapshot üzerinde
tekrarlanabilir. Fikstür replay ölçümleri `issue39-fixture-replay.json` içinde.

Master başlangıç testi: **272 passed, 13 skipped** (PostgreSQL yapılandırılmadan).
Son tam paket: **289 passed, 3 skipped**, 274.39 saniye; PostgreSQL etkin.
Üç skip Windows'ta desteklenmeyen POSIX/Gunicorn başlatma senaryolarıdır;
Linux CI'da çalışırlar. Sonradan eklenen PostgreSQL migration regresyonu ayrıca
geçti (CI tam paketi bu testi de içerir). Yeni PostgreSQL testi
eski kolonları olmayan şemadaki migration'ı ve gerçek JSON alan aggregation'ını
iki kez initialize ederek doğruladı: **8/8 yeni regresyon testi geçti**.
Son matcher/parser/cooldown paketi: **43 passed**. Ayrı Chromium paketi:
**7 passed**, 17.61 saniye (1440/768/390 ekranlar, market dialog, kupon taslağı,
otomatik kupon düzenleme ve geçmiş). JavaScript runtime:
**6 dashboard + 17 prediction + 4 market + 7 coupon = 34
senaryo geçti**. JavaScript syntax ve Python compileall geçti.

Üretimde düzeltilmiş kodun çalıştığı iddia edilmiyor; deployment yapılmadı.
Eşleşmeyen çizgiler ve olmayan kaynak marketler bilinçli olarak fiyatlanmamıştır.
