# Jev Trader

Bybit vadeli işlemlerinde (USDT perpetual) çalışan, **dakikalık RSI kuralıyla**
işlem açan ve her kurulumu **Jev'e onaylatan** bir bot.

## Kural

```
RSI(14), 1 dakikalık mumda
  2 kapanmış mum üst üste  < 30   →  LONG
  2 kapanmış mum üst üste  > 70   →  SHORT
```

Yönü kural belirler. Jev'in tek yetkisi **hayır demek**: tetiklenen kurulumu
görür ve "al" ya da "atla" der. Jev kendiliğinden pozisyon açtıramaz; kural
tetiklemediyse Jev'e soru bile sorulmaz (bu aynı zamanda API maliyetini
dakikada bir istekten, yalnızca sinyal anlarına indirir).

İki ayrıntı bu botun düzgün çalışmasının sebebi:

- **Açık mum sayılmaz.** Bybit'in döndürdüğü son mum henüz kapanmamıştır;
  RSI'si saniyede bir değişir. Sinyal yalnızca kapanmış mumlardan hesaplanır.
- **Bir mum bir kez tetikler.** RSI 30'un altında yirmi dakika kalırsa bu
  yirmi ayrı sinyal değildir. Seriyi tamamlayan mum tetikler ve o mumun zamanı
  hatırlanır. Bot bir tik kaçırırsa `grace_bars` (2 mum) kadar gecikmeyle yine
  girer; daha geç kalmışsa girmez — geçmiş hareketi kovalamaz.

---

## Mimari

```
Her tik (60 saniye)
│
├─ 1. Bybit V5 /v5/market/kline   1 dakikalık mumlar (anahtarsız)
│
├─ 2. strategy.evaluate()   kapanmış mumlardan RSI serisi, ardışık mum sayısı
│      └─ kural tetiklemediyse burada durur: yalnızca `watch` tablosu güncellenir
│
├─ 3. jev.ask(CONFIRM_QUESTIONS)   POST /v1/systemone
│      take_setup      choice  take / skip        + confidence
│      setup_quality   score   3 seviyeli rubrik
│      falling_knife   noul    0–1  (bu düşüş devam eder mi?)
│      crowding_risk   noul    0–1
│
├─ 4. risk.confirm_decision()  →  open / none      (deterministik, Jev veto eder)
│      risk.size_position()    →  miktar, kaldıraç, stop, hedef
│      risk.guardrails()       →  günlük zarar, bekleme, pozisyon sayısı
│
├─ 5. Yürütme: paper / demo / testnet / live
│
└─ 6. site/data/trader.json  →  (--publish) git push  →  GitHub Pages paneli
```

Çıkış üç yoldan olur: borsadaki **stop/hedef** emirleri, RSI'nin **nötre
dönmesi** (50) ve **süre** (varsayılan 30 dakika). Açık pozisyon varken Jev'e
soru sorulmaz — çıkış mekaniktir.

### Dosyalar

| Yol | Ne yapar |
| --- | --- |
| `trader/strategy.py` | RSI serisi, ardışık mum sayımı, tetikleme ve çıkış kuralları. |
| `trader/bybit.py` | Bybit V5 REST: imzalama, yeniden deneme, piyasa verisi, emir. |
| `trader/features.py` | Gösterge hesabı ve Jev'e giden ölçekten bağımsız özet. |
| `trader/jev.py` | Jev istemcisi, onay soru seti, cevap normalizasyonu. |
| `trader/risk.py` | Politika, onay kenarı, boyutlandırma, güvenlik kilitleri. |
| `trader/paper.py` | Kâğıt defter: komisyon, stop/hedef kontrolü. |
| `trader/bot.py` | Tik döngüsü, yürütücüler, durum dosyası, yayın, CLI. |
| `site/trader.html` | Panel (vanilla JS, build adımı yok). |
| `tests/test_trader.py` | 130+ birim testi; ağ kullanmaz. |

