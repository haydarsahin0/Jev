"""Ileri yuruyen (walk-forward) degerlendirme ve kalibrasyon.

Kural: bir mac degerlendirilirken model YALNIZCA o tarihten onceki maclarla
egitilmis olur. Model `refit_days` gunde bir sifirdan yeniden kestirilir,
aradaki maclar o modelle tahmin edilir. Boylece raporlanan sayilar,
sistemin o gun gercekten verebilecegi tahminlerin sayilari olur.

Olculen sey: log-loss, Brier, RPS (siralamaya duyarli, 1X2 icin dogru olcu),
isabet orani ve kalibrasyon tablosu. Referanslar: duz 1/3 dagilimi ve
egitim verisinden gelen taban oranlar.
"""

from __future__ import annotations

import datetime as dt
import math

from . import blend as blend_module
from . import features as features_module
from . import jev as jev_module
from . import ratings as ratings_module

OUTCOMES = ("home", "draw", "away")
UNIFORM_LOG_LOSS = math.log(3.0)


def outcome_index(match):
    if match["hg"] > match["ag"]:
        return 0
    return 1 if match["hg"] == match["ag"] else 2


def _add_days(date, days):
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


def walk_forward(matches, start, end=None, refit_days=21, decay=None,
                 ridge=None, iterations=250, min_train=200, window=6,
                 api_key=None, use_jev=False, mock=False, cache=None,
                 model_name="jev-latest", notes=None, progress=None,
                 jev_limit=None):
    """`start`..`end` arasindaki maclari sirayla tahmin eder ve kaydeder.

    Doner: her mac icin bir satir. Satirlar hem metrik hesabinda hem de
    harman parametrelerinin aranmasinda kullanilir; Jev cagrildiysa yanit da
    satirda durur, boylece parametre aramasi tekrar para harcamaz.
    """
    decay = ratings_module.DEFAULT_DECAY if decay is None else decay
    ridge = ratings_module.DEFAULT_RIDGE if ridge is None else ridge

    history = features_module.History(matches)
    _, elo_pre = ratings_module.run_elo(history.matches)

    evaluated = [m for m in history.matches
                 if m["date"] >= start and (end is None or m["date"] <= end)]
    rows = []
    model = None
    model_valid_until = None
    jev_calls = 0

    for match in evaluated:
        date = match["date"]
        if model is None or date >= model_valid_until:
            train = history.before(date)
            if len(train) < min_train:
                continue
            model = ratings_module.fit_dixon_coles(
                train, as_of=date, decay=decay, ridge=ridge,
                iterations=iterations)
            model_valid_until = _add_days(date, refit_days)
            if progress:
                progress("  yeniden egitim %s (%d mac)" % (date, len(train)))

        bundle = features_module.build(
            history, match, model, window=window,
            elo_pre=elo_pre.get(ratings_module.elo_key(match)))

        jev_result = None
        if use_jev and (jev_limit is None or jev_calls < jev_limit):
            state = jev_module.build_state(bundle, notes=notes)
            try:
                jev_result, _ = jev_module.ask(state, api_key, model=model_name,
                                               mock=mock, cache=cache)
                jev_calls += 1
            except RuntimeError as error:
                if progress:
                    progress("  Jev atlandi (%s): %s" % (date, error))

        baseline = bundle["baseline"]
        row = {
            "date": date,
            "league": match.get("league"),
            "home": match["home"],
            "away": match["away"],
            "hg": match["hg"],
            "ag": match["ag"],
            "outcome": outcome_index(match),
            "baseline": [baseline["p_home"], baseline["p_draw"], baseline["p_away"]],
            "baseline_total": baseline["expected_total"],
            "lambda": [baseline["lambda_home"], baseline["lambda_away"]],
            "rho": model.get("rho", 0.0),
        }
        if jev_result:
            row["jev"] = {
                "probabilities": jev_result.get("probabilities"),
                "expected_total": jev_result.get("expected_total"),
                "blind_spot": jev_result.get("blind_spot"),
                "btts": jev_result.get("btts"),
            }
        if match.get("odds"):
            row["odds"] = match["odds"]
        rows.append(row)

    if cache is not None:
        cache.save()
    return rows


# --------------------------------------------------------------------------
# Metrikler
# --------------------------------------------------------------------------

def log_loss(probabilities, outcome):
    return -math.log(max(probabilities[outcome], 1e-12))


def brier(probabilities, outcome):
    total = 0.0
    for index, value in enumerate(probabilities):
        target = 1.0 if index == outcome else 0.0
        total += (value - target) ** 2
    return total


