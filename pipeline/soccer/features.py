"""Fikstur basina ozellik demeti -- tamami mac gununden ONCEKI veriden.

Sizinti bu dosyanin tek ciddi riski. Onlemi tek bir kuralla saglaniyor:
butun sorgular `before` tarihini alir ve kesin kucuktur (`<`) ile filtreler.
Ayni gun oynanan baska maclar bile disarida kalir. Backtest'in anlamli
olmasi buna bagli.
"""

from __future__ import annotations

import datetime as dt

from . import grid as grid_module
from . import ratings as ratings_module

FORM_WINDOW = 6
H2H_LIMIT = 5


class History:
    """Oynanmis maclarin takim ve lig bazinda indekslenmis hali."""

    def __init__(self, matches):
        self.matches = sorted(
            [m for m in matches
             if isinstance(m.get("hg"), int) and isinstance(m.get("ag"), int)],
            key=lambda item: (item["date"], item.get("home", "")),
        )
        self.by_team = {}
        for match in self.matches:
            self.by_team.setdefault(match["home"], []).append(match)
            self.by_team.setdefault(match["away"], []).append(match)

    def before(self, date):
        """Verilen tarihten once oynanmis butun maclar."""
        return [match for match in self.matches if match["date"] < date]

    def team_matches(self, team, before, limit=None, venue=None):
        """Bir takimin `before` tarihinden onceki maclari, yeniden eskiye.

        `venue` 'home' veya 'away' ise yalnizca o rolde oynadiklari alinir.
        """
        found = []
        for match in reversed(self.by_team.get(team, [])):
            if match["date"] >= before:
                continue
            at_home = match["home"] == team
            if venue == "home" and not at_home:
                continue
            if venue == "away" and at_home:
                continue
            found.append(match)
            if limit and len(found) >= limit:
                break
        return found

    def head_to_head(self, home, away, before, limit=H2H_LIMIT):
        found = []
        for match in reversed(self.by_team.get(home, [])):
            if match["date"] >= before:
                continue
            if away not in (match["home"], match["away"]):
                continue
            found.append(match)
            if len(found) >= limit:
                break
        return found

    def table(self, league, season, before):
        """Sezon ici puan durumu (mac gunune kadar)."""
        rows = {}
        for match in self.matches:
            if match["date"] >= before:
                break
            if match.get("league") != league or match.get("season") != season:
                continue
            for team, scored, conceded in (
                (match["home"], match["hg"], match["ag"]),
                (match["away"], match["ag"], match["hg"]),
            ):
                row = rows.setdefault(team, {"team": team, "played": 0, "points": 0,
                                             "gf": 0, "ga": 0})
                row["played"] += 1
                row["gf"] += scored
                row["ga"] += conceded
                row["points"] += 3 if scored > conceded else (1 if scored == conceded else 0)
        ordered = sorted(rows.values(),
                         key=lambda item: (-item["points"],
                                           -(item["gf"] - item["ga"]), -item["gf"]))
        for position, row in enumerate(ordered, start=1):
            row["position"] = position
        return ordered


def outcome_letter(scored, conceded):
    """G/B/M -- galibiyet, beraberlik, maglubiyet."""
    if scored > conceded:
        return "G"
    return "B" if scored == conceded else "M"


def form_summary(matches, team):
    """Bir takimin verilen maclardaki (yeniden eskiye) form ozeti."""
    if not matches:
        return {"played": 0, "points_per_game": None, "goals_for": None,
                "goals_against": None, "form": "", "recent": []}

    points = goals_for = goals_against = 0
    letters = []
    recent = []
    for match in matches:
        at_home = match["home"] == team
        scored = match["hg"] if at_home else match["ag"]
        conceded = match["ag"] if at_home else match["hg"]
        letter = outcome_letter(scored, conceded)
        letters.append(letter)
        points += 3 if letter == "G" else (1 if letter == "B" else 0)
        goals_for += scored
        goals_against += conceded
        recent.append({
            "date": match["date"],
            "opponent": match["away"] if at_home else match["home"],
            "venue": "ev" if at_home else "deplasman",
            "score": "%d-%d" % (scored, conceded),
            "result": letter,
        })

    count = len(matches)
    return {
        "played": count,
        "points_per_game": points / count,
        "goals_for": goals_for / count,
        "goals_against": goals_against / count,
        "form": "".join(letters),          # en yeni mac basta
        "recent": recent,
    }


