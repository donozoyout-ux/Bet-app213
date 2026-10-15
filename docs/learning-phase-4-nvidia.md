# Faz 4 — NVIDIA AI ders anlatıcısı

## Mimari ve güven sınırı

`LearningReport` ve `LearningLesson` değişmez kaynak olarak kalır. `learning_ai.evidence()` rapor/ders ilişkisini, yöntem sürümünü, veri parmak izini, sonlu sayısal alanları, örneklem sınırlarını ve güven aralığını doğrular. Kaynakta olmayan değer hesaplanmaz; eksik değer sıfıra çevrilmez. Kapsam metrikleri ile raporun genel Brier/log loss değerleri ayrı alanlardadır.

NVIDIA'ya yalnızca bu sınırlı sayı/durum paketi gönderilir. Başlık, lig adı, serbest kanıt metni, takım adı, ham tahmin, scraper çıktısı veya kullanıcı metni gönderilmez. Bu seçim prompt injection yüzeyini daraltır.

**Kontrollü dil üretimi:** Model serbest bir yorum yazmaz; yedi bölüm için, kanıta göre backend'in izin verdiği ikişer denetlenmiş Türkçe cümleden birini seçerek açıklama oluşturur. Bu bilinçli bir kapsam sınırıdır: genel amaçlı bir LLM'in serbest metnindeki bütün kanıt dışı iddiaları bir regex ile güvenilir biçimde doğrulamak mümkün değildir. Burada kabul edilen her cümle bilinen bir anlam taşır; yeni bir sayısal, nedensel, avantaj veya kesin öğrenme iddiası kabul edilmez. Gerçekleşen başarının beklenenin altında/üstünde/eşit olduğu ifade grubu da kayıtlı oranlarla backend tarafından sınırlandırılır. Modelin katkısı izin verilen Türkçe açıklamayı birleştirmektir; yeni istatistik veya yeni ders üretmek değildir.

Yanıt tam JSON nesnesi, tam yedi alan, her alanda izin verilen tam cümle ve en çok 3000 karakter olmalıdır. Ek alan, yinelenen JSON alanı, sayı, HTML, URL, bozuk JSON, kesilmiş çıktı veya izin listesi dışı iddia reddedilir. `insufficient_sample` durumunda yalnızca yetersiz örneklem/gözlem açıklaması mümkündür. `supported_candidate` için kaynak örneklem ve fark aralığı ayrıca kontrol edilir; bu durumda da kesinleşmiş ders veya gelecek kazanç iddiası mümkün değildir. Sayıları LLM döndürmez; backend doğruladığı `facts` alanını ayrıca ekler.

## Kayıtlar ve sürümleme

- `learning_ai_explanations`: rapor/ders FK'leri, model, prompt sürümü, kanıt paketi SHA-256 özeti, kabul edilmiş bölümler, backend sayıları, oluşturulma zamanı. `(lesson_id, prompt_version)` benzersizdir. SQLite ve PostgreSQL trigger'ları UPDATE/DELETE'i engeller.
- `learning_ai_tasks`: kalıcı görev, deneme sayısı, son güvenli hata kodu, sonraki deneme zamanı. Kuyruk ve toparlanma sorguları indekslidir.
- `learning_ai_quotas`: UTC günü başına, çağrı **öncesinde** commit edilen rezervasyon sayısı. Başarısız ve belirsiz denemeler de kotayı tüketir.
- `learning_ai_gates`: bütün worker'ların paylaştığı sağlayıcı backoff zamanı.

Yeni rapor farklı ders kimlikleri oluşturur ve yeni açıklamalar alır; eski açıklama silinmez veya değiştirilmez. Model değişikliği tek başına bitmiş açıklamayı yeniden üretmez. Aynı dersin bilinçli yeniden üretimi yeni bir kodlanmış `PROMPT_VERSION` gerektirir. Böylece model ayarıyla yanlışlıkla bütün rapora yeniden ücretli çağrı yapılmaz.

## Arka plan ve maliyet sınırları

HTTP handler ve sayfa açılışı üretim başlatmaz. Mevcut worker runner'ında ayrı, düşük öncelikli bir task çalışır. AI kapalıysa bu task ve ayrı DB havuzu hiç oluşturulmaz. Açık durumda AI havuzu, scraper/Telegram/öğrenme havuzlarından ayrıdır. PostgreSQL advisory lock `213067`, tüm replica'larda **tek eşzamanlı sağlayıcı HTTP çağrısı** sağlar. Lock bağlantısı boyunca açık yazma transaction'ı tutulmaz; sağlayıcı beklenirken görev session'ı kapanmıştır. Üretimde en fazla lock için bir bağlantı ve kısa kayıt işlemleri için bir bağlantı gerekir. SQLite kilidi yalnızca test/yerel kullanım içindir; üretim PostgreSQL ister.

