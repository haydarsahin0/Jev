#!/usr/bin/env python3
"""Jev Trader: Bybit vadeli islemlerinde Jev kararlariyla calisan bot.

Akis (sembol basina, her tik):

    Bybit mumlari + ticker
        -> features.snapshot          olcekten bagimsiz sayisal ozet
        -> jev.ask                    tipli kararlar (yon, kanaat, risk)
        -> risk.decide                deterministik eylem
        -> risk.size_position         risk once, kaldirac sonra
        -> yurutme (kagit / testnet / gercek)
        -> site/data/trader.json      durum + gunluk (panel bunu okur)

Modlar:
    paper    emir gonderilmez, gercek piyasa verisiyle simule edilir (varsayilan)
    testnet  Bybit testnet'ine gercek emir gonderir (sahte para)
    live     gercek para. BYBIT_ALLOW_LIVE=yes olmadan calismaz.

Kullanim:
    python trader/bot.py --mock --once            # anahtarsiz deneme
    python trader/bot.py --mode paper --once      # gercek veri, sahte para
    python trader/bot.py --mode testnet --loop 900
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit as bybit_api          # noqa: E402
import features                    # noqa: E402
import jev                         # noqa: E402
import paper as paper_book         # noqa: E402
import risk                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "site", "data", "trader.json")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVAL = "60"                 # dakika cinsinden mum araligi
KLINE_LIMIT = 220               # 200 EMA icin yeterli
START_EQUITY = 1000.0           # kagit modunda baslangic ozkaynagi (USDT)

TICK_HISTORY = 200              # gunlukte tutulan karar sayisi
TRADE_HISTORY = 100
CURVE_HISTORY = 500

UTC = dt.timezone.utc


def log(message):
    """Tek satirlik log. API anahtari asla buraya girmez."""
    print(message, flush=True)


def now_ms():
    return int(time.time() * 1000)


def iso(ms=None):
    moment = dt.datetime.fromtimestamp((ms or now_ms()) / 1000.0, UTC)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Durum dosyasi
# --------------------------------------------------------------------------

def load_state(path=STATE_PATH, start_equity=START_EQUITY, mode="paper"):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if isinstance(state, dict) and state.get("format") == "jev-trader/1":
            state.setdefault("book", paper_book.new_book(start_equity))
            state["book"].setdefault("positions", {})
            state["book"].setdefault("trades", [])
            return state
    except (OSError, ValueError):
        pass
    return {
        "format": "jev-trader/1",
        "mode": mode,
        "created_at": iso(),
        "book": paper_book.new_book(start_equity),
        "day": {"date": None, "start_equity": start_equity, "realized_pnl": 0.0},
        "consecutive_losses": 0,
        "cooldown_until": 0,
        "last_entry_ms": 0,
        "ticks": [],
        "trades": [],
        "equity_curve": [],
    }


def save_state(state, path=STATE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    os.replace(temporary, path)


def roll_day(state, equity, today=None):
    """Gun degistiyse gunluk zarar sayaci sifirlanir."""
    today = today or dt.datetime.now(UTC).strftime("%Y-%m-%d")
    day = state.setdefault("day", {})
    if day.get("date") != today:
        state["day"] = {"date": today, "start_equity": equity, "realized_pnl": 0.0}
    return state["day"]


def record_trade(state, trade):
    day = state.setdefault("day", {})
    day["realized_pnl"] = round(day.get("realized_pnl", 0.0) + trade["pnl"], 4)
    if trade["pnl"] < 0:
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
    else:
        state["consecutive_losses"] = 0
    policy = state.get("policy_values") or {}
    cooldown = policy.get("cooldown_minutes", risk.POLICY["cooldown_minutes"])
    if state["consecutive_losses"] >= policy.get(
            "max_consecutive_losses", risk.POLICY["max_consecutive_losses"]):
        state["cooldown_until"] = now_ms() + int(cooldown * 60000)
    state.setdefault("trades", []).append(trade)
    state["trades"] = state["trades"][-TRADE_HISTORY:]


# --------------------------------------------------------------------------
# Yurutme: kagit ve borsa icin ayni arayuz
# --------------------------------------------------------------------------

class PaperExecutor:
    """Emir gondermez; defteri gunceller."""

    live = False

    def __init__(self, state):
        self.state = state
        self.book = state["book"]

    def equity(self):
        return float(self.book.get("equity", 0.0))

    def position(self, symbol):
        return self.book["positions"].get(symbol)

    def open_count(self):
        return len(self.book["positions"])

    def all_positions(self):
        return list(self.book["positions"].values())

    def open(self, symbol, side, qty, price, stop, target, leverage, edge_value):
        position = paper_book.open_position(
            self.book, symbol, side, qty, price, stop, target,
            leverage, now_ms(), edge_value)
        return {"ok": True, "detail": "kagit giris @ %.4f" % price, "position": position}

    def close(self, symbol, price, reason):
        trade = paper_book.close_position(self.book, symbol, price, reason, now_ms())
        if trade:
            record_trade(self.state, trade)
        return trade


class ExchangeExecutor:
    """Bybit'e gercek emir gonderir (testnet veya mainnet)."""

    live = True

    def __init__(self, client, state, dry_run=False, category="linear"):
        self.client = client
        self.state = state
        self.dry_run = dry_run
        self.category = category
        self._positions = None
        self._equity = None

    def _load(self):
        if self._positions is None:
            self._positions = {}
            for row in self.client.positions(category=self.category):
                self._positions[row["symbol"]] = {
                    "symbol": row["symbol"],
                    "side": "long" if row["side"] == "Buy" else "short",
                    "qty": row["size"],
                    "entry": row["entry"],
                    "stop": row["stop_loss"] or None,
                    "target": row["take_profit"] or None,
                    "leverage": row["leverage"],
                    "unrealised": row["unrealised"],
                    "opened_ms": 0,
                }
        return self._positions

    def equity(self):
        if self._equity is None:
            self._equity = self.client.equity()
        return self._equity

    def position(self, symbol):
        return self._load().get(symbol)

    def open_count(self):
        return len(self._load())

    def all_positions(self):
        return list(self._load().values())

    def open(self, symbol, side, qty, price, stop, target, leverage, edge_value):
        filters = self.state["_filters"][symbol]
        tick = filters["tick_size"]
        stop_text = bybit_api.round_step(stop, tick, "near")
        target_text = bybit_api.round_step(target, tick, "near")
        qty_text = bybit_api.round_step(qty, filters["qty_step"], "down")
        if self.dry_run:
            return {"ok": True, "detail": "kuru calisma: %s %s x%d emri gonderilmedi"
                                          % (side, qty_text, leverage)}
        self.client.set_leverage(symbol, leverage, category=self.category)
        self.client.place_order(
            symbol=symbol,
            side="Buy" if side == "long" else "Sell",
            qty=qty_text,
            category=self.category,
            reduce_only=False,
            stop_loss=stop_text,
            take_profit=target_text,
            order_link_id="jev-%s-%d" % (symbol.lower(), now_ms()),
        )
        self._positions = None
        return {"ok": True, "detail": "emir gonderildi: %s %s x%d (SL %s / TP %s)"
                                      % (side, qty_text, leverage, stop_text, target_text)}

    def close(self, symbol, price, reason):
        position = self.position(symbol)
        if not position:
            return None
        filters = self.state["_filters"].get(symbol, {})
        qty_text = bybit_api.round_step(position["qty"],
                                        filters.get("qty_step", 0.001), "down")
        if not self.dry_run:
            self.client.place_order(
                symbol=symbol,
                side="Sell" if position["side"] == "long" else "Buy",
                qty=qty_text,
                category=self.category,
                reduce_only=True,
                order_link_id="jevx-%s-%d" % (symbol.lower(), now_ms()),
            )
            self._positions = None
        trade = {
            "symbol": symbol,
            "side": position["side"],
            "qty": position["qty"],
            "entry": position["entry"],
            "exit": price,
            "opened_ms": position.get("opened_ms", 0),
            "closed_ms": now_ms(),
            "gross_pnl": round(position.get("unrealised", 0.0), 4),
            "fees": 0.0,
            "pnl": round(position.get("unrealised", 0.0), 4),
            "reason": reason,
            "leverage": position.get("leverage"),
        }
        record_trade(self.state, trade)
        return trade


