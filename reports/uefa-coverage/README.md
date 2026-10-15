# Avrupa Ligi kapsam denetimi — 15 Ekim 2026

Üretim PostgreSQL verisi Render'ın **salt okunur işlem** aracıyla 20:10:59 UTC'de alındı. 2024 sonrası 10.456 maç, takım Goaloo kimlikleri ve lig kataloğu `source.json.gz` içinde korunur. Anahtar, bağlantı dizesi veya kullanıcı bilgisi içermez. `report.json`, bu girdiden yeniden hesaplanan rapordur; test raporla tam eşitliği denetler.

## Gerçek önce / sonra

| Ölçüm | Önce | Bu PR sonrası üretimde |
|---|---:|---:|
| Önümüzdeki 30 gün Avrupa Ligi maçı | 36 | 36 |
| Mevcut modelle gerçekten analiz edilebilir | 12 | 12 |
| En az bir takımın aynı turnuvada 5 maçı yok | 23 | 23 |
| Araştırma adayı için yeterli veri | 13 | Canlıya alınmadı |

**Gerçek kapsam artışı: 0 maç.** Üretime yazılmadı, backfill tetiklenmedi ve adayın canlı kullanımı açılmadı. 13 sayısı bir araştırma modelinin örneklem kapısından geçişidir; güvenilirlik veya kupon kalitesi kanıtı değildir. Takım sayısı kapısını geçen 13 mevcut maçtan biri ev/deplasman kapısında elenir: bu nedenle mevcut model kapsamı 12'dir. Eşikler 5 takım, 3 gerekli saha, 20 lig olarak korunur.

Her maç/takım için Avrupa toplam/ev/deplasman, yerel toplam/ev/deplasman, kaynak ligleri ve eksiklik nedenleri `report.json` → `fixtures` içinde bulunur. UI bu sayıları API'nin `uefa_coverage` alanından güvenli metin düğümleriyle gösterir. `domestic_context` son 10 form penceresiyle sınırlı kalır; kapsam sayısı tüm uygun gözlemleri sayar. İsim benzerliğiyle takım eşleme yapılmaz.

## Kaynak kontrolü ve backfill

`source-check.json`: doğrulanmış katalogdaki 13 yerel lig için 65 mantıksal kaynak isteği (HTTP yeniden denemeleri hariç). 2023–2024 sezonunun 2024 tarihli maçları dahil edilir. Tam Goaloo takım kimlikleriyle eşleşen **2.089** bitmiş maçın **527** tanesi mevcut dış maç kimlikleri arasında yok. Bu farklar yeterlilik artışı anlamına gelmez. Dokuz ayrıştırılamayan lig aşaması satırı raporda kayıtlıdır; veri üretilmez. 13 takımın bu sınırlı katalogdaki kaynaklarında eşleşmesi yoktur; diğer ligler kontrol edilmediği için bunlar “Goaloo'da yok” olarak sınıflandırılmaz.

Yeni `domestic_history` işi mevcut kalıcı iş kuyruğunu, kaynak istemcisinin hız/yeniden deneme sınırlarını, round checkpoint'lerini ve strict ayrıştırıcıyı kullanır. Manuel tetiklenir; otomatik takvim eklenmez. Aynı ligde aktif iş varsa yeni iş oluşturulmaz; tekrar denemede 7 gün beklenir. PostgreSQL advisory lock ile kuyruk ekleme tekilleştirilir. En fazla dört sezon, yalnızca 2024 sonrası bitmiş maçlar; odds işi üretilmez. Mevcut maçlar tamamen atlanır, böylece orijinal skor/gözlem zamanı/tahmin hafızası yeniden yazılmaz. Yeni kayıtların gözlem zamanı gerçek toplama zamanıdır. Devre dışı/doğrulanmamış ligler otomatik etkinleştirilmez. Hiç yerel kimlik eşleşmesi olmayan takım için lig tahmin edilmez; katalog genişletme/kimlik doğrulama gerekir.

```powershell
# Salt okunur, Goaloo HTTP kontrolü; DB'ye yazmaz
python -m scripts.uefa_source_check source.json source-check.json
# Arşivlenmiş PostgreSQL girdisinden hesaplama; DB ve ağ kullanmaz
python -m scripts.uefa_coverage_report --input reports/uefa-coverage/source.json.gz --output report.json
# Yalnızca operatörün seçtiği DB'de eksik arşiv işleri kuyruğa alınır
python -m scripts.uefa_coverage_report --enqueue-backfill
```

