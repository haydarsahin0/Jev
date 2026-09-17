# Jev Trader

Bybit vadeli işlemlerinde (USDT perpetual), kararı **Jev**'e sorup emri kendisi
hesaplayan bir bot. `pipeline/radar.py` ile aynı fikir: Jev'den metin değil
**tipli karar** alınır, sayısal cevaplar deterministik bir politikadan geçer.

Model emir vermez. Model yalnızca ölçülebilir şeyler söyler — yön, kanaat,
trend kalitesi, zamanlama, testere riski, kalabalık riski — ve o sayılar
`risk.py` içindeki eşiklerden geçtikten sonra emir doğar. Aynı cevaplar her
zaman aynı emri üretir; bir işlemin neden açıldığını tek satırda okuyabilirsiniz.

---

## Mimari

```
Her tik (varsayılan: saat başı)
│
├─ 1. Bybit V5 (anahtarsız, herkese açık uçlar)
│      /v5/market/kline  ·  /v5/market/tickers  ·  /v5/market/instruments-info
│
├─ 2. features.snapshot()
│      EMA 21/55/200, RSI 14, ATR 14, 48 mumluk kanal konumu, hacim oranı,
│      fonlama oranı  ──►  ölçekten bağımsız sayılar (yüzde, ATR katı, 0–1)
│
├─ 3. jev.ask()   POST /v1/systemone      (sembol başına bir istek)
│      direction   choice  long / short / flat  + confidence
│      conviction  score   4 seviyeli rubrik
│      trend_qual. score   3 seviyeli rubrik
│      entry_timing / chop_risk / crowding_risk   noul (0–1)
│      exit_now    noul  — yalnızca açık pozisyon varken sorulur
│
├─ 4. risk.decide()  →  open / close / hold / none        (deterministik)
│      risk.size_position()  →  miktar, kaldıraç, stop, hedef
│      risk.guardrails()     →  günlük zarar, bekleme, pozisyon sayısı
│
├─ 5. Yürütme
│      paper    defterde simüle edilir, emir gitmez
│      testnet  /v5/order/create → api-testnet.bybit.com
│      live     aynı uç, mainnet — yalnızca BYBIT_ALLOW_LIVE=yes ile
│
└─ 6. site/data/trader.json  →  site/trader.html (statik panel)
```

### Dosyalar

| Yol | Ne yapar |
| --- | --- |
| `trader/bybit.py` | Bybit V5 REST: imzalama, yeniden deneme, piyasa verisi, emir. |
| `trader/features.py` | Mumlardan gösterge ve ölçekten bağımsız durum özeti. |
| `trader/jev.py` | Jev istemcisi, soru seti, cevap normalizasyonu, sahte üretici. |
| `trader/risk.py` | Politika, kenar hesabı, karar, boyutlandırma, güvenlik kilitleri. |
| `trader/paper.py` | Kâğıt defter: simüle giriş/çıkış, komisyon, stop/hedef kontrolü. |
| `trader/bot.py` | Tik döngüsü, yürütücüler, durum dosyası, CLI. |
| `site/trader.html` | Panel (vanilla JS, build adımı yok). |
| `tests/test_trader.py` | Birim testleri; ağ kullanmaz. |

Harici paket yok — `pipeline/radar.py` gibi yalnızca standart kütüphane.

---

## Karardan emre

**Kenar (edge).** Jev'in cevapları tek bir 0–1 sayısına indirgenir:

```
yön_payı  = (yön_güveni − 1/3) / (2/3)        # 3 seçenekli soruda 1/3 = şans
pozitif   = 0.30·kanaat + 0.25·trend + 0.20·zamanlama + 0.25·yön_payı
kenar     = pozitif × (1 − 0.70·testere) × (1 − 0.50·kalabalık)
```

**Giriş.** Kenar eşiği geçmeli, yön `flat` olmamalı, yön güveni ≥ 0.55,
testere riski ≤ 0.55, kalabalık riski ≤ 0.70. Biri tutmazsa işlem yok ve
günlüğe hangisinin tutmadığı yazılır.

**Boyut.** Önce risk tanımlanır, miktar ondan türer:

```
stop_mesafesi = ATR × 1.8
risk_tutarı   = özkaynak × %0.75 × kenar_ölçeği      (kenar_ölçeği: 0.5 – 1.0)
miktar        = risk_tutarı / stop_mesafesi
```

Sonra pozisyon büyüklüğü (`özkaynak × 3`) ve kaldıraç tavanı (x5) uygulanır.
**Kaldıraç bir hedef değil, bu hesabın sonucudur:** stop dar olduğu için gereken
kaldıraç kendiliğinden çıkar, tavanı aşarsa miktar küçültülür — kaldıraç değil.

