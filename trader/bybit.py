#!/usr/bin/env python3
"""Bybit V5 REST istemcisi. Sadece standart kutuphane.

Imza kurali (V5):
    sign = HMAC_SHA256(timestamp + api_key + recv_window + payload, api_secret)
    payload = GET icin sorgu dizgisi, POST icin govdenin birebir kendisi.

API anahtari/gizli anahtar yalnizca baslik ve imzada kullanilir; loglara,
hataya veya uretilen JSON'a hicbir sekilde girmez.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

MAINNET = "https://api.bybit.com"
TESTNET = "https://api-testnet.bybit.com"
DEMO = "https://api-demo.bybit.com"        # Bybit "demo trading" hesaplari

USER_AGENT = "jev-trader/1.0 (+https://github.com/haydarsahin0/Jev)"
RECV_WINDOW = "8000"
TIMEOUT = 20
MAX_ATTEMPTS = 4

# Gecici kabul edilen Bybit hata kodlari (yeniden denenir).
RETRY_RET_CODES = (10002, 10016, 10006, 170007)
# "leverage not modified" ve "position mode not modified": hata degil, no-op.
BENIGN_RET_CODES = (110043, 110025, 34036)


class BybitError(RuntimeError):
    """Bybit'in retCode != 0 dondurdugu durum."""

    def __init__(self, ret_code, ret_msg, endpoint):
        super().__init__("Bybit %s: retCode=%s %s" % (endpoint, ret_code, ret_msg))
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.endpoint = endpoint


def _now_ms():
    return str(int(time.time() * 1000))


def sign(secret, timestamp, api_key, payload, recv_window=RECV_WINDOW):
    """V5 imzasi. Test edilebilir olsun diye ayri fonksiyon."""
    message = "%s%s%s%s" % (timestamp, api_key, recv_window, payload)
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def query_string(params):
    """Bybit imzasi, gonderilen sorgu dizgisinin birebir aynisini bekler."""
    if not params:
        return ""
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    return urllib.parse.urlencode(clean)


def _retry_delay(attempt, base=1.6, cap=12.0):
    return min(base ** attempt, cap) + random.uniform(0, 0.4)