def ranked_probability_score(probabilities, outcome):
    """Siralamaya duyarli olcu: 1-X-2 dogal bir sira tasir.

    Ev galibiyetini bekleyip beraberlik gormek, deplasman galibiyeti
    gormekten daha az hatalidir; RPS bunu goren tek standart olcu.
    """
    cumulative_prediction = 0.0
    cumulative_actual = 0.0
    total = 0.0
    for index in range(len(probabilities) - 1):
        cumulative_prediction += probabilities[index]
        cumulative_actual += 1.0 if index == outcome else 0.0
        total += (cumulative_prediction - cumulative_actual) ** 2
    return total / (len(probabilities) - 1)


def probabilities_for(row, params=None, use_jev=True):
    """Bir backtest satirindan nihai olasiliklari uretir."""
    baseline = {
        "p_home": row["baseline"][0],
        "p_draw": row["baseline"][1],
        "p_away": row["baseline"][2],
        "expected_total": row.get("baseline_total", 2.7),
    }
    jev_result = row.get("jev") if use_jev else None
    merged = blend_module.combine(baseline, jev_result, params)
    return merged["probabilities"]


def evaluate(rows, params=None, use_jev=True, label=""):
    if not rows:
        return {"label": label, "matches": 0}

    totals = {"log_loss": 0.0, "brier": 0.0, "rps": 0.0}
    hits = 0
    base_rate = [0, 0, 0]
    for row in rows:
        probabilities = probabilities_for(row, params, use_jev)
        outcome = row["outcome"]
        totals["log_loss"] += log_loss(probabilities, outcome)
        totals["brier"] += brier(probabilities, outcome)
        totals["rps"] += ranked_probability_score(probabilities, outcome)
        if max(range(3), key=lambda index: probabilities[index]) == outcome:
            hits += 1
        base_rate[outcome] += 1

    count = len(rows)
    result = {
        "label": label,
        "matches": count,
        "log_loss": totals["log_loss"] / count,
        "brier": totals["brier"] / count,
        "rps": totals["rps"] / count,
        "accuracy": hits / count,
        "uniform_log_loss": UNIFORM_LOG_LOSS,
        "outcome_rates": [value / count for value in base_rate],
    }
    result["skill_vs_uniform"] = 1.0 - result["log_loss"] / UNIFORM_LOG_LOSS
    market = market_metrics(rows)
    if market:
        result["market"] = market
    return result


def market_metrics(rows):
    """Kapanis oranlari varsa piyasanin ayni maclardaki skoru.

    Piyasa, futbolda asilmasi cok zor bir referanstir; buradaki amac
    ustune cikmak degil, aradaki mesafeyi durustce gostermektir.
    """
    priced = [row for row in rows if row.get("odds")]
    if not priced:
        return None
    totals = {"log_loss": 0.0, "brier": 0.0, "rps": 0.0}
    hits = 0
    for row in priced:
        probabilities = _implied(row["odds"])
        outcome = row["outcome"]
        totals["log_loss"] += log_loss(probabilities, outcome)
        totals["brier"] += brier(probabilities, outcome)
        totals["rps"] += ranked_probability_score(probabilities, outcome)
        if max(range(3), key=lambda index: probabilities[index]) == outcome:
            hits += 1
    count = len(priced)
    return {
        "matches": count,
        "log_loss": totals["log_loss"] / count,
        "brier": totals["brier"] / count,
        "rps": totals["rps"] / count,
        "accuracy": hits / count,
    }


def _implied(odds):
    raw = [1.0 / value for value in odds]
    total = sum(raw)
    return [value / total for value in raw]


def calibration_table(rows, params=None, use_jev=True, bins=10):
    """Soylenen olasilik ile gerceklesen oran yan yana.

    Her tahmin uc gozleme sayilir (1, X, 2); kova genisligi 1/bins.
    """
    buckets = [{"count": 0, "predicted": 0.0, "observed": 0.0} for _ in range(bins)]
    for row in rows:
        probabilities = probabilities_for(row, params, use_jev)
        for index, value in enumerate(probabilities):
            slot = min(bins - 1, max(0, int(value * bins)))
            buckets[slot]["count"] += 1
            buckets[slot]["predicted"] += value
            buckets[slot]["observed"] += 1.0 if index == row["outcome"] else 0.0

    table = []
    for index, bucket in enumerate(buckets):
        if not bucket["count"]:
            continue
        table.append({
            "range": "%.1f-%.1f" % (index / bins, (index + 1) / bins),
            "count": bucket["count"],
            "predicted": bucket["predicted"] / bucket["count"],
            "observed": bucket["observed"] / bucket["count"],
        })
    return table


def calibration_error(rows, params=None, use_jev=True, bins=10):
    """Beklenen kalibrasyon hatasi (ECE): agirlikli |soylenen - gerceklesen|."""
    table = calibration_table(rows, params, use_jev, bins)
    total = sum(entry["count"] for entry in table) or 1
    return sum(entry["count"] * abs(entry["predicted"] - entry["observed"])
               for entry in table) / total


# --------------------------------------------------------------------------
# Parametre aramasi
# --------------------------------------------------------------------------

