"""Jev katmani: mac basina tipli sorular, state uretimi ve yanit okuma.

Onemli olan sinir: Jev'e istatistik modelin ciktisi ZATEN verilir. Jev'den
sifirdan bir tahmin istenmiyor; elindeki tabani gorup "bu taban neyi
kaciriyor" sorusuna tipli cevap vermesi isteniyor. Bu yuzden sorular
`model` alanina acikca atif yapar.

Tipler radar.py ile ayni: `noul` kalibre bir olasilik, `score` sirali bir
rubrik uzerinde olasilik agirlikli konum, `choice` tek secim.
"""

from __future__ import annotations

import hashlib
import json
import os

from ..radar import call_jev as _call_jev
from . import grid as grid_module

QUESTION_SET_VERSION = "match/1"

TOTAL_GOALS_LEVELS = [
    "Low scoring: 0 or 1 goals in the match in total",
    "Medium scoring: 2 or 3 goals in the match in total",
    "High scoring: 4 or more goals in the match in total",
]

# Rubrik seviyelerinin toplam gol karsiligi; `score` konumu bunlar arasinda
# dogrusal olarak yorumlanir.
TOTAL_GOALS_ANCHORS = [0.85, 2.50, 4.80]

QUESTIONS = {
    "home_win": {
        "type": "noul",
        "instructions": (
            "Will `match.home` win this match in 90 minutes? `model` already "
            "holds a goal model fitted on past results; treat it as the prior "
            "and adjust it only for what `home`, `away`, `head_to_head` and "
            "`context` show that the ratings cannot have absorbed yet."
        ),
    },
    "draw": {
        "type": "noul",
        "instructions": (
            "Will this match end in a draw in 90 minutes? Use `model` as the "
            "prior and adjust for recent form, rest, table position and "
            "`context`."
        ),
    },
    "away_win": {
        "type": "noul",
        "instructions": (
            "Will `match.away` win this match in 90 minutes? Use `model` as "
            "the prior and adjust for the same evidence."
        ),
    },
    "total_goals": {
        "type": "score",
        "instructions": (
            "How many goals will this match produce in total? `model."
            "expected_total` is the fitted expectation; move away from it only "
            "for concrete reasons in `home`, `away`, `head_to_head` or "
            "`context`."
        ),
        "criteria": TOTAL_GOALS_LEVELS,
    },
    "btts": {
        "type": "noul",
        "instructions": "Will both teams score at least one goal?",
    },
    "blind_spot": {
        "type": "noul",
        "instructions": (
            "Does the evidence contain a pre-match factor that the goal model "
            "in `model` structurally cannot see -- a manager change, a "
            "congested or dead-rubber fixture, a derby, a squad in transition, "
            "or a form swing the ratings have not caught up with -- that "
            "should move the line? Answer low when the ratings and the recent "
            "evidence agree."
        ),
    },
}


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def _round(value, digits=3):
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return value


def _team_state(side):
    form = side.get("form", {})
    venue = side.get("venue_form", {})
    return {
        "team": side.get("team"),
        "last_matches": form.get("recent", []),
        "form_string_newest_first": form.get("form"),
        "points_per_game": _round(form.get("points_per_game")),
        "goals_for_per_game": _round(form.get("goals_for")),
        "goals_against_per_game": _round(form.get("goals_against")),
        "venue_points_per_game": _round(venue.get("points_per_game")),
        "rest_days": side.get("rest_days"),
        "table": side.get("table"),
    }


