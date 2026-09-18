"""Skor izgarasi: (lambda, mu, rho) -> skor olasiliklari ve pazarlar.

Ters yon de burada: harmanlanmis 1X2 olasiligi ve beklenen toplam gol
verildiginde, onlara en yakin dusen (lambda, mu) ikilisi aranir. Boylece
raporlanan skor tahminleri ile raporlanan 1X2 olasiliklari birbirini tutar --
iki ayri modelden iki ayri cevap cikmaz.
"""

from __future__ import annotations

import math

from .ratings import tau

MAX_GOALS = 10


def _poisson(k, rate):
    return math.exp(-rate + k * math.log(rate) - _log_factorial(k))


_LOG_FACT = [0.0]


def _log_factorial(k):
    while len(_LOG_FACT) <= k:
        _LOG_FACT.append(_LOG_FACT[-1] + math.log(len(_LOG_FACT)))
    return _LOG_FACT[k]


def score_matrix(lam, mu, rho=0.0, max_goals=MAX_GOALS):
    """Normalize edilmis skor olasiligi matrisi: matrix[ev_gol][dep_gol]."""
    lam = min(max(float(lam), 1e-3), 15.0)
    mu = min(max(float(mu), 1e-3), 15.0)
    home_pmf = [_poisson(k, lam) for k in range(max_goals + 1)]
    away_pmf = [_poisson(k, mu) for k in range(max_goals + 1)]

    matrix = []
    total = 0.0
    for x in range(max_goals + 1):
        row = []
        for y in range(max_goals + 1):
            value = home_pmf[x] * away_pmf[y] * max(tau(x, y, lam, mu, rho), 1e-9)
            row.append(value)
            total += value
        matrix.append(row)

    if total <= 0:
        raise ValueError("skor izgarasi bos")
    return [[value / total for value in row] for row in matrix]


def outcome_probabilities(matrix):
    """(ev galibiyeti, beraberlik, deplasman galibiyeti)."""
    home = draw = away = 0.0
    for x, row in enumerate(matrix):
        for y, value in enumerate(row):
            if x > y:
                home += value
            elif x == y:
                draw += value
            else:
                away += value
    total = home + draw + away
    return home / total, draw / total, away / total


def over_probability(matrix, line=2.5):
    total = 0.0
    for x, row in enumerate(matrix):
        for y, value in enumerate(row):
            if x + y > line:
                total += value
    return total


def btts_probability(matrix):
    total = 0.0
    for x, row in enumerate(matrix):
        if x == 0:
            continue
        total += sum(row[1:])
    return total


def expected_total(matrix):
    total = 0.0
    for x, row in enumerate(matrix):
        for y, value in enumerate(row):
            total += value * (x + y)
    return total


def top_scores(matrix, count=5):
    """En olasi skorlar: [{'score': '2-1', 'p': 0.11}, ...]."""
    flat = []
    for x, row in enumerate(matrix):
        for y, value in enumerate(row):
            flat.append((value, x, y))
    flat.sort(reverse=True)
    return [{"score": "%d-%d" % (x, y), "p": value} for value, x, y in flat[:count]]


def double_chance(home, draw, away):
    return {"1X": home + draw, "12": home + away, "X2": draw + away}


def markets(lam, mu, rho=0.0, max_goals=MAX_GOALS, score_count=5):
    """Tek cagrida butun turetilmis pazarlar."""
    matrix = score_matrix(lam, mu, rho, max_goals)
    home, draw, away = outcome_probabilities(matrix)
    return {
        "lambda_home": lam,
        "lambda_away": mu,
        "p_home": home,
        "p_draw": draw,
        "p_away": away,
        "double_chance": double_chance(home, draw, away),
        "over_2_5": over_probability(matrix, 2.5),
        "over_1_5": over_probability(matrix, 1.5),
        "over_3_5": over_probability(matrix, 3.5),
        "btts": btts_probability(matrix),
        "expected_total": expected_total(matrix),
        "top_scores": top_scores(matrix, score_count),
    }


# --------------------------------------------------------------------------
# Ters cozum
# --------------------------------------------------------------------------

def _objective(lam, mu, rho, target_probs, target_total, total_weight):
    matrix = score_matrix(lam, mu, rho)
    home, draw, away = outcome_probabilities(matrix)
    loss = 0.0
    for target, current in zip(target_probs, (home, draw, away)):
        if target > 0:
            loss += target * math.log(target / max(current, 1e-9))
    if target_total:
        gap = math.log(expected_total(matrix)) - math.log(target_total)
        loss += total_weight * gap * gap
    return loss


