"""Egitim: zaman agirlikli Dixon-Coles Poisson MLE ve Elo.

Egitilen sey Jev degil -- Jev'in ogrenilebilir agirligi yok, tipli bir karar
API'sidir. Egitilen, maclarin kendisinden cikarilan sayilardir:

    lambda = exp(hucum_ev  - savunma_dep + ev_avantaji)
    mu     = exp(hucum_dep - savunma_ev)

artı dusuk skorlu sonuclari duzelten Dixon-Coles `rho` katsayisi. Eski maclar
`decay` ile ustel olarak azalan agirlik alir, takim parametreleri `ridge` ile
lig ortalamasina cekilir (yeni cikan takimlar icin sart).

Saf Python: numpy/scipy yok, Adam + rho icin 1 boyutlu arama.
"""

from __future__ import annotations

import datetime as dt
import math

# Varsayilanlar Premier Lig uzerinde walk-forward izgara aramasiyla secildi
# (2024-08..2026-06, 760 mac): decay 0.003 -> ~231 gun yari omur.
DEFAULT_DECAY = 0.0030      # gun basina
DEFAULT_RIDGE = 0.0030
DEFAULT_ITERATIONS = 500
DEFAULT_LR = 0.08
RHO_BOUNDS = (-0.28, 0.28)
RHO_EVERY = 25              # kac adimda bir rho yeniden aranir

ELO_BASE = 1500.0
ELO_K = 20.0
ELO_HOME = 60.0
ELO_SEASON_REGRESS = 0.25   # sezon basinda ortalamaya cekme orani


# --------------------------------------------------------------------------
# Dixon-Coles
# --------------------------------------------------------------------------

def tau(x, y, lam, mu, rho):
    """Dusuk skorlu dort hucre icin bagimlilik duzeltmesi."""
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    if x == 0 and y == 1:
        return 1.0 + lam * rho
    if x == 1 and y == 0:
        return 1.0 + mu * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def _tau_grads(x, y, lam, mu, rho):
    """(dlog(tau)/dlambda, dlog(tau)/dmu). Duzeltme disi hucrelerde (0, 0)."""
    value = tau(x, y, lam, mu, rho)
    if value <= 1e-9:
        return 0.0, 0.0
    if x == 0 and y == 0:
        return -mu * rho / value, -lam * rho / value
    if x == 0 and y == 1:
        return rho / value, 0.0
    if x == 1 and y == 0:
        return 0.0, rho / value
    return 0.0, 0.0


def _days_between(earlier, later):
    return (dt.date.fromisoformat(later) - dt.date.fromisoformat(earlier)).days


def build_weights(matches, as_of, decay):
    """Her mac icin exp(-decay * gun_farki); gelecek maclar agirlik 1 alir."""
    weights = []
    for match in matches:
        age = _days_between(match["date"], as_of)
        weights.append(math.exp(-decay * age) if age > 0 else 1.0)
    return weights


