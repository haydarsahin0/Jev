"""Jev Trader birim testleri:  python -m unittest discover -s tests -v

Ag yok: Bybit ve Jev cagrilari sahte opener'larla karsilanir.
"""

import io
import json
import math
import os
import sys
import tempfile
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "trader"))

import bot          # noqa: E402
import bybit        # noqa: E402
import features     # noqa: E402
import jev          # noqa: E402
import paper        # noqa: E402
import risk         # noqa: E402


# --------------------------------------------------------------------------
# Sahte HTTP
# --------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def json_response(payload):
    return FakeResponse(json.dumps(payload).encode("utf-8"))


class RecordingOpener:
    """Sirayla verilen yanitlari dondurur, istekleri kaydeder."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return json_response(item)


def ok(result):
    return {"retCode": 0, "retMsg": "OK", "result": result}


def candles(count=240, start_price=100.0, drift=0.35, amp=3.5):
    """Geri cekilmeleri olan yukari trend.

    Duz bir dogru RSI'yi 100'e cikarir ve gercek piyasaya benzemez; bu yuzden
    trende bir dalga bindirilir (RSI ~68, EMA dizilimi yukari).
    """
    rows = []
    price = start_price
    for i in range(count):
        previous = price
        price = start_price + drift * i + amp * math.sin(i / 2.0)
        rows.append({"start": 1700000000000 + i * 3600000,
                     "open": previous,
                     "high": max(previous, price) + 0.4,
                     "low": min(previous, price) - 0.4,
                     "close": price,
                     "volume": 1000.0 + (i % 7) * 50})
    return rows


# --------------------------------------------------------------------------
# Bybit istemcisi
# --------------------------------------------------------------------------

class SigningTests(unittest.TestCase):
    def test_signature_is_deterministic_and_order_sensitive(self):
        first = bybit.sign("secret", "1700000000000", "key", "category=linear")
        second = bybit.sign("secret", "1700000000000", "key", "category=linear")
        third = bybit.sign("secret", "1700000000000", "key", "category=spot")
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertEqual(len(first), 64)

    def test_query_string_drops_empty_values(self):
        self.assertEqual(bybit.query_string({"a": 1, "b": None, "c": ""}), "a=1")
        self.assertEqual(bybit.query_string({}), "")

    def test_private_call_sends_all_signature_headers(self):
        opener = RecordingOpener([ok({"list": []})])
        client = bybit.Bybit("key", "secret", opener=opener)
        client.positions(symbol="BTCUSDT")
        headers = opener.requests[0].headers
        for name in ("X-bapi-api-key", "X-bapi-sign", "X-bapi-timestamp",
                     "X-bapi-recv-window"):
            self.assertIn(name, headers)

    def test_private_call_without_keys_is_refused(self):
        client = bybit.Bybit(opener=RecordingOpener([ok({})]))
        with self.assertRaises(RuntimeError):
            client.equity()

    def test_post_body_is_signed_verbatim(self):
        opener = RecordingOpener([ok({"orderId": "1"})])
        client = bybit.Bybit("key", "secret", opener=opener)
        client.place_order("BTCUSDT", "Buy", "0.01")
        request = opener.requests[0]
        body = request.data.decode("utf-8")
        expected = bybit.sign("secret", request.headers["X-bapi-timestamp"], "key", body)
        self.assertEqual(request.headers["X-bapi-sign"], expected)
        self.assertEqual(json.loads(body)["qty"], "0.01")

    def test_reduce_only_close_order(self):
        opener = RecordingOpener([ok({})])
        client = bybit.Bybit("key", "secret", opener=opener)
        client.place_order("BTCUSDT", "Sell", "0.01", reduce_only=True)
        body = json.loads(opener.requests[0].data.decode("utf-8"))
        self.assertTrue(body["reduceOnly"])

    def test_error_ret_code_raises(self):
        client = bybit.Bybit("key", "secret",
                             opener=RecordingOpener([{"retCode": 110007,
                                                      "retMsg": "insufficient balance"}]))
        with self.assertRaises(bybit.BybitError) as caught:
            client.equity()
        self.assertEqual(caught.exception.ret_code, 110007)

    def test_benign_ret_code_is_not_an_error(self):
        client = bybit.Bybit("key", "secret",
                             opener=RecordingOpener([{"retCode": 110043,
                                                      "retMsg": "leverage not modified"}]))
        self.assertEqual(client.set_leverage("BTCUSDT", 3), {})

    def test_retries_then_succeeds(self):
        error = urllib.error.HTTPError("u", 503, "busy", None, None)
        opener = RecordingOpener([error, ok({"list": []})])
        client = bybit.Bybit(opener=opener, sleep=lambda _s: None)
        self.assertEqual(client.klines("BTCUSDT"), [])
        self.assertEqual(len(opener.requests), 2)

    def test_client_error_is_not_retried(self):
        error = urllib.error.HTTPError("u", 400, "bad", None, None)
        opener = RecordingOpener([error, ok({"list": []})])
        client = bybit.Bybit(opener=opener, sleep=lambda _s: None)
        with self.assertRaises(RuntimeError):
            client.klines("BTCUSDT")
        self.assertEqual(len(opener.requests), 1)

    def test_klines_are_returned_oldest_first(self):
        rows = [["1700003600000", "2", "3", "1", "2.5", "10", "0"],
                ["1700000000000", "1", "2", "0.5", "1.5", "5", "0"]]
        client = bybit.Bybit(opener=RecordingOpener([ok({"list": rows})]))
        result = client.klines("BTCUSDT")
        self.assertEqual([c["start"] for c in result],
                         [1700000000000, 1700003600000])
        self.assertEqual(result[0]["close"], 1.5)

    def test_positions_skip_flat_rows(self):
        rows = [{"symbol": "BTCUSDT", "side": "Buy", "size": "0.01", "avgPrice": "100"},
                {"symbol": "ETHUSDT", "side": "None", "size": "0"}]
        client = bybit.Bybit("k", "s", opener=RecordingOpener([ok({"list": rows})]))
        result = client.positions()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "BTCUSDT")


class RoundingTests(unittest.TestCase):
    def test_round_step_down_up_near(self):
        self.assertEqual(bybit.round_step(0.0123456, 0.001), "0.012")
        self.assertEqual(bybit.round_step(0.0123456, 0.001, "up"), "0.013")
        self.assertEqual(bybit.round_step(101.34, 0.5, "near"), "101.5")

    def test_exact_multiples_are_not_pushed_down(self):
        self.assertEqual(bybit.round_step(0.3, 0.1), "0.3")
        self.assertEqual(bybit.round_step(3.0, 0.001), "3.000")

    def test_filters_have_safe_defaults(self):
        filters = bybit.instrument_filters({})
        self.assertGreater(filters["qty_step"], 0)
        self.assertGreater(filters["tick_size"], 0)

    def test_filters_are_read_from_instrument(self):
        filters = bybit.instrument_filters({
            "lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01"},
            "priceFilter": {"tickSize": "0.05"},
            "leverageFilter": {"maxLeverage": "25"}})
        self.assertEqual(filters["qty_step"], 0.01)
        self.assertEqual(filters["min_qty"], 0.01)
        self.assertEqual(filters["tick_size"], 0.05)
        self.assertEqual(filters["max_leverage"], 25.0)


# --------------------------------------------------------------------------
# Gostergeler
# --------------------------------------------------------------------------

class IndicatorTests(unittest.TestCase):
    def test_ema_of_constant_series_is_the_constant(self):
        self.assertAlmostEqual(features.ema([5.0] * 50, 21), 5.0, places=9)

    def test_ema_needs_enough_data(self):
        self.assertIsNone(features.ema([1, 2, 3], 21))

    def test_rsi_bounds(self):
        rising = [float(i) for i in range(1, 60)]
        falling = list(reversed(rising))
        self.assertGreater(features.rsi(rising), 95)
        self.assertLess(features.rsi(falling), 5)

    def test_rsi_of_flat_series_is_neutral(self):
        self.assertEqual(features.rsi([10.0] * 40), 50.0)

    def test_atr_tracks_the_bar_range(self):
        def bars(width):
            return [{"open": 100.0, "high": 100.0 + width, "low": 100.0 - width,
                     "close": 100.0, "volume": 1.0} for _ in range(40)]

        narrow = features.atr(bars(0.5))
        wide = features.atr(bars(2.0))
        self.assertGreater(narrow, 0)
        self.assertGreater(wide, narrow)

    def test_atr_needs_enough_bars(self):
        self.assertIsNone(features.atr(candles(5)))

    def test_channel_position_at_extremes(self):
        def bar(value):
            return {"open": value, "high": value, "low": value, "close": value,
                    "volume": 1.0}

        rising = [bar(float(i)) for i in range(60)]
        self.assertAlmostEqual(features.channel_position(rising, 48), 1.0, places=6)
        self.assertAlmostEqual(features.channel_position(list(reversed(rising)), 48),
                               0.0, places=6)
        self.assertAlmostEqual(features.channel_position([bar(5.0)] * 50, 48),
                               0.5, places=6)

    def test_change_pct_handles_short_series(self):
        self.assertIsNone(features.change_pct([1.0, 2.0], 24))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snap = features.snapshot(candles(240),
                                      {"fundingRate": "0.0001",
                                       "price24hPcnt": "0.031",
                                       "markPrice": "195.6"})

    def test_uptrend_is_detected(self):
        self.assertEqual(self.snap["ema_stack"], "yukari")
        self.assertTrue(self.snap["above_ema200"])

    def test_percentages_are_converted(self):
        self.assertAlmostEqual(self.snap["funding_rate_pct"], 0.01, places=4)
        self.assertAlmostEqual(self.snap["price_24h_pct"], 3.1, places=4)

    def test_short_history_is_rejected(self):
        with self.assertRaises(ValueError):
            features.snapshot(candles(10))

    def test_jev_state_carries_no_raw_price(self):
        state = features.jev_state("BTCUSDT", self.snap)
        self.assertNotIn("price", state["market"])
        self.assertEqual(state["market"]["symbol"], "BTCUSDT")
        self.assertIn("rsi_14", state["market"])

    def test_jev_state_includes_position_and_policy(self):
        state = features.jev_state("BTCUSDT", self.snap,
                                   {"side": "long", "unrealised_pct": 1.2,
                                    "move_pct": 0.8, "bars_held": 3,
                                    "stop_distance_atr": 1.6},
                                   {"max_leverage": 5})
        self.assertEqual(state["open_position"]["side"], "long")
        self.assertEqual(state["policy"]["max_leverage"], 5)


# --------------------------------------------------------------------------
# Jev
# --------------------------------------------------------------------------

class JevNormalizeTests(unittest.TestCase):
    def test_score_is_scaled_by_top_level(self):
        signals = jev.normalize({
            "conviction": {"type": "score", "score": 1.5,
                           "probabilities": {"0": 0.1, "1": 0.4, "2": 0.4, "3": 0.1}},
        })
        self.assertAlmostEqual(signals["conviction"], 0.5, places=6)

    def test_noul_is_taken_as_is(self):
        signals = jev.normalize({"entry_timing": {"type": "noul", "noul": 0.73}})
        self.assertAlmostEqual(signals["entry_timing"], 0.73)

    def test_choice_and_confidence(self):
        signals = jev.normalize({"direction": {"type": "choice", "choice": "short",
                                               "confidence": 0.81}})
        self.assertEqual(signals["side"], "short")
        self.assertAlmostEqual(signals["direction_confidence"], 0.81)

    def test_unknown_choice_falls_back_to_flat(self):
        signals = jev.normalize({"direction": {"type": "choice", "choice": "moon"}})
        self.assertEqual(signals["side"], "flat")

    def test_broken_answer_is_safe(self):
        signals = jev.normalize({"direction": "nope", "chop_risk": None})
        self.assertEqual(signals["side"], "flat")
        self.assertEqual(signals["chop_risk"], 1.0)
        self.assertEqual(signals["crowding_risk"], 1.0)
        self.assertEqual(risk.decide(signals)["action"], "none")

    def test_values_are_clamped(self):
        signals = jev.normalize({"chop_risk": {"type": "noul", "noul": 1.9}})
        self.assertEqual(signals["chop_risk"], 1.0)

    def test_exit_question_only_with_a_position(self):
        self.assertNotIn("exit_now", jev.questions_for(False))
        self.assertIn("exit_now", jev.questions_for(True))
        self.assertEqual(jev.questions_for(False), jev.BASE_QUESTIONS)


class JevTransportTests(unittest.TestCase):
    def test_request_carries_key_only_in_header(self):
        opener = RecordingOpener([{"answers": {"direction": {"type": "choice",
                                                             "choice": "long",
                                                             "confidence": 0.7}}}])
        answers = jev.ask({"market": {}}, "secret-key", opener=opener)
        request = opener.requests[0]
        self.assertEqual(request.headers["Authorization"], "Bearer secret-key")
        self.assertNotIn("secret-key", request.data.decode("utf-8"))
        self.assertEqual(answers["direction"]["choice"], "long")

    def test_retries_on_429_then_returns(self):
        error = urllib.error.HTTPError("u", 429, "slow down", {}, None)
        opener = RecordingOpener([error, {"answers": {}}])
        self.assertEqual(jev.ask({}, "k", opener=opener, sleep=lambda _s: None), {})

    def test_bad_request_is_not_retried(self):
        error = urllib.error.HTTPError("u", 400, "bad", {}, None)
        opener = RecordingOpener([error, {"answers": {}}])
        with self.assertRaises(RuntimeError):
            jev.ask({}, "k", opener=opener, sleep=lambda _s: None)
        self.assertEqual(len(opener.requests), 1)

    def test_missing_answers_key_is_an_error(self):
        opener = RecordingOpener([{"nope": 1}] * jev.MAX_ATTEMPTS)
        with self.assertRaises(RuntimeError):
            jev.ask({}, "k", opener=opener, sleep=lambda _s: None)

    def test_mock_answers_are_deterministic(self):
        state = features.jev_state("BTCUSDT", features.snapshot(candles(240)))
        first = jev.mock_answers(state)
        second = jev.mock_answers(state)
        self.assertEqual(first, second)
        self.assertEqual(jev.normalize(first)["side"], "long")


# --------------------------------------------------------------------------
# Risk ve karar
# --------------------------------------------------------------------------

STRONG = {"side": "long", "direction_confidence": 0.85, "conviction": 0.85,
          "trend_quality": 0.8, "entry_timing": 0.75, "chop_risk": 0.15,
          "crowding_risk": 0.1, "exit_now": None}


def weaken(**overrides):
    signals = dict(STRONG)
    signals.update(overrides)
    return signals


class EdgeTests(unittest.TestCase):
    def test_strong_setup_beats_threshold(self):
        self.assertGreater(risk.edge(STRONG), risk.POLICY["min_edge"])

    def test_chop_lowers_the_edge(self):
        self.assertLess(risk.edge(weaken(chop_risk=0.9)), risk.edge(STRONG))

    def test_chance_level_confidence_contributes_nothing(self):
        at_chance = risk.edge(weaken(direction_confidence=1.0 / 3.0))
        below = risk.edge(weaken(direction_confidence=0.0))
        self.assertAlmostEqual(at_chance, below, places=6)

    def test_edge_stays_in_unit_range(self):
        for signals in (STRONG, weaken(chop_risk=1.0, crowding_risk=1.0),
                        {"side": "long"}):
            self.assertGreaterEqual(risk.edge(signals), 0.0)
            self.assertLessEqual(risk.edge(signals), 1.0)


class DecideTests(unittest.TestCase):
    def test_strong_signal_opens(self):
        action = risk.decide(STRONG)
        self.assertEqual(action["action"], "open")
        self.assertEqual(action["side"], "long")

    def test_flat_direction_does_nothing(self):
        self.assertEqual(risk.decide(weaken(side="flat"))["action"], "none")

    def test_low_confidence_blocks_entry(self):
        self.assertEqual(risk.decide(weaken(direction_confidence=0.4))["action"], "none")

    def test_choppy_market_blocks_entry(self):
        self.assertEqual(risk.decide(weaken(chop_risk=0.9))["action"], "none")

    def test_crowded_market_blocks_entry(self):
        self.assertEqual(risk.decide(weaken(crowding_risk=0.95))["action"], "none")

    def test_open_position_is_held_by_default(self):
        action = risk.decide(weaken(exit_now=0.1), {"side": "long"})
        self.assertEqual(action["action"], "hold")

    def test_exit_signal_closes(self):
        action = risk.decide(weaken(exit_now=0.9), {"side": "long"})
        self.assertEqual(action["action"], "close")

    def test_direction_flip_closes_first(self):
        action = risk.decide(weaken(side="short"), {"side": "long"})
        self.assertEqual(action["action"], "close")

    def test_weak_opposite_signal_does_not_flip(self):
        action = risk.decide(weaken(side="short", conviction=0.1, trend_quality=0.1,
                                    entry_timing=0.1, direction_confidence=0.4,
                                    exit_now=0.2), {"side": "long"})
        self.assertEqual(action["action"], "hold")

    def test_every_decision_carries_a_reason(self):
        for signals in (STRONG, weaken(side="flat"), weaken(chop_risk=1.0)):
            self.assertTrue(risk.decide(signals)["reason"])


class SizingTests(unittest.TestCase):
    FILTERS = {"qty_step": 0.001, "min_qty": 0.001, "max_leverage": 25.0}

    def test_risk_amount_matches_policy(self):
        sizing = risk.size_position(1000.0, 100.0, 2.0, 0.9, self.FILTERS)
        self.assertTrue(sizing["ok"])
        # kenar yuksek -> olcek 1'e yakin, risk ~ %0.75
        self.assertLessEqual(sizing["risk_pct"], 0.75 + 1e-9)
        self.assertGreater(sizing["risk_pct"], 0.5)

    def test_loss_at_stop_is_about_the_risk_amount(self):
        sizing = risk.size_position(1000.0, 100.0, 2.0, 0.9, self.FILTERS)
        loss = sizing["qty"] * 2.0
        self.assertAlmostEqual(loss, sizing["risk_amount"], delta=0.05)

    def test_weak_edge_takes_a_smaller_size(self):
        weak = risk.size_position(1000.0, 100.0, 2.0, 0.31, self.FILTERS)
        strong = risk.size_position(1000.0, 100.0, 2.0, 0.95, self.FILTERS)
        self.assertLess(weak["qty"], strong["qty"])

    def test_notional_and_leverage_are_capped(self):
        # cok dar stop -> devasa miktar isterdi; tavan devreye girer
        sizing = risk.size_position(1000.0, 100.0, 0.01, 1.0, self.FILTERS)
        self.assertLessEqual(sizing["notional"], 1000.0 * risk.POLICY["max_notional_x"] + 1e-6)
        self.assertLessEqual(sizing["leverage"], risk.POLICY["max_leverage"])

    def test_exchange_leverage_cap_is_respected(self):
        sizing = risk.size_position(1000.0, 100.0, 0.01, 1.0,
                                    {"qty_step": 0.001, "min_qty": 0.001,
                                     "max_leverage": 2.0})
        self.assertLessEqual(sizing["leverage"], 2)

    def test_tiny_account_is_refused(self):
        sizing = risk.size_position(5.0, 100.0, 2.0, 0.9, self.FILTERS)
        self.assertFalse(sizing["ok"])

    def test_below_minimum_quantity_is_refused(self):
        sizing = risk.size_position(100.0, 60000.0, 1200.0, 0.9,
                                    {"qty_step": 0.001, "min_qty": 0.001})
        self.assertFalse(sizing["ok"])
        self.assertIn("minimum", sizing["reason"])

    def test_quantity_is_a_multiple_of_the_step(self):
        sizing = risk.size_position(1000.0, 100.0, 2.0, 0.9,
                                    {"qty_step": 0.01, "min_qty": 0.01})
        units = sizing["qty"] / 0.01
        self.assertAlmostEqual(units, round(units), places=6)

    def test_stop_and_target_sit_on_the_right_sides(self):
        stop, target, distance = risk.stop_and_target("long", 100.0, 2.0)
        self.assertLess(stop, 100.0)
        self.assertGreater(target, 100.0)
        self.assertAlmostEqual((target - 100.0) / distance, risk.POLICY["take_profit_r"])
        stop, target, _ = risk.stop_and_target("short", 100.0, 2.0)
        self.assertGreater(stop, 100.0)
        self.assertLess(target, 100.0)


class GuardrailTests(unittest.TestCase):
    def base_state(self, **overrides):
        state = {"day": {"date": "2026-09-17", "start_equity": 1000.0,
                         "realized_pnl": 0.0},
                 "consecutive_losses": 0, "cooldown_until": 0, "last_entry_ms": 0}
        state.update(overrides)
        return state

    def test_clear_state_allows_entry(self):
        allowed, _ = risk.guardrails(self.base_state(), equity=1000.0)
        self.assertTrue(allowed)

    def test_daily_loss_limit_stops_trading(self):
        state = self.base_state(day={"date": "2026-09-17", "start_equity": 1000.0,
                                     "realized_pnl": -31.0})
        allowed, why = risk.guardrails(state, equity=969.0)
        self.assertFalse(allowed)
        self.assertIn("Gunluk zarar", why)

    def test_cooldown_after_consecutive_losses(self):
        state = self.base_state(consecutive_losses=3, cooldown_until=10_000_000)
        allowed, why = risk.guardrails(state, now_ms=9_000_000, equity=1000.0)
        self.assertFalse(allowed)
        self.assertIn("beklemede", why)

    def test_cooldown_expires(self):
        state = self.base_state(consecutive_losses=3, cooldown_until=10_000_000)
        allowed, _ = risk.guardrails(state, now_ms=11_000_000, equity=1000.0)
        self.assertTrue(allowed)

    def test_open_position_cap(self):
        allowed, why = risk.guardrails(self.base_state(), open_positions=2, equity=1000.0)
        self.assertFalse(allowed)
        self.assertIn("Acik pozisyon", why)

    def test_entries_are_spaced_apart(self):
        state = self.base_state(last_entry_ms=1_000_000)
        allowed, why = risk.guardrails(state, now_ms=1_000_000 + 60_000, equity=1000.0)
        self.assertFalse(allowed)
        self.assertIn("bekleme", why)

    def test_small_equity_blocks_entry(self):
        allowed, _ = risk.guardrails(self.base_state(), equity=10.0)
        self.assertFalse(allowed)


# --------------------------------------------------------------------------
# Kagit defter
# --------------------------------------------------------------------------

class PaperBookTests(unittest.TestCase):
    def setUp(self):
        self.book = paper.new_book(1000.0)

    def test_open_charges_a_fee(self):
        paper.open_position(self.book, "BTCUSDT", "long", 1.0, 100.0, 98.0, 104.0, 3, 0)
        self.assertLess(self.book["equity"], 1000.0)

    def test_winning_long_increases_equity(self):
        paper.open_position(self.book, "BTCUSDT", "long", 1.0, 100.0, 98.0, 104.0, 3, 0)
        trade = paper.close_position(self.book, "BTCUSDT", 104.0, "hedef", 1)
        self.assertAlmostEqual(trade["gross_pnl"], 4.0, places=6)
        self.assertGreater(trade["pnl"], 3.5)
        self.assertGreater(self.book["equity"], 1000.0)

    def test_short_profits_when_price_falls(self):
        paper.open_position(self.book, "ETHUSDT", "short", 2.0, 100.0, 102.0, 96.0, 3, 0)
        trade = paper.close_position(self.book, "ETHUSDT", 96.0, "hedef", 1)
        self.assertAlmostEqual(trade["gross_pnl"], 8.0, places=6)

    def test_closing_removes_the_position(self):
        paper.open_position(self.book, "BTCUSDT", "long", 1.0, 100.0, 98.0, 104.0, 3, 0)
        paper.close_position(self.book, "BTCUSDT", 101.0, "elle", 1)
        self.assertEqual(self.book["positions"], {})
        self.assertEqual(len(self.book["trades"]), 1)

    def test_closing_an_unknown_symbol_is_harmless(self):
        self.assertIsNone(paper.close_position(self.book, "XRPUSDT", 1.0, "x", 1))

    def test_stop_is_assumed_first_when_both_are_touched(self):
        position = {"side": "long", "entry": 100.0, "stop": 98.0, "target": 104.0,
                    "qty": 1.0}
        hit = paper.check_exits(position, {"high": 105.0, "low": 97.0}, 100.0)
        self.assertEqual(hit, (98.0, "stop"))

    def test_target_hit_alone(self):
        position = {"side": "short", "entry": 100.0, "stop": 102.0, "target": 96.0,
                    "qty": 1.0}
        self.assertEqual(paper.check_exits(position, {"high": 101.0, "low": 95.0},
                                           100.0), (96.0, "hedef"))

    def test_no_exit_inside_the_range(self):
        position = {"side": "long", "entry": 100.0, "stop": 98.0, "target": 104.0,
                    "qty": 1.0}
        self.assertIsNone(paper.check_exits(position, {"high": 103.0, "low": 99.0},
                                            100.0))


# --------------------------------------------------------------------------
# Bot: tam tur
# --------------------------------------------------------------------------

class FakeData:
    """Bybit yerine gecen piyasa verisi kaynagi."""

    def __init__(self, rows=None, ticker=None):
        self.rows = rows if rows is not None else candles(240)
        self._ticker = ticker or {"fundingRate": "0.0001", "price24hPcnt": "0.02",
                                  "markPrice": str(self.rows[-1]["close"])}

    def klines(self, symbol, interval="60", limit=220):
        return self.rows

    def ticker(self, symbol):
        return self._ticker

    def instrument(self, symbol):
        return {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"},
                "priceFilter": {"tickSize": "0.1"},
                "leverageFilter": {"maxLeverage": "25"}}


class TickTests(unittest.TestCase):
    def setUp(self):
        self.state = bot.load_state("/nonexistent.json", 1000.0)
        self.executor = bot.PaperExecutor(self.state)

    def run_tick(self, data=None, symbols=("BTCUSDT",), policy=None):
        return bot.tick(self.state, data or FakeData(), self.executor, "",
                        symbols=list(symbols), policy=policy, mock=True)

    def test_uptrend_opens_a_long(self):
        entries = self.run_tick()
        self.assertEqual(entries[0]["action"], "open")
        position = self.executor.position("BTCUSDT")
        self.assertEqual(position["side"], "long")
        self.assertLess(position["stop"], position["entry"])
        self.assertGreater(position["target"], position["entry"])

    def test_position_is_kept_across_ticks(self):
        self.run_tick()
        opened = dict(self.executor.position("BTCUSDT"))
        self.run_tick()
        self.assertEqual(self.executor.position("BTCUSDT")["entry"], opened["entry"])

    def test_entry_spacing_blocks_a_second_symbol(self):
        entries = self.run_tick(symbols=("BTCUSDT", "ETHUSDT"))
        self.assertEqual(entries[0]["action"], "open")
        self.assertEqual(entries[1]["action"], "blocked")

    def test_data_error_is_recorded_not_raised(self):
        class Broken(FakeData):
            def klines(self, *args, **kwargs):
                raise OSError("down")

        entries = self.run_tick(data=Broken())
        self.assertEqual(entries[0]["action"], "error")
        self.assertEqual(self.executor.position("BTCUSDT"), None)

    def test_flat_market_does_not_trade(self):
        flat = [dict(row, open=100.0, high=100.5, low=99.5, close=100.0)
                for row in candles(240)]
        entries = self.run_tick(data=FakeData(flat))
        self.assertIn(entries[0]["action"], ("none", "blocked"))
        self.assertEqual(self.executor.book["positions"], {})

    def test_stop_is_taken_on_the_next_tick(self):
        self.run_tick()
        position = self.executor.position("BTCUSDT")
        crash = candles(240)
        crash[-1] = dict(crash[-1], low=position["stop"] - 5, close=position["stop"] - 4)
        entries = self.run_tick(data=FakeData(crash))
        self.assertEqual(entries[0]["action"], "close")
        self.assertEqual(self.state["trades"][-1]["reason"], "stop")
        self.assertLess(self.state["trades"][-1]["pnl"], 0)

    def test_tick_records_signals_and_equity_curve(self):
        self.run_tick()
        entry = self.state["ticks"][-1]
        self.assertIn("signals", entry)
        self.assertIn("edge", entry)
        self.assertEqual(len(self.state["equity_curve"]), 1)
        self.assertIn("day_pnl_pct", self.state)

    def test_history_is_capped(self):
        self.state["ticks"] = [{"t": "x"}] * (bot.TICK_HISTORY + 20)
        self.run_tick()
        self.assertLessEqual(len(self.state["ticks"]), bot.TICK_HISTORY)


class StateTests(unittest.TestCase):
    def test_round_trip_through_disk(self):
        state = bot.load_state("/nonexistent.json", 500.0)
        state["ticks"].append({"t": "now", "symbol": "BTCUSDT", "action": "none"})
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "trader.json")
            bot.save_state(state, path)
            again = bot.load_state(path, 500.0)
        self.assertEqual(again["book"]["equity"], 500.0)
        self.assertEqual(len(again["ticks"]), 1)

    def test_corrupt_file_falls_back_to_a_fresh_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "trader.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ not json")
            state = bot.load_state(path, 250.0)
        self.assertEqual(state["book"]["equity"], 250.0)

    def test_day_rollover_resets_the_loss_counter(self):
        state = bot.load_state("/nonexistent.json", 1000.0)
        bot.roll_day(state, 1000.0, "2026-09-17")
        state["day"]["realized_pnl"] = -20.0
        bot.roll_day(state, 980.0, "2026-09-18")
        self.assertEqual(state["day"]["realized_pnl"], 0.0)
        self.assertEqual(state["day"]["start_equity"], 980.0)

    def test_same_day_keeps_the_counter(self):
        state = bot.load_state("/nonexistent.json", 1000.0)
        bot.roll_day(state, 1000.0, "2026-09-17")
        state["day"]["realized_pnl"] = -20.0
        bot.roll_day(state, 980.0, "2026-09-17")
        self.assertEqual(state["day"]["realized_pnl"], -20.0)

    def test_losses_stack_then_reset(self):
        state = bot.load_state("/nonexistent.json", 1000.0)
        state["policy_values"] = risk.merge_policy({})
        for _ in range(3):
            bot.record_trade(state, {"pnl": -5.0})
        self.assertEqual(state["consecutive_losses"], 3)
        self.assertGreater(state["cooldown_until"], bot.now_ms())
        bot.record_trade(state, {"pnl": 8.0})
        self.assertEqual(state["consecutive_losses"], 0)

    def test_realized_pnl_accumulates_for_the_day(self):
        state = bot.load_state("/nonexistent.json", 1000.0)
        bot.record_trade(state, {"pnl": -5.0})
        bot.record_trade(state, {"pnl": 12.0})
        self.assertAlmostEqual(state["day"]["realized_pnl"], 7.0)


class SafetyTests(unittest.TestCase):
    def test_live_mode_needs_an_explicit_opt_in(self):
        previous = os.environ.pop("BYBIT_ALLOW_LIVE", None)
        os.environ["TYPESAFE_API_KEY"] = "x"
        try:
            self.assertEqual(bot.main(["--mode", "live", "--once"]), 2)
        finally:
            os.environ.pop("TYPESAFE_API_KEY", None)
            if previous is not None:
                os.environ["BYBIT_ALLOW_LIVE"] = previous

    def test_missing_jev_key_without_mock_is_refused(self):
        previous = os.environ.pop("TYPESAFE_API_KEY", None)
        try:
            self.assertEqual(bot.main(["--mode", "paper", "--once"]), 2)
        finally:
            if previous is not None:
                os.environ["TYPESAFE_API_KEY"] = previous

    def test_paper_mode_reads_mainnet_market_data(self):
        self.assertEqual(bot.base_url_for("testnet"), bybit.TESTNET)
        self.assertEqual(bot.base_url_for("live"), bybit.MAINNET)
        self.assertEqual(bot.base_url_for("demo"), bybit.DEMO)

    def test_cli_overrides_reach_the_policy(self):
        args = bot.parse_args(["--max-leverage", "2", "--min-edge", "0.5"])
        policy = bot.build_policy(args)
        self.assertEqual(policy["max_leverage"], 2)
        self.assertEqual(policy["min_edge"], 0.5)
        self.assertEqual(policy["risk_per_trade"], risk.POLICY["risk_per_trade"])

    def test_dry_run_exchange_executor_sends_nothing(self):
        opener = RecordingOpener([])
        client = bybit.Bybit("k", "s", opener=opener)
        state = bot.load_state("/nonexistent.json", 1000.0)
        state["_filters"] = {"BTCUSDT": {"qty_step": 0.001, "tick_size": 0.1}}
        executor = bot.ExchangeExecutor(client, state, dry_run=True)
        result = executor.open("BTCUSDT", "long", 0.01, 100.0, 98.0, 104.0, 3, 0.5)
        self.assertTrue(result["ok"])
        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