**Çıkış.** Stop ve hedef emirle birlikte borsaya yazılır (`stopLoss`/`takeProfit`).
Ayrıca Jev her tikte `exit_now` sorulur; 0.60 üzerindeyse pozisyon kapatılır.
Yön dönerse ve yeni kenar ≥ 0.40 ise önce kapatılır (aynı tikte ters yöne
girilmez — bir sonraki tik yeniden değerlendirir).

---

## Güvenlik kilitleri

Stop tek bir işlemi korur; bunlar günü korur. Hepsi `risk.POLICY` içinde.

| Kilit | Varsayılan | Ne yapar |
| --- | --- | --- |
| `daily_loss_limit_pct` | 3.0 | Gün başı özkaynağın %3'ü kaybedilirse gün biter. |
| `max_consecutive_losses` + `cooldown_minutes` | 3 / 180 | Üst üste 3 zarardan sonra 3 saat giriş yok. |
| `max_open_positions` | 2 | Aynı anda en fazla iki pozisyon. |
| `min_minutes_between_entries` | 45 | Girişler arası bekleme (aynı anda üç sembole girmeyi engeller). |
| `min_equity` | 25 USDT | Altında hiç işlem açılmaz. |
| `max_leverage` / `max_notional_x` | 5 / 3 | Kaldıraç ve pozisyon büyüklüğü tavanı. |

Bunlara ek olarak:

- **Gerçek para varsayılan değildir.** `--mode live` ancak ortamda
  `BYBIT_ALLOW_LIVE=yes` varsa çalışır, yoksa bot çıkış kodu 2 ile durur.
- Bozuk veya eksik Jev cevabı **güvenli tarafa** düşer: yön `flat`, risk
  sinyalleri 1.0 → işlem açılmaz.
- Bir sembolde veri veya API hatası olursa o sembol atlanır, diğerleri işlenir.
- API anahtarları yalnızca `Authorization` / `X-BAPI-*` başlıklarında kullanılır;
  loga, `trader.json`'a veya siteye yazılmaz.

---

## Kurulum

### 1. Anahtarlar

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Ne için |
| --- | --- |
| `TYPESAFE_API_KEY` | Jev kararları (zaten arXiv Radar için ekli). |
| `BYBIT_API_KEY` | Yalnızca `testnet`/`live` modunda gerekir. |
| `BYBIT_API_SECRET` | Aynı. |

Bybit anahtarını oluştururken **yalnızca "Trade" (Contract/Unified Trading)
iznini** verin; "Withdraw" iznini asla açmayın. Anahtar IP kısıtlıysa GitHub
runner'ının sabit IP'si olmadığını unutmayın — Actions üzerinden çalıştıracaksanız
IP kısıtlamasını kapatmanız ya da botu sabit IP'li kendi sunucunuzda çalıştırmanız
gerekir.

### 2. Gerçek paraya geçiş (isteğe bağlı)

**Settings → Secrets and variables → Actions → Variables**

- `BYBIT_ALLOW_LIVE` = `yes`
- `TRADER_MODE` = `live`

İkisi birden ayarlanana kadar zamanlanmış koşular `paper` modunda kalır.

### 3. İlk çalıştırma

```bash
# Anahtarsız, ağsız: her şeyin bağlandığını görmek için
python trader/bot.py --offline --mock --once

# Gerçek Bybit verisi, sahte para, gerçek Jev kararları
python trader/bot.py --mode paper --once

# Paneli aç
python -m http.server -d site 8000     # → http://localhost:8000/trader.html
```

`--offline --mock` ile üretilen dosyaya `"demo": true` yazılır ve panelde
"deneme verisi" uyarısı çıkar. Denemeden sonra `site/data/trader.json`
dosyasını silmeyi unutmayın — demo verisi commit edilmemeli.

---

## Modlar

| Mod | Emir gider mi | Anahtar | Piyasa verisi |
| --- | --- | --- | --- |
| `paper` | Hayır, defterde simüle edilir | Yalnızca Jev | mainnet (anahtarsız) |
| `testnet` | Evet, `api-testnet.bybit.com` | Bybit testnet anahtarı | testnet |
| `demo` | Evet, `api-demo.bybit.com` | Bybit demo hesabı | demo |
| `live` | **Evet, gerçek para** | Bybit mainnet anahtarı | mainnet |

`--dry-run` borsa modlarında emri hesaplar, günlüğe yazar ama göndermez.

### Bayraklar