def solve_lambdas(target_probs, rho=0.0, target_total=None, start=(1.4, 1.1),
                  total_weight=0.6, iterations=60, step=0.18, tol=1e-7):
    """Hedef 1X2 (ve varsa toplam gol) ile tutarli (lambda, mu) bulur.

    KL(hedef || izgara) + toplam gol cezasi, log uzayinda sayisal turevle
    azaltilir. Iki parametre oldugu icin basit bir inis yeterli ve hizlidir.
    """
    target_probs = _normalize3(target_probs)
    log_lam = math.log(max(start[0], 1e-3))
    log_mu = math.log(max(start[1], 1e-3))
    current = _objective(math.exp(log_lam), math.exp(log_mu), rho,
                         target_probs, target_total, total_weight)

    delta = 1e-4
    for _ in range(iterations):
        base_lam, base_mu = math.exp(log_lam), math.exp(log_mu)
        grad_lam = (_objective(math.exp(log_lam + delta), base_mu, rho,
                               target_probs, target_total, total_weight)
                    - current) / delta
        grad_mu = (_objective(base_lam, math.exp(log_mu + delta), rho,
                              target_probs, target_total, total_weight)
                   - current) / delta

        moved = False
        trial_step = step
        for _ in range(6):
            next_log_lam = log_lam - trial_step * grad_lam
            next_log_mu = log_mu - trial_step * grad_mu
            next_log_lam = min(max(next_log_lam, math.log(0.05)), math.log(9.0))
            next_log_mu = min(max(next_log_mu, math.log(0.05)), math.log(9.0))
            candidate = _objective(math.exp(next_log_lam), math.exp(next_log_mu),
                                   rho, target_probs, target_total, total_weight)
            if candidate < current:
                log_lam, log_mu = next_log_lam, next_log_mu
                if current - candidate < tol:
                    current = candidate
                    moved = False
                    break
                current = candidate
                moved = True
                break
            trial_step *= 0.5
        if not moved:
            break

    return math.exp(log_lam), math.exp(log_mu)


def _normalize3(values):
    values = [max(float(value), 1e-6) for value in values]
    total = sum(values)
    return [value / total for value in values]


def reweight_to_outcomes(matrix, target_probs):
    """Izgarayi hedef 1X2'ye birebir oturtur.

    Ev/beraberlik/deplasman hucre gruplarinin her biri hedef toplamina
    olceklenir; grup ICINDEKI skor dagilimi Poisson sekliyle aynen kalir.
    Boylece raporlanan skorlar ile raporlanan 1X2 celismez -- ayni izgaradan
    okunurlar.
    """
    home_target, draw_target, away_target = _normalize3(target_probs)
    current = outcome_probabilities(matrix)
    scales = []
    for target, value in zip((home_target, draw_target, away_target), current):
        scales.append(target / value if value > 1e-12 else 0.0)

    adjusted = []
    for x, row in enumerate(matrix):
        new_row = []
        for y, value in enumerate(row):
            group = 0 if x > y else (1 if x == y else 2)
            new_row.append(value * scales[group])
        adjusted.append(new_row)

    total = sum(sum(row) for row in adjusted)
    if total <= 0:
        return matrix
    return [[value / total for value in row] for row in adjusted]


def markets_from_matrix(matrix, lam=None, mu=None, score_count=5):
    """Hazir bir izgaradan butun pazarlari okur."""
    home, draw, away = outcome_probabilities(matrix)
    return {
        "lambda_home": lam,
        "lambda_away": mu,
        "p_home": home,
        "p_draw": draw,
        "p_away": away,
        "double_chance": double_chance(home, draw, away),
        "over_2_5": over_probability(matrix, 2.5),
        "over_1_5": over_probability(matrix, 1.5),
        "over_3_5": over_probability(matrix, 3.5),
        "btts": btts_probability(matrix),
        "expected_total": expected_total(matrix),
        "top_scores": top_scores(matrix, score_count),
    }


def grid_for_targets(target_probs, rho=0.0, target_total=None, start=(1.4, 1.1),
                     score_count=5):
    """Hedeflere oturtulmus izgara + pazarlar.

    Once (lambda, mu) toplam gol ve galibiyet dengesi icin cozulur, sonra
    izgara hedef 1X2'ye birebir olceklenir. Doner: (pazarlar, lambda, mu).
    """
    lam, mu = solve_lambdas(target_probs, rho=rho, target_total=target_total,
                            start=start)
    matrix = reweight_to_outcomes(score_matrix(lam, mu, rho), target_probs)
    return markets_from_matrix(matrix, lam, mu, score_count), lam, mu
