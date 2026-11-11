# Kuponanalizii: tam PostgreSQL yedeği (elle başlatılan, şifreli)

Bu işlem canlı veritabanındaki **bütün şemayı ve verileri** `pg_dump` özel formatında dışa aktarır. Kaynak veritabanı **yalnızca okunur**; veri temizleme, model/kupon/Telegram/NVIDIA çağrısı yapmaz.

> **Durum:** İş akışını depoya eklemek tek başına yedek almaz. Önce aşağıdaki iki GitHub Actions secret tanımlanmalı, sonra iş akışı elle başlatılmalı. Başarılı çalışmanın ardından şifreli dosya indirilmeli. Hiçbir bağlantı adresini veya parolayı issue/PR/chat içine yapıştırmayın.

## 1. Gizli ayarları hazırlama (repo yöneticisi)

1. Render → **Kuponanaliziivv7** → **Connect / External Database URL** bölümünden aktif veritabanının URL'sini kopyalayın. Eski, durdurulmuş `betappv33` veya `betappv33333` veritabanlarının bağlantılarını **kullanmayın**.
2. GitHub → `donozoyout-ux/Bet-app213` → Settings → Secrets and variables → Actions → New repository secret:
   - `KUPON_BACKUP_DATABASE_URL`: aktif **kuponanaliziivv7** veritabanının dış bağlantı URL'si.
   - `KUPON_BACKUP_PASSPHRASE`: en az **24 karakter**, yüksek entropili, sadece sizde olan uzun şifre; ayrıca çevrimdışı şifre yöneticisinde saklayın.
3. İki secret değeri de kullanıcı arayüzüne veya koda yazılmaz. Kaynak adresinin host, veritabanı adı ve TLS gereksinimi çalışmadan önce kontrol edilir. Şifre bilinmeden arşivin kurtarılması mümkün değildir.

## 2. Yedek alma

GitHub → Actions → **Encrypted PostgreSQL full backup (manual)** → **Run workflow**, ana dal `master`.

İşlem:
- PostgreSQL 18 `pg_dump` ile **tam** şema+veri dökümü alır.
- Ayrı ve geçici PostgreSQL 18 veritabanına `pg_restore --exit-on-error` ile **gerçek geri yükleme** yapar; en az 12.000 maç kaydını doğrular.
- Yedeği GPG AES-256 ile şifreler; çözümleyerek SHA-256 içerik eşitliğini doğrular.
- GitHub Actions artifact'ine **yalnızca** `full.dump.gpg`, `full.dump.gpg.sha256`, `verified.txt` yükler. Şifresiz `full.dump` hiçbir artifact'a dahil edilmez; geçici dosya silinir.
- Artifact saklama talebi 60 gündür (depo politikasının izin verdiği süreye bağlı). **Mutlaka bilgisayara indirin ve ayrıca güvenli ikinci bir yerde tutun.**

Başarılı çalışma ve artifact indirme bitmedikçe yedek tamamlandı sayılmamalıdır.

## 3. Yedeği açma ve ileride geri yükleme

Linux, macOS veya WSL üzerinde GPG, `sha256sum` ve PostgreSQL 18 istemcileri kurulmuş olmalı. Artifact ZIP dosyasını özel bir klasöre çıkarın:

```bash
sha256sum -c full.dump.gpg.sha256
gpg --output full.dump --decrypt full.dump.gpg
pg_restore --list full.dump
```

**Yalnızca yeni/boş bir test veritabanına** restore edin:

```bash
pg_restore --exit-on-error --no-owner --no-acl --dbname="$EMPTY_TEST_DATABASE_URL" full.dump
```

Canlı üretim veritabanının üstüne doğrudan geri yükleme yapmayın. Yerel çözülmüş `full.dump` dosyasını güvenli biçimde saklayın/silin. Parolayı kimseyle paylaşmayın.

## İzlenmesi gereken kontroller

- Render'ın ücretsiz PostgreSQL planında yönetilen yedekler yok; veritabanı oluşturulduktan **30 gün** sonra sona eriyor.
- Bu örnekte aktif `Kuponanaliziivv7` için bildirilen son tarih: **23 Kasım 2026**. İşlem bundan önce tamamlanmalı.
- GitHub deponuz **public**. Yalnızca GPG ile şifreli arşiv artifact olarak saklanmalıdır; **şifresiz dump veya şifreyi repoya commit etmeyin.**
- İş akışı zamanlanmadı; elle başlatılana kadar hiçbir SQL işlemi yapmaz. Tamamlandıktan sonra gelecekte düzenli aralıklarla tekrarlanabilir.