def fit_dixon_coles(matches, as_of=None, decay=DEFAULT_DECAY,
                    ridge=DEFAULT_RIDGE, iterations=DEFAULT_ITERATIONS,
                    lr=DEFAULT_LR, rho=0.0, verbose=False):
    """Oynanmis maclardan takim parametrelerini kestirir.

    `matches` yalnizca `hg`/`ag` tasiyan kayitlar olmalidir; cagiran taraf
    tarih filtresini uygular, boylece sizinti buraya hic gelmez.
    """
    matches = [m for m in matches
               if isinstance(m.get("hg"), int) and isinstance(m.get("ag"), int)]
    if not matches:
        raise ValueError("egitim icin oynanmis mac yok")

    as_of = as_of or max(match["date"] for match in matches)
    weights = build_weights(matches, as_of, decay)
    total_weight = sum(weights) or 1.0

    teams = sorted({m["home"] for m in matches} | {m["away"] for m in matches})
    index = {name: position for position, name in enumerate(teams)}
    count = len(teams)

    attack = [0.0] * count
    defence = [0.0] * count
    home_adv = 0.25

    # Adam durumu: hucum, savunma, ev avantaji tek vektorde tutulur.
    size = 2 * count + 1
    moment1 = [0.0] * size
    moment2 = [0.0] * size
    beta1, beta2, eps = 0.9, 0.999, 1e-8

    rows = [(index[m["home"]], index[m["away"]], m["hg"], m["ag"], weights[i])
            for i, m in enumerate(matches)]

    for step in range(1, iterations + 1):
        grad = [0.0] * size
        for home_i, away_i, x, y, weight in rows:
            lam = math.exp(attack[home_i] - defence[away_i] + home_adv)
            mu = math.exp(attack[away_i] - defence[home_i])
            lam = min(max(lam, 1e-6), 25.0)
            mu = min(max(mu, 1e-6), 25.0)

            d_lam, d_mu = _tau_grads(x, y, lam, mu, rho)
            # d/dtheta [x*log(lam) - lam + log(tau)] = (x - lam) + lam*dlogtau/dlam
            home_term = weight * ((x - lam) + lam * d_lam)
            away_term = weight * ((y - mu) + mu * d_mu)

            grad[home_i] += home_term                  # hucum_ev
            grad[count + away_i] -= home_term          # savunma_dep
            grad[away_i] += away_term                  # hucum_dep
            grad[count + home_i] -= away_term          # savunma_ev
            grad[2 * count] += home_term               # ev avantaji

        for position in range(size):
            grad[position] /= total_weight
        for position in range(count):
            grad[position] -= 2.0 * ridge * attack[position]
            grad[count + position] -= 2.0 * ridge * defence[position]

        for position in range(size):
            moment1[position] = beta1 * moment1[position] + (1 - beta1) * grad[position]
            moment2[position] = (beta2 * moment2[position]
                                 + (1 - beta2) * grad[position] * grad[position])
            corrected1 = moment1[position] / (1 - beta1 ** step)
            corrected2 = moment2[position] / (1 - beta2 ** step)
            delta = lr * corrected1 / (math.sqrt(corrected2) + eps)
            if position < count:
                attack[position] += delta
            elif position < 2 * count:
                defence[position - count] += delta
            else:
                home_adv += delta

        # Kimlik belirsizligini kaldir: hucum ve savunma ortalamasi sifir.
        attack = _center(attack)
        defence = _center(defence)
        home_adv = max(-1.0, min(1.0, home_adv))

        if step % RHO_EVERY == 0 or step == iterations:
            rho = _fit_rho(rows, attack, defence, home_adv, count)
            if verbose:
                ll = _log_likelihood(rows, attack, defence, home_adv, rho)
                print("  adim %4d  LL=%.4f  rho=%+.3f  ev=%.3f"
                      % (step, ll / total_weight, rho, home_adv), flush=True)

    log_likelihood = _log_likelihood(rows, attack, defence, home_adv, rho)
    return {
        "format": "jev-soccer-dc/1",
        "as_of": as_of,
        "decay": decay,
        "ridge": ridge,
        "home_advantage": home_adv,
        "rho": rho,
        "matches": len(matches),
        "effective_matches": total_weight,
        "log_likelihood_per_match": log_likelihood / total_weight,
        "teams": {
            name: {"attack": attack[index[name]], "defence": defence[index[name]]}
            for name in teams
        },
    }


def _center(values):
    if not values:
        return values
    mean = sum(values) / len(values)
    return [value - mean for value in values]


def _log_likelihood(rows, attack, defence, home_adv, rho):
    total = 0.0
    for home_i, away_i, x, y, weight in rows:
        lam = math.exp(attack[home_i] - defence[away_i] + home_adv)
        mu = math.exp(attack[away_i] - defence[home_i])
        adjust = tau(x, y, lam, mu, rho)
        if adjust <= 1e-9:
            adjust = 1e-9
        total += weight * (x * math.log(lam) - lam + y * math.log(mu) - mu
                           + math.log(adjust))
    return total


