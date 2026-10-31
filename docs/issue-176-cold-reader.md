# Issue 176: sabit GitHub geçmiş snapshot okuyucusu

İkinci bağımsız PR, güncel master tabanlıdır. `/history-archive` sayfası ve
`/api/history/archive` gerçek FastAPI uygulamasına bağlanır. PostgreSQL bağlantısı
almadan özel GitHub Releases içindeki şifreli geçmiş görünümünü okur.
Tarih (Europe/Istanbul), match_id, board_version ve revizyon korunur.
Sayfalama manifest ciphertext SHA256 kimliğine sabitlenir; sayfalar arasında
snapshot değişirse açık hata alınır. Aynı maç/sürüm/revizyon tekrar edemez.

Bu okuyucu tam kurtarma dump'ının yerine geçmez. İndekslenmiş, uygulamaya açık
maç/odds/evidence projeksiyonudur. Tam ilişkisel arşiv ve kupon/öğrenme/JSON
bağımlılık kapanışı ayrıca korunmalıdır. Mevcut `/api/matches`, kupon ve analiz
uçlarına otomatik sıcak/soğuk birleşim henüz yapılmaz. Bu nedenle cleanup kapısı
her yanıtta false kalır; bu PR tüm Issue176'nın tamamlandığı anlamına gelmez.

## Sunucu yapılandırması (varsayılan kapalı)

- HISTORY_ARCHIVE_ENABLED=true
- HISTORY_ARCHIVE_REPOSITORY=owner/private-repository
- HISTORY_ARCHIVE_TOKEN: yalnız sunucuda, private contents read yetkisi
- HISTORY_ARCHIVE_KEY: base64 32-byte anahtar, yalnız sunucuda
- HISTORY_ARCHIVE_MANIFEST_ASSET: pozitif GitHub release asset ID
- HISTORY_ARCHIVE_MANIFEST_SHA256: sahibin doğruladığı manifest ciphertext hash

Bu PR ortam değişkeni ayarlamaz, anahtar/ZIP yüklemez, arşiv yayımlamaz veya deploy
etmez. Gerçek arşiv projeksiyonu sahibi tarafından üretildikten ve tam kurtarma
kanıtıyla ilişkilendirildikten sonra ayrı etkinleştirme değerlendirilmelidir.

## Şifreli görünüm sözleşmesi

AES256-GCM, 12-byte nonce + ciphertext/tag. Sıkıştırma yok; parça en çok 8 MiB.
Manifest AAD `history-read-v1:manifest`. Manifest alanları:
schema=history-read-v1, timezone=Europe/Istanbul, entries (en çok 50.000).
Her entry: day (YYYY-MM-DD), match_id, board_version (64 hex), revision,
asset (pozitif ID), sha256 (ciphertext checksum).
Kaydın AAD'si `history-read-v1:{match_id}:{board_version}:{revision}`.
Kayıt aynı dört kimlik alanını, mevcut MatchResponse şemasında `match` alanını
(odds dahil) ve tipli `evidence` listesini içerir. Kanıt alanları:
revision, market, selection, probability, outcome (won/lost/void/pending),
recorded_at, source, home_score, away_score. Önceki revizyon üzerine yazılmaz.
Kimlik, checksum, şifre doğrulaması veya İstanbul tarih eşleşmesi hatasında 503.
API key/token, raw odds payload ve bilinmeyen kayıt alanlarını tarayıcıya vermez.

Token yalnız api.github.com'a gönderilir. Asset redirect yalnız iki GitHub asset
hostname'ine HTTPS olarak ve Authorization başlığı olmadan izlenir; ikinci
redirect reddedilir. Tüm istekler GET. Günlükler secret/yanıt/SQL içermez.
İstek toplam süresi 30 saniye; HTTP timeout 15 saniye; sayfa en çok 50 kayıt
ve 16 MiB indirilen parça. Süreç başına en çok iki eşzamanlı okuma.
Kalıcı cache yok: eski/başarısız doğrulama sessiz başarıya dönmez.

## Test ve henüz doğrulanmayanlar

MockTransport ile indirilen şifreli manifest/parçalar gerçek ASGI uygulamasından
okunur; boş/erişilmeyen PostgreSQL'e bağımlılık yok. İki revizyon, tarih sınırı,
oran ve sonuç kanıtı, pagination, token redaksiyonu, signed redirect, bozuk
ciphertext, tekrarlanan kimlik, kesinti ve varsayılan kapalı durum test edilir.
Bu sentetik testler gerçek GitHub yükleme/download veya owner ZIP doğrulaması
olarak sunulmaz. Canlı kaynaktan tam satır parity ve kupon dahil tüm uygulama
geçmiş yolları tamamlanmadan retention için izin verilemez.
