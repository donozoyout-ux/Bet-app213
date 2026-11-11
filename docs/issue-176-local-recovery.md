# Issue 176: kayıpsız yerel kurtarma doğrulaması

İlk PR yalnız owner-operated yerel kurtarma yoludur. Eski 90 commitlik dal
alınmaz. 319cdc13 içinden yalnız ZIP formatı, sabit ZIP/dump kimliği, yerel
konteyner ve envanter yardımcıları ayrıştırıldı. PR174'ün aşama/SQLSTATE fikri
uygulandı; sıkıştırma ve canlı bağlantı yolları taşınmadı.

## Teşhis

Eski `isolated_pg18_source_restore_and_compaction` etiketi üç ayrı işlemi
gizliyordu. Yeni rapor first_pg18_restore, first_inventory, second_pg18_restore,
second_inventory ve full_content_parity ayrımını yapar. psycopg RaiseException
SQLSTATE P0001 verir. Canlı katalogda immutable_history DELETE/UPDATE koruması
PR175'te doğrulandı. Sentetik PG18 testinde aynı koruma ve DELETE ile P0001
yeniden üretilir; yeni kurtarma yolu hiçbir DELETE çalıştırmaz. Gerçek ZIP'teki
hatanın hangi aşamada oluştuğu ancak owner çalıştırmasında kesinleşir.

Hata çıktısı yalnız sabit aşama ve beş karakterli SQLSTATE içerir. pg_restore
stderr veya SQL metni gösterilmez. Alt süreç SQLSTATE sağlamazsa `unavailable`
yazılır; P0001 tahmin edilmez. Sunucu log verbosity ayarı client stderr'de
SQLSTATE bulunacağını garanti etmez.

## Windows PowerShell 5.1

Docker Desktop Linux motoru açık, postgres:18 imajı önceden yerelde mevcut,
Python bağımlılıkları kurulu, 5433/5434 portları boş olmalı. Komut imaj indirmez.
Yolları gerçek **mutlak yerel yollarla** değiştirin; OutputRoot mevcut olmamalı.

```powershell
powershell.exe -NoProfile -File .\scripts\verify-offline-recovery.ps1 -Zip 'C:\Recovery\pg-recovery-vv755-owner-rescue-20261031-e.zip' -KeyFile 'C:\Recovery\recovery.key' -OutputRoot 'C:\Recovery\verify-176-new'
```

Wrapper yeni çıktı klasörünü yalnız mevcut Windows kullanıcısına açık ACL ile
oluşturur. Orijinal ZIP ve anahtar yalnız okunur; içerikleri yüklenmez/loglanmaz.
ZIP SHA256: `566e475ab72d3b4a6b05b88bbca9811c72f43d6bf7fea62927dff5d4fe626ae4`.
Kimliği sabit orijinal dump şifreli parçalardan iki kez doğrulanıp açılır.
İki ayrı, etiketli, loopback PostgreSQL18 konteynerinde restore yapılır.
Parolalar process argümanlarına yazılmaz. Yalnız bu çalışmanın etiketli
konteynerleri kaldırılır. Geçici açık dump normal çıkış/hata sırasında temizlenir;
makine kapanması gibi sert kesinti sonrası özel çıktı klasörü sahibi incelemelidir.

Her tablo satırının SHA256 çoklu-küme özeti (tekrarlar dahil), satır sayısı,
sequence durumu, large-object ID ve tüm sayfa içerikleri karşılaştırılır.
FK/constraint tanımları ve validation durumu karşılaştırılır; doğrulanmamış
constraint varsa durulur. JSON alanlarının tamamı satır fingerprint'ine dahildir;
bu JSON içindeki tüm uygulama referanslarının semantik geçerliliğini kanıtlamaz.
Foreign table varsa kapsam eksik sayılır ve durulur.

`verification.json` yalnız iki yerel restore'un eşitliğini kanıtlar. Eski
rescue manifest kaynak satır parity'si içermediği için canlı kaynakla parity,
GitHub download ve uygulama cold read-through **false** kalır. Temizlik kapısı
kapalıdır. Başarılı sentetik CI, owner ZIP çalıştırmasının yerini tutmaz.
Üretimde değişiklik, yeni üretim veritabanı, merge veya deploy yoktur.

## Test

Unit: SQLSTATE redaksiyonu, şifreli parça bozulması/kimlik bağlama, yanlış ZIP'in
yazmadan reddi, sıra bağımsız/tekrar duyarlı içerik özeti.
`Offline recovery PG18` CI: sentetik eski/yeni board, immutable trigger, FK,
sequence ve large object ile iki tam restore; bütün eski kayıtlar korunmalı.
Yerelde Docker testi açıkça etkin değilse skip edilir.