BASELINE_TEMPERATURES = [0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.20]
WEIGHTS = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.65]
GATES = [0.0, 0.35, 0.5, 0.75]
TEMPERATURES = [0.90, 0.95, 1.00, 1.05, 1.15]
TOTAL_WEIGHTS = [0.0, 0.25, 0.5, 0.75]


def fit_baseline_temperature(rows, candidates=BASELINE_TEMPERATURES):
    """Tabanin kendi guven ayari: Jev yokken bile ise yarar."""
    best, best_loss = 1.0, None
    for candidate in candidates:
        params = {"baseline_temperature": candidate, "weight": 0.0}
        loss = evaluate(rows, params, use_jev=False)["log_loss"]
        if best_loss is None or loss < best_loss:
            best, best_loss = candidate, loss
    return best, best_loss


def paired_gain(rows, params, baseline_params):
    """Mac basina log-loss farki: (ortalama_kazanc, standart_hata).

    Esli karsilastirma -- ayni maclarda taban ve harman. Kucuk orneklerde
    "Jev kazandirdi mi" sorusunun tek durust cevabi bu.
    """
    differences = []
    for row in rows:
        with_jev = log_loss(probabilities_for(row, params, True), row["outcome"])
        without = log_loss(
            probabilities_for(row, baseline_params, False), row["outcome"])
        differences.append(without - with_jev)
    count = len(differences)
    if count < 2:
        return (differences[0] if differences else 0.0), float("inf")
    mean = sum(differences) / count
    variance = sum((value - mean) ** 2 for value in differences) / (count - 1)
    return mean, math.sqrt(variance / count)


def fit_blend(rows, baseline_temperature=1.0, weights=WEIGHTS, gates=GATES,
              temperatures=TEMPERATURES, total_weights=TOTAL_WEIGHTS,
              min_gain=0.002, min_sigma=1.0):
    """Jev'li satirlar uzerinde harman parametrelerini arar (log-loss en az).

    Izgara bilincli olarak kaba: az sayida macla ogrenilen ince ayar,
    ogrenilmis degil ezberlenmis olur.

    Secilen agirlik ancak kazanc hem `min_gain` esigini hem de `min_sigma`
    standart hata sinirini gecerse korunur; gecemezse agirlik sifirlanir.
    Gurultuden kazanc uydurmamak icin sart: rastgele bir "Jev" bu esigi
    gecemez.
    """
    with_jev = [row for row in rows if row.get("jev", {}).get("probabilities")]
    if not with_jev:
        return None

    baseline_params = {"weight": 0.0,
                       "baseline_temperature": baseline_temperature}
    baseline_loss = evaluate(with_jev, baseline_params, use_jev=False)["log_loss"]

    best_params, best_loss = None, None
    for weight in weights:
        for gate in (gates if weight > 0 else [0.0]):
            for temperature in temperatures:
                for total_weight in (total_weights if weight > 0 else [0.0]):
                    params = {
                        "weight": weight,
                        "gate": gate,
                        "temperature": temperature,
                        "total_weight": total_weight,
                        "baseline_temperature": baseline_temperature,
                    }
                    loss = evaluate(with_jev, params, use_jev=True)["log_loss"]
                    if best_loss is None or loss < best_loss:
                        best_params, best_loss = params, loss

    gain, sigma = paired_gain(with_jev, best_params, baseline_params)
    significant = gain >= min_gain and gain >= min_sigma * sigma
    accepted = dict(best_params)
    if not significant:
        accepted["weight"] = 0.0
        accepted["total_weight"] = 0.0

    return {
        "params": accepted,
        "searched_params": best_params,
        "log_loss": best_loss,
        "accepted_log_loss": evaluate(with_jev, accepted,
                                      use_jev=accepted["weight"] > 0)["log_loss"],
        "matches": len(with_jev),
        "baseline_log_loss": baseline_loss,
        "gain": gain,
        "gain_stderr": sigma,
        "significant": significant,
    }


def search_hyperparameters(matches, start, end, decays, ridges, refit_days=28,
                           iterations=200, progress=None):
    """Zaman sonumu ve ridge icin walk-forward izgara aramasi.

    Pahali ama yilda bir kez yapilir; sonuc model dosyasina yazilir.
    """
    results = []
    for decay in decays:
        for ridge in ridges:
            rows = walk_forward(matches, start, end, refit_days=refit_days,
                                decay=decay, ridge=ridge, iterations=iterations,
                                progress=None)
            metrics = evaluate(rows, {"weight": 0.0}, use_jev=False)
            results.append({"decay": decay, "ridge": ridge,
                            "log_loss": metrics["log_loss"],
                            "rps": metrics["rps"],
                            "matches": metrics["matches"]})
            if progress:
                progress("  decay=%.5f ridge=%.3f -> log-loss %.4f (%d mac)"
                         % (decay, ridge, metrics["log_loss"], metrics["matches"]))
    results.sort(key=lambda item: item["log_loss"])
    return results