| Kontrol | Sınır |
| --- | --- |
| Varsayılan | Kapalı, günlük limit sıfır, model ve anahtar boş |
| Günlük rezervasyon | Açıkça verilen 1–1000; başlangıç önerisi 10 |
| Worker tick | 60 saniye; tick başına en fazla bir çağrı |
| Eşzamanlılık | PostgreSQL genelinde 1 |
| Her görev | En çok 3 deneme; deneme sayısı DB CHECK ile de sınırlı |
| Zaman aşımı | Bağlanma 5 s, HTTP 25 s, toplam 30 s |
| Kanıt + ifade seçenekleri | En çok 8192 UTF-8 byte; sabit sistem mesajı ve JSON zarfı ayrıca küçük/sınırlıdır |
| Yanıt gövdesi | En çok 16384 byte; akış okunurken sınır uygulanır |
| Çıktı token tavanı | Çağrı başına 800 |
| Geçici hata | Yalnızca 429/500/502/503/504 ve transport/timeout yeniden denenir |
| Backoff | En az 5 dakika, sonraki denemelerde 10/20 dakika; sayısal Retry-After 5 dakika–24 saat aralığında kullanılır |
| Kalıcı hata / biçim reddi | Görev terminal olarak reddedilir; global sağlayıcı kapısı 15 dakika bekler |

Limit 10 ise tüm hata/yeniden denemeler dahil günde en fazla 10 rezervasyon ve yapılandırılmış çıktı tavanı toplam 8000 token'dır. Bu bir parasal fiyat garantisi değildir: sağlayıcı ücretleri ve input token sayıları modele/sözleşmeye bağlıdır. Uygulama gerçek faturalamayı tahmin etmez. Commit edilmiş fakat gönderilememiş çağrı da güvenli tarafta kalmak için rezervasyon tüketir.

Süreç kaybında `running` rezervasyonun sağlayıcı tarafından işlenip işlenmediği bilinemez. On dakika sonra görev `uncertain` olur ve **otomatik yeniden gönderilmez**. Sağlayıcı idempotency garantisi varsayılmaz. Tamamlanan görev/sonuç tek transaction'da yazılır. İnceleme olmadan `running/uncertain` kayıtları resetlenmemelidir. Eski raporların yarım kalan işleri yerine güncel LearningHead işlenir; eski kabul edilmiş metinler arşivde tutulur.

Sağlayıcı kesintisi/kota/bozuk çıktı durumunda mevcut rapor endpoint'leri çalışır. Ders endpoint'i yalnızca kayıtlı açıklamayı toplu okur; açıklama yoksa `not_prepared`, yapılandırma kapalıysa `disabled` döner. UI'daki ayrı **AI Açıklaması** bölümü dayanak raporu, model/prompt sürümünü ve tarihi gösterir. Metinler `textContent` ile yazılır. Açıklaması bekleyen raporun mevcut sayfa yenilemesi, istatistikleri tekrar indirmeden yalnızca ders kayıtlarını okur.

## Güvenlik denetimi

- Anahtar yalnızca proses ortamından okunur; config repr'inde gizlidir, DB modelinde alanı yoktur.
- Anahtar URL'ye veya prompt'a eklenmez; sadece sabit HTTPS endpoint'inin Authorization başlığına konur. Redirect takip edilmez, ortam proxy ayarları kullanılmaz.
- Sağlayıcının hata gövdesi okunmaz, kaydedilmez veya loglanmaz. Başarı gövdesi yalnızca sınırlı bellekte doğrulanır; ham gövde/headers/reasoning/usage kaydedilmez.
- HTTP istemcilerinin debug logger kayıtlarına ayrıca NVIDIA anahtarı maskeleme filtresi uygulanır; diğer istemcilerin log seviyeleri değiştirilmez.
- Exception metni/traceback, request veya config uygulama tarafından loglanmaz. Görevlerde yalnızca uygulamanın sabit hata kodları bulunur.
- Model adı uzunluk/karakter açısından doğrulanır; anahtarı içeriyorsa özellik kapalı sayılır.
- Kaynak öğrenme tabloları, olasılıklar ve sonuçlar güncellenmez. AI yalnızca kendi görev/kota/açıklama tablolarına yazar.
- Test ortamı NVIDIA anahtarını temizler ve özelliği kapatır. Bütün sağlayıcı testleri `httpx.MockTransport` kullanır; gerçek çağrı yapılmaz.

