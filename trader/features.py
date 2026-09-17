#!/usr/bin/env python3
"""Mumlardan sayisal ozellik cikarimi. Saf fonksiyonlar, disa bagimlilik yok.

Jev'e ham mum dizisi gonderilmez: burada hesaplanan kucuk ve olcekten bagimsiz
bir ozet gonderilir (yuzdeler, ATR katlari, 0-1 arasi konumlar). Boylece ayni
sorular BTC icin de dusuk fiyatli bir altcoin icin de ayni anlama gelir.
"""

from __future__ import annotations


def closes(candles):
    return [c["close"] for c in candles]


def ema(values, period):
    """Ustel hareketli ortalama; ilk deger basit ortalamayla tohumlanir."""
    if not values or period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = value * k + current * (1 - k)
    return current


def rsi(values, period=14):
    """Wilder RSI. Yetersiz veri varsa None."""
    if len(values) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles, period=14):
    """Wilder ATR (gercek aralik ortalamasi)."""
    if len(candles) < period + 1:
        return None
    ranges = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    current = sum(ranges[:period]) / period
    for value in ranges[period:]:
        current = (current * (period - 1) + value) / period
    return current


def change_pct(values, bars):
    """`bars` mum oncesine gore yuzde degisim."""
    if len(values) <= bars or values[-1 - bars] == 0:
        return None
    return (values[-1] / values[-1 - bars] - 1.0) * 100.0


def channel_position(candles, period=48):
    """Son fiyatin `period` mumluk kanaldaki konumu: 0 = dip, 1 = tepe."""
    window = candles[-period:]
    if len(window) < 2:
        return None
    high = max(c["high"] for c in window)
    low = min(c["low"] for c in window)
    if high <= low:
        return 0.5
    return (candles[-1]["close"] - low) / (high - low)


def volume_ratio(candles, period=24):
    """Son mumun hacmi, onceki `period` mumun ortalamasinin kac kati."""
    if len(candles) < period + 1:
        return None
    window = candles[-period - 1:-1]
    average = sum(c["volume"] for c in window) / len(window)
    if average <= 0:
        return None
    return candles[-1]["volume"] / average


def _round(value, digits=2):
    return None if value is None else round(value, digits)


def snapshot(candles, ticker=None, timeframe="60"):
    """Tek sembol icin sayisal durum ozeti."""
    ticker = ticker or {}
    values = closes(candles)
    if len(values) < 30:
        raise ValueError("yetersiz mum verisi (%d)" % len(values))

    price = values[-1]
    ema_fast = ema(values, 21)
    ema_slow = ema(values, 55)
    ema_trend = ema(values, 200) if len(values) >= 200 else None
    atr_value = atr(candles, 14)
    atr_pct = (atr_value / price * 100.0) if (atr_value and price) else None

    def gap_in_atr(reference):
        if reference is None or not atr_value:
            return None
        return (price - reference) / atr_value

    return {
        "timeframe": timeframe,
        "price": price,
        "atr": atr_value,
        "atr_pct": _round(atr_pct),
        "ema_fast_gap_pct": _round((price / ema_fast - 1) * 100 if ema_fast else None),
        "ema_slow_gap_pct": _round((price / ema_slow - 1) * 100 if ema_slow else None),
        "ema_stack": _stack(ema_fast, ema_slow, ema_trend),
        "above_ema200": None if ema_trend is None else price > ema_trend,
        "price_vs_fast_atr": _round(gap_in_atr(ema_fast)),
        "rsi": _round(rsi(values, 14), 1),
        "change_1_pct": _round(change_pct(values, 1)),
        "change_6_pct": _round(change_pct(values, 6)),
        "change_24_pct": _round(change_pct(values, 24)),
        "channel_position": _round(channel_position(candles, 48), 3),
        "volume_ratio": _round(volume_ratio(candles, 24)),
        "funding_rate_pct": _round(_float(ticker.get("fundingRate")) * 100
                                   if ticker.get("fundingRate") is not None else None, 4),
        "price_24h_pct": _round(_float(ticker.get("price24hPcnt")) * 100
                                if ticker.get("price24hPcnt") is not None else None),
        "mark_price": _float(ticker.get("markPrice")) or price,
    }


def _stack(fast, slow, trend):
    """EMA dizilimi: yukari / asagi / karisik."""
    if fast is None or slow is None:
        return "bilinmiyor"
    if trend is None:
        return "yukari" if fast > slow else "asagi"
    if fast > slow > trend:
        return "yukari"
    if fast < slow < trend:
        return "asagi"
    return "karisik"


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def jev_state(symbol, snap, position=None, policy=None, setup=None):
    """Jev'e gonderilen durum. Fiyat degil, olcekten bagimsiz sinyaller."""
    market = {
        "symbol": symbol,
        "timeframe_minutes": int(snap["timeframe"]) if str(snap["timeframe"]).isdigit() else snap["timeframe"],
        "trend_ema_stack": snap["ema_stack"],
        "above_200_ema": snap["above_ema200"],
        "price_above_fast_ema_in_atr": snap["price_vs_fast_atr"],
        "distance_to_fast_ema_pct": snap["ema_fast_gap_pct"],
        "distance_to_slow_ema_pct": snap["ema_slow_gap_pct"],
        "rsi_14": snap["rsi"],
        "return_last_bar_pct": snap["change_1_pct"],
        "return_6_bars_pct": snap["change_6_pct"],
        "return_24_bars_pct": snap["change_24_pct"],
        "range_position_48_bars": snap["channel_position"],
        "volume_vs_average": snap["volume_ratio"],
        "atr_percent_of_price": snap["atr_pct"],
        "funding_rate_pct_per_8h": snap["funding_rate_pct"],
        "change_24h_pct": snap["price_24h_pct"],
    }
    state = {"market": {k: v for k, v in market.items() if v is not None}}

    if position:
        state["open_position"] = {
            "side": position.get("side"),
            "unrealised_pct_of_equity": position.get("unrealised_pct"),
            "move_since_entry_pct": position.get("move_pct"),
            "bars_held": position.get("bars_held"),
            "distance_to_stop_in_atr": position.get("stop_distance_atr"),
        }
    if setup:
        state["setup"] = setup
    if policy:
        state["policy"] = policy
    return state
