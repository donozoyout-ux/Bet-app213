# Tahmin denetimi ve maç 1982

## Salt okunur gözlem

27 Ekim 2026 18:23 İstanbul saatinde GET `/api/matches/1982/dossier`
yanıtından alınan alanlar `prediction-audit-1982-observation.json` dosyasındadır.
Torino–Como için ilk kayıt 24 Ekim 23:39:56 İstanbul saatidir:
`under_2_5_second_half_goals` (%84,346) ve `over_1_5_goals` (%81,925).
Model: `quality-poisson-v3-specialist-adapters-v1`.
Gözlem sırasında geçerli güncel seçim dönmedi. Daha önce görülen güncel
seçimin içeriğini veya hangi kaynak değişikliğinin onu tetiklediğini bu yanıt
kanıtlamaz. Eski bir yayının zamanı/modeli tahmin edilmedi.

Kodda ana sayfa `read_board` ile değişebilen lig yayınını okur; ilk seçimler
`capture`/`capture_many` ile match_id başına yalnızca bir kez eklenir.
Yeni veri ve model hesaplaması güncel yayını değiştirebilir, ilk kaydı değiştirmez.
Mevcut içerik adresli `board_editions` arşivi tek başına aynı içeriğe geri dönüş
zamanını veya açık bir maç/model yayın zincirini kaydetmez.

## Uygulama

- #151 tabanı ve #150 CSS değişiklikleri birleştirildi; iki açık PR değiştirilmedi.
  #151 HTML'inin çağırdığı eksik `/dashboard-audit.js` rotası eklendi.
- `prediction_publications` yeni eklenen tablodur. Worker'ın mevcut seri yayın
  işlemi içinde ilk gözlem, seçim/olasılık/model/kickoff değişimi ve seçimlerin
  kaldırılması eklenir. A→B→A ayrı olaylardır; değişmeyen kontrol yeni satır açmaz.
  Mevcut tahmin ve yayın satırlarına backfill, UPDATE veya DELETE uygulanmaz.
- Snapshot ve yeni olaylar SQLite/PostgreSQL UPDATE/DELETE trigger'larıyla korunur.
  Yeni tablo mevcut `create_all` kurulumuyla oluşur; ayrı hizmet gerekmez.
- GET `/api/matches/{id}/prediction-audit` salt okunurdur. İlk kayıt, geçerli güncel
  yayın, model sürümleri, eklenen/çıkarılan/değişen seçimler ve kayıt sorunlarını
  döndürür. Boş veya süresi dolmuş yayın eşleşme sayılmaz. Hatalı ilk kayıt
  saklandığı haliyle raporlanır; düzeltilmez veya doğrulanmış tahmin sayılmaz.
- Geçmiş maç indeksiyle sayfalanır (varsayılan 25, en fazla 100). Geçmiş yalnızca
  özelliğin devreye alınmasından sonra gözlenen yayınları kapsar. İlk kayıt zamanı
  yakalama zamanıdır; yeni olay zamanı worker'ın yayını gözlediği zamandır.
- Arşiv istatistiksel seçim alanlarını tutar; eski oranları güncel fiyat gibi
  sunmaz. Model sürümünün değişmesi kendi başına kalite iyileşmesi kanıtı değildir.
- Render yapılandırması değişmedi. NVIDIA/Telegram çağrısı, üretim işi,
  üretim verisi değişikliği veya deployment yapılmadı.

## Doğrulama

`tests/test_prediction_audit.py`: tekrar kontrol, olasılık değişimi, eski içeriğe
geri dönüş, kaldırma, snapshot değişmezliği, UPDATE/DELETE engeli, salt okunur
tekrar GET, sayfalama, süre aşımı, eksik/hatalı kayıtlar, mobil/masaüstü Chromium
ve izole PostgreSQL testleri. Test girdileri yalnızca geçici test veritabanındadır.