| Bayrak | Ne yapar |
| --- | --- |
| `--mode` | `paper` (varsayılan) / `testnet` / `demo` / `live` |
| `--symbols` | Virgülle ayrılmış liste (varsayılan `BTCUSDT,ETHUSDT,SOLUSDT`) |
| `--interval` | Mum aralığı, dakika (varsayılan `60`) |
| `--once` / `--loop N` | Tek tik / N saniyede bir sürekli |
| `--mock` | Jev'i çağırmaz, durumdan türetilmiş sahte sinyal üretir |
| `--offline` | Ağ kullanmaz, tohumlanmış sahte piyasa verisi üretir (yalnızca `paper`) |
| `--dry-run` | Borsa modunda emri göndermez |
| `--equity` | Kâğıt modunda başlangıç özkaynağı |
| `--risk-per-trade`, `--max-leverage`, `--min-edge` | Politikayı tek seferlik ez |
| `--state` | Durum dosyası yolu |
| `--reset` | Durumu sıfırdan oluştur |

---

## Ayarlar nerede

| Dosya | Sabit | Ne işe yarar |
| --- | --- | --- |
| `trader/risk.py` | `POLICY` | Risk, kaldıraç, eşikler, bütün güvenlik kilitleri. |
| `trader/bot.py` | `SYMBOLS`, `INTERVAL`, `START_EQUITY` | Varsayılan semboller, mum aralığı, kâğıt sermaye. |
| `trader/jev.py` | `BASE_QUESTIONS` | Jev'e sorulan soruların tam metni ve rubrikler. |
| `trader/paper.py` | `TAKER_FEE` | Simülasyonda kesilen komisyon (varsayılan %0.055). |

En çok işe yarayan ayar `min_edge` ve `risk_per_trade`. Kenar eşiğini yükseltmek
işlem sayısını düşürür; `risk_per_trade` ise tek bir işlemin en kötü gününüzü
ne kadar etkileyeceğini belirler.

---

## Veri biçimi

`site/data/trader.json` hem botun durumu hem panelin kaynağıdır:

```json
{
  "format": "jev-trader/1",
  "mode": "paper",
  "demo": false,
  "updated_at": "2026-09-17T15:05:11Z",
  "equity": 1012.44,
  "day": { "date": "2026-09-17", "start_equity": 1000.0, "realized_pnl": 12.44 },
  "consecutive_losses": 0,
  "cooldown_until": 0,
  "positions": [
    { "symbol": "ETHUSDT", "side": "long", "qty": 0.42, "entry": 2480.1,
      "stop": 2431.6, "target": 2577.1, "leverage": 2, "edge": 0.41 }
  ],
  "ticks": [
    { "t": "2026-09-17T15:05:11Z", "symbol": "BTCUSDT", "action": "none",
      "price": 64210.5, "edge": 0.22, "reason": "Kenar eşiğin altında (0.22 < 0.30)",
      "signals": { "side": "long", "direction_confidence": 0.61, "conviction": 0.55,
                   "trend_quality": 0.60, "entry_timing": 0.48,
                   "chop_risk": 0.38, "crowding_risk": 0.20 } }
  ],
  "trades": [
    { "symbol": "BTCUSDT", "side": "long", "entry": 63100.0, "exit": 64250.0,
      "pnl": 11.9, "reason": "hedef", "closed_ms": 1789000000000 }
  ],
  "equity_curve": [{ "t": "2026-09-17T15:05:11Z", "equity": 1012.44 }]
}
```

`ticks` son 200, `trades` son 100, `equity_curve` son 500 kayıtla sınırlıdır —
dosya sınırsız büyümez.

---

## Bilinmesi gerekenler

- **Bu bir yatırım tavsiyesi değildir.** Kaldıraçlı kripto işlemlerinde
  sermayenizin tamamını kaybedebilirsiniz. Önce `paper`, sonra `testnet`,
  ancak kaybetmeyi göze aldığınız bir tutarla `live`.
- `--mock` bir strateji değildir: durumdan türetilmiş tutarlı ama rastgele
  sayılar üretir. Rastgele bir yürüyüşte komisyonla birlikte para kaybetmesi
  beklenen davranıştır; boru hattının çalıştığını gösterir, kârlılığı değil.
- Kâğıt modda doldurmalar iyimser değildir: piyasa emri, her iki tarafta taker
  komisyonu ve bir mumda hem stop hem hedef vurulduysa **stop** varsayılır.
- Borsa modunda pozisyon gerçeği borsadadır; bot her tikte `/v5/position/list`
  ile okur. Paneldeki gerçekleşen kâr, borsa modunda `unrealisedPnl`'den
  türetilen yaklaşık bir değerdir — kesin muhasebe için Bybit'in kendi
  `closed-pnl` kayıtlarına bakın.
- Tek yönlü (one-way) pozisyon modu varsayılır (`positionIdx: 0`). Hedge modu
  açıksa Bybit emri reddeder.