Harici paket yok — `pipeline/radar.py` gibi yalnızca standart kütüphane.

---

## Kurulum

### 1. Bybit demo hesabı ve anahtar

Bybit'te sağ üstten **Demo Trading**'e geçin, demo hesabına bakiye ekleyin ve
demo hesabın **kendi** API anahtarını oluşturun. Demo anahtarı yalnızca
`api-demo.bybit.com` üzerinde çalışır; gerçek paraya erişemez.

Anahtar oluştururken **yalnızca "Trade" (Unified Trading) iznini** verin,
"Withdraw" iznini asla açmayın.

### 2. Anahtarları makinenize koyun

Anahtarları hiçbir yere yapıştırmayın, dosyaya yazmayın, commit etmeyin.
Terminalde ortam değişkeni olarak verin:

```bash
export TYPESAFE_API_KEY="..."     # Jev
export BYBIT_API_KEY="..."        # Bybit demo
export BYBIT_API_SECRET="..."
```

Kalıcı olsun isterseniz `~/.bashrc` (veya `~/.zshrc`) içine ekleyin. Depoda
`.env` gibi bir dosya tutmayın.

### 3. Botu çalıştırın

```bash
# Önce ağsız/anahtarsız duman testi
python trader/bot.py --offline --mock --once

# Demo parayla, gerçek Bybit ve gerçek Jev, dakikada bir tik
python trader/bot.py --mode demo --loop 60 --publish
```

Bot `Ctrl+C` ile durur ve durumu diske yazar. Kapatıp açtığınızda kaldığı
yerden devam eder (pozisyonları borsadan okur).

### 4. Paneli GitHub Pages'te açın

**Settings → Pages → Build and deployment → Source:** `GitHub Actions`.

`--publish` durum dosyasını commit'leyip push eder; bu push
`.github/workflows/pages.yml` iş akışını tetikler ve panel yayınlanır:

```
https://<kullanıcı-adınız>.github.io/<depo-adı>/trader.html
```

Panel 30 saniyede bir kendini yeniler. Yayın sıklığı `--publish-every`
(varsayılan 10 dakika) ile ayarlanır; bir işlem açılır veya kapanırsa beklemez,
hemen yayınlar. Push'un çalışması için depoya yazma yetkiniz olan bir
`git remote` gerekir (normal `git push` çalışıyorsa yeterli).

> **Neden Actions cron değil?** GitHub Actions en sık 5 dakikada bir tetiklenir,
> üstüne kuyruk gecikmesi biner. 1 dakikalık bir kural için bot sürekli
> çalışmalı: kendi bilgisayarınız, bir Raspberry Pi veya küçük bir VPS.
> `.github/workflows/trader.yml` yalnızca elle tek tik çalıştırmak (anahtar ve
> bağlantı sınaması) içindir.

---

## Modlar

| Mod | Emir gider mi | Anahtar | Piyasa verisi |
| --- | --- | --- | --- |
| `paper` | Hayır, defterde simüle edilir | Yalnızca Jev | mainnet (anahtarsız) |
| `demo` | Evet, `api-demo.bybit.com` — **demo para** | Bybit demo anahtarı | mainnet |
| `testnet` | Evet, `api-testnet.bybit.com` | Bybit testnet anahtarı | testnet |
| `live` | **Evet, gerçek para** | Bybit mainnet anahtarı | mainnet |

`live` modu ortamda `BYBIT_ALLOW_LIVE=yes` yoksa açılmaz, bot çıkış kodu 2 ile
durur. `--dry-run` borsa modlarında emri hesaplar, günlüğe yazar ama göndermez.

### Bayraklar

