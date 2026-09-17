#!/usr/bin/env python3
"""TypeSafe Jev istemcisi -- alim satim kararlari icin.

Jev metin degil **tipli karar** dondurur. Bot bunu kullanir:

    direction     choice  long / short / flat      (+ confidence)
    conviction    score   4 seviyeli rubrik        (olasilik agirlikli konum)
    trend_quality score   3 seviyeli rubrik
    entry_timing  noul    0-1 kalibre olasilik
    chop_risk     noul    0-1
    exit_now      noul    0-1  (yalnizca acik pozisyon varken sorulur)

Modelin serbest metni yorumlanmaz; sayisal cevaplar risk.py'deki deterministik
politikaya girer. Boylece ayni cevaplar her zaman ayni emri uretir.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
TIMEOUT = 60
MAX_ATTEMPTS = 4
USER_AGENT = "jev-trader/1.0 (+https://github.com/haydarsahin0/Jev)"

CONVICTION_LEVELS = [
    "No usable setup: signals disagree or the market is directionless",
    "Weak: a plausible lean, but easily invalidated by noise",
    "Solid: several independent signals point the same way",
    "Strong: trend, momentum and location agree with little conflicting evidence",
]

TREND_LEVELS = [
    "No trend: price oscillates inside a range",
    "Developing trend: direction is forming but not yet confirmed",
    "Established trend: ordered moving averages and higher highs or lower lows",
]

DIRECTIONS = {
    "long": "Buy: the next move over the coming bars is more likely up than down",
    "short": "Sell: the next move over the coming bars is more likely down than up",
    "flat": "Stay out: no position is justified by the current state",
}

BASE_QUESTIONS = {
    "direction": {
        "type": "choice",
        "instructions": (
            "Using only `market`, which side has the better expectancy for the "
            "next few bars on this timeframe? Choose `flat` whenever the state "
            "does not clearly favour a side."
        ),
        "criteria": DIRECTIONS,
    },
    "conviction": {
        "type": "score",
        "instructions": "How strong is the evidence in `market` for the side you chose?",
        "criteria": CONVICTION_LEVELS,
    },
    "trend_quality": {
        "type": "score",
        "instructions": "How well established is the trend described by `market`?",
        "criteria": TREND_LEVELS,
    },
    "entry_timing": {
        "type": "noul",
        "instructions": (
            "Is this a reasonable place to enter rather than a chase of an "
            "already extended move? Consider `price_above_fast_ema_in_atr`, "
            "`range_position_48_bars` and `rsi_14`."
        ),
    },
    "chop_risk": {
        "type": "noul",
        "instructions": (
            "Is the market choppy or mean-reverting enough that a trend entry "
            "would likely be stopped out before reaching its target?"
        ),
    },
    "crowding_risk": {
        "type": "noul",
        "instructions": (
            "Does `market` look crowded or stretched -- extreme funding, an "
            "exhausted move or unusual volume -- so that a sharp reversal "
            "against the obvious side is likely?"
        ),
    },
}

EXIT_QUESTION = {
    "exit_now": {
        "type": "noul",
        "instructions": (
            "Given `open_position` and `market`, should the position be closed "
            "now instead of held for its target?"
        ),
    },
}

SIGNAL_DEFAULTS = {
    "conviction": 0.0,
    "trend_quality": 0.0,
    "entry_timing": 0.0,
    "chop_risk": 1.0,
    "crowding_risk": 1.0,
}


def questions_for(has_position):
    questions = dict(BASE_QUESTIONS)
    if has_position:
        questions.update(EXIT_QUESTION)
    return questions


def _retry_delay(attempt, retry_after=None, base=2.0, cap=30.0):
    if retry_after is not None:
        return min(float(retry_after), 60.0)
    return min(base ** attempt, cap) + random.uniform(0, 0.5)


def _parse_retry_after(headers):
    if headers is None:
        return None
    for key, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        value = headers.get(key)
        if value:
            try:
                return float(value) / divisor
            except ValueError:
                continue
    return None


def ask(state, api_key, questions=None, endpoint=ENDPOINT, model=MODEL,
        max_attempts=MAX_ATTEMPTS, sleep=time.sleep, opener=None):
    """POST /v1/systemone -> `answers` sozlugu.

    429 ve 5xx yanitlarinda ustel geri cekilme ile yeniden dener. API anahtari
    yalnizca Authorization basliginda kullanilir, loglanmaz.
    """
    open_url = opener or urllib.request.urlopen
    body = json.dumps({
        "state": state,
        "model": model,
        "questions": questions if questions is not None else BASE_QUESTIONS,
    }).encode("utf-8")
    last_error = None

    for attempt in range(max_attempts):
        request = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            })
        try:
            with open_url(request, timeout=TIMEOUT) as response:
                parsed = json.loads(response.read().decode("utf-8"))
            answers = parsed.get("answers")
            if not isinstance(answers, dict):
                raise ValueError("yanitta 'answers' sozlugu yok")
            return answers
        except urllib.error.HTTPError as error:
            last_error = "HTTP %s" % error.code
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == max_attempts - 1:
                raise RuntimeError(last_error) from None
            sleep(_retry_delay(attempt, _parse_retry_after(error.headers)))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            last_error = type(error).__name__
            if attempt == max_attempts - 1:
                raise RuntimeError(last_error) from None
            sleep(_retry_delay(attempt))

    raise RuntimeError(last_error or "bilinmeyen hata")


def normalize(answers):
    """Jev yanitini 0-1 arasi sinyallere cevirir.

    noul  -> olasiligin kendisi sinyaldir (tasarim geregi confidence tasimaz).
    score -> seviyeler uzerinde olasilik agirlikli konum; en buyuk seviye
             indeksine (seviye_sayisi - 1) bolunerek 0-1'e cekilir.
    choice-> secilen etiket + confidence.

    Eksik veya bozuk cevaplar **guvenli tarafa** dusulur: yon `flat`, risk
    sinyalleri 1.0. Boylece kirik bir yanit asla islem acmaz.
    """
    signals = dict(SIGNAL_DEFAULTS)
    signals["exit_now"] = None

    for key in ("conviction", "trend_quality"):
        answer = answers.get(key)
        if isinstance(answer, dict) and isinstance(answer.get("score"), (int, float)):
            signals[key] = clamp01(float(answer["score"]) / max_level(answer))

    for key in ("entry_timing", "chop_risk", "crowding_risk", "exit_now"):
        answer = answers.get(key)
        if isinstance(answer, dict) and isinstance(answer.get("noul"), (int, float)):
            signals[key] = clamp01(float(answer["noul"]))

    side = "flat"
    confidence = 0.0
    choice = answers.get("direction")
    if isinstance(choice, dict):
        name = choice.get("choice")
        if isinstance(name, str) and name.lower() in DIRECTIONS:
            side = name.lower()
        confidence = clamp01(_float(choice.get("confidence")))
    signals["side"] = side
    signals["direction_confidence"] = confidence
    return signals


def max_level(answer):
    """Score yanitinda en buyuk seviye indeksi = seviye_sayisi - 1."""
    levels = []
    for field in ("probabilities", "legend"):
        block = answer.get(field)
        if isinstance(block, dict):
            for key in block:
                try:
                    levels.append(int(key))
                except (TypeError, ValueError):
                    continue
        if levels:
            break
    top = max(levels) if levels else 0
    return top if top > 0 else 1


def clamp01(value):
    if value != value:      # NaN
        return 0.0
    return max(0.0, min(1.0, value))


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def mock_answers(state, has_position=False):
    """Anahtarsiz deneme icin: durumdan turetilmis, deterministik sahte yanit.

    Gercek bir strateji degil. Amaci boru hattinin ucundan ucuna calistigini
    gostermek; yine de tutarli olsun diye sayilar durumun kendisinden tureler
    (dizilim netse kanaat yuksek ve testere riski dusuk, RSI ucta ise
    kalabalik riski yuksek).
    """
    market = state.get("market", {})
    seed = hashlib.sha256(json.dumps(market, sort_keys=True).encode("utf-8")).digest()

    def unit(index):
        return seed[index] / 255.0

    stack = market.get("trend_ema_stack")
    rsi_value = market.get("rsi_14")
    rsi_value = 50.0 if rsi_value is None else float(rsi_value)
    position_in_range = market.get("range_position_48_bars")
    aligned = stack in ("yukari", "asagi")
    stretched = rsi_value > 75.0 or rsi_value < 25.0

    if stack == "yukari" and rsi_value < 78:
        side, confidence = "long", 0.60 + 0.30 * unit(0)
    elif stack == "asagi" and rsi_value > 22:
        side, confidence = "short", 0.60 + 0.30 * unit(1)
    else:
        side, confidence = "flat", 0.40 + 0.25 * unit(2)

    conviction = (1.7 + 1.2 * unit(3)) if aligned else (0.3 + 0.9 * unit(3))
    trend_quality = (1.2 + 0.8 * unit(4)) if aligned else (0.2 + 0.6 * unit(4))
    timing = 0.80 - 0.35 * unit(5)
    if isinstance(position_in_range, (int, float)):
        extreme = abs(float(position_in_range) - 0.5) * 2.0
        timing -= 0.25 * max(0.0, extreme - 0.75)
    chop = (0.08 + 0.25 * unit(6)) if aligned else (0.45 + 0.40 * unit(6))
    crowding = 0.08 + 0.25 * unit(7) + (0.35 if stretched else 0.0)

    answers = {
        "direction": {"type": "choice", "choice": side,
                      "confidence": round(min(confidence, 0.97), 3)},
        "conviction": {"type": "score", "score": round(conviction, 2),
                       "probabilities": {"0": 0.1, "1": 0.3, "2": 0.4, "3": 0.2}},
        "trend_quality": {"type": "score", "score": round(trend_quality, 2),
                          "probabilities": {"0": 0.2, "1": 0.4, "2": 0.4}},
        "entry_timing": {"type": "noul", "noul": round(clamp01(timing), 3)},
        "chop_risk": {"type": "noul", "noul": round(clamp01(chop), 3)},
        "crowding_risk": {"type": "noul", "noul": round(clamp01(crowding), 3)},
    }
    if has_position:
        answers["exit_now"] = {"type": "noul", "noul": round(0.10 + 0.45 * unit(8), 3)}
    return answers


# --------------------------------------------------------------------------
# Onay soru seti: mekanik kural tetikledikten SONRA sorulur
# --------------------------------------------------------------------------
#
# Burada yon sorulmaz. Yonu kural belirler (RSI-2). Jev'in isi tek bir sey:
# bu **ornegi** almaya deger mi, yoksa atlanmali mi? Trend sorulari burada
# yaniltici olurdu -- asiri satimdan alis zaten trende karsi bir islemdir.

SETUP_QUALITY_LEVELS = [
    "Poor: this looks like the start of a sustained move against the entry, "
    "not a stretch that snaps back",
    "Acceptable: an ordinary short-term stretch where mean reversion is plausible",
    "Good: a stretched move showing exhaustion -- fading momentum, shrinking "
    "bars or a level that has held before",
]

TAKE_OR_SKIP = {
    "take": "Take this instance of the rule",
    "skip": "Skip this instance: the context makes it a poor version of the setup",
}

CONFIRM_QUESTIONS = {
    "take_setup": {
        "type": "choice",
        "instructions": (
            "A mechanical rule in `setup` has fired: RSI stayed beyond its "
            "threshold for the required number of closed bars, so the side is "
            "already decided. Using `market` and `setup`, should this instance "
            "be taken or skipped? Judge the instance, not the rule."
        ),
        "criteria": TAKE_OR_SKIP,
    },
    "setup_quality": {
        "type": "score",
        "instructions": (
            "How good is this instance of the mean-reversion setup described "
            "by `setup` and `market`?"
        ),
        "criteria": SETUP_QUALITY_LEVELS,
    },
    "falling_knife": {
        "type": "noul",
        "instructions": (
            "Is price in a strong one-way move that is likely to keep running "
            "against this entry -- a trend or news-driven flush rather than a "
            "stretch that mean-reverts?"
        ),
    },
    "crowding_risk": {
        "type": "noul",
        "instructions": (
            "Does `market` look crowded or stretched -- extreme funding, an "
            "exhausted move or unusual volume -- so that a sharp move against "
            "this entry is likely?"
        ),
    },
}

CONFIRM_DEFAULTS = {
    "setup_quality": 0.0,
    "falling_knife": 1.0,
    "crowding_risk": 1.0,
}


def normalize_confirm(answers):
    """Onay yanitini sinyallere cevirir.

    Eksik veya bozuk yanit **atla** demektir: onay katmani yalnizca hayir
    diyebildigi icin, cevabin okunamamasi islem acmamakla sonuclanir.
    """
    signals = dict(CONFIRM_DEFAULTS)
    signals["take"] = False
    signals["take_confidence"] = 0.0

    answer = answers.get("setup_quality")
    if isinstance(answer, dict) and isinstance(answer.get("score"), (int, float)):
        signals["setup_quality"] = clamp01(float(answer["score"]) / max_level(answer))

    for key in ("falling_knife", "crowding_risk"):
        answer = answers.get(key)
        if isinstance(answer, dict) and isinstance(answer.get("noul"), (int, float)):
            signals[key] = clamp01(float(answer["noul"]))

    choice = answers.get("take_setup")
    if isinstance(choice, dict):
        name = choice.get("choice")
        if isinstance(name, str) and name.lower() == "take":
            signals["take"] = True
            signals["take_confidence"] = clamp01(_float(choice.get("confidence")))
    return signals


def mock_confirm(state):
    """Anahtarsiz deneme icin onay yaniti; durumdan turer, rastgele degil."""
    payload = {"market": state.get("market", {}), "setup": state.get("setup", {})}
    seed = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).digest()

    def unit(index):
        return seed[index] / 255.0

    market = payload["market"]
    setup = payload["setup"]
    side = setup.get("side")
    change = market.get("return_6_bars_pct")
    change = 0.0 if not isinstance(change, (int, float)) else float(change)
    # Girise karsi guclu bir hareket varsa bicak riski yuksek sayilir.
    against = (-change if side == "long" else change)
    knife = clamp01(0.08 + 0.22 * unit(0) + min(0.45, max(0.0, against) * 0.12))
    quality = 2.0 - 1.4 * knife - 0.3 * unit(1)

    return {
        "take_setup": {"type": "choice",
                       "choice": "take" if knife < 0.50 else "skip",
                       "confidence": round(0.55 + 0.35 * unit(2), 3)},
        "setup_quality": {"type": "score", "score": round(max(0.0, quality), 2),
                          "probabilities": {"0": 0.2, "1": 0.4, "2": 0.4}},
        "falling_knife": {"type": "noul", "noul": round(knife, 3)},
        "crowding_risk": {"type": "noul",
                          "noul": round(clamp01(0.08 + 0.30 * unit(3)), 3)},
    }