# --------------------------------------------------------------------------
# Agsiz deneme verisi
# --------------------------------------------------------------------------

class SyntheticData:
    """Tohumlanmis rastgele yuruyus: ag olmadan boru hattini calistirmak icin.

    Gercek piyasa degil, sadece tekrarlanabilir bir oyuncak seri. Her tikte
    seri bir mum uzar, boylece ard arda tikler farkli fiyat gorur.
    """

    def __init__(self, seed=7, bars=260, drift=0.0004, volatility=0.006):
        self.seed = seed
        self.bars = bars
        self.drift = drift
        self.volatility = volatility
        self._series = {}

    def _rows(self, symbol):
        if symbol not in self._series:
            rng = random.Random("%s-%d" % (symbol, self.seed))
            price = 100.0 + rng.random() * 50.0
            rows = []
            for i in range(self.bars):
                rows.append(self._bar(rng, price, i))
                price = rows[-1]["close"]
            self._series[symbol] = (rng, rows)
        rng, rows = self._series[symbol]
        rows.append(self._bar(rng, rows[-1]["close"], len(rows)))
        return rows[-self.bars:]

    def _bar(self, rng, price, index):
        step = price * (self.drift + rng.gauss(0.0, self.volatility))
        close = max(0.01, price + step)
        high = max(price, close) * (1 + abs(rng.gauss(0, self.volatility / 2)))
        low = min(price, close) * (1 - abs(rng.gauss(0, self.volatility / 2)))
        return {"start": 1700000000000 + index * 3600000, "open": price,
                "high": high, "low": low, "close": close,
                "volume": 1000.0 * (0.6 + rng.random())}

    def klines(self, symbol, interval="60", limit=KLINE_LIMIT):
        return self._rows(symbol)[-limit:]

    def ticker(self, symbol):
        rows = self._series.get(symbol, (None, [{"close": 100.0}]))[1]
        return {"fundingRate": "0.0001", "price24hPcnt": "0.01",
                "markPrice": str(rows[-1]["close"])}

    def instrument(self, symbol):
        return {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"},
                "priceFilter": {"tickSize": "0.01"},
                "leverageFilter": {"maxLeverage": "25"}}


