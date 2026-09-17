#!/usr/bin/env python3
"""Jev Trader: Bybit vadeli islemlerinde calisan bot.

Iki strateji var:

    rsi2  (varsayilan)  Dakikalik RSI(14) iki mum ust uste 30'un altindaysa
                        LONG, iki mum ust uste 70'in ustundeyse SHORT. Yonu
                        kural belirler; Jev yalnizca o ornegi **veto edebilir**.
                        Jev sadece kural tetikledigi an cagrilir (ucuz).

    jev                 Yonu de Jev'in sectigi trend modu (saatlik mum icin).

Akis (rsi2, sembol basina, her tik):

    Bybit 1 dakikalik mumlar
        -> strategy.evaluate     yalnizca KAPANMIS mumlardan RSI serisi
        -> kural tetiklerse jev.ask(CONFIRM_QUESTIONS)   al / atla
        -> risk.confirm_decision + risk.size_position    miktar, kaldirac
        -> yurutme (kagit / demo / testnet / gercek)
        -> site/data/trader.json -> site/trader.html

Modlar:
    paper    emir gonderilmez, gercek piyasa verisiyle simule edilir
    demo     Bybit Demo Trading (api-demo.bybit.com) -- gercek anahtar, demo para
    testnet  Bybit testnet (api-testnet.bybit.com)
    live     gercek para. BYBIT_ALLOW_LIVE=yes olmadan calismaz.

Kullanim:
    python trader/bot.py --offline --mock --once           # anahtarsiz deneme
    python trader/bot.py --mode demo --loop 60 --publish    # demo parayla surekli
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit as bybit_api          # noqa: E402
import features                    # noqa: E402
import jev                         # noqa: E402
import paper as paper_book         # noqa: E402
import risk                        # noqa: E402
import strategy as strategy_rules  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "site", "data", "trader.json")

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
INTERVAL = "1"                  # dakika cinsinden mum araligi
KLINE_LIMIT = 240
START_EQUITY = 1000.0           # kagit modunda baslangic ozkaynagi (USDT)

TICK_HISTORY = 120              # gunlukte tutulan **olayli** karar sayisi
TRADE_HISTORY = 100
CURVE_HISTORY = 500
CURVE_MIN_GAP_MS = 5 * 60 * 1000    # egriye en fazla 5 dakikada bir nokta
PUBLISH_EVERY_MINUTES = 10          # varsayilan yayin araligi

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
            state.setdefault("memory", {})
            state.setdefault("watch", {})
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
        "memory": {},        # sembol basina: son tetikleyen mum
        "watch": {},         # sembol basina: son bakista RSI ve durum
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


def public_state(state, policy, config):
    """Diske/panele giden hal: ic alanlar (_ ile baslayan) disarida kalir."""
    out = {k: v for k, v in state.items() if not k.startswith("_")}
    out["policy"] = {k: policy[k] for k in (
        "risk_per_trade", "max_leverage", "max_notional_x", "atr_stop_mult",
        "take_profit_r", "min_edge", "daily_loss_limit_pct",
        "max_open_positions", "min_minutes_between_entries") if k in policy}
    out["strategy_config"] = dict(config or {})
    return out


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
    state["_dirty"] = True


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

    def all_positions(self):
        return list(self.book["positions"].values())

    def open_count(self):
        return len(self.book["positions"])

    def open(self, symbol, side, qty, price, stop, target, leverage, edge_value):
        position = paper_book.open_position(
            self.book, symbol, side, qty, price, stop, target,
            leverage, now_ms(), edge_value)
        return {"ok": True, "detail": "kagit giris @ %.4f" % price,
                "position": position}

    def close(self, symbol, price, reason):
        trade = paper_book.close_position(self.book, symbol, price, reason, now_ms())
        if trade:
            record_trade(self.state, trade)
        return trade


class ExchangeExecutor:
    """Bybit'e gercek emir gonderir (demo, testnet veya mainnet)."""

    live = True

    def __init__(self, client, state, dry_run=False, category="linear"):
        self.client = client
        self.state = state
        self.dry_run = dry_run
        self.category = category
        self._positions = None
        self._equity = None

    def refresh(self):
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
                    "opened_ms": self.state.get("memory", {})
                                     .get(row["symbol"], {}).get("opened_ms", 0),
                }
        return self._positions

    def equity(self):
        if self._equity is None:
            self._equity = self.client.equity()
        return self._equity

    def position(self, symbol):
        return self._load().get(symbol)

    def all_positions(self):
        return list(self._load().values())

    def open_count(self):
        return len(self._load())

    def open(self, symbol, side, qty, price, stop, target, leverage, edge_value):
        filters = self.state.get("_filters", {}).get(symbol, {})
        tick_size = filters.get("tick_size", 0.01)
        stop_text = bybit_api.round_step(stop, tick_size, "near")
        target_text = bybit_api.round_step(target, tick_size, "near")
        qty_text = bybit_api.round_step(qty, filters.get("qty_step", 0.001), "down")
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
        memory = self.state.setdefault("memory", {}).setdefault(symbol, {})
        memory["opened_ms"] = now_ms()
        self.refresh()
        return {"ok": True, "detail": "emir gonderildi: %s %s x%d (SL %s / TP %s)"
                                      % (side, qty_text, leverage, stop_text, target_text)}

    def close(self, symbol, price, reason):
        position = self.position(symbol)
        if not position:
            return None
        filters = self.state.get("_filters", {}).get(symbol, {})
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
            self.refresh()
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

    Gercek piyasa degil, tekrarlanabilir bir oyuncak seri. Her tikte seri bir
    mum uzar, boylece ard arda tikler farkli fiyat gorur.
    """

    def __init__(self, seed=7, bars=260, drift=0.0, volatility=0.0035):
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
        return {"start": 1700000000000 + index * 60000, "open": price,
                "high": high, "low": low, "close": close,
                "volume": 1000.0 * (0.6 + rng.random())}

    def klines(self, symbol, interval="1", limit=KLINE_LIMIT):
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
         policy=None, mock=False, strategy="rsi2", config=None, use_jev=True):
    """Butun semboller icin bir tur. Gunluge yazilan **olay** satirlarini dondurur.

    Sessiz tikler (kural tetiklemedi, pozisyon yok) gunluge yazilmaz; onlarin
    yerine `watch` tablosu guncellenir. Boylece dakikalik calisirken dosya
    sismez ve gunlukte yalnizca gercekten olan seyler kalir.
    """
    policy = risk.merge_policy(policy)
    config = strategy_rules.merge_config(config)
    state["policy_values"] = policy
    filters_cache = state.setdefault("_filters", {})
    memory_all = state.setdefault("memory", {})
    watch = state.setdefault("watch", {})
    equity = executor.equity()
    roll_day(state, equity)
    entries = []
    interval_minutes = int(interval) if str(interval).isdigit() else 60

    for symbol in symbols:
        try:
            candles = data_client.klines(symbol, interval=interval, limit=KLINE_LIMIT)
            ticker = data_client.ticker(symbol)
            snap = features.snapshot(candles, ticker, timeframe=interval)
        except Exception as error:
            log("  %-9s veri hatasi: %s" % (symbol, type(error).__name__))
            entries.append(_entry(symbol, "error",
                                  reason="Veri hatasi: %s" % type(error).__name__))
            continue

        if symbol not in filters_cache:
            try:
                filters_cache[symbol] = bybit_api.instrument_filters(
                    data_client.instrument(symbol))
            except Exception:
                filters_cache[symbol] = {"qty_step": 0.001, "min_qty": 0.001,
                                         "tick_size": 0.1, "max_leverage": 10.0}

        price = snap["price"]
        memory = memory_all.setdefault(symbol, {})
        view = strategy_rules.evaluate(candles, config)
        position = executor.position(symbol)

        watch[symbol] = {
            "t": iso(), "price": round(price, 6), "rsi": view["rsi"],
            "long_bars": view["long_bars"], "short_bars": view["short_bars"],
            "signal": view["signal"], "position": position["side"] if position else None,
        }

        # 1) Kagit modda stop/hedef mumun icinde vurulmus olabilir.
        if position and not executor.live:
            hit = paper_book.check_exits(position, candles[-1], price)
            if hit:
                exit_price, reason = hit
                trade = executor.close(symbol, exit_price, reason)
                log("  %-9s %s vuruldu @ %.4f  pnl %.2f"
                    % (symbol, reason, exit_price, trade["pnl"]))
                entries.append(_entry(symbol, "close", price=exit_price,
                                      reason="%s vuruldu" % reason, trade=trade,
                                      view=view))
                position = None

        # 2) Acik pozisyon: strateji cikisi (RSI notre dondu / sure doldu).
        if position:
            done, why = strategy_rules.exit_view(view, position, config, now_ms(),
                                                 interval_minutes)
            if done:
                trade = executor.close(symbol, price, why)
                log("  %-9s kapat: %s  pnl %.2f"
                    % (symbol, why, (trade or {}).get("pnl", 0.0)))
                entries.append(_entry(symbol, "close", price=price, reason=why,
                                      trade=trade, view=view))
            continue          # pozisyon varken yeni giris aranmaz

        # 3) Kural tetikledi mi?
        fire, why = strategy_rules.should_fire(view, memory, config)
        if not fire:
            continue          # sessiz tik: yalnizca watch guncellendi

        # Bu mum degerlendirildi; sonuc ne olursa olsun tekrar tetiklemesin.
        memory["last_fired_bar"] = view["bar"]
        state["_dirty"] = True
        entry = _entry(symbol, "signal", price=price, reason=why, view=view)

        allowed, guard_reason = risk.guardrails(state, policy, now_ms(),
                                                executor.open_count(), equity)
        if not allowed:
            entry["action"] = "blocked"
            entry["reason"] = "%s | %s" % (why, guard_reason)
            log("  %-9s kilit: %s" % (symbol, guard_reason))
            entries.append(entry)
            continue

        # 4) Jev onayi (yalnizca kural tetikledigi an cagrilir).
        if use_jev:
            setup = {
                "rule": "RSI(%d) %d closed bars beyond %s" % (
                    config["rsi_period"], config["confirm_bars"],
                    config["oversold"] if view["signal"] == "long"
                    else config["overbought"]),
                "side": view["signal"],
                "rsi_now": view["rsi"],
                "rsi_previous": view.get("rsi_previous"),
                "bars_beyond_threshold": (view["long_bars"] if view["signal"] == "long"
                                          else view["short_bars"]),
                "timeframe_minutes": interval_minutes,
            }
            payload = features.jev_state(
                symbol, snap, None,
                policy={"risk_per_trade_pct": round(policy["risk_per_trade"] * 100, 3),
                        "max_leverage": policy["max_leverage"],
                        "stop_distance_atr": policy["atr_stop_mult"],
                        "target_r_multiple": policy["take_profit_r"]},
                setup=setup)
            try:
                answers = (jev.mock_confirm(payload) if mock
                           else jev.ask(payload, api_key,
                                        questions=jev.CONFIRM_QUESTIONS))
            except Exception as error:
                entry["action"] = "error"
                entry["reason"] = "Jev hatasi: %s" % type(error).__name__
                log("  %-9s Jev hatasi: %s" % (symbol, type(error).__name__))
                entries.append(entry)
                continue
            signals = jev.normalize_confirm(answers)
            action = risk.confirm_decision(view["signal"], signals, policy)
            entry["signals"] = signals
            entry["edge"] = action["edge"]
        else:
            action = {"action": "open", "side": view["signal"], "edge": 1.0,
                      "reason": "Jev kapali: kural dogrudan uygulaniyor"}
            entry["edge"] = 1.0

        if action["action"] != "open":
            entry["action"] = "veto"
            entry["reason"] = "%s | %s" % (why, action["reason"])
            log("  %-9s veto: %s" % (symbol, action["reason"]))
            entries.append(entry)
            continue

        # 5) Boyut ve emir.
        stop, target, distance = risk.stop_and_target(
            action["side"], price, snap["atr"] or price * 0.002, policy)
        sizing = risk.size_position(equity, price, distance, action["edge"],
                                    filters_cache[symbol], policy)
        if not sizing["ok"]:
            entry["action"] = "blocked"
            entry["reason"] = "%s | %s" % (why, sizing["reason"])
            log("  %-9s boyut: %s" % (symbol, sizing["reason"]))
            entries.append(entry)
            continue

        result = executor.open(symbol, action["side"], sizing["qty"], price,
                               stop, target, sizing["leverage"], action["edge"])
        state["last_entry_ms"] = now_ms()
        entry.update({"action": "open", "qty": sizing["qty"],
                      "leverage": sizing["leverage"], "stop": round(stop, 6),
                      "target": round(target, 6), "notional": sizing["notional"],
                      "risk_pct": sizing["risk_pct"], "detail": result.get("detail"),
                      "reason": "%s | %s" % (why, action["reason"])})
        log("  %-9s AC %s  miktar %.6f  x%d  giris %.4f  stop %.4f  hedef %.4f"
            % (symbol, action["side"], sizing["qty"], sizing["leverage"],
               price, stop, target))
        entries.append(entry)

    _finish_tick(state, executor, entries)
    return entries


def _entry(symbol, action, price=None, reason="", view=None, trade=None):
    entry = {"t": iso(), "symbol": symbol, "action": action, "reason": reason}
    if price is not None:
        entry["price"] = round(price, 6)
    if view:
        entry["rsi"] = view.get("rsi")
        if view.get("signal"):
            entry["side"] = view["signal"]
            entry["bars"] = (view["long_bars"] if view["signal"] == "long"
                             else view["short_bars"])
    if trade:
        entry["trade"] = trade
    return entry


def _finish_tick(state, executor, entries):
    """Tik sonunda panelin okudugu alanlari tazele."""
    equity = executor.equity()
    state["equity"] = round(equity, 4)
    state["updated_at"] = iso()
    state["positions"] = _public_positions(executor)
    if entries:
        state["ticks"] = (state.get("ticks", []) + entries)[-TICK_HISTORY:]
        state["_dirty"] = True

    curve = state.setdefault("equity_curve", [])
    last = curve[-1] if curve else None
    changed = (not last) or abs(last.get("equity", 0.0) - state["equity"]) > 1e-9
    stale = True
    if last:
        try:
            previous = dt.datetime.fromisoformat(last["t"].replace("Z", "+00:00"))
            stale = (now_ms() - previous.timestamp() * 1000) >= CURVE_MIN_GAP_MS
        except (KeyError, ValueError, AttributeError):
            stale = True
    if changed or stale:
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
# Panele yayin: durum dosyasini commit'leyip push eder
# --------------------------------------------------------------------------

def publish(path=STATE_PATH, root=ROOT, run=subprocess.run):
    """Yalnizca durum dosyasini commit eder ve push eder.

    Baska hicbir dosyaya dokunmaz. Push basarisiz olursa bot durmaz; bir
    sonraki yayinda tekrar denenir.
    """
    relative = os.path.relpath(path, root)

    def git(*args):
        return run(["git"] + list(args), cwd=root, capture_output=True, text=True)

    added = git("add", "--", relative)
    if added.returncode != 0:
        return False, (added.stderr or "git add basarisiz").strip()
    if git("diff", "--cached", "--quiet", "--", relative).returncode == 0:
        return False, "degisiklik yok"

    message = "trader: %s durumu" % iso()
    committed = git("commit", "-m", message, "--", relative)
    if committed.returncode != 0:
        return False, (committed.stderr or "git commit basarisiz").strip()

    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
    pushed = git("push", "-u", "origin", branch)
    if pushed.returncode != 0:
        # Baskasi araya girdiyse: yeniden temellendir ve bir kez daha dene.
        git("pull", "--rebase", "--autostash", "origin", branch)
        pushed = git("push", "-u", "origin", branch)
        if pushed.returncode != 0:
            return False, (pushed.stderr or "git push basarisiz").strip()
    return True, message


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


def data_url_for(mode, override=None):
    """Piyasa verisi hangi adresten okunur.

    Demo hesap mainnet fiyatlariyla calisir, bu yuzden veri de mainnet'ten
    okunur. Testnet'in kendi (ince) defteri vardir; orada testnet verisi dogru
    olandir.
    """
    if override:
        return override
    if mode == "testnet":
        return bybit_api.TESTNET
    return bybit_api.MAINNET


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Jev Trader -- Bybit + Jev")
    parser.add_argument("--mode", choices=["paper", "demo", "testnet", "live"],
                        default=os.environ.get("TRADER_MODE", "paper"),
                        help="Yurutme modu (varsayilan: paper)")
    parser.add_argument("--strategy", choices=["rsi2", "jev"], default="rsi2",
                        help="rsi2: dakikalik RSI kurali (varsayilan). "
                             "jev: yonu Jev secer (trend modu)")
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--interval", default=INTERVAL,
                        help="Mum araligi, dakika (varsayilan 1)")
    parser.add_argument("--once", action="store_true", help="Tek tik calistir ve cik")
    parser.add_argument("--loop", type=int, default=0,
                        help="Saniye cinsinden dongu araligi (0 = tek tik)")
    parser.add_argument("--mock", action="store_true",
                        help="Jev'i cagirmadan sahte sinyal uret (anahtar gerekmez)")
    parser.add_argument("--offline", action="store_true",
                        help="Ag kullanmadan tohumlanmis sahte piyasa verisi uret")
    parser.add_argument("--dry-run", action="store_true",
                        help="Borsa modunda emirleri hesapla ama gonderme")
    parser.add_argument("--no-jev", action="store_true",
                        help="Onay katmanini kapat: kural tek basina karar verir")
    parser.add_argument("--state", default=STATE_PATH)
    parser.add_argument("--equity", type=float, default=START_EQUITY,
                        help="Kagit modunda baslangic ozkaynagi")
    parser.add_argument("--data-url", default=None,
                        help="Piyasa verisi icin farkli bir Bybit adresi")
    # Strateji ayarlari
    parser.add_argument("--rsi-period", type=int, default=None)
    parser.add_argument("--oversold", type=float, default=None,
                        help="Asiri satim esigi (varsayilan 30)")
    parser.add_argument("--overbought", type=float, default=None,
                        help="Asiri alim esigi (varsayilan 70)")
    parser.add_argument("--confirm-bars", type=int, default=None,
                        help="Kac mum ust uste (varsayilan 2)")
    parser.add_argument("--max-hold", type=int, default=None,
                        help="Pozisyon en fazla kac dakika tutulur")
    # Risk ayarlari
    parser.add_argument("--risk-per-trade", type=float, default=None)
    parser.add_argument("--max-leverage", type=float, default=None)
    parser.add_argument("--min-edge", type=float, default=None)
    # Yayin
    parser.add_argument("--publish", action="store_true",
                        help="Durum dosyasini commit'leyip push eder (GitHub Pages)")
    parser.add_argument("--publish-every", type=int, default=PUBLISH_EVERY_MINUTES,
                        help="Yayin araligi, dakika (varsayilan 10)")
    parser.add_argument("--reset", action="store_true",
                        help="Durum dosyasini sifirdan olustur")
    return parser.parse_args(argv)


def build_policy(args):
    return risk.policy_for(args.interval, {
        "risk_per_trade": args.risk_per_trade,
        "max_leverage": args.max_leverage,
        "min_edge": args.min_edge,
    })


def build_config(args):
    return strategy_rules.merge_config({
        "rsi_period": args.rsi_period,
        "oversold": args.oversold,
        "overbought": args.overbought,
        "confirm_bars": args.confirm_bars,
        "max_hold_minutes": args.max_hold,
    })


def main(argv=None):
    args = parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    policy = build_policy(args)
    config = build_config(args)
    use_jev = not args.no_jev

    api_key = os.environ.get("TYPESAFE_API_KEY", "")
    if use_jev and not api_key and not args.mock:
        log("TYPESAFE_API_KEY yok. Anahtarsiz denemek icin --mock, "
            "Jev'siz calistirmak icin --no-jev kullanin.")
        return 2

    if args.mode == "live" and os.environ.get("BYBIT_ALLOW_LIVE", "").lower() != "yes":
        log("Gercek para modu kilitli. Bilerek acmak icin: BYBIT_ALLOW_LIVE=yes")
        return 2

    if args.reset and os.path.exists(args.state):
        os.remove(args.state)
    state = load_state(args.state, args.equity, args.mode)
    state.update({"mode": args.mode, "demo": bool(args.mock or args.offline),
                  "symbols": symbols, "interval": args.interval,
                  "strategy": args.strategy, "jev": "confirm" if use_jev else "off"})

    if args.offline:
        if args.mode != "paper":
            log("--offline yalnizca kagit modda kullanilir.")
            return 2
        data_client = SyntheticData()
    else:
        data_client = bybit_api.Bybit(base_url=data_url_for(args.mode, args.data_url))

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

    log("Jev Trader  mod=%s  strateji=%s  semboller=%s  mum=%s dk  onay=%s%s"
        % (args.mode, args.strategy, ",".join(symbols), args.interval,
           "Jev" if use_jev else "kapali", "  (sahte sinyal)" if args.mock else ""))
    if args.strategy == "rsi2":
        log("Kural: RSI(%d) %d kapanmis mum ust uste <%s -> LONG, >%s -> SHORT"
            % (config["rsi_period"], config["confirm_bars"],
               config["oversold"], config["overbought"]))

    interval_seconds = max(0, args.loop)
    last_publish = 0
    rounds = 0
    try:
        while True:
            rounds += 1
            try:
                if isinstance(executor, ExchangeExecutor):
                    executor.refresh()
                tick(state, data_client, executor, api_key, symbols, args.interval,
                     policy, args.mock, args.strategy, config, use_jev)
            except Exception as error:
                log("Tik hatasi: %s: %s" % (type(error).__name__, error))
            finally:
                save_state(public_state(state, policy, config), args.state)

            if args.publish:
                due = (now_ms() - last_publish) >= args.publish_every * 60000
                if state.pop("_dirty", False) or due:
                    ok, detail = publish(args.state)
                    if ok:
                        last_publish = now_ms()
                        log("Yayinlandi: %s" % detail)
                    elif detail != "degisiklik yok":
                        log("Yayin basarisiz: %s" % detail)

            if not interval_seconds or args.once:
                break
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        log("Durduruldu.")
        save_state(public_state(state, policy, config), args.state)

    log("Durum yazildi: %s  (%d tik)" % (args.state, rounds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
