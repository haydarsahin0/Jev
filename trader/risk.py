#!/usr/bin/env python3
"""Deterministik karar ve risk katmani.

Jev'in tipli cevaplari buraya girer, cikan sey emrin kendisidir. Model
dogrudan emir vermez: her sey burada, okunabilir esiklerle olculur. Ayni
sinyaller her zaman ayni emri uretir ve her sinir tek bir yerde durur.
"""

from __future__ import annotations

import math

# --------------------------------------------------------------------------
# Varsayilan politika -- degistirmek isteyeceginiz her sey burada.
# --------------------------------------------------------------------------

POLICY = {
    # --- pozisyon boyutu ---
    "risk_per_trade": 0.0075,     # islem basina riske edilen ozkaynak orani (%0.75)
    "max_leverage": 5.0,          # kaldirac tavani (borsanin izni daha yuksekse bile)
    "max_notional_x": 3.0,        # pozisyon buyuklugu <= ozkaynak x bu
    "min_equity": 25.0,           # bunun altinda hic islem acilmaz (USDT)

    # --- stop ve hedef ---
    "atr_stop_mult": 1.8,         # stop mesafesi = ATR x bu
    "take_profit_r": 2.0,         # hedef = risk mesafesi x bu

    # --- Jev esikleri ---
    "min_edge": 0.30,             # bilesik kenar bunun altindaysa islem yok
    "min_direction_confidence": 0.55,
    "max_chop_risk": 0.55,
    "max_crowding_risk": 0.70,
    "exit_now_threshold": 0.60,   # acik pozisyonu kapatma esigi
    "flip_min_edge": 0.40,        # ters yone donmek icin gereken kenar

    # --- guvenlik kilitleri ---
    "daily_loss_limit_pct": 3.0,  # gunluk zarar tavani (gun basi ozkaynagin %'si)
    "max_consecutive_losses": 3,
    "cooldown_minutes": 180,      # ust uste zarardan sonra bekleme
    "max_open_positions": 2,
    "min_minutes_between_entries": 45,
}


# Dakikalik RSI scalp'i icin on ayar. Saatlik trend ayarlari 1 dakikalik
# mumda anlamsiz kalir: 45 dakikalik giris beklemesi gunde bir islem demektir,
# 1.8 ATR'lik stop ise 1 dakikalik mumda komisyon kadar bir mesafedir.
SCALP = {
    "risk_per_trade": 0.005,
    "atr_stop_mult": 2.2,
    "take_profit_r": 1.2,
    "min_stop_pct": 0.18,          # stop en az fiyatin %0.18'i (komisyon payi)
    "min_edge": 0.22,
    "min_direction_confidence": 0.45,
    "max_chop_risk": 0.75,
    "max_crowding_risk": 0.85,
    "cooldown_minutes": 30,
    "max_consecutive_losses": 4,
    "min_minutes_between_entries": 3,
}

# Komisyon stopu yutmasin: stop mesafesi fiyatin en az bu kadari olur.
POLICY["min_stop_pct"] = 0.0


def merge_policy(overrides=None):
    policy = dict(POLICY)
    policy.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return policy


def policy_for(interval_minutes=60, overrides=None):
    """Mum araligina gore taban politika; uzerine kullanicinin ezmeleri."""
    policy = dict(POLICY)
    try:
        minutes = int(interval_minutes)
    except (TypeError, ValueError):
        minutes = 60
    if minutes <= 5:
        policy.update(SCALP)
    policy.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return policy


# --------------------------------------------------------------------------
# Kenar (edge)
# --------------------------------------------------------------------------

def edge(signals):
    """Jev sinyallerinden 0-1 arasi tek bir kenar degeri.

    Yon guveni uc secenekli bir soruda gelir; 1/3 sans seviyesidir, oradan
    yukarisi olceklenir. Risk sinyalleri carpan olarak kenari kucultur.
    """
    confidence = _unit(signals.get("direction_confidence"))
    scaled_confidence = max(0.0, (confidence - 1.0 / 3.0) / (2.0 / 3.0))

    positive = (0.30 * _unit(signals.get("conviction"))
                + 0.25 * _unit(signals.get("trend_quality"))
                + 0.20 * _unit(signals.get("entry_timing"))
                + 0.25 * scaled_confidence)

    penalty = (1.0 - 0.70 * _unit(signals.get("chop_risk"), 1.0)) \
        * (1.0 - 0.50 * _unit(signals.get("crowding_risk"), 1.0))
    return round(max(0.0, min(1.0, positive * penalty)), 4)


def _unit(value, default=0.0):
    if not isinstance(value, (int, float)) or value != value:
        return default
    return max(0.0, min(1.0, float(value)))