# --------------------------------------------------------------------------
# Tek tik
# --------------------------------------------------------------------------

def tick(state, data_client, executor, api_key, symbols=SYMBOLS, interval=INTERVAL,
         policy=None, mock=False):
    """Butun semboller icin bir tur. Gunluk satirlarini dondurur."""
    policy = risk.merge_policy(policy)
    state["policy_values"] = policy
    filters_cache = state.setdefault("_filters", {})
    equity = executor.equity()
    roll_day(state, equity)
    entries = []

    for symbol in symbols:
        try:
            candles = data_client.klines(symbol, interval=interval, limit=KLINE_LIMIT)
            ticker = data_client.ticker(symbol)
            snap = features.snapshot(candles, ticker, timeframe=interval)
        except Exception as error:                      # veri yoksa sembolu atla
            log("  %-9s veri hatasi: %s" % (symbol, type(error).__name__))
            entries.append(_entry(symbol, "error", reason="Veri hatasi: %s"
                                                          % type(error).__name__))
            continue

        if symbol not in filters_cache:
            try:
                filters_cache[symbol] = bybit_api.instrument_filters(
                    data_client.instrument(symbol))
            except Exception:
                filters_cache[symbol] = {"qty_step": 0.001, "min_qty": 0.001,
                                         "tick_size": 0.1, "max_leverage": 10.0}

        price = snap["price"]
        position = executor.position(symbol)

        # 1) Kagit modda stop/hedef mumun icinde vurulmus olabilir.
        if position and not executor.live:
            hit = paper_book.check_exits(position, candles[-1], price)
            if hit:
                exit_price, reason = hit
                trade = executor.close(symbol, exit_price, reason)
                log("  %-9s %s vuruldu @ %.4f  pnl %.2f"
                    % (symbol, reason, exit_price, trade["pnl"]))
                entries.append(_entry(symbol, "close", price=exit_price,
                                      reason="%s vuruldu" % reason, trade=trade))
                position = None

        # 2) Jev'e sor.
        position_context = _position_context(position, price, snap, executor)
        state_payload = features.jev_state(
            symbol, snap, position_context,
            policy={"risk_per_trade_pct": round(policy["risk_per_trade"] * 100, 3),
                    "max_leverage": policy["max_leverage"],
                    "stop_distance_atr": policy["atr_stop_mult"],
                    "target_r_multiple": policy["take_profit_r"]})
        questions = jev.questions_for(bool(position))
        try:
            answers = (jev.mock_answers(state_payload, bool(position)) if mock
                       else jev.ask(state_payload, api_key, questions=questions))
        except Exception as error:
            log("  %-9s Jev hatasi: %s" % (symbol, type(error).__name__))
            entries.append(_entry(symbol, "error", price=price,
                                  reason="Jev hatasi: %s" % type(error).__name__))
            continue

        signals = jev.normalize(answers)
        action = risk.decide(signals, position_context, policy)

        # 3) Eylemi uygula.
        entry = _entry(symbol, action["action"], price=price, reason=action["reason"],
                       signals=signals, edge=action["edge"], snapshot=snap)

        if action["action"] == "close" and position:
            trade = executor.close(symbol, price, action["reason"])
            entry["trade"] = trade
            log("  %-9s kapat @ %.4f  pnl %.2f"
                % (symbol, price, (trade or {}).get("pnl", 0.0)))

        elif action["action"] == "open":
            allowed, why = risk.guardrails(state, policy, now_ms(),
                                           executor.open_count(), equity)
            if not allowed:
                entry["action"] = "blocked"
                entry["reason"] = why
                log("  %-9s kilit: %s" % (symbol, why))
            else:
                stop, target, distance = risk.stop_and_target(
                    action["side"], price, snap["atr"] or price * 0.01, policy)
                sizing = risk.size_position(equity, price, distance, action["edge"],
                                            filters_cache[symbol], policy)
                if not sizing["ok"]:
                    entry["action"] = "blocked"
                    entry["reason"] = sizing["reason"]
                    log("  %-9s boyut: %s" % (symbol, sizing["reason"]))
                else:
                    result = executor.open(symbol, action["side"], sizing["qty"],
                                           price, stop, target, sizing["leverage"],
                                           action["edge"])
                    state["last_entry_ms"] = now_ms()
                    entry.update({"qty": sizing["qty"], "leverage": sizing["leverage"],
                                  "stop": round(stop, 6), "target": round(target, 6),
                                  "notional": sizing["notional"],
                                  "risk_pct": sizing["risk_pct"],
                                  "detail": result.get("detail")})
                    log("  %-9s AC %s  miktar %.6f  x%d  giris %.4f  stop %.4f  hedef %.4f"
                        % (symbol, action["side"], sizing["qty"], sizing["leverage"],
                           price, stop, target))
        else:
            log("  %-9s %-7s %s" % (symbol, action["action"], action["reason"]))

        entries.append(entry)

    _finish_tick(state, executor, entries)
    return entries