def build_state(bundle, notes=None, reader=None):
    """Ozellik demetinden Jev'in okuyacagi sade state'i uretir.

    Izgaranin butun hucreleri degil, karar icin gereken ozet gonderilir --
    hem ucuz hem de modelin dikkatini dagitmaz.
    """
    fixture = bundle["fixture"]
    model = bundle["model"]
    elo = bundle["elo"]

    state = {
        "match": {
            "league": fixture.get("league"),
            "season": fixture.get("season"),
            "date": fixture.get("date"),
            "kickoff": fixture.get("time"),
            "home": fixture.get("home"),
            "away": fixture.get("away"),
            "home_team_is_new_to_the_model": not fixture["known_teams"]["home"],
            "away_team_is_new_to_the_model": not fixture["known_teams"]["away"],
        },
        "model": {
            "note": (
                "Dixon-Coles Poisson model fitted on %s past matches with "
                "exponential time decay. Attack is positive-is-better; "
                "defence is positive-is-better (concedes fewer)."
                % model.get("trained_matches")
            ),
            "expected_goals_home": _round(model["lambda_home"], 2),
            "expected_goals_away": _round(model["lambda_away"], 2),
            "expected_total": _round(model["expected_total"], 2),
            "p_home_win": _round(model["p_home"]),
            "p_draw": _round(model["p_draw"]),
            "p_away_win": _round(model["p_away"]),
            "p_both_teams_score": _round(model["btts"]),
            "most_likely_scores": [
                {"score": item["score"], "p": _round(item["p"])}
                for item in model.get("top_scores", [])[:3]
            ],
            "home_attack": _round(model["home_attack"]),
            "home_defence": _round(model["home_defence"]),
            "away_attack": _round(model["away_attack"]),
            "away_defence": _round(model["away_defence"]),
            "home_advantage": _round(model.get("home_advantage")),
        },
        "elo": {
            "home": _round(elo["home"], 1),
            "away": _round(elo["away"], 1),
            "difference": _round(elo["difference"], 1),
        },
        "home": _team_state(bundle["home"]),
        "away": _team_state(bundle["away"]),
        "head_to_head": bundle["head_to_head"],
    }
    if notes:
        state["context"] = notes
    if reader:
        state["reader"] = reader
    return state


