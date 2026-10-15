# Veri toplama ve Avrupa analizi denetimi — 15 Ekim 2026

## Gerçek üretim durumu

Render PostgreSQL salt okunur ölçümü (11:35 UTC / 14:35 Türkiye):

- 12.904 kayıtlı maç; son 24 saatte 0 yeni kayıt.
- Son yeni kayıt: 13 Ekim 2026 12:02:46 UTC.
- Mevcut maçlarda son güncelleme ve kaynak kontrolleri 15 Ekim'de devam ediyor.
- 11 aktif lig var. Son 11 günlük güncelleme işinin **fikstür keşifleri tamamlanmış**;
  keşif parse hatası yok. Bugünkü keşif 36 mevcut maçı güncellemiş, yeni maç eklememiş.
- Denetim anında etkin liglerde `running` veya `queued` toplama işi bulunmuyor.
  Takılı bir backfill doğrulanmadı; çalışan işi iptal etmek veya kayıt silmek gerekmedi.
- Üç güncelleme işi oran eksikleri nedeniyle `partial`: Türkiye'de 2, Şampiyonlar
  Ligi'nde 18, Konferans Ligi'nde 1 başarısız öğe. Fikstür keşfi bu işlerde de başarılı.

Goaloo güncel sezon kaynağı ayrıca çekildi, tüm round/stage bölümleri genişletildi
ve gerçek maç kimlikleri PostgreSQL ile karşılaştırıldı:

| Lig | Kaynakta 2026–2027 maçı | Veritabanında eşleşen | Eksik / yanlış lig |
| --- | ---: | ---: | ---: |
| Premier League | 380 | 380 | 0 / 0 |
| La Liga | 380 | 380 | 0 / 0 |
| Serie A | 380 | 380 | 0 / 0 |
| Bundesliga | 306 | 306 | 0 / 0 |
| Ligue 1 | 306 | 306 | 0 / 0 |
| Süper Lig | 306 | 306 | 0 / 0 |
| Eredivisie | 306 | 306 | 0 / 0 |
| Portekiz | 306 | 306 | 0 / 0 |
| Şampiyonlar Ligi | 234 | 234 | 0 / 0 |
| Avrupa Ligi | 224 | 224 | 0 / 0 |
| Konferans Ligi | 366 | 366 | 0 / 0 |
| **Toplam** | **3.494** | **3.494** | **0 / 0** |

**Bu ölçümde sıfır yeni maç bir keşif hatası değil:** yayımlanmış mevcut sezon
fikstürleri zaten kayıtlı. Kaynak henüz yayımlamadığı sonraki tur eşleşmelerini
üretmek veya tekrar maç ekleyerek sayacı artırmak veri doğruluğunu bozardı.

Günlük GitHub workflow'u `04:15 UTC` saatinde update-all işlerini kuyruğa alıyor;
öncelik ve round checkpoint akışı incelendi. Daemon worker'ın kendi düzenli keşif
tetiklemesi yoktu. Dış zamanlayıcı kaçırılırsa boşta çalışan worker yeni güncelleme
başlatamıyordu. Yeni güvenlik ağı üretim daemon'unda beş dakikada bir kontrol eder:
başarılı keşif son 24 saatteyse veya aktif update varsa yeni iş açmaz; başarısız
denemeden sonra en az altı saat bekler. Enqueue PostgreSQL transaction advisory lock
ile seri; çalışan backfill, checkpoint'ler ve mevcut günlük workflow korunur.
Tek seferlik bakım komutlarına bu otomatik kuyruk eklenmez.

## Oran eksiklerinin ayrılması

Sonraki 7 günde 122 programlanmış maç için üretimde tam fiyat alanları:

| Kaynak | Market | Tam açılış | Tam son fiyat | Bir son fiyat alanı ≤900 saniye |
| --- | --- | ---: | ---: | ---: |
| Bet365 | 1X2 / AH / OU | 122 / 122 / 122 | 122 / 122 / 122 | 36 / 36 / 36 |
| Crown | 1X2 / AH / OU | 104 / 104 / 104 | 104 / 104 / 104 | 36 / 36 / 36 |
| Sbobet | 1X2 / AH / OU | 119 / 120 / 120 | 119 / 120 / 120 | 35 / 36 / 36 |

Son sütun yalnızca `latest_home` veya `latest_over` alanının gerçek gözlem zamanını
ölçer; bütün fiyatların/çizgilerin taze olduğunu veya kupona uygunluğu iddia etmez.
Kuponun her fiyat/çizgi eşleşmesi ve 900 saniye kontrolü aynen korunur. Gelecek maçın
henüz oluşmamış kapanışını eksik veri hatası saymıyoruz; bitmiş maçlar açılış/kapanış
olarak denetleniyor. `r` (in-play) fiyatı kapanış olarak kullanılmıyor.

Canlı kaynaktan yeniden doğrulanan örnekler:

- **3026754:** Sbobet'in 1X2, AH ve OU açılış/son alanları kaynakta boş.
- **3073094:** Crown'un üç marketi kaynakta boş.
- **3073880:** Sbobet'in 1X2 marketi boş; diğer marketlerini yok saymak yanlış olur.
- **3073151:** Günlük iş sırasında eksik olan Crown fiyatları artık kaynakta var,
  fakat üretim kaydı inceleme anında altı açılış/son alanında da NULL.