| Bayrak | Ne yapar |
| --- | --- |
| `--mode` | `paper` (varsayılan) / `demo` / `testnet` / `live` |
| `--symbols` | Virgülle ayrılmış liste (varsayılan `BTCUSDT,ETHUSDT`) |
| `--interval` | Mum aralığı, dakika (varsayılan `1`) |
| `--loop N` | N saniyede bir tik (dakikalık kural için `--loop 60`) |
| `--once` | Tek tik çalıştır ve çık |
| `--oversold`, `--overbought` | RSI eşikleri (varsayılan 30 / 70) |
| `--confirm-bars` | Kaç mum üst üste (varsayılan 2) |
| `--rsi-period` | RSI periyodu (varsayılan 14) |
| `--max-hold` | Pozisyon en fazla kaç dakika tutulur (varsayılan 30) |
| `--no-jev` | Onay katmanını kapat: kural tek başına karar verir |
| `--mock` | Jev'i çağırmaz, durumdan türetilmiş sahte onay üretir |
| `--offline` | Ağ kullanmaz, sahte piyasa verisi üretir (yalnızca `paper`) |
| `--dry-run` | Borsa modunda emri göndermez |
| `--publish`, `--publish-every N` | Durumu commit'leyip push eder |
| `--risk-per-trade`, `--max-leverage`, `--min-edge` | Politikayı ez |
| `--strategy jev` | Eski trend modu: yönü de Jev seçer (saatlik mum için) |
| `--state`, `--equity`, `--reset` | Durum dosyası yolu, kâğıt sermaye, sıfırla |

---

## Boyut ve risk

Önce risk tanımlanır, miktar ondan türer:

```
stop_mesafesi = max(ATR × 2.2, fiyatın %0.18'i)     # komisyon stopu yutmasın
risk_tutarı   = özkaynak × %0.5 × kenar_ölçeği       (kenar_ölçeği: 0.5 – 1.0)
miktar        = risk_tutarı / stop_mesafesi
hedef         = giriş ± stop_mesafesi × 1.2
```

Sonra pozisyon büyüklüğü (`özkaynak × 3`) ve kaldıraç tavanı (x5) uygulanır.
**Kaldıraç bir hedef değil, bu hesabın sonucudur;** tavanı aşarsa miktar
küçültülür, kaldıraç yükseltilmez.

1 dakikalık mum seçildiğinde `risk.py` otomatik olarak `SCALP` ön ayarına
geçer (daha geniş ATR çarpanı, daha kısa hedef, 3 dakikalık giriş beklemesi).
Saatlik mumda klasik `POLICY` değerleri geçerlidir.

### Güvenlik kilitleri

| Kilit | Scalp varsayılanı | Ne yapar |
| --- | --- | --- |
| `daily_loss_limit_pct` | 3.0 | Gün başı özkaynağın %3'ü kaybedilirse gün biter. |
| `max_consecutive_losses` + `cooldown_minutes` | 4 / 30 | Üst üste 4 zarardan sonra 30 dakika giriş yok. |
| `max_open_positions` | 2 | Aynı anda en fazla iki pozisyon. |
| `min_minutes_between_entries` | 3 | Girişler arası bekleme. |
| `min_equity` | 25 USDT | Altında hiç işlem açılmaz. |
| `max_leverage` / `max_notional_x` | 5 / 3 | Kaldıraç ve pozisyon büyüklüğü tavanı. |

Ek olarak:

- Bozuk veya eksik Jev cevabı **atla** demektir — onay katmanı yalnızca hayır
  diyebildiği için, cevabın okunamaması işlem açmamakla sonuçlanır.
- Bir sembolde veri veya API hatası olursa o sembol atlanır, diğerleri işlenir.
- API anahtarları yalnızca `Authorization` / `X-BAPI-*` başlıklarında kullanılır;
  loga, `trader.json`'a veya siteye yazılmaz.

---

## Panel

`site/trader.html` — tek dosya, build adımı yok, hiçbir dış servisi çağırmaz.