# --------------------------------------------------------------------------
# Karar
# --------------------------------------------------------------------------

def decide(signals, position=None, policy=None):
    """Sinyal + mevcut pozisyon -> eylem.

    Eylemler: `open` (yeni pozisyon), `close` (kapat), `hold` (tut), `none`.
    Her karar tek cumlelik bir gerekce ile birlikte doner; gunluge o yazilir.
    """
    policy = merge_policy(policy)
    side = signals.get("side", "flat")
    value = edge(signals)

    if position:
        held = position.get("side")           # long / short
        exit_now = signals.get("exit_now")
        if isinstance(exit_now, (int, float)) and exit_now >= policy["exit_now_threshold"]:
            return _act("close", None, value,
                        "Jev kapat diyor (exit_now %.2f)" % exit_now)
        if side != "flat" and side != held and value >= policy["flip_min_edge"]:
            return _act("close", None, value,
                        "Yon degisti: %s -> %s (kenar %.2f)" % (held, side, value))
        if _unit(signals.get("crowding_risk"), 1.0) >= 0.85:
            return _act("close", None, value, "Asiri kalabalik piyasa, pozisyon kapatiliyor")
        return _act("hold", held, value, "Pozisyon korunuyor (kenar %.2f)" % value)

    if side == "flat":
        return _act("none", None, value, "Jev yon vermiyor")
    if signals.get("direction_confidence", 0.0) < policy["min_direction_confidence"]:
        return _act("none", None, value, "Yon guveni dusuk (%.2f)"
                    % signals.get("direction_confidence", 0.0))
    if _unit(signals.get("chop_risk"), 1.0) > policy["max_chop_risk"]:
        return _act("none", None, value, "Yatay/testere riski yuksek (%.2f)"
                    % signals.get("chop_risk", 1.0))
    if _unit(signals.get("crowding_risk"), 1.0) > policy["max_crowding_risk"]:
        return _act("none", None, value, "Kalabalik/asiri gerilmis piyasa (%.2f)"
                    % signals.get("crowding_risk", 1.0))
    if value < policy["min_edge"]:
        return _act("none", None, value, "Kenar esigin altinda (%.2f < %.2f)"
                    % (value, policy["min_edge"]))
    return _act("open", side, value, "Giris: %s, kenar %.2f" % (side, value))


def _act(action, side, value, reason):
    return {"action": action, "side": side, "edge": value, "reason": reason}


# --------------------------------------------------------------------------
# Boyutlandirma
# --------------------------------------------------------------------------

def stop_and_target(side, price, atr_value, policy=None):
    """ATR'ye dayali stop ve hedef fiyatlari."""
    policy = merge_policy(policy)
    floor_pct = policy.get("min_stop_pct", 0.0) / 100.0
    distance = max(atr_value * policy["atr_stop_mult"],
                   price * max(floor_pct, 0.001))
    if side == "long":
        return price - distance, price + distance * policy["take_profit_r"], distance
    return price + distance, price - distance * policy["take_profit_r"], distance


def size_position(equity, price, stop_distance, edge_value, filters=None, policy=None):
    """Kac adet ve kac kaldirac.

    Risk once tanimlanir (ozkaynagin sabit bir orani), miktar ondan tureler:
        miktar = (ozkaynak x risk_orani x kenar_olcegi) / stop_mesafesi
    Sonra pozisyon buyuklugu ve kaldirac tavanlari uygulanir. Yani kaldirac
    bir hedef degil, ortaya cikan sonuctur.
    """
    policy = merge_policy(policy)
    filters = filters or {}
    if equity < policy["min_equity"]:
        return {"ok": False, "reason": "Ozkaynak alt sinirin altinda (%.2f)" % equity}
    if price <= 0 or stop_distance <= 0:
        return {"ok": False, "reason": "Gecersiz fiyat veya stop mesafesi"}

    span = max(1e-9, 1.0 - policy["min_edge"])
    scale = 0.5 + 0.5 * max(0.0, min(1.0, (edge_value - policy["min_edge"]) / span))
    risk_amount = equity * policy["risk_per_trade"] * scale

    qty = risk_amount / stop_distance
    max_notional = equity * min(policy["max_notional_x"], policy["max_leverage"])
    if qty * price > max_notional:
        qty = max_notional / price

    step = filters.get("qty_step") or 0.001
    qty = math.floor((qty / step) + 1e-9) * step
    min_qty = filters.get("min_qty") or 0.0
    if qty <= 0 or (min_qty and qty < min_qty):
        return {"ok": False,
                "reason": "Hesaplanan miktar borsa minimumunun altinda "
                          "(%.8f < %.8f)" % (qty, min_qty)}

    notional = qty * price
    exchange_max = filters.get("max_leverage") or policy["max_leverage"]
    leverage = min(policy["max_leverage"], exchange_max,
                   max(1.0, math.ceil(notional / equity)))
    return {
        "ok": True,
        "qty": qty,
        "notional": round(notional, 4),
        "leverage": int(leverage),
        "risk_amount": round(risk_amount, 4),
        "risk_pct": round(risk_amount / equity * 100.0, 3),
    }