class Bybit:
    """Tek bir hesap/ortam icin ince REST sarmalayici."""

    def __init__(self, api_key="", api_secret="", base_url=TESTNET,
                 recv_window=RECV_WINDOW, timeout=TIMEOUT,
                 opener=None, sleep=time.sleep):
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.base_url = base_url.rstrip("/")
        self.recv_window = recv_window
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep

    # ---------------- alt katman ----------------

    @property
    def authenticated(self):
        return bool(self.api_key and self.api_secret)

    def _headers(self, payload, private):
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if private:
            if not self.authenticated:
                raise RuntimeError("Bu cagri API anahtari istiyor "
                                   "(BYBIT_API_KEY / BYBIT_API_SECRET)")
            timestamp = _now_ms()
            headers.update({
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": self.recv_window,
                "X-BAPI-SIGN": sign(self.api_secret, timestamp, self.api_key,
                                    payload, self.recv_window),
            })
        return headers

    def request(self, method, path, params=None, private=False,
                max_attempts=MAX_ATTEMPTS):
        """result sozlugunu dondurur; retCode != 0 ise BybitError firlatir."""
        method = method.upper()
        last_error = None

        for attempt in range(max_attempts):
            if method == "GET":
                payload = query_string(params)
                url = self.base_url + path + (("?" + payload) if payload else "")
                body = None
            else:
                payload = json.dumps({k: v for k, v in (params or {}).items()
                                      if v is not None},
                                     separators=(",", ":"))
                url = self.base_url + path
                body = payload.encode("utf-8")

            request = urllib.request.Request(
                url, data=body, method=method,
                headers=self._headers(payload, private))
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                last_error = "HTTP %s" % error.code
                if error.code < 500 and error.code != 429:
                    raise RuntimeError("Bybit %s: %s" % (path, last_error)) from None
                if attempt == max_attempts - 1:
                    raise RuntimeError("Bybit %s: %s" % (path, last_error)) from None
                self._sleep(_retry_delay(attempt))
                continue
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
                last_error = type(error).__name__
                if attempt == max_attempts - 1:
                    raise RuntimeError("Bybit %s: %s" % (path, last_error)) from None
                self._sleep(_retry_delay(attempt))
                continue

            ret_code = parsed.get("retCode")
            if ret_code == 0 or ret_code in BENIGN_RET_CODES:
                return parsed.get("result") or {}
            if ret_code in RETRY_RET_CODES and attempt < max_attempts - 1:
                last_error = "retCode %s" % ret_code
                self._sleep(_retry_delay(attempt))
                continue
            raise BybitError(ret_code, parsed.get("retMsg", ""), path)

        raise RuntimeError("Bybit %s: %s" % (path, last_error or "bilinmeyen hata"))

    # ---------------- piyasa verisi (anahtarsiz) ----------------

    def klines(self, symbol, interval="60", limit=200, category="linear"):
        """Eskiden yeniye sirali mum listesi.

        Bybit yeniden eskiye dondurur; burada ters cevrilir ki gostergeler
        dogal sirada calissin.
        """
        result = self.request("GET", "/v5/market/kline", {
            "category": category, "symbol": symbol,
            "interval": str(interval), "limit": limit,
        })
        rows = result.get("list") or []
        candles = []
        for row in reversed(rows):
            try:
                candles.append({
                    "start": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            except (TypeError, ValueError, IndexError):
                continue
        return candles

    def ticker(self, symbol, category="linear"):
        result = self.request("GET", "/v5/market/tickers", {
            "category": category, "symbol": symbol})
        rows = result.get("list") or []
        return rows[0] if rows else {}

    def instrument(self, symbol, category="linear"):
        result = self.request("GET", "/v5/market/instruments-info", {
            "category": category, "symbol": symbol})
        rows = result.get("list") or []
        return rows[0] if rows else {}

    # ---------------- hesap (anahtar ister) ----------------

    def equity(self, account_type="UNIFIED", coin="USDT"):
        """Hesabin toplam ozkaynagi (USDT)."""
        result = self.request("GET", "/v5/account/wallet-balance",
                              {"accountType": account_type, "coin": coin},
                              private=True)
        rows = result.get("list") or []
        if not rows:
            return 0.0
        account = rows[0]
        for key in ("totalEquity", "totalWalletBalance"):
            value = _to_float(account.get(key))
            if value:
                return value
        for entry in account.get("coin") or []:
            if entry.get("coin") == coin:
                return _to_float(entry.get("equity")) or _to_float(entry.get("walletBalance"))
        return 0.0

    def positions(self, symbol=None, category="linear", settle_coin="USDT"):
        params = {"category": category}
        if symbol:
            params["symbol"] = symbol
        else:
            params["settleCoin"] = settle_coin
        result = self.request("GET", "/v5/position/list", params, private=True)
        out = []
        for row in result.get("list") or []:
            size = _to_float(row.get("size"))
            if size <= 0:
                continue
            out.append({
                "symbol": row.get("symbol"),
                "side": row.get("side"),                  # Buy / Sell
                "size": size,
                "entry": _to_float(row.get("avgPrice")),
                "leverage": _to_float(row.get("leverage")),
                "unrealised": _to_float(row.get("unrealisedPnl")),
                "stop_loss": _to_float(row.get("stopLoss")),
                "take_profit": _to_float(row.get("takeProfit")),
                "position_idx": row.get("positionIdx", 0),
            })
        return out

    def set_leverage(self, symbol, leverage, category="linear"):
        value = str(int(leverage))
        return self.request("POST", "/v5/position/set-leverage", {
            "category": category, "symbol": symbol,
            "buyLeverage": value, "sellLeverage": value,
        }, private=True)

    def place_order(self, symbol, side, qty, category="linear",
                    order_type="Market", reduce_only=False,
                    stop_loss=None, take_profit=None, position_idx=0,
                    time_in_force="IOC", order_link_id=None):
        params = {
            "category": category,
            "symbol": symbol,
            "side": side,                      # Buy / Sell
            "orderType": order_type,
            "qty": str(qty),
            "timeInForce": time_in_force,
            "positionIdx": position_idx,
        }
        if reduce_only:
            params["reduceOnly"] = True
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        if order_link_id:
            params["orderLinkId"] = order_link_id[:36]
        return self.request("POST", "/v5/order/create", params, private=True)

    def closed_pnl(self, symbol=None, limit=50, category="linear"):
        params = {"category": category, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        result = self.request("GET", "/v5/position/closed-pnl", params, private=True)
        return result.get("list") or []


# --------------------------------------------------------------------------
# Enstruman filtreleri: miktar/fiyat yuvarlamasi
# --------------------------------------------------------------------------

def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _decimals(step):
    text = ("%.10f" % step).rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def round_step(value, step, mode="down"):
    """Degeri adim buyuklugune yuvarlar ve dizgi olarak dondurur.

    Borsa fazladan ondalik kabul etmez; float artiklarini temizlemek icin
    sonuc her zaman adimin ondalik sayisiyla bicimlendirilir.
    """
    if step <= 0:
        return "%s" % value
    units = value / step
    eps = 1e-9
    if mode == "up":
        units = math.ceil(units - eps)
    elif mode == "near":
        units = math.floor(units + 0.5)
    else:
        units = math.floor(units + eps)
    return "%.*f" % (_decimals(step), units * step)


def instrument_filters(instrument):
    """instruments-info yanitindan ihtiyac duyulan alanlar."""
    lot = instrument.get("lotSizeFilter") or {}
    price = instrument.get("priceFilter") or {}
    lev = instrument.get("leverageFilter") or {}
    return {
        "qty_step": _to_float(lot.get("qtyStep"), 0.001),
        "min_qty": _to_float(lot.get("minOrderQty"), 0.0),
        "max_qty": _to_float(lot.get("maxOrderQty"), 0.0),
        "tick_size": _to_float(price.get("tickSize"), 0.01),
        "max_leverage": _to_float(lev.get("maxLeverage"), 10.0),
    }
