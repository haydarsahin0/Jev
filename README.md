# arXiv Radar

Günde iki kez arXiv'deki yeni AI makalelerini tarayan, TypeSafe'in **Jev**
modeliyle değerlendiren ve sonuçları GitHub Pages'te yayınlayan tek kullanıcılık
bir takip sistemi.

Jev her makale için metin değil, **tipli kararlar** döndürür: yenilik ve kanıt
gücü için sıralı rubrik puanı, uygulanabilirlik/ilgi/kod/abartı için olasılık,
konu için tek seçim. Site bu sinyalleri sizin ağırlıklarınızla tek bir puana
çevirir ve listeyi ona göre sıralar.

> **Depoda ikinci bir sistem var:** [**Jev Trader**](trader/README.md) — Bybit
> vadeli işlemlerinde aynı deseni kullanan kaldıraçlı bir bot. Jev'e yön,
> kanaat ve risk sorar; emri `trader/risk.py` içindeki deterministik politika
> hesaplar. Varsayılan mod kâğıt (emir gönderilmez); gerçek para ancak
> `BYBIT_ALLOW_LIVE=yes` ile açılır. Panel: `site/trader.html`.

---

## Mimari

```
GitHub Actions  (cron: her 12 saatte bir, 05:00 & 17:00 UTC  +  elle çalıştırma)
│
├─ 1. pipeline/radar.py
│      arXiv API ──► son 48 saat, görülmemiş makaleler
│                └─► Jev (POST /v1/systemone, makale başına bir istek, 4 paralel)
│                     └─► site/data/radar-YYYY-MM-DD.json
│
├─ 2. site/data/index.json yeniden yazılır (mevcut günlük dosyaların listesi)
│      60 günden eski günlük dosyalar silinir ve index'ten çıkarılır
│
├─ 3. site/data/ altındaki değişiklikler commit + push edilir
│
└─ 4. AYNI workflow içinde site/ klasörü Pages'e deploy edilir
       (actions/upload-pages-artifact ──► actions/deploy-pages)

GitHub Pages  (statik site, build adımı yok)
└─ site/index.html ──fetch──► data/index.json ──► data/radar-*.json
```

Deploy'un neden aynı workflow'da olduğu: `GITHUB_TOKEN` ile yapılan bir push
başka workflow'ları **tetiklemez**. Tarama ayrı, deploy ayrı workflow olsaydı
site hiç güncellenmezdi.

### Dosyalar

| Yol | Ne yapar |
| --- | --- |
| `pipeline/radar.py` | Tarayıcı. Sadece standart kütüphane, harici paket yok. |
| `.github/workflows/radar.yml` | Zamanlama, tarama, commit ve Pages deploy. |
| `site/index.html` | Tek dosyalık arayüz (vanilla JS, build adımı yok). |
| `site/data/` | Üretilen veri. İlk çalıştırmadan önce boştur. |
| `tests/` | `radar.py` birim testleri (unittest). |

---

## Kurulum

Üç adım, hepsi bir kere.

### 1. API anahtarını secret olarak ekleyin

Depoda **Settings → Secrets and variables → Actions → New repository secret**:

- Name: `TYPESAFE_API_KEY`
- Secret: TypeSafe API anahtarınız

Anahtar yalnızca workflow'un tarama adımında, `Authorization: Bearer …`
başlığında kullanılır. Koda, loglara, üretilen JSON'a veya siteye **hiçbir
şekilde** girmez.

### 2. Pages'i açın

**Settings → Pages → Build and deployment → Source:** `GitHub Actions` seçin.
(`Deploy from a branch` **değil** — bu workflow artifact yükleyerek deploy eder.)

### 3. İlk çalıştırmayı ucuza yapın

**Actions → arXiv Radar → Run workflow**, `limit` kutusuna `3` yazın ve
çalıştırın. Bu yalnızca 3 makale değerlendirir; boru hattının ucundan ucuna
çalıştığını birkaç kuruşa doğrulamış olursunuz.

Çalışma bitince site şu adreste yayında olur:

```
https://<kullanıcı-adınız>.github.io/<depo-adı>/
```

Sonrasında her 12 saatte bir (05:00 ve 17:00 UTC) kendiliğinden çalışır.

> `limit` boş bırakılırsa günlük sınır olan `MAX_PAPERS` (120) geçerli olur.
> `mock` kutusu işaretlenirse API çağrısı yapılmaz, sahte sinyal üretilir ve
> veriye `demo: true` yazılır — sitede "deneme verisi" uyarısı çıkar.

---

## Ayarlar nerede

### `pipeline/radar.py` — dosyanın en üstü