- **Canlı takip:** her sembol için anlık RSI (30/70 bantlı çubuk), kaç mumdur
  eşiğin ötesinde olduğu, açık pozisyon veya bekleme durumu.
- **Özkaynak eğrisi**, açık pozisyonlar, kapanan işlemler.
- **Karar günlüğü:** yalnızca *olaylar* — sinyal, veto, giriş, çıkış, kilit,
  hata. Sessiz dakikalar yazılmaz; her satırda o anın RSI'si, kaç mum üst üste
  olduğu, Jev'in onay sinyalleri ve tek cümlelik gerekçe vardır.
- **Politika:** kuralın ve risk ayarlarının o anki değerleri.

---

## Veri biçimi

`site/data/trader.json` hem botun durumu hem panelin kaynağıdır:

```json
{
  "format": "jev-trader/1",
  "mode": "demo",
  "strategy": "rsi2",
  "jev": "confirm",
  "interval": "1",
  "updated_at": "2026-09-17T15:49:02Z",
  "equity": 1012.44,
  "day": { "date": "2026-09-17", "start_equity": 1000.0, "realized_pnl": 12.44 },
  "watch": {
    "BTCUSDT": { "t": "…", "price": 64210.5, "rsi": 28.4,
                 "long_bars": 2, "short_bars": 0, "signal": "long",
                 "position": null }
  },
  "positions": [ { "symbol": "BTCUSDT", "side": "long", "qty": 0.012,
                   "entry": 64210.5, "stop": 64050.1, "target": 64403.0,
                   "leverage": 3, "edge": 0.52 } ],
  "ticks": [ { "t": "…", "symbol": "BTCUSDT", "action": "open", "side": "long",
               "rsi": 28.4, "bars": 2, "edge": 0.52,
               "reason": "RSI 28.4: 2 mum ust uste asiri satim | Kural + Jev onayi" } ],
  "trades": [ { "symbol": "BTCUSDT", "side": "long", "entry": 64210.5,
                "exit": 64403.0, "pnl": 2.31, "reason": "hedef" } ],
  "equity_curve": [ { "t": "…", "equity": 1012.44 } ]
}
```

`ticks` son 120 **olay**, `trades` son 100, `equity_curve` son 500 nokta
(en sık 5 dakikada bir) ile sınırlıdır — dakikada bir çalışsa da dosya şişmez.

---

## Bilinmesi gerekenler

- **Bu bir yatırım tavsiyesi değildir.** Kaldıraçlı kripto işlemlerinde
  sermayenizin tamamını kaybedebilirsiniz. Önce `paper`, sonra `demo`, ancak
  kaybetmeyi göze aldığınız bir tutarla `live`.
- RSI-2 bir **ortalamaya dönüş** kuralıdır: güçlü bir trendde asırı satım
  seviyesi günlerce asırı satım kalabilir. Jev'in `falling_knife` sorusu tam
  olarak bunu elemek içindir, ama hiçbir filtre bunu sıfırlamaz.
- Dakikalık işlemde **komisyon belirleyicidir.** Bybit taker komisyonu iki
  tarafta yaklaşık %0.11'dir; bu yüzden stop mesafesinin altına bir taban
  (`min_stop_pct`) konmuştur. Hedefi daha da kısaltırsanız komisyon kârı yer.
- `--mock` bir strateji değildir: durumdan türetilmiş tutarlı ama rastgele
  sayılar üretir. Boru hattının çalıştığını gösterir, kârlılığı değil.
- Kâğıt modda doldurmalar iyimser değildir: piyasa emri, her iki tarafta taker
  komisyonu ve bir mumda hem stop hem hedef vurulduysa **stop** varsayılır.
- Tek yönlü (one-way) pozisyon modu varsayılır (`positionIdx: 0`). Hedge modu
  açıksa Bybit emri reddeder.
- Bybit API anahtarınız IP kısıtlıysa botu çalıştırdığınız makinenin IP'sini
  izin listesine ekleyin.
