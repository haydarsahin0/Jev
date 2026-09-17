#!/usr/bin/env python3
"""RSI-2 stratejisi: dakikalik mumda iki mum ust uste asiri satim/alim.

Kural:
    RSI(14) son iki **kapanmis** mumda esigin altindaysa  -> LONG
    RSI(14) son iki **kapanmis** mumda esigin ustundeyse  -> SHORT

Iki ayrinti bu dosyanin varlik sebebi:

1. **Acik mum sayilmaz.** Bybit mum listesinin sonuncusu henuz kapanmamis
   mumdur; RSI'si her saniye degisir. Sinyal yalnizca kapanmis mumlardan
   hesaplanir (`drop_last=True`).
2. **Bir kere tetiklenir.** RSI 30'un altinda yirmi dakika kalirsa bu yirmi
   ayri sinyal degildir. Seriyi tamamlayan mum tetikler; tetikleyen mumun
   zamani `last_fired_bar` olarak tutulur ve ayni mum ikinci kez tetiklemez.
   Bot bir tik kacirirsa `grace` kadar gecikmeyle yine de girer.

Cikis tarafi: RSI notr bolgeye donunce (varsayilan 50) veya sure dolunca
pozisyon kapatilir. Stop ve hedef bunlardan bagimsiz, borsada durur.
"""

from __future__ import annotations

CONFIG = {
    "rsi_period": 14,
    "oversold": 30.0,          # bunun ALTINDA = asiri satim -> long adayi
    "overbought": 70.0,        # bunun USTUNDE = asiri alim  -> short adayi
    "confirm_bars": 2,         # kac mum ust uste (kullanicinin kurali: 2)
    "grace_bars": 2,           # tik kacarsa kac mum gecikmeye izin verilir
    "exit_rsi_long": 50.0,     # long, RSI bunun ustune donunce kapanir
    "exit_rsi_short": 50.0,    # short, RSI bunun altina donunce kapanir
    "max_hold_minutes": 30,    # scalp: bu sureden uzun tutulmaz
}


def merge_config(overrides=None):
    config = dict(CONFIG)
    config.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return config


def rsi_series(values, period=14):
    """Her mum icin Wilder RSI; ilk `period` eleman None.

    Tek gecisli: her bar icin bastan hesaplamak yerine ortalama guncellenir.
    """
    out = [None] * len(values)
    if len(values) < period + 1:
        return out

    gains = losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def streak(series, threshold, below=True):
    """Serinin sonundan geriye dogru esigi asan ardisik mum sayisi."""
    count = 0
    for value in reversed(series):
        if value is None:
            break
        if (value < threshold) if below else (value > threshold):
            count += 1
        else:
            break
    return count


def evaluate(candles, config=None, drop_last=True):
    """Kapanmis mumlardan strateji durumu.

    Doner: rsi (son kapanmis mumun RSI'si), long_bars / short_bars (ardisik
    mum sayisi), signal (`long` / `short` / None), bar (tetikleyen mumun
    baslangic zamani), ready (sinyal kurali saglandi mi).
    """
    config = merge_config(config)
    bars = candles[:-1] if (drop_last and len(candles) > 1) else list(candles)
    closes = [bar["close"] for bar in bars]
    series = rsi_series(closes, config["rsi_period"])
    latest = series[-1] if series else None

    if latest is None:
        return {"rsi": None, "long_bars": 0, "short_bars": 0, "signal": None,
                "bar": None, "ready": False, "closed_bars": len(bars)}

    long_bars = streak(series, config["oversold"], below=True)
    short_bars = streak(series, config["overbought"], below=False)
    needed = config["confirm_bars"]
    limit = needed + config["grace_bars"]

    signal = None
    if needed <= long_bars <= limit:
        signal = "long"
    elif needed <= short_bars <= limit:
        signal = "short"

    return {
        "rsi": round(latest, 2),
        "rsi_previous": round(series[-2], 2) if len(series) > 1 and series[-2] is not None else None,
        "long_bars": long_bars,
        "short_bars": short_bars,
        "signal": signal,
        "bar": bars[-1]["start"] if bars else None,
        "ready": signal is not None,
        "closed_bars": len(bars),
    }


def should_fire(view, memory, config=None):
    """Sinyal var ama bu mum daha once tetiklemis olabilir mi?

    `memory` sembol basina saklanan kucuk sozluk: {"last_fired_bar": ms}.
    """
    if not view.get("signal"):
        return False, "Kural saglanmadi"
    last = (memory or {}).get("last_fired_bar")
    if last and view.get("bar") and last >= view["bar"]:
        return False, "Bu mum zaten tetikledi"
    return True, "RSI %s: %d mum ust uste %s" % (
        view["rsi"],
        view["long_bars"] if view["signal"] == "long" else view["short_bars"],
        "asiri satim" if view["signal"] == "long" else "asiri alim")


def exit_view(view, position, config=None, now_ms=None, interval_minutes=1):
    """Strateji tarafli cikis: RSI notre dondu mu, sure doldu mu?

    Stop/hedefin yerine gecmez; onlar borsada durur. Bu, scalp'in amacina
    ulastiktan sonra pozisyonu bosuna tasimamak icindir.
    """
    config = merge_config(config)
    rsi_value = view.get("rsi")
    side = position.get("side")

    if rsi_value is not None:
        if side == "long" and rsi_value >= config["exit_rsi_long"]:
            return True, "RSI notre dondu (%.1f)" % rsi_value
        if side == "short" and rsi_value <= config["exit_rsi_short"]:
            return True, "RSI notre dondu (%.1f)" % rsi_value

    opened = position.get("opened_ms")
    if opened and now_ms:
        held = (now_ms - opened) / 60000.0
        if held >= config["max_hold_minutes"]:
            return True, "Sure doldu (%.0f dakika)" % held

    return False, ""