| Sabit | Varsayılan | Ne işe yarar |
| --- | --- | --- |
| `CATEGORIES` | `cs.AI, cs.CL, cs.LG, cs.SD` | Taranan arXiv kategorileri |
| `WINDOW_HOURS` | `48` | Kaç saat geriye bakılır |
| `MAX_RESULTS` | `100` | Her kategori için arXiv'den çekilen kayıt |
| `MAX_PAPERS` | `120` | Günde değerlendirilecek en fazla makale (maliyet sınırı) |
| `WORKERS` | `4` | Aynı anda kaç Jev isteği |
| `SEEN_LIMIT` | `5000` | `seen.json` içinde tutulan ID sayısı |
| `RETENTION_DAYS` | `60` | Bundan eski günlük dosyalar silinir |
| `READER` | öğrenci geliştirici tanımı | `relevance` sorusunun kime göre sorulduğu |
| `TOPICS` | 8 konu | `topic` sorusunun seçenekleri |
| `QUESTIONS` | 7 soru | Jev'e sorulan soruların tam metni |

`READER`'ı kendinize göre yazmak en çok işe yarayan ayardır: `relevance`
sinyali doğrudan bu tanıma bakar.

### `site/index.html` — script bloğunun başı

| Sabit | Varsayılan | Ne işe yarar |
| --- | --- | --- |
| `SIGNALS[].weight` | 0.30 / 0.20 / 0.30 / 0.20 / 0.10 / 0.30 | Varsayılan ağırlıklar |
| `INITIAL_DAYS` | `14` | Açılışta yüklenen gün sayısı |
| `MORE_DAYS` | `14` | "Daha eski günleri yükle" her basışta |
| `TIER_READ` | `70` | "Mutlaka oku" eşiği |
| `TIER_SKIM` | `50` | "Göz at" eşiği |

Ağırlıkları kalıcı olarak değiştirmek için buradaki `weight` değerlerini
düzenleyin. Geçici denemeler için sitedeki kaydırıcıları kullanın — onlar
tarayıcınızda saklanır ve kodu değiştirmez.

### Puan nasıl hesaplanıyor

```
puan = ( Σ pozitif_ağırlık × sinyal  −  abartı_ağırlığı × abartı )
       / Σ pozitif_ağırlık                              → 0–100 arasına sıkıştırılır
```

Pozitif sinyaller: yenilik, kanıt, uygulanabilirlik, ilgi, kod. Abartı (`hype`)
puanı düşürür. Kaydırıcıyı oynattığınız anda liste yeniden sıralanır
(`prefers-reduced-motion` açıksa animasyonsuz).

### Zamanlamayı değiştirmek

`.github/workflows/radar.yml` içindeki `cron: "0 5,17 * * *"` satırı. Saat
**UTC**'dir; Türkiye saati için 3 saat ekleyin (05:00 ve 17:00 UTC = 08:00 ve
20:00 TSİ). Günde bir kez yeterliyse `"0 5 * * *"`, sadece hafta içi için
`"0 5,17 * * 1-5"` yazın.

---

## Notlarınız nerede duruyor

**Durumlar** (Yeni / Okunacak / Önemli / Okundu / Atlandı), **notlar** ve
**ağırlıklar** yalnızca kullandığınız tarayıcının `localStorage`'ında tutulur.

Bunun anlamı:

- Sunucuya, depoya veya başka bir yere gitmezler. Sadece sizde kalırlar.
- **Başka bir tarayıcıya veya cihaza geçmezler.**
- Tarayıcı verisini temizlerseniz kaybolurlar.

Bu yüzden iki düğme var:

- **Notlarımı dışa aktar** — durumları, notları ve ağırlıkları tek bir JSON
  dosyası olarak indirir.
- **Yedeği geri yükle** — o dosyayı geri yükler (onay ister, mevcutların
  üzerine yazar).

Cihaz değiştirmeden veya tarayıcı verisini temizlemeden önce dışa aktarın.
Depolama tamamen kapalıysa sayfa yine çalışır, sadece not ve durumlar
sayfa yenilenince kaybolur.

---

## Yerel çalıştırma

```bash
# Birim testleri
python -m unittest discover -s tests -v

# Sahte sinyallerle veri üret (API anahtarı gerekmez, demo: true yazar)
python pipeline/radar.py --mock

# Siteyi aç
python -m http.server -d site 8000
# → http://localhost:8000
```

Faydalı bayraklar:

| Bayrak | Ne yapar |
| --- | --- |
| `--mock` | Jev'i çağırmaz, makale ID'sinden türetilmiş sabit sahte sinyal üretir |
| `--limit N` | En fazla N makale değerlendirir |
| `--data-dir YOL` | Çıktıyı başka bir klasöre yazar |
| `--xml DOSYA` | arXiv'e istek atmak yerine bir Atom XML dosyasını okur (çevrimdışı deneme) |

Yerel denemeden sonra `site/data/` altındaki sahte dosyaları silmeyi unutmayın —
`demo: true` verisi commit edilmemeli.

---

## Veri biçimi

**`site/data/radar-YYYY-MM-DD.json`**

```json
{
  "format": "arxiv-radar/1",
  "generated_at": "2026-09-16T05:00:12Z",
  "demo": false,
  "papers": [
    {
      "id": "2609.01234",
      "title": "…",
      "abstract": "…",
      "authors": ["…"],
      "published": "2026-09-16T05:30:00Z",
      "categories": ["cs.AI", "cs.CL"],
      "url": "https://arxiv.org/abs/2609.01234v1",
      "pdf": "https://arxiv.org/pdf/2609.01234v1",
      "signals": {
        "novelty": 0.5, "evidence": 0.85, "practical": 0.73,
        "relevance": 0.61, "code": 0.94, "hype": 0.12
      },
      "topic": "Retrieval and RAG",
      "topic_confidence": 0.62,
      "novelty_confidence": 0.8
    }
  ]
}
```

**`site/data/index.json`** — en yeni gün başta:

```json
{
  "updated_at": "2026-09-16T05:00:12Z",
  "files": [{ "date": "2026-09-16", "path": "data/radar-2026-09-16.json", "count": 37 }]
}
```

**`site/data/seen.json`** — son 5000 makale ID'si, tekrar değerlendirmeyi önler.
Bir makale hata yüzünden atlanırsa görülmüş sayılmaz, ertesi gün tekrar denenir.

### Sinyaller nasıl normalize ediliyor

Jev iki farklı tipte cevap döndürür:

- **noul** (`practical`, `relevance`, `code`, `hype`) — zaten 0–1 arası kalibre
  bir olasılık, olduğu gibi alınır. *Noul cevapları tasarım gereği `confidence`
  taşımaz; olasılığın kendisi sinyaldir.*
- **score** (`novelty`, `evidence`) — sıralı rubrik üzerinde olasılık ağırlıklı
  konum, tam sayı değil (3 seviyeli bir rubrikte `1.7` normaldir). En büyük
  seviye indeksine, yani `seviye_sayısı − 1`'e bölünerek 0–1'e çekilir.
- **choice** (`topic`) — seçilen etiket, `confidence` ile birlikte.

---

## arXiv isteği neden kategori başına ayrı?

arXiv API'si `search_query=cat:cs.AI+OR+cat:cs.CL` gibi **OR'lu sorguları
`406 Not Acceptable` ile reddediyor.** Bu ölçülerek bulundu: GitHub runner'ında
arka arkaya 11 farklı OR sorgusu (farklı `Accept`/`User-Agent` başlıkları, `+`
ve `%20` kodlaması, parantezli hâli, sıralamalı ve sıralamasız) hepsi 406
verdi; hemen ardından tek kategorili `search_query=cat:cs.AI` başlıksız bile
200 döndü.

Bu yüzden `radar.py` her kategoriyi ayrı istekle çeker, aralarında en az 3
saniye bekler ve sonuçları yerelde ID'ye göre birleştirir. Bir kategori
başarısız olursa diğerleri yine işlenir.

arXiv yük altında 429 yerine de 406 dönebiliyor (aynı koşuda tek başına
çalışan bir kategori sorgusu bir kez 406 verdi), o yüzden 406 geçici kabul
edilip yeniden denenir; `400` gibi gerçekten istek kaynaklı hatalar denenmez.

---

## Güvenlik notları

- `TYPESAFE_API_KEY` yalnızca GitHub Secret'tır. Workflow'da tek bir adımın
  ortamına verilir; loglara, JSON çıktısına ve siteye asla yazılmaz.
- Site **hiçbir harici API çağırmaz.** Yalnızca kendi `data/` dosyalarını okur
  (bir de Google Fonts'tan yazı tipi; fontlar engellenirse site yine çalışır).
- Veriden gelen bütün metinler (başlık, özet, yazar, konu) DOM'a `textContent`
  ile yazılır — HTML olarak yorumlanmaz.
- Bağlantılar yalnızca `http`/`https` şemasındaysa link olur; başka bir şema
  sessizce atılır.
- Workflow yalnızca `site/data` altını commit eder, başka hiçbir dosyayı değil.
