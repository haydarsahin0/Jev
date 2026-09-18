# Jev boru hatları

Bu depoda TypeSafe'in **Jev** modeliyle çalışan iki sistem var. İkisi de aynı
fikre dayanır: Jev'den metin değil, **tipli karar** istemek.

| Sistem | Ne yapar | Bölüm |
| --- | --- | --- |
| arXiv Radar | Yeni AI makalelerini tarar, Jev'e değerlendirtir, Pages'te yayınlar | [aşağıda](#arxiv-radar) |
| Skor Tahmini | Geçmiş maçlarla model eğitir, bültendeki maçların 1/X/2 ve skor olasılıklarını verir | [aşağıda](#skor-tahmini) |

---

# arXiv Radar

Günde iki kez arXiv'deki yeni AI makalelerini tarayan, TypeSafe'in **Jev**
modeliyle değerlendiren ve sonuçları GitHub Pages'te yayınlayan tek kullanıcılık
bir takip sistemi.

Jev her makale için metin değil, **tipli kararlar** döndürür: yenilik ve kanıt
gücü için sıralı rubrik puanı, uygulanabilirlik/ilgi/kod/abartı için olasılık,
konu için tek seçim. Site bu sinyalleri sizin ağırlıklarınızla tek bir puana
çevirir ve listeyi ona göre sıralar.

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

---

# Skor Tahmini

Geçmiş maç sonuçlarıyla eğitilen bir gol modeli, üstüne Jev'in tipli kararı.
Çıktı, bültendeki her maç için: **1 / X / 2 olasılıkları**, en olası skorlar,
beklenen toplam gol, alt/üst ve karşılıklı gol.

## Önce dürüst olan kısım: "Jev'i eğitmek" ne demek

Jev'in ince ayar (fine-tuning) uç noktası **yok**. `POST /v1/systemone`
ağırlıkları güncellenen bir model değil, verdiğiniz `state`'e tipli cevap
veren bir karar API'si. "Geçmiş maçlarla Jev'i eğitmek" cümlesinin
karşılığı bu yüzden şu iki katman:

1. **İstatistik model — gerçekten eğitilir.** Geçmiş maçlardan, zaman
   ağırlıklı Dixon-Coles Poisson kestirimiyle her takımın hücum/savunma
   gücü, ligin ev avantajı ve düşük skor düzeltmesi `rho` çıkarılır. Bunlar
   maksimum olabilirlikle bulunan gerçek parametrelerdir.
2. **Jev — eğitilmez, kalibre edilir.** Her maçta Jev'e istatistik modelin
   çıktısı *zaten verilir*; ondan sıfırdan tahmin değil, "bu taban neyi
   kaçırıyor" sorusuna tipli cevap istenir. Jev'in nihai sonuçtaki ağırlığı
   geçmiş maçlar üzerinde ölçülerek bulunur — yani öğrenilen şey Jev'in
   ağırlıkları değil, **Jev'e ne kadar güvenileceği**.

Bu ayrım kozmetik değil: sistemin ölçülebilir kısmı 1. katman, 2. katmanın
katkısı ise ancak yeterli maçta ölçülürse kabul ediliyor (aşağıda).

---

## Mimari

```
python -m pipeline.soccer …
│
├─ fetch ──► openfootball / football-data.co.uk ──► site/data/football/matches-<lig>.json
│              (aynı kulübün farklı yazımları tek ada bağlanır)
│
├─ train ──► Dixon-Coles MLE (Adam + rho araması) + Elo
│              └─► ileri yürüyen backtest ──► site/data/football/model-<lig>.json
│
├─ backtest ─► her maç, YALNIZCA o tarihten önceki veriyle tahmin edilir
│              --with-jev ile Jev de çağrılır ve harman ağırlığı öğrenilir
│
└─ predict ─► bülten satırları ──► özellik demeti ──► Jev (maç başına 1 istek)
                 └─► harman ──► skor ızgarası ──► site/data/football/bulten-<gün>.json
```

### Dosyalar

| Yol | Ne yapar |
| --- | --- |
| `pipeline/soccer/data.py` | Veri sağlayıcıları, takım adı eşleme, depolama |
| `pipeline/soccer/ratings.py` | Dixon-Coles MLE ve Elo — **eğitim burada** |
| `pipeline/soccer/grid.py` | Skor ızgarası, 1X2/AÜ/KG, ters çözüm |
| `pipeline/soccer/features.py` | Maç öncesi özellik demeti (sızıntısız) |
| `pipeline/soccer/jev.py` | Tipli sorular, `state`, yanıt okuma, önbellek |
| `pipeline/soccer/blend.py` | Taban + Jev harmanı, nihai tahmin kaydı |
| `pipeline/soccer/backtest.py` | İleri yürüyen değerlendirme ve kalibrasyon |
| `pipeline/soccer/cli.py` | `fetch` / `train` / `backtest` / `predict` |
| `.github/workflows/skor.yml` | Elle çalıştırılan boru hattı (cron kapalı gelir) |
| `tests/test_soccer.py` | 98 birim testi |

---

## Beş dakikada çalıştırma

```bash
# 1. Son 6 sezonun maçlarını indir (anahtar gerekmez)
python -m pipeline.soccer fetch --league tr.1 --seasons 6

# 2. Modeli eğit + geriye dönük test et
python -m pipeline.soccer train --league tr.1

# 3. Bülteni tahmin et — anahtarsız denemek için --mock
python -m pipeline.soccer predict --league tr.1 --days 7 --mock

# 4. Gerçek Jev ile (TYPESAFE_API_KEY gerekir, maç başına 1 istek)
export TYPESAFE_API_KEY=…
python -m pipeline.soccer predict --league tr.1 --days 7 --detail
```

Çıktı hem ekrana tablo olarak basılır hem de
`site/data/football/bulten-YYYY-AA-GG.json` dosyasına yazılır.

```
tarih      ev                     deplasman                   1     X     2  skor top    AÜ2.5  KG
--------------------------------------------------------------------------------------------------------
2026-09-20 Galatasaray            Trabzonspor              0.66  0.26  0.08  1-0   2.33    0.41  0.36
2026-09-20 Fenerbahçe             Beşiktaş                 0.57  0.24  0.19  1-1   3.07    0.59  0.58
```

`--detail` her maç için tabanı, Jev'in cevabını, harmanı, adil oranları ve
en olası altı skoru ayrı ayrı gösterir.

---

## Bülteninizi verme

Depodaki fikstür listesi yerine kendi bülteninizi verebilirsiniz:

```bash
python -m pipeline.soccer predict --league tr.1 --bulletin bulten.txt
```

Kabul edilen satır biçimleri (hepsi aynı dosyada karışık olabilir):

```
# yorum satırı
Galatasaray - Fenerbahçe
2026-09-20 19:00 Beşiktaş - Trabzonspor
1234 20/09 22:00 Konyaspor - Rizespor          ← iddaa benzeri, baştaki kod atılır
G.Saray - F.Bahçe                              ← kısaltmalar tanınır
```

Ayırıcı olarak `-`, `–`, `vs`, `v`, `x` çalışır. Takım adları modeldeki
adlara şöyle bağlanır: birebir eşleşme → takma ad tablosu (`G.Saray`,
`Man Utd`, `Başakşehir` …) → alt dize → `difflib` ile en yakın eşleşme.
**Bağlanamayan satır sessizce tahmin edilmez**, ekrana `? eşleştirilemedi`
diye yazılır — yanlış takıma bağlamaktansa atlamak doğrusu.

`.json` uzantılı dosyalar da okunur:
`[{"date": "2026-09-20", "home": "…", "away": "…"}]`.

---

## Ne öğreniliyor

Her maç için iki Poisson oranı:

```
lambda (ev)        = exp( hücum_ev  − savunma_dep + ev_avantajı )
mu     (deplasman) = exp( hücum_dep − savunma_ev )
```

artı düşük skorlu dört hücreyi (0-0, 1-0, 0-1, 1-1) düzelten Dixon-Coles
`rho` katsayısı. Bağımsız Poisson bu skorları sistematik olarak yanlış
sayar; `rho` bunu kapatır.

- **Zaman sönümü**: her maç `exp(−decay × gün)` ağırlığı alır. Eski sezonlar
  sayılır ama daha az.
- **Ridge**: takım parametreleri lig ortalamasına çekilir — az maç oynamış
  ve yeni çıkan takımlar için şart.
- **Sıfır ortalama kısıtı**: hücum ve savunma ortalaması sıfırlanır, yoksa
  parametreler belirsiz kalır.
- **Elo** ayrıca tutulur (sezon başında ortalamaya çekilerek) ve Jev'e
  ikinci bir görüş olarak verilir.

`decay` ve `ridge` sabit sayılar değil, **ölçülerek seçilir**:

```bash
python -m pipeline.soccer train --league tr.1 --search
```

ileri yürüyen backtest üzerinde ızgara araması yapar, en iyi çifti seçer ve
model dosyasına yazar. Süper Lig için seçilen `decay=0.0018` (≈ 1 yıl yarı
ömür), Premier Lig için `decay=0.0030`.

---

## Jev'e ne soruluyor

Maç başına tek istek, altı tipli soru:

| Soru | Tip | Ne için |
| --- | --- | --- |
| `home_win` | `noul` | Ev sahibi kazanır mı |
| `draw` | `noul` | Beraberlik |
| `away_win` | `noul` | Deplasman kazanır mı |
| `total_goals` | `score` | 3 seviyeli rubrik (az / orta / çok gollü) |
| `btts` | `noul` | Karşılıklı gol |
| `blind_spot` | `noul` | **Gol modelinin yapısal olarak göremeyeceği bir şey var mı** |

`state` içinde Jev'e giden şey: fikstür, istatistik modelin tam çıktısı
(lambda/mu, 1X2, en olası skorlar, hücum/savunma katsayıları), Elo,
her iki takımın son 6 maçı ve ev/deplasman formu, dinlenme günü, puan
durumu, son 5 karşılaşma ve `--notes` ile verdiğiniz serbest bağlam
(sakatlık, motivasyon, hoca değişikliği).

Üç sonuç ayrı ayrı `noul` olarak soruluyor çünkü `noul` cevapları tasarım
gereği 1'e toplanmak zorunda değil; toplam normalize ediliyor. Üçüne birden
"hayır" gelirse dağılım kullanılmıyor ve taban aynen geçiyor.

`blind_spot` sorusu sistemin kilit parçası: Jev'in o maçtaki ağırlığı
kendi verdiği bu cevapla ölçekleniyor. "Ratingler ve form aynı şeyi
söylüyor" dediğinde taban ağır basıyor, "burada modelin göremediği bir şey
var" dediğinde sözü daha çok geçiyor.

---

## Harman ve kalibrasyon

```
p ∝ taban^(1−w) × jev^w        (logaritmik fikir havuzu)
p ∝ p^(1/T)                     (sıcaklık)

w = taban_ağırlık × (1 − kapı + kapı × 2 × blind_spot),  üst sınır 0.75
```

`w`, `T` ve taban sıcaklığı geçmiş maçlarda **ölçülerek** bulunur:

```bash
# Jev'i geçmiş maçlarda çağır, harman ağırlığını öğren, model dosyasına yaz
python -m pipeline.soccer backtest --league tr.1 --from 2025-08-01 \
    --with-jev --limit 200 --save
```

Her yanıt `jev-cache.json` içinde saklanır; aynı backtest'i tekrar
çalıştırmak ikinci kez para harcamaz.

**Gürültüden kazanç uydurulmaması için bir eşik var.** Bulunan ağırlık
ancak maç başına log-loss kazancı hem `0.002`'yi hem de bir standart hatayı
geçerse kabul edilir; geçemezse ağırlık `0`'a çekilir ve ekrana şu yazılır:

```
Jev'li 410 maçta maç başına log-loss kazancı: +0.0003 ± 0.0015
kazanç gürültüden ayırt edilemiyor -> Jev ağırlığı 0'a çekildi (daha çok maç gerekiyor)
```

(Yukarıdaki çıktı `--mock` ile, yani rastgele bir "Jev" ile alındı —
eşiğin çalıştığının kanıtı. Gerçek anahtarla çalıştırıp kendi sayınızı
görmeniz gerekir; model dosyalarındaki varsayılan `weight: 0.30` **ölçülmüş
değil, makul bir öncül**.)

---

## Ölçülen sonuçlar

İleri yürüyen backtest, 2024-08-01 sonrası, 21 günde bir sıfırdan yeniden
eğitim, yalnızca istatistik taban (Jev hariç):

| Lig | Maç | log-loss | RPS | Brier | İsabet | Kalibrasyon hatası |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Süper Lig (`tr.1`) | 437 | **0.9730** | 0.1962 | 0.5746 | %55.6 | 0.0158 |
| Premier Lig (`en.1`) | 790 | **1.0124** | 0.2074 | 0.6067 | %49.2 | 0.0200 |
| düz 1/3 dağılımı | — | 1.0986 | — | — | ≈%33 | — |

Okunuşu:

- **log-loss** düşük iyi. Düz dağılıma göre kazanç Süper Lig'de %11.4,
  Premier Lig'de %7.9. Süper Lig'in daha öngörülebilir olması beklenen bir
  sonuç.
- **RPS**, 1-X-2'nin doğal sırasını gören tek ölçü: ev galibiyeti bekleyip
  beraberlik görmek, deplasman galibiyeti görmekten daha az hatalı sayılır.
- **Kalibrasyon hatası** 0.016–0.020: "%60 diyorum" dediğinde yaklaşık %60
  çıkıyor. Tahminlerin oran olarak kullanılabilmesi için asıl önemli olan
  sayı bu, isabet oranı değil.
- Veride kapanış oranı varsa (`football-data` sağlayıcısı) aynı maçlarda
  **piyasanın** skoru da basılır. Piyasayı geçmek beklenmemeli; amaç aradaki
  mesafeyi dürüstçe göstermek.

Sızıntıya karşı tek bir kural var ve testlerle bağlanmış: bütün özellik
sorguları maç gününden **kesin önceki** veriyi okur, aynı gün oynanan diğer
maçlar bile dışarıda kalır.

---

## Veri kaynakları

| Sağlayıcı | Kapsam | Not |
| --- | --- | --- |
| `openfootball` (varsayılan) | Sezon başına tek JSON, anahtarsız | Bazı sezonlar/ligler eksik olabilir |
| `football-data` | Skor **ve kapanış oranları**, Türkiye dahil | `--provider football-data` |

`fetch` aynı ligi tekrar indirdiğinde veriyi birleştirir: oynanmamış bir
fikstür sonradan skoruyla güncellenir, tersi olmaz.

Desteklenen lig kodları: `tr.1`, `en.1`, `en.2`, `es.1`, `de.1`, `it.1`,
`fr.1`, `nl.1`, `pt.1`, `be.1`, `gr.1`, `sc.1`.

> **openfootball kapsam uyarısı.** Süper Lig için 2021-22, 2022-23 ve
> 2023-24 sezonları bu kaynakta yok; içinde bulunulan sezon da geç
> ekleniyor. Tam geçmiş ve bültenin kendisi için `--provider football-data`
> daha iyi, ya da bülteninizi `--bulletin` ile elle verin.

---

## Komutlar

| Komut | Ne yapar |
| --- | --- |
| `fetch --league X --seasons N` | Son N sezonu indirir ve birleştirir |
| `train --league X [--search]` | Modeli eğitir, backtest eder, `model-X.json` yazar |
| `backtest --league X --from TARİH [--with-jev] [--save]` | İleri yürüyen değerlendirme, harman kalibrasyonu |
| `predict --league X [--days N \| --bulletin DOSYA]` | Bülteni tahmin eder |

Sık kullanılan bayraklar:

| Bayrak | Ne yapar |
| --- | --- |
| `--mock` | Jev'i çağırmaz, `state`'ten türetilmiş sahte sinyal üretir (ücretsiz) |
| `--no-jev` | Yalnızca istatistik model |
| `--notes "…"` | Jev'e serbest bağlam (sakatlık, motivasyon, kadro) |
| `--detail` | Maç başına taban / Jev / harman / adil oran dökümü |
| `--limit N` | Jev çağrısını N maçla sınırlar (maliyet) |
| `--data-dir YOL` | Başka bir veri klasörü |

---

## Çıktı biçimi

**`site/data/football/bulten-YYYY-AA-GG.json`**

```json
{
  "format": "jev-soccer-bulletin/1",
  "league": "tr.1",
  "demo": false,
  "blend": { "weight": 0.3, "gate": 0.5, "temperature": 1.0 },
  "matches": [
    {
      "date": "2026-09-20",
      "home": "Galatasaray",
      "away": "Trabzonspor",
      "probabilities": { "home": 0.663, "draw": 0.255, "away": 0.082 },
      "odds_fair":     { "home": 1.51,  "draw": 3.92,  "away": 12.24 },
      "pick": "1",
      "score": "1-0",
      "scores": [{ "score": "1-0", "p": 0.16 }, { "score": "2-0", "p": 0.15 }],
      "expected_goals": { "home": 1.72, "away": 0.61 },
      "over_under": { "over_1_5": 0.63, "over_2_5": 0.41, "over_3_5": 0.21 },
      "btts": 0.36,
      "baseline": { "home": 0.663, "draw": 0.241, "away": 0.096 },
      "jev": { "used": true, "weight": 0.34, "blind_spot": 0.62,
               "probabilities": [0.683, 0.267, 0.050] }
    }
  ]
}
```

Raporlanan her şey **tek bir skor ızgarasından** okunur: ızgara önce
harmanlanmış toplam gole göre çözülür, sonra harmanlanmış 1X2'ye birebir
oturtulur. Yani "1X2 şunu diyor ama skor tahmini bunu diyor" durumu
oluşamaz.

---

## Bilinen sınırlar

- **Yalnızca 90 dakika.** Uzatma ve penaltılar hesaba katılmaz.
- **Sakatlık, kadro, hava durumu verisi yok.** Bunları bilen tek yer
  `--notes` ile Jev'e yazdığınız metin.
- **Lig içi maçlar.** Kupa ve Avrupa maçları için takımların ratingleri
  aynı ligden gelir; farklı liglerin güçleri kıyaslanmaz.
- **Jev'in payı henüz ölçülmedi.** Varsayılan `weight: 0.30` bir öncül;
  gerçek anahtarla `backtest --with-jev --save` çalıştırana kadar ölçülmüş
  bir sayı değil.
- Bu sayılar olasılıktır, garanti değil. %66 dediği maçların yaklaşık üçte
  biri kaybedilir — kalibrasyonun iyi olması tam olarak bunu garanti eder.