def _position_context(position, price, snap, executor):
    """risk.decide ve Jev icin pozisyon ozeti (hepsi olcekten bagimsiz)."""
    if not position:
        return None
    equity = executor.equity() or 1.0
    direction = 1.0 if position["side"] == "long" else -1.0
    entry_price = position.get("entry") or price
    move_pct = (price / entry_price - 1.0) * 100.0 * direction if entry_price else 0.0
    unrealised = position.get("unrealised")
    if unrealised is None:
        unrealised = (price - entry_price) * position.get("qty", 0.0) * direction
    atr_value = snap.get("atr") or 0.0
    stop = position.get("stop")
    stop_atr = abs(price - stop) / atr_value if (stop and atr_value) else None
    bars = None
    if position.get("opened_ms"):
        minutes = (now_ms() - position["opened_ms"]) / 60000.0
        interval = int(snap["timeframe"]) if str(snap["timeframe"]).isdigit() else 60
        bars = int(minutes // max(1, interval))
    return {
        "side": position["side"],
        "qty": position.get("qty"),
        "entry": entry_price,
        "stop": stop,
        "target": position.get("target"),
        "move_pct": round(move_pct, 3),
        "unrealised": round(unrealised, 4),
        "unrealised_pct": round(unrealised / equity * 100.0, 3),
        "stop_distance_atr": round(stop_atr, 2) if stop_atr else None,
        "bars_held": bars,
        "leverage": position.get("leverage"),
    }


def _entry(symbol, action, price=None, reason="", signals=None, edge=None,
           trade=None, snapshot=None):
    entry = {"t": iso(), "symbol": symbol, "action": action, "reason": reason}
    if price is not None:
        entry["price"] = round(price, 6)
    if edge is not None:
        entry["edge"] = edge
    if signals:
        entry["signals"] = {k: v for k, v in signals.items() if v is not None}
    if snapshot:
        entry["market"] = {
            "rsi": snapshot.get("rsi"),
            "atr_pct": snapshot.get("atr_pct"),
            "trend": snapshot.get("ema_stack"),
            "change_24_pct": snapshot.get("change_24_pct"),
            "funding_pct": snapshot.get("funding_rate_pct"),
        }
    if trade:
        entry["trade"] = trade
    return entry


def _finish_tick(state, executor, entries):
    """Tik sonunda panelin okudugu alanlari tazele."""
    equity = executor.equity()
    state["equity"] = round(equity, 4)
    state["updated_at"] = iso()
    state["positions"] = _public_positions(executor)
    state["ticks"] = (state.get("ticks", []) + entries)[-TICK_HISTORY:]
    curve = state.setdefault("equity_curve", [])
    curve.append({"t": state["updated_at"], "equity": state["equity"]})
    state["equity_curve"] = curve[-CURVE_HISTORY:]
    day = state.get("day", {})
    start = day.get("start_equity") or equity or 1.0
    state["day_pnl_pct"] = round((day.get("realized_pnl", 0.0) / start) * 100.0, 3)


def _public_positions(executor):
    out = []
    for position in executor.all_positions():
        out.append({k: position.get(k) for k in
                    ("symbol", "side", "qty", "entry", "stop", "target",
                     "leverage", "opened_ms", "edge")})
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def base_url_for(mode, override=None):
    if override:
        return override
    if mode == "live":
        return bybit_api.MAINNET
    if mode == "demo":
        return bybit_api.DEMO
    return bybit_api.TESTNET


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Jev Trader -- Bybit + Jev")
    parser.add_argument("--mode", choices=["paper", "testnet", "demo", "live"],
                        default="paper", help="Yurutme modu (varsayilan: paper)")
    parser.add_argument("--symbols", default=",".join(SYMBOLS),
                        help="Virgulle ayrilmis sembol listesi")
    parser.add_argument("--interval", default=INTERVAL,
                        help="Mum araligi, dakika (varsayilan 60)")
    parser.add_argument("--once", action="store_true",
                        help="Tek tik calistir ve cik (varsayilan)")
    parser.add_argument("--loop", type=int, default=0,
                        help="Saniye cinsinden dongu araligi (0 = tek tik)")
    parser.add_argument("--mock", action="store_true",
                        help="Jev'i cagirmadan sahte sinyal uret (anahtar gerekmez)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Borsa modunda emirleri hesapla ama gonderme")
    parser.add_argument("--state", default=STATE_PATH, help="Durum dosyasi yolu")
    parser.add_argument("--equity", type=float, default=START_EQUITY,
                        help="Kagit modunda baslangic ozkaynagi")
    parser.add_argument("--offline", action="store_true",
                        help="Ag kullanmadan tohumlanmis sahte piyasa verisi uret")
    parser.add_argument("--data-url", default=None,
                        help="Piyasa verisi icin farkli bir Bybit adresi")
    parser.add_argument("--risk-per-trade", type=float, default=None,
                        help="Islem basina risk orani (0.0075 = %%0.75)")
    parser.add_argument("--max-leverage", type=float, default=None)
    parser.add_argument("--min-edge", type=float, default=None)
    parser.add_argument("--reset", action="store_true",
                        help="Durum dosyasini sifirdan olustur")
    return parser.parse_args(argv)


def build_policy(args):
    return risk.merge_policy({
        "risk_per_trade": args.risk_per_trade,
        "max_leverage": args.max_leverage,
        "min_edge": args.min_edge,
    })


def main(argv=None):
    args = parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    policy = build_policy(args)

    api_key = os.environ.get("TYPESAFE_API_KEY", "")
    if not api_key and not args.mock:
        log("TYPESAFE_API_KEY yok. Anahtarsiz denemek icin --mock kullanin.")
        return 2

    if args.mode == "live" and os.environ.get("BYBIT_ALLOW_LIVE", "").lower() != "yes":
        log("Gercek para modu kilitli. Bilerek acmak icin: BYBIT_ALLOW_LIVE=yes")
        return 2

    if args.reset and os.path.exists(args.state):
        os.remove(args.state)
    state = load_state(args.state, args.equity, args.mode)
    state["mode"] = args.mode
    state["demo"] = bool(args.mock)
    state["symbols"] = symbols
    state["interval"] = args.interval

    # Piyasa verisi her zaman anahtarsiz okunur. Kagit modda testnet'in ince
    # defterine bakmanin anlami yok; varsayilan mainnet verisidir.
    if args.offline:
        if args.mode != "paper":
            log("--offline yalnizca kagit modda kullanilir.")
            return 2
        data_client = SyntheticData()
        state["demo"] = True
    else:
        data_url = args.data_url or (bybit_api.MAINNET if args.mode == "paper"
                                     else base_url_for(args.mode))
        data_client = bybit_api.Bybit(base_url=data_url)

    if args.mode == "paper":
        executor = PaperExecutor(state)
    else:
        key = os.environ.get("BYBIT_API_KEY", "")
        secret = os.environ.get("BYBIT_API_SECRET", "")
        if not key or not secret:
            log("BYBIT_API_KEY / BYBIT_API_SECRET yok.")
            return 2
        client = bybit_api.Bybit(key, secret, base_url=base_url_for(args.mode))
        executor = ExchangeExecutor(client, state, dry_run=args.dry_run)

    interval_seconds = max(0, args.loop)
    rounds = 0
    while True:
        rounds += 1
        log("[%s] tik %d  mod=%s%s  ozkaynak=%.2f"
            % (iso(), rounds, args.mode, "  (sahte sinyal)" if args.mock else "",
               executor.equity()))
        try:
            tick(state, data_client, executor, api_key, symbols, args.interval,
                 policy, args.mock)
        except Exception as error:
            log("Tik hatasi: %s: %s" % (type(error).__name__, error))
        finally:
            public = {k: v for k, v in state.items() if not k.startswith("_")}
            public["policy"] = {k: policy[k] for k in (
                "risk_per_trade", "max_leverage", "max_notional_x", "atr_stop_mult",
                "take_profit_r", "min_edge", "daily_loss_limit_pct",
                "max_open_positions")}
            save_state(public, args.state)

        if not interval_seconds or args.once:
            break
        time.sleep(interval_seconds)

    log("Durum yazildi: %s" % args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