# --------------------------------------------------------------------------
# Guvenlik kilitleri
# --------------------------------------------------------------------------

def guardrails(state, policy=None, now_ms=None, open_positions=0, equity=None):
    """Islem acmaya izin var mi? (izin, gerekce)

    Kilitler stop'un yerine gecmez; stop tek bir islemi, bunlar gunu korur.
    """
    policy = merge_policy(policy)
    now_ms = now_ms or 0
    day = state.get("day") or {}

    start_equity = day.get("start_equity") or equity or 0.0
    realized = day.get("realized_pnl", 0.0)
    if start_equity > 0:
        loss_pct = -realized / start_equity * 100.0
        if loss_pct >= policy["daily_loss_limit_pct"]:
            return False, ("Gunluk zarar tavani: %%%.2f kayip (tavan %%%.2f)"
                           % (loss_pct, policy["daily_loss_limit_pct"]))

    if state.get("consecutive_losses", 0) >= policy["max_consecutive_losses"]:
        cooldown_until = state.get("cooldown_until", 0)
        if now_ms < cooldown_until:
            minutes = (cooldown_until - now_ms) / 60000.0
            return False, ("Ust uste %d zarar: %.0f dakika beklemede"
                           % (state.get("consecutive_losses", 0), minutes))

    if open_positions >= policy["max_open_positions"]:
        return False, ("Acik pozisyon siniri (%d)" % policy["max_open_positions"])

    last_entry = state.get("last_entry_ms", 0)
    gap_minutes = (now_ms - last_entry) / 60000.0 if last_entry else 1e9
    if gap_minutes < policy["min_minutes_between_entries"]:
        return False, ("Girisler arasi bekleme: %.0f dakika kaldi"
                       % (policy["min_minutes_between_entries"] - gap_minutes))

    if equity is not None and equity < policy["min_equity"]:
        return False, "Ozkaynak alt sinirin altinda (%.2f)" % equity

    return True, "Kilitler acik"


# --------------------------------------------------------------------------
# Onay katmani (RSI-2 gibi mekanik kurallar icin)
# --------------------------------------------------------------------------

def confirm_edge(signals):
    """Onay sorularindan 0-1 arasi kenar.

    Burada yon zaten kuraldan gelir; Jev'in isi bu **ornegi** yargilamak:
    kurulum kalitesi, alma/atlama tercihi ve dusen bicak / kalabalik riski.
    """
    take_confidence = _unit(signals.get("take_confidence"))
    if signals.get("take") is False:
        take_confidence = 0.0
    positive = (0.45 * _unit(signals.get("setup_quality"))
                + 0.55 * take_confidence)
    penalty = (1.0 - 0.60 * _unit(signals.get("falling_knife"), 1.0)) \
        * (1.0 - 0.35 * _unit(signals.get("crowding_risk"), 1.0))
    return round(max(0.0, min(1.0, positive * penalty)), 4)


def confirm_decision(setup_side, signals, policy=None):
    """Kural tetikledi; Jev onayliyor mu?

    Jev yalnizca **hayir** diyebilir. Kural yoksa islem de yoktur; Jev'in
    kendiliginden pozisyon actirmasi mumkun degil.
    """
    policy = merge_policy(policy)
    value = confirm_edge(signals)
    if setup_side not in ("long", "short"):
        return _act("none", None, value, "Kural sinyal vermedi")
    if signals.get("take") is False:
        return _act("none", None, value, "Jev bu kurulumu atla diyor")
    if _unit(signals.get("falling_knife"), 1.0) > policy["max_chop_risk"]:
        return _act("none", None, value, "Dusen bicak riski yuksek (%.2f)"
                    % signals.get("falling_knife", 1.0))
    if _unit(signals.get("crowding_risk"), 1.0) > policy["max_crowding_risk"]:
        return _act("none", None, value, "Kalabalik/asiri gerilmis piyasa (%.2f)"
                    % signals.get("crowding_risk", 1.0))
    if value < policy["min_edge"]:
        return _act("none", None, value, "Onay kenari esigin altinda (%.2f < %.2f)"
                    % (value, policy["min_edge"]))
    return _act("open", setup_side, value, "Kural + Jev onayi (kenar %.2f)" % value)