def _fit_rho(rows, attack, defence, home_adv, count, steps=57):
    """rho yalnizca dort dusuk skor hucresini etkiler: 1 boyutlu izgara yeter."""
    low, high = RHO_BOUNDS
    best_value, best_score = 0.0, None
    for step in range(steps):
        candidate = low + (high - low) * step / (steps - 1)
        score = 0.0
        feasible = True
        for home_i, away_i, x, y, weight in rows:
            if x > 1 or y > 1:
                continue
            lam = math.exp(attack[home_i] - defence[away_i] + home_adv)
            mu = math.exp(attack[away_i] - defence[home_i])
            adjust = tau(x, y, lam, mu, candidate)
            if adjust <= 1e-6:
                feasible = False
                break
            score += weight * math.log(adjust)
        if not feasible:
            continue
        if best_score is None or score > best_score:
            best_score, best_value = score, candidate
    return best_value


def team_strength(model, name):
    """Bilinmeyen takim lig ortalamasidir (yeni cikanlar icin makul taban)."""
    entry = model.get("teams", {}).get(name)
    if not entry:
        return 0.0, 0.0
    return float(entry.get("attack", 0.0)), float(entry.get("defence", 0.0))


def expected_goals(model, home, away):
    """(lambda, mu): ev ve deplasman icin beklenen gol sayisi."""
    attack_home, defence_home = team_strength(model, home)
    attack_away, defence_away = team_strength(model, away)
    home_adv = float(model.get("home_advantage", 0.25))
    lam = math.exp(attack_home - defence_away + home_adv)
    mu = math.exp(attack_away - defence_home)
    return min(max(lam, 0.05), 12.0), min(max(mu, 0.05), 12.0)


# --------------------------------------------------------------------------
# Elo
# --------------------------------------------------------------------------

def _goal_multiplier(difference):
    difference = abs(difference)
    if difference <= 1:
        return 1.0
    if difference == 2:
        return 1.5
    return (11.0 + difference) / 8.0


def run_elo(matches, k=ELO_K, home_adv=ELO_HOME, base=ELO_BASE,
            season_regress=ELO_SEASON_REGRESS):
    """Maclari tarih sirasiyla isler.

    Doner: (son_dereceler, mac_oncesi_dereceler). Ikincisi mac kimligi ->
    (ev, deplasman) sozlugudur ve tanim geregi sizintisizdir: bir macin
    dereceleri o mac islenmeden once kaydedilir.
    """
    ratings = {}
    last_season = {}
    pre_match = {}

    ordered = sorted(
        [m for m in matches
         if isinstance(m.get("hg"), int) and isinstance(m.get("ag"), int)],
        key=lambda item: (item["date"], item.get("home", "")),
    )

    for match in ordered:
        home, away = match["home"], match["away"]
        season = match.get("season")
        for team in (home, away):
            if team not in ratings:
                ratings[team] = base
            elif season and last_season.get(team) not in (None, season):
                ratings[team] += (base - ratings[team]) * season_regress
            last_season[team] = season

        rating_home, rating_away = ratings[home], ratings[away]
        pre_match[elo_key(match)] = (rating_home, rating_away)

        expected = 1.0 / (1.0 + 10 ** ((rating_away - rating_home - home_adv) / 400.0))
        difference = match["hg"] - match["ag"]
        actual = 1.0 if difference > 0 else (0.5 if difference == 0 else 0.0)
        change = k * _goal_multiplier(difference) * (actual - expected)
        ratings[home] = rating_home + change
        ratings[away] = rating_away - change

    return ratings, pre_match


def elo_key(match):
    return "%s|%s|%s" % (match["date"], match["home"], match["away"])


def elo_probability(rating_home, rating_away, home_adv=ELO_HOME):
    """Elo'nun ev galibiyeti beklentisi (beraberlik 0.5 sayilir)."""
    return 1.0 / (1.0 + 10 ** ((rating_away - rating_home - home_adv) / 400.0))