def rest_days(history, team, before):
    previous = history.team_matches(team, before, limit=1)
    if not previous:
        return None
    return (dt.date.fromisoformat(before)
            - dt.date.fromisoformat(previous[0]["date"])).days


def table_row(table, team):
    for row in table:
        if row["team"] == team:
            return row
    return None


def team_features(history, team, before, window=FORM_WINDOW, venue=None):
    overall = form_summary(history.team_matches(team, before, limit=window), team)
    split = form_summary(
        history.team_matches(team, before, limit=window, venue=venue), team)
    return {
        "team": team,
        "form": overall,
        "venue_form": split,
        "rest_days": rest_days(history, team, before),
    }


def h2h_summary(history, home, away, before, limit=H2H_LIMIT):
    matches = history.head_to_head(home, away, before, limit)
    meetings = []
    home_wins = draws = away_wins = 0
    for match in matches:
        host_is_home_team = match["home"] == home
        scored = match["hg"] if host_is_home_team else match["ag"]
        conceded = match["ag"] if host_is_home_team else match["hg"]
        letter = outcome_letter(scored, conceded)
        if letter == "G":
            home_wins += 1
        elif letter == "B":
            draws += 1
        else:
            away_wins += 1
        meetings.append({
            "date": match["date"],
            "venue": "ev" if host_is_home_team else "deplasman",
            "score": "%d-%d" % (scored, conceded),
            "result": letter,
        })
    return {
        "played": len(matches),
        "home_wins": home_wins,
        "draws": draws,
        "away_wins": away_wins,
        "meetings": meetings,
    }


def build(history, fixture, model, elo_ratings=None, window=FORM_WINDOW,
          elo_pre=None):
    """Bir fikstur icin tam ozellik demeti.

    `elo_pre` verilirse (backtest'te) o macin mac-oncesi Elo'su kullanilir;
    verilmezse `elo_ratings` sozlugundeki guncel degerler alinir.
    """
    before = fixture["date"]
    home, away = fixture["home"], fixture["away"]

    lam, mu = ratings_module.expected_goals(model, home, away)
    baseline = grid_module.markets(lam, mu, model.get("rho", 0.0))

    if elo_pre:
        elo_home, elo_away = elo_pre
    else:
        elo_ratings = elo_ratings or {}
        elo_home = elo_ratings.get(home, ratings_module.ELO_BASE)
        elo_away = elo_ratings.get(away, ratings_module.ELO_BASE)

    table = history.table(fixture.get("league"), fixture.get("season"), before)
    attack_home, defence_home = ratings_module.team_strength(model, home)
    attack_away, defence_away = ratings_module.team_strength(model, away)

    return {
        "fixture": {
            "date": before,
            "time": fixture.get("time"),
            "league": fixture.get("league"),
            "season": fixture.get("season"),
            "home": home,
            "away": away,
            "known_teams": {
                "home": home in model.get("teams", {}),
                "away": away in model.get("teams", {}),
            },
        },
        "model": {
            "lambda_home": lam,
            "lambda_away": mu,
            "p_home": baseline["p_home"],
            "p_draw": baseline["p_draw"],
            "p_away": baseline["p_away"],
            "expected_total": baseline["expected_total"],
            "over_2_5": baseline["over_2_5"],
            "btts": baseline["btts"],
            "top_scores": baseline["top_scores"],
            "home_attack": attack_home,
            "home_defence": defence_home,
            "away_attack": attack_away,
            "away_defence": defence_away,
            "home_advantage": model.get("home_advantage"),
            "trained_matches": model.get("matches"),
        },
        "elo": {
            "home": elo_home,
            "away": elo_away,
            "difference": elo_home - elo_away,
            "home_expectation": ratings_module.elo_probability(elo_home, elo_away),
        },
        "home": _with_table(team_features(history, home, before, window, "home"),
                            table),
        "away": _with_table(team_features(history, away, before, window, "away"),
                            table),
        "head_to_head": h2h_summary(history, home, away, before),
        "baseline": baseline,
    }


def _with_table(features, table):
    row = table_row(table, features["team"])
    if row:
        features["table"] = {
            "position": row["position"],
            "played": row["played"],
            "points": row["points"],
            "goal_difference": row["gf"] - row["ga"],
        }
    else:
        features["table"] = None
    return features
