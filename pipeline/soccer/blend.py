"""Istatistik tabani ile Jev'in tipli karari nasil birlestirilir.

Yontem logaritmik fikir havuzu (log-opinion pool):

    p ~ taban^(1-w) * jev^w      sonra sicaklik:  p ~ p^(1/T)

`w` backtest'te ogrenilir; mac basina Jev'in kendi `blind_spot` cevabiyla
olceklenir -- yani Jev "burada modelin goremedigi bir sey var" dedikce
sozu daha cok gecer, "ratingler ve form ayni seyi soyluyor" dedikce taban
agir basar. `T` de backtest'te ogrenilir ve asiri/eksik guveni duzeltir.

Hicbir adim sonucu bilmez: butun parametreler mac gununden onceki veriyle
kestirilir.
"""

from __future__ import annotations

import math

from . import grid as grid_module

DEFAULT_PARAMS = {
    "weight": 0.30,              # Jev'in temel agirligi
    "gate": 0.50,                # blind_spot'un agirligi ne kadar oynatacagi
    "max_weight": 0.75,          # tek macta Jev'e verilecek ust sinir
    "temperature": 1.00,         # son olasiliklarin sicakligi (>1 yumusatir)
    "baseline_temperature": 1.00,
    "total_weight": 0.50,        # beklenen toplam golde Jev'in payi
}


def normalize(values):
    values = [max(float(value), 1e-9) for value in values]
    total = sum(values)
    return [value / total for value in values]


def temper(probabilities, temperature):
    """T > 1 dagilimi duzlestirir, T < 1 keskinlestirir."""
    if temperature is None or abs(temperature - 1.0) < 1e-9:
        return normalize(probabilities)
    power = 1.0 / max(float(temperature), 1e-3)
    return normalize([max(value, 1e-9) ** power for value in probabilities])


def log_pool(first, second, weight):
    """first^(1-w) * second^w, normalize edilmis."""
    weight = max(0.0, min(1.0, float(weight)))
    if weight <= 0.0:
        return normalize(first)
    if weight >= 1.0:
        return normalize(second)
    pooled = []
    for left, right in zip(first, second):
        pooled.append(math.exp((1 - weight) * math.log(max(left, 1e-9))
                               + weight * math.log(max(right, 1e-9))))
    return normalize(pooled)


def effective_weight(params, blind_spot):
    """Jev'in bu mactaki agirligi.

    blind_spot bilinmiyorsa (soru cevapsiz dondu) taban agirlik kullanilir.
    """
    weight = float(params.get("weight", DEFAULT_PARAMS["weight"]))
    gate = float(params.get("gate", DEFAULT_PARAMS["gate"]))
    ceiling = float(params.get("max_weight", DEFAULT_PARAMS["max_weight"]))
    if blind_spot is None:
        scale = 1.0
    else:
        scale = (1.0 - gate) + gate * 2.0 * max(0.0, min(1.0, blind_spot))
    return max(0.0, min(ceiling, weight * scale))


def combine(baseline, jev_result, params=None):
    """Taban ve Jev'den nihai 1X2 + beklenen toplam gol.

    `baseline`: (p_ev, p_beraberlik, p_dep) ve beklenen toplam gol tasiyan
    sozluk. `jev_result`: jev.normalize_answers ciktisi veya None.
    """
    params = dict(DEFAULT_PARAMS, **(params or {}))
    base_probabilities = temper(
        [baseline["p_home"], baseline["p_draw"], baseline["p_away"]],
        params["baseline_temperature"])
    base_total = float(baseline["expected_total"])

    if not jev_result or not jev_result.get("probabilities"):
        final = temper(base_probabilities, params["temperature"])
        return {
            "probabilities": final,
            "expected_total": base_total,
            "jev_weight": 0.0,
            "blind_spot": (jev_result or {}).get("blind_spot"),
            "used_jev": False,
        }

    weight = effective_weight(params, jev_result.get("blind_spot"))
    pooled = log_pool(base_probabilities, jev_result["probabilities"], weight)
    final = temper(pooled, params["temperature"])

    total = base_total
    jev_total = jev_result.get("expected_total")
    if jev_total:
        total_weight = weight * float(params.get("total_weight", 0.5)) / max(
            float(params.get("weight", 0.3)) or 1e-6, 1e-6)
        total_weight = max(0.0, min(1.0, total_weight))
        total = (1 - total_weight) * base_total + total_weight * float(jev_total)

    return {
        "probabilities": final,
        "expected_total": total,
        "jev_weight": weight,
        "blind_spot": jev_result.get("blind_spot"),
        "jev_probabilities": jev_result["probabilities"],
        "used_jev": True,
    }


def predict(bundle, jev_result=None, params=None, rho=0.0, score_count=6):
    """Tek fikstur icin nihai tahmin kaydi.

    Once 1X2 harmanlanir, sonra skor izgarasi bu 1X2'ye ve harmanlanmis
    toplam gole oturtulur. Raporlanan her sey ayni izgaradan okunur.
    """
    baseline = bundle["baseline"]
    merged = combine(baseline, jev_result, params)

    markets, lam, mu = grid_module.grid_for_targets(
        merged["probabilities"],
        rho=rho,
        target_total=merged["expected_total"],
        start=(baseline["lambda_home"], baseline["lambda_away"]),
        score_count=score_count,
    )

    fixture = bundle["fixture"]
    home, draw, away = markets["p_home"], markets["p_draw"], markets["p_away"]
    pick = max((("1", home), ("X", draw), ("2", away)), key=lambda item: item[1])

    record = {
        "date": fixture["date"],
        "time": fixture.get("time"),
        "league": fixture.get("league"),
        "home": fixture["home"],
        "away": fixture["away"],
        "probabilities": {"home": home, "draw": draw, "away": away},
        "odds_fair": {
            "home": _fair_odds(home),
            "draw": _fair_odds(draw),
            "away": _fair_odds(away),
        },
        "pick": pick[0],
        "pick_probability": pick[1],
        "double_chance": markets["double_chance"],
        "expected_goals": {"home": lam, "away": mu},
        "expected_total": markets["expected_total"],
        "scores": markets["top_scores"],
        "score": markets["top_scores"][0]["score"] if markets["top_scores"] else None,
        "over_under": {
            "over_1_5": markets["over_1_5"],
            "over_2_5": markets["over_2_5"],
            "over_3_5": markets["over_3_5"],
        },
        "btts": markets["btts"],
        "baseline": {
            "home": baseline["p_home"],
            "draw": baseline["p_draw"],
            "away": baseline["p_away"],
            "expected_total": baseline["expected_total"],
        },
        "jev": {
            "used": merged["used_jev"],
            "weight": merged["jev_weight"],
            "blind_spot": merged.get("blind_spot"),
            "probabilities": merged.get("jev_probabilities"),
            "expected_total": (jev_result or {}).get("expected_total"),
            "btts": (jev_result or {}).get("btts"),
        },
        "unknown_teams": [
            side for side in ("home", "away")
            if not fixture["known_teams"][side]
        ],
    }
    return record


def _fair_odds(probability):
    if probability <= 1e-9:
        return None
    return 1.0 / probability