## Alternatif model ve zaman deneyi

`uefa-opponent-elo-v1-research` ayrı, çevrimdışı bir 1/X/2 adayıdır. Gol ortalaması birleştirmez; maç sonucu ile rakip gücüne göre takım rating'i günceller. Yerel lig üyeliği yalnızca daha önce gözlemlenmiş tam kimlikli yerel maçtan gelir. Lig ofsetleri yalnızca geçmiş UEFA karşılaşmalarında, iki tarafın yerel lig üyeliği biliniyorsa güncellenir. İki lig arasında bağlantı yoksa veya her ligde 20 köprü gözlemi yoksa çekimser kalır. Her iki takım için yerel 5 maç/ilgili saha 3 maç/lig 20 maç şartı vardır. UEFA örneklemi şartını gevşeten bir canlı Poisson yolu değildir.

Araştırma sabitleri: Elo ölçeği 400, K=20, saha avantajı 60, lig güncelleme payı 1/4; uç rating farkı ±1200; geçmiş beraberlik oranına 20 maçlık, 5 beraberlik önseli. Üç sınıf olasılığı beraberlik ağırlıklı normalize edilir. Bunlar doğrulanmış katsayı değildir, bu veri üzerinde optimize edilmemiştir. Kavramsal rakip gücü güncellemesi için [FIFA'nın Elo/SUM açıklaması](https://inside.fifa.com/fifa-rankings/world-ranking/procedure-men) referanstır; bu aday FIFA modelinin aynısı değildir.

2024–2026 döneminde **646** bitmiş Avrupa Ligi maçı kronolojik olarak yürütüldü. Her hedefte yalnızca 3 saat önce başlamış, `created_at`, `updated_at` ve varsa `last_scraped_at` zamanları hedef başlama anını aşmayan sonuçlar kullanıldı. Geçmiş düzeltmelerin eski sürümleri yoksa düzeltilmiş kayıt eski kesitte dışlanır. Veri sızıntısını önlemek için bugün indirilen arşivler geçmişte biliniyormuş gibi kullanılmadı. Hedef sonucu rating'e kendi tahmininden önce verilmez; eş zamanlı ve henüz bitmemiş maçlar dışlanır. Sabit aday için öğrenme yalnızca hedef öncesinde gerçekleşir; gelecekle hiperparametre ayarı yapılmaz.

| Aynı maçlardan değerlendirme | Maç | Brier (3 sınıf toplamı) | Log loss |
|---|---:|---:|---:|
| Mevcut — tüm uygun | 1 | 0.836977 | 1.356615 |
| Aday — tüm uygun | 3 | 0.714249 | 1.129353 |
| Mevcut — ortak test | 1 | 0.836977 | 1.356615 |
| Aday — ortak test | 1 | 1.075274 | 1.585194 |

Farklı maç kümelerinin ortalamalarından üstünlük çıkarılamaz. Ortak testte aday daha kötü; yalnızca bir maçla anlamlı belirsizlik/üstünlük aralığı hesaplanamaz. Kalibrasyon aralıkları ve yıl bazında kapsam JSON raporundadır; birkaç gözlemle kalibre model iddiası yapılmaz. Asgari 200 bağımsız doğrulama maçı bile yoktur; bağımsız ileri tarihli holdout da yoktur. **Canlıya geçiş reddedildi.** Tek maçtaki üç sonuç kategorisi üç bağımsız maç sayılmaz. Bu DB, 2024–2025 için gerçek zamanda gözlenmiş backtest kanıtı sağlayamaz; geçmiş gözlem kaydı olmadan güçlü bir 2024–2026 performans iddiası üretmek doğru değildir.

## Korunan sınırlar

Canlı Poisson hesaplaması, engine kaydı/seçimi, kupon eşikleri, oran tazeliği, Telegram, NVIDIA, Dersler ve settlement/snapshot kodu değiştirilmedi. Aday modülünün canlı tahmin üretimine çağrı yolu yoktur. API yalnızca ek kapsam kanıtı alır. Merge/deploy/gerçek mesaj gönderimi yapılmadı.

Test sonuçları PR açıklamasında ve CI çalışmasında sunulur. PostgreSQL testleri kapsam/tekilleştirme, Chromium 390/768/1440 ekranlarda API şemasıyla gerçek uçtan uca sayılar, Python testleri zaman damgaları/rakip etkisi/ham gol birleştirmeme/rapor tekrarlanabilirliği ve eksik veri çekimserliğini kapsar.