def state_fingerprint(state, model_name):
    """Onbellek anahtari: ayni state + ayni soru seti = ayni cevap."""
    blob = json.dumps(
        {"state": state, "model": model_name, "questions": QUESTION_SET_VERSION},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# Yanit okuma
# --------------------------------------------------------------------------

def _noul(answers, key):
    answer = answers.get(key)
    if isinstance(answer, dict) and answer.get("type") == "noul":
        value = answer.get("noul")
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
    return None


def _score_position(answer):
    """`score` yanitini 0..1 arasi konuma cevirir (radar.py ile ayni kural)."""
    if not isinstance(answer, dict) or answer.get("type") != "score":
        return None
    value = answer.get("score")
    if not isinstance(value, (int, float)):
        return None
    levels = []
    for field in ("probabilities", "legend"):
        holder = answer.get(field)
        if isinstance(holder, dict):
            for key in holder:
                try:
                    levels.append(int(key))
                except (TypeError, ValueError):
                    continue
            if levels:
                break
    top = max(levels) if levels else len(TOTAL_GOALS_LEVELS) - 1
    top = top if top > 0 else 1
    return max(0.0, min(1.0, float(value) / top))


def total_goals_from_position(position, anchors=TOTAL_GOALS_ANCHORS):
    """Rubrik konumunu beklenen toplam gole cevirir (parcali dogrusal)."""
    if position is None:
        return None
    span = len(anchors) - 1
    scaled = max(0.0, min(1.0, position)) * span
    low = int(scaled)
    if low >= span:
        return anchors[-1]
    weight = scaled - low
    return anchors[low] * (1 - weight) + anchors[low + 1] * weight


def normalize_answers(answers):
    """Jev yanitini kullanilabilir sayilara cevirir.

    1X2 icin uc ayri `noul` sorulur; bunlar tasarim geregi 1'e toplanmak
    zorunda degildir, bu yuzden normalize edilir. Toplami sifira yakinsa
    (Jev uc soruya da "hayir" demisse) `probabilities` None doner ve cagiran
    taraf tabana duser.
    """
    home = _noul(answers, "home_win")
    draw = _noul(answers, "draw")
    away = _noul(answers, "away_win")

    probabilities = None
    raw_total = None
    if None not in (home, draw, away):
        raw_total = home + draw + away
        if raw_total > 1e-3:
            probabilities = [home / raw_total, draw / raw_total, away / raw_total]

    position = _score_position(answers.get("total_goals"))
    return {
        "probabilities": probabilities,
        "raw": {"home_win": home, "draw": draw, "away_win": away},
        "raw_sum": raw_total,
        "total_goals_position": position,
        "expected_total": total_goals_from_position(position),
        "btts": _noul(answers, "btts"),
        "blind_spot": _noul(answers, "blind_spot"),
    }


# --------------------------------------------------------------------------
# Cagri, onbellek ve mock
# --------------------------------------------------------------------------

def mock_answers(state):
    """Anahtarsiz calisma: state'in ozetinden turetilen sahte ama tutarli yanit.

    Taban olasiliklarin etrafinda kucuk, deterministik bir sapma uretir --
    boru hattini ucretsiz test etmeye yarar, tahmin degeri yoktur.
    """
    digest = hashlib.sha256(
        json.dumps(state, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).digest()

    def unit(index):
        return digest[index] / 255.0

    model = state.get("model", {})
    base = [model.get("p_home_win", 0.4), model.get("p_draw", 0.27),
            model.get("p_away_win", 0.33)]
    shifted = []
    for position, value in enumerate(base):
        nudge = (unit(position) - 0.5) * 0.18
        shifted.append(max(0.02, min(0.96, value + nudge)))

    return {
        "home_win": {"type": "noul", "noul": shifted[0]},
        "draw": {"type": "noul", "noul": shifted[1]},
        "away_win": {"type": "noul", "noul": shifted[2]},
        "total_goals": {
            "type": "score",
            "score": 0.4 + 1.2 * unit(3),
            "probabilities": {"0": 0.2, "1": 0.5, "2": 0.3},
        },
        "btts": {"type": "noul",
                 "noul": max(0.05, min(0.95, model.get("p_both_teams_score", 0.5)
                                       + (unit(4) - 0.5) * 0.15))},
        "blind_spot": {"type": "noul", "noul": 0.15 + 0.5 * unit(5)},
        "demo": True,
    }


class Cache:
    """Jev yanitlarinin diskteki onbellegi.

    Backtest ayni maci defalarca gorur; her seferinde para odememek icin
    state parmak izi -> yanit eslemesi saklanir.
    """

    def __init__(self, path):
        self.path = path
        self.entries = {}
        self.hits = 0
        self.misses = 0
        self._dirty = False
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if isinstance(payload, dict):
                    entries = payload.get("entries")
                    if isinstance(entries, dict):
                        self.entries = entries
            except (OSError, ValueError):
                self.entries = {}

    def get(self, key):
        value = self.entries.get(key)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
        return value

    def put(self, key, value):
        self.entries[key] = value
        self._dirty = True

    def save(self):
        if not self.path or not self._dirty:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"format": "jev-soccer-cache/1",
                       "questions": QUESTION_SET_VERSION,
                       "entries": self.entries},
                      handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(tmp, self.path)
        self._dirty = False


def ask(state, api_key, model="jev-latest", mock=False, cache=None,
        caller=_call_jev):
    """Tek mac icin Jev'e sorar. (normalize_edilmis, ham_yanit) doner."""
    key = state_fingerprint(state, "mock" if mock else model)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return normalize_answers(cached), cached

    answers = mock_answers(state) if mock else caller(
        state, api_key, questions=QUESTIONS, model=model)
    if cache is not None:
        cache.put(key, answers)
    return normalize_answers(answers), answers


def baseline_targets(bundle):
    """Jev yoksa kullanilan hedefler: dogrudan istatistik modelin ciktisi."""
    model = bundle["model"]
    return {
        "probabilities": [model["p_home"], model["p_draw"], model["p_away"]],
        "expected_total": model["expected_total"],
        "btts": model["btts"],
    }


def markets_from_targets(probabilities, expected_total, rho, start):
    """Hedeflerden nihai izgarayi kurar (grid modulune ince sarmal)."""
    return grid_module.grid_for_targets(probabilities, rho=rho,
                                        target_total=expected_total,
                                        start=start)