Yazılım açığı: öncelikli fiyat yenileme yalnızca modelin önceden `qualifies`
seçimleri üzerinden çalışıyordu. Model/veri eksikliği veya kupon seçimi olmayan
bir maç, yeni yayımlanan fiyatı tekrar kontrol etmeden kalabiliyordu. Artık önümüzdeki
48 saatteki tüm desteklenen programlanmış maçlar tam zaman 1X2/AH/OU kontrolüne
girer; mevcut fiyatlı model seçimlerinin diğer market yolları devam eder. Döngü
başına bir endpoint, sekiz saniye bütçe, başarılı gözlemde beş dakika aralık,
başarısızlıkta kalıcı artan backoff korunur. En uzun bekleyen endpoint önce gelir;
bugünkü maçlar eşitlik halinde öncelikli, diğer ligler sürekli aç bırakılamaz.

Kaynak eksikleri artık bookmaker/market/stage olarak iş hata kaydında açıklanır.
Boş kaynak yanıtı mevcut fiyatın gözlem saatini yenilemez; tamamı boş market yeni
bir dolu kayıt gibi yazılmaz. Kısmi yanıttaki gerçek alanlar saklanır, eski alanlar
silinmez veya yeni gözlem yapılmış gibi tarihlenmez.

## Avrupa takımlarının yerel lig geçmişi

Ana model `Match.league_id == hedef lig` ile yarışma geçmişini seçiyor. Bu kapsam
doğru biçimde yarışmaya özgü; ham yerel gol oranlarını UEFA örneklemine katmak
rakip gücü/lig seviyesi kalibrasyonu olmadan güveni yapay biçimde yükseltirdi.

Yeni `domestic_context`, üç UEFA kupası için desteklenen sekiz aktif yerel ligdeki
son on gerçek maçı analiz yanıtına ve Form sekmesine ekler. Eşleşme yalnızca mevcut
Goaloo takım kimliğine bağlı foreign key ile yapılır; isim benzerliği kullanılmaz.
Her kaydın bitmiş olması, skoru bulunması, kickoff'un veri kesiminden en az üç saat
önce olması ve oluşturma/güncelleme/kaynak gözlem zamanlarının kesimden sonra
olmaması gerekir. Avrupa maçı, milli maç, devre arası veya gelecekte bitmiş gibi
işaretlenen maç bu yerel geçmişe girmez.

15 Ekim UEFA maçlarının gerçek kapsamı: 36 maç / 72 takım tarafı; 25 tarafta en az
beş uygun yerel kayıt, 47 tarafta desteklenen yerel geçmiş yok. Eksikler başka takım
adıyla veya yeni istatistikle tamamlanmıyor. Form, kaynak ligleri ve kapsam eksikliği
açık gösterilir. Bu değişiklik **model olasılığını, örneklem yeterlilik şartlarını
veya güveni yükseltmez**; kalibre edilmemiş yarışma karışımı uygulanmaz.

## Sayaçlar, önce/sonra ve sınırlar

`/api/status` mevcut alanlarına ek olarak son başarılı keşif, son başarılı kaynak
verisi güncellemesi, lig başına keşif sayaçları, geciken ligler, son 24 saatteki
başarısız öğe/kısmi iş sayıları ve bekleme durumlarını döndürür. Ana sayfadaki
**Veri Toplama Durumu** bölümü bunları gösterir. Yeni maç sayısı `created_at`;
güncelleme/kaynak kontrolü ayrı alanlardır. Fiyat tazeliği satır güncelleme zamanından
türetilmez. Eksik backend değeri arayüzde sıfır diye uydurulmaz.

| Ölçüm | Önce | Sonra |
| --- | --- | --- |
| Üretimde güncel kaynak fikstürü kapsamı | 3.494 / 3.494 | Aynı; üretime yazılmadı |
| Üretimde yeni kayıt / son 24 saat | 0 | Aynı; kayıt uydurulmadı |
| Gerçek kaynak kaydı 3073151, Crown 1X2 yerel tekrar testi | 0 / 3 son fiyat alanı | 3 / 3; yalnızca gözlenen alanlar yeni timestamp aldı |
| Model board'u olmadan fiyat kontrolü | Fiyat toplama adayı yok | 48 saatlik maç kaynak kuyruğuna giriyor |
| Dış günlük tetikleme kaçırıldığında | Worker kendi update işini açmıyor | 24 saatlik başarılı keşif / 6 saatlik hata aralığıyla idempotent fallback |
| Yerel lig formu | UEFA analizinde görünmüyor | Kesin kimlikli, ayrı ve sızıntısız bağlam |

Gerçek kaynak JSON'u `goaloo_3073151_20261015.json`, salt okunur ölçümler
`collection_audit_20261015.json` test fixtures'ında saklandı. Gerçek fiyat tekrar
testi izolasyon içinde; üretimde gerçekleşmiş bir iyileşme olarak sunulmuyor.

Python/SQLite ve PostgreSQL testleri yeni fiyat keşfi, eksik/boş yanıt, kısmi alan
koruma, kalıcı cooldown, eşzamanlı scheduler idempotency, tarih kesimi ve kesin takım
kimliğini doğrular. Mevcut round keşfi/aynı maçın tekrar eklenmemesi testleri korunur.
Chromium hem yeni izlemeyi ve yerel formu hem mevcut sekiz ekran boyutunu kontrol
eder. JavaScript testleri bilinmeyen değerleri, sıfır yeni maçla başarılı keşfi ve
yerel form gösterimini kapsar. Son CI sonuçları PR açıklamasında raporlanır.

Telegram, kalite eşikleri ve dinamik kupon seçim algoritması değiştirilmedi.
Master'a merge, Render deploy, gerçek Telegram mesajı veya üretim veri yazımı yok.