## Render ortam değişkenleri kurulum rehberi

Bu PR Render ortamını değiştirmez ve deploy başlatmaz. `.env.example` güvenli varsayılanları içerir. Model adının runtime varsayılanı yoktur.

Önerilen başlangıç modeli: `qwen/qwen3-next-80b-a3b-instruct`. Bu kimlik [NVIDIA chat completions referansında](https://docs.api.nvidia.com/nim/reference/qwen-qwen3-next-80b-a3b-instruct-infer) listelenir. Instruct çeşidi kısa, yapılandırılmış yanıt için seçilmiştir. [Qwen3 dil açıklaması](https://qwenlm.github.io/blog/qwen3/) Türkçeyi desteklenen diller arasında sayar; [Next model kartı](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct) model ailesini açıklar. Bu PR'da gerçek sağlayıcı kalite/erişim denemesi yapılmadı. Türkçe kullanıcı metni izin listesiyle denetlendiği için modelin serbest çevirisine güvenilmez. Başka bir desteklenen chat-completions modeli `NVIDIA_MODEL` ile seçilebilir.

| Render değişkeni | Güvenli başlangıç | Sonradan açık etkinleştirme |
| --- | --- | --- |
| `NVIDIA_AI_ENABLED` | `false` | `true` |
| `NVIDIA_API_KEY` | Yok/boş | NVIDIA hesabında oluşturulan gizli anahtar |
| `NVIDIA_MODEL` | Boş | Seçilen tam model kimliği |
| `NVIDIA_DAILY_REQUEST_LIMIT` | `0` | Örneğin `10` |

1. İleride yetkili kurulum sırasında Render servisinin Environment alanına bu dört değişkeni ekleyin. Anahtarı chat, Git, PR, terminal komutu veya frontend'e yapıştırmayın; gizli değer olarak Dashboard'a girin.
2. Önce `NVIDIA_AI_ENABLED=false` ile kaydedin. Anahtar/model/pozitif limit birlikte doğrulanmadan etkinleştirmeyin. `SCRAPER_WORKER_ENABLED=true` olan mevcut production runner gerekir; Telegram saatleri değiştirilmez.
3. Etkinleştirme ve ortam değişikliklerini uygulayan restart/deploy ayrı bir operatör işlemidir; bu çalışma kapsamında yapılmaz. Render'ın **Save only** seçeneği deploy başlatmadan değerleri saklamak için kullanılabilir. Blueprint'te `sync: false` mevcut servise sonradan anahtar eklemez; bu PR bu nedenle canlı Blueprint değerlerini değiştirmez.
4. İlk çalışma sonrasında `learning_ai_tasks` durumlarını ve UTC günlük `learning_ai_quotas.requests` sayısını salt okunur sorgularla kontrol edin. Kartlarda dayanak rapor ve “AI anlatımı” ayrımını doğrulayın. Yanıt biçimi reddediliyorsa limiti artırarak kör tekrar yaptırmayın.
5. Kapatmak için `NVIDIA_AI_ENABLED=false` uygulayın. Tek başına anahtar eklemek etkinleştirmez. Eksik/geçersiz model, anahtar veya limit de fail-closed davranır.

## Test kapsamı

Sahte NVIDIA ile üretim → kalıcı kayıt → salt okunur API → Chromium UI zinciri; sayı/yazıyla sayı/HTML/kazanç iddiası reddi; kaynak parmak izi/yöntem uyuşmazlığı; prompt sürümü; gerçek settlement düzeltmesi sonrası yeni rapor; retry/backoff/timeout/kota/gün geçişi; süreç iptali ve belirsiz rezervasyon; PostgreSQL genel kilidi ve değişmez kayıt trigger'ları; ayrı havuz temizliği ve kapalı durumda havuz açılmaması test edilir. AI UI 320/768/1536 genişliklerinde, mevcut Dersler testleri ayrıca 320/390/768/1024/1536 genişliklerinde çalışır. Tam Python/PostgreSQL/Chromium ve mevcut JavaScript regresyonları teslimatta raporlanır.
