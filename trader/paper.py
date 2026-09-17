#!/usr/bin/env python3
"""Kagit (simulasyon) defteri.

Gercek piyasa verisiyle calisir, emir gondermez. Amaci stratejinin kendi
kurallariyla ne yaptigini para riske etmeden gormek. Doldurmalar iyimser
degildir: giris ve cikis piyasa emri kabul edilir, her iki tarafta taker
komisyonu kesilir ve bir mumun icinde hem stop hem hedef vurulmussa **stop**
once kabul edilir.
"""

from __future__ import annotations

TAKER_FEE = 0.00055        # Bybit linear perp taker komisyonu (%0.055)


def new_book(equity):
    return {"equity": float(equity), "positions": {}, "trades": []}


def open_position(book, symbol, side, qty, price, stop, target, leverage,
                  now_ms, edge_value=0.0, fee_rate=TAKER_FEE):
    """Piyasa emriyle pozisyon acar; komisyonu hemen ozkaynaktan duser."""
    notional = qty * price
    fee = notional * fee_rate
    book["equity"] -= fee
    book["positions"][symbol] = {
        "symbol": symbol,
        "side": side,                 # long / short
        "qty": qty,
        "entry": price,
        "stop": stop,
        "target": target,
        "leverage": leverage,
        "opened_ms": now_ms,
        "fees": fee,
        "edge": edge_value,
    }
    return book["positions"][symbol]


def unrealised(position, price):
    direction = 1.0 if position["side"] == "long" else -1.0
    return (price - position["entry"]) * position["qty"] * direction


def check_exits(position, candle, price):
    """Stop veya hedef vuruldu mu? (fiyat, gerekce) ya da None.

    Mum icinde her ikisi de vurulmussa stop kabul edilir -- hangisinin once
    geldigini mum verisinden bilemeyiz, bu yuzden kotu olani varsayilir.
    """
    high = max(candle["high"], price) if candle else price
    low = min(candle["low"], price) if candle else price
    stop = position.get("stop")
    target = position.get("target")

    if position["side"] == "long":
        if stop and low <= stop:
            return stop, "stop"
        if target and high >= target:
            return target, "hedef"
    else:
        if stop and high >= stop:
            return stop, "stop"
        if target and low <= target:
            return target, "hedef"
    return None


def close_position(book, symbol, price, reason, now_ms, fee_rate=TAKER_FEE):
    """Pozisyonu kapatir, gerceklesen kari deftere yazar ve islemi dondurur."""
    position = book["positions"].pop(symbol, None)
    if not position:
        return None
    gross = unrealised(position, price)
    fee = position["qty"] * price * fee_rate
    net = gross - fee
    book["equity"] += net

    trade = {
        "symbol": symbol,
        "side": position["side"],
        "qty": position["qty"],
        "entry": position["entry"],
        "exit": price,
        "opened_ms": position["opened_ms"],
        "closed_ms": now_ms,
        "gross_pnl": round(gross, 4),
        "fees": round(position["fees"] + fee, 4),
        "pnl": round(net, 4),
        "reason": reason,
        "leverage": position.get("leverage"),
        "edge": position.get("edge"),
    }
    book["trades"].append(trade)
    return trade
