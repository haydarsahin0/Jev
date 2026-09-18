"""pipeline/soccer birim testleri:  python -m unittest discover -s tests -v"""

import datetime as dt
import json
import math
import os
import random
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline.soccer import (  # noqa: E402
    backtest, blend, cli, data, features, grid, jev, ratings,
)


def match(date, home, away, hg=None, ag=None, league="x.1", season="2024-25"):
    record = {"date": date, "home": home, "away": away,
              "league": league, "season": season}
    if hg is not None:
        record["hg"], record["ag"] = hg, ag
    return record


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

class NameTests(unittest.TestCase):
    def test_turkish_letters_fold_predictably(self):
        self.assertEqual(data.fold("Beşiktaş"), "besiktas")
        self.assertEqual(data.fold("İstanbul Başakşehir"), "istanbul basaksehir")
        self.assertEqual(data.fold("Kasımpaşa SK"), "kasimpasa sk")

    def test_team_key_drops_club_suffixes_but_keeps_spor(self):
        self.assertEqual(data.team_key("Galatasaray SK"), "galatasaray")
        self.assertEqual(data.team_key("Arsenal FC"), "arsenal")
        self.assertEqual(data.team_key("Konyaspor"), "konyaspor")

    def test_team_key_survives_names_made_only_of_suffixes(self):
        self.assertEqual(data.team_key("FC"), "fc")

    def test_index_resolves_exact_alias_substring_and_fuzzy(self):
        index = data.TeamIndex([
            "Galatasaray", "Fenerbahçe", "Beşiktaş JK", "Trabzonspor",
            "İstanbul Başakşehir", "Konyaspor",
        ])
        self.assertEqual(index.resolve("Galatasaray"), "Galatasaray")
        self.assertEqual(index.resolve("G.Saray"), "Galatasaray")
        self.assertEqual(index.resolve("Besiktas"), "Beşiktaş JK")
        self.assertEqual(index.resolve("fenerbahce"), "Fenerbahçe")
        self.assertEqual(index.resolve("Başakşehir"), "İstanbul Başakşehir")

    def test_index_returns_none_for_unknown_team(self):
        index = data.TeamIndex(["Galatasaray", "Fenerbahçe"])
        self.assertIsNone(index.resolve("Rizespor"))
        self.assertIsNone(index.resolve(""))


class OpenFootballTests(unittest.TestCase):
    PAYLOAD = {
        "name": "Test Lig",
        "matches": [
            {"date": "2024-08-09", "time": "21:00", "team1": "A", "team2": "B",
             "score": {"ht": [0, 0], "ft": [2, 1]}},
            {"date": "2024-08-10", "team1": "C", "team2": "D",
             "score": [1, 1]},
            {"date": "2024-08-11", "team1": "E", "team2": "F", "score": None},
            {"date": "2024-08-12", "team1": "", "team2": "H"},
            {"date": "bozuk", "team1": "I", "team2": "J"},
        ],
    }

    def test_parses_dict_list_and_missing_scores(self):
        records = data.parse_openfootball(self.PAYLOAD, "x.1", "2024-25")
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["hg"], 2)
        self.assertEqual(records[0]["time"], "21:00")
        self.assertEqual(records[1]["ag"], 1)
        self.assertNotIn("hg", records[2])

    def test_accepts_json_text(self):
        records = data.parse_openfootball(json.dumps(self.PAYLOAD), "x.1", "2024-25")
        self.assertEqual(len(records), 3)


class FootballDataCsvTests(unittest.TestCase):
    CSV = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,PSCH,PSCD,PSCA\n"
        "T1,09/08/2024,Galatasaray,Hatayspor,2,1,H,1.25,6.00,11.0\n"
        "T1,10/08/24,Konyaspor,Rizespor,0,0,D,,,\n"
        "T1,,Bozuk,Satir,1,1,D,,,\n"
    )

    def test_parses_dates_scores_and_odds(self):
        records = data.parse_football_data_csv(self.CSV, "tr.1", "2024-25")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["date"], "2024-08-09")
        self.assertEqual(records[1]["date"], "2024-08-10")
        self.assertEqual(records[0]["odds"], [1.25, 6.00, 11.0])
        self.assertNotIn("odds", records[1])


class StoreTests(unittest.TestCase):
    def test_result_overwrites_fixture_but_not_the_other_way(self):
        fixture = match("2024-08-09", "A", "B")
        result = match("2024-08-09", "A", "B", 3, 0)
        merged = data.merge_records([fixture], [result])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["hg"], 3)
        merged_back = data.merge_records([result], [fixture])
        self.assertEqual(merged_back[0]["hg"], 3)

    def test_match_id_ignores_club_suffix_differences(self):
        self.assertEqual(data.match_id(match("2024-08-09", "Arsenal FC", "B")),
                         data.match_id(match("2024-08-09", "Arsenal", "B")))

    def test_round_trip_through_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            records = [match("2024-08-09", "A", "B", 1, 0),
                       match("2024-09-01", "B", "A")]
            data.save_league(folder, "x.1", records)
            loaded = data.load_league(folder, "x.1")
            self.assertEqual(len(loaded), 2)
            self.assertEqual(len(data.played_only(loaded)), 1)
            self.assertEqual(len(data.fixtures_only(loaded)), 1)
            self.assertEqual(data.fixtures_only(loaded, since="2024-10-01"), [])

    def test_missing_league_file_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(data.load_league(folder, "yok.1"), [])


class CanonicalNameTests(unittest.TestCase):
    """Ayni kulubun iki yazimi tek takim sayilmali; yoksa gecmisi ikiye boluner."""

    ROWS = [
        match("2023-08-01", "Aston Villa", "Arsenal FC", 1, 1),
        match("2024-08-01", "Aston Villa FC", "Arsenal", 2, 0),
        match("2025-08-01", "Aston Villa FC", "Arsenal FC", 0, 1),
    ]

    def test_spellings_collapse_to_the_most_common(self):
        mapping = data.canonical_names(self.ROWS)
        self.assertEqual(mapping.get("Aston Villa"), "Aston Villa FC")
        self.assertEqual(mapping.get("Arsenal"), "Arsenal FC")

    def test_canonicalise_rewrites_every_record(self):
        fixed = data.canonicalize(self.ROWS)
        self.assertEqual(len(data.teams_in(fixed)), 2)
        self.assertEqual(self.ROWS[0]["home"], "Aston Villa")   # kaynak bozulmaz

    def test_already_clean_data_is_untouched(self):
        clean = [match("2024-08-01", "A", "B", 1, 0)]
        self.assertEqual(data.canonical_names(clean), {})
        self.assertIs(data.canonicalize(clean), clean)

    def test_loading_applies_canonicalisation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "matches-x.1.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"matches": self.ROWS}, handle)
            self.assertEqual(len(data.teams_in(data.load_league(folder, "x.1"))), 2)


class SeasonTests(unittest.TestCase):
    def test_labels_and_codes(self):
        self.assertEqual(data.season_label(2024), "2024-25")
        self.assertEqual(data.season_label(2009), "2009-10")
        self.assertEqual(data.season_code(2024), "2425")

    def test_season_starts_in_july(self):
        self.assertEqual(data.current_season_start(dt.date(2026, 6, 30)), 2025)
        self.assertEqual(data.current_season_start(dt.date(2026, 7, 1)), 2026)

    def test_uk_dates(self):
        self.assertEqual(data._parse_uk_date("09/08/2024"), "2024-08-09")
        self.assertEqual(data._parse_uk_date("09/08/24"), "2024-08-09")
        self.assertIsNone(data._parse_uk_date("bozuk"))


# --------------------------------------------------------------------------
# ratings
# --------------------------------------------------------------------------

def poisson_sample(rate, rng):
    """Knuth; testte numpy yok."""
    limit = math.exp(-rate)
    total, count = rng.random(), 0
    while total > limit:
        count += 1
        total *= rng.random()
    return count


def synthetic_league(seed=7, rounds=26):
    """Bilinen guclerden mac uretir; kestirimin geri bulmasi beklenir."""
    rng = random.Random(seed)
    strengths = {
        "Guclu": (0.45, 0.35), "Iyi": (0.20, 0.15), "Orta": (0.0, 0.0),
        "Zayif": (-0.25, -0.20), "Cok Zayif": (-0.45, -0.35),
        "Ortalama": (0.05, -0.05),
    }
    home_advantage = 0.30
    teams = list(strengths)
    matches = []
    day = dt.date(2023, 8, 1)
    for _ in range(rounds):
        for home in teams:
            for away in teams:
                if home == away:
                    continue
                lam = math.exp(strengths[home][0] - strengths[away][1] + home_advantage)
                mu = math.exp(strengths[away][0] - strengths[home][1])
                matches.append(match(day.isoformat(), home, away,
                                     poisson_sample(lam, rng),
                                     poisson_sample(mu, rng)))
        day += dt.timedelta(days=7)
    return matches, strengths, home_advantage


class DixonColesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matches, cls.strengths, cls.home_advantage = synthetic_league()
        cls.model = ratings.fit_dixon_coles(
            cls.matches, as_of="2024-03-01", decay=0.0, ridge=0.001,
            iterations=250)

    def test_recovers_the_strength_ordering(self):
        ranked = sorted(self.model["teams"],
                        key=lambda name: -self.model["teams"][name]["attack"])
        self.assertEqual(ranked[0], "Guclu")
        self.assertEqual(ranked[-1], "Cok Zayif")

    def test_recovers_home_advantage_within_tolerance(self):
        self.assertAlmostEqual(self.model["home_advantage"],
                               self.home_advantage, delta=0.10)

    def test_parameters_are_centred(self):
        attacks = [entry["attack"] for entry in self.model["teams"].values()]
        defences = [entry["defence"] for entry in self.model["teams"].values()]
        self.assertAlmostEqual(sum(attacks), 0.0, places=6)
        self.assertAlmostEqual(sum(defences), 0.0, places=6)

    def test_expected_goals_follow_the_ordering(self):
        strong, _ = ratings.expected_goals(self.model, "Guclu", "Cok Zayif")
        weak, _ = ratings.expected_goals(self.model, "Cok Zayif", "Guclu")
        self.assertGreater(strong, weak)

    def test_unknown_team_is_league_average(self):
        self.assertEqual(ratings.team_strength(self.model, "Yok Boyle"), (0.0, 0.0))

    def test_empty_input_is_rejected(self):
        with self.assertRaises(ValueError):
            ratings.fit_dixon_coles([])

    def test_fixtures_without_scores_are_ignored(self):
        mixed = self.matches + [match("2024-03-05", "Guclu", "Iyi")]
        model = ratings.fit_dixon_coles(mixed, as_of="2024-03-06",
                                        iterations=20)
        self.assertEqual(model["matches"], len(self.matches))


class TauTests(unittest.TestCase):
    def test_correction_only_touches_low_scores(self):
        self.assertEqual(ratings.tau(3, 2, 1.5, 1.2, 0.1), 1.0)
        self.assertNotEqual(ratings.tau(0, 0, 1.5, 1.2, 0.1), 1.0)
        self.assertEqual(ratings.tau(1, 1, 1.5, 1.2, 0.1), 0.9)

    def test_zero_rho_is_the_identity(self):
        for x in range(3):
            for y in range(3):
                self.assertEqual(ratings.tau(x, y, 1.4, 1.1, 0.0), 1.0)


class WeightTests(unittest.TestCase):
    def test_older_matches_weigh_less(self):
        rows = [match("2024-01-01", "A", "B", 1, 0),
                match("2024-07-01", "A", "B", 1, 0)]
        weights = ratings.build_weights(rows, "2024-07-01", 0.003)
        self.assertLess(weights[0], weights[1])
        self.assertAlmostEqual(weights[1], 1.0)

    def test_zero_decay_weighs_everything_equally(self):
        rows = [match("2020-01-01", "A", "B", 1, 0),
                match("2024-07-01", "A", "B", 1, 0)]
        self.assertEqual(ratings.build_weights(rows, "2024-07-01", 0.0), [1.0, 1.0])


class EloTests(unittest.TestCase):
    def test_winner_gains_what_loser_loses(self):
        rows = [match("2024-08-01", "A", "B", 3, 0)]
        final, _ = ratings.run_elo(rows)
        self.assertAlmostEqual(final["A"] + final["B"], 2 * ratings.ELO_BASE,
                               places=6)
        self.assertGreater(final["A"], final["B"])

    def test_pre_match_ratings_exclude_the_match_itself(self):
        rows = [match("2024-08-01", "A", "B", 3, 0),
                match("2024-08-08", "A", "B", 3, 0)]
        _, pre = ratings.run_elo(rows)
        first = pre["2024-08-01|A|B"]
        second = pre["2024-08-08|A|B"]
        self.assertEqual(first, (ratings.ELO_BASE, ratings.ELO_BASE))
        self.assertGreater(second[0], first[0])

    def test_bigger_win_moves_more(self):
        narrow, _ = ratings.run_elo([match("2024-08-01", "A", "B", 1, 0)])
        wide, _ = ratings.run_elo([match("2024-08-01", "A", "B", 5, 0)])
        self.assertGreater(wide["A"], narrow["A"])

    def test_season_change_regresses_towards_the_mean(self):
        rows = [match("2024-08-01", "A", "B", 5, 0, season="2024-25"),
                match("2025-08-01", "A", "B", 0, 0, season="2025-26")]
        _, pre = ratings.run_elo(rows)
        after_first, _ = ratings.run_elo(rows[:1])
        self.assertLess(pre["2025-08-01|A|B"][0], after_first["A"])


# --------------------------------------------------------------------------
# grid
# --------------------------------------------------------------------------

class GridTests(unittest.TestCase):
    def test_matrix_is_a_distribution(self):
        matrix = grid.score_matrix(1.6, 1.1, -0.05)
        self.assertAlmostEqual(sum(sum(row) for row in matrix), 1.0, places=9)
        self.assertTrue(all(value >= 0 for row in matrix for value in row))

    def test_outcomes_sum_to_one_and_follow_the_rates(self):
        home, draw, away = grid.outcome_probabilities(grid.score_matrix(2.0, 0.8))
        self.assertAlmostEqual(home + draw + away, 1.0, places=9)
        self.assertGreater(home, away)

    def test_expected_total_matches_the_rates(self):
        matrix = grid.score_matrix(1.5, 1.2, 0.0)
        self.assertAlmostEqual(grid.expected_total(matrix), 2.7, places=3)

    def test_over_and_btts_move_with_the_rates(self):
        low = grid.markets(0.7, 0.6)
        high = grid.markets(2.4, 2.1)
        self.assertLess(low["over_2_5"], high["over_2_5"])
        self.assertLess(low["btts"], high["btts"])

    def test_top_scores_are_sorted_and_plausible(self):
        scores = grid.top_scores(grid.score_matrix(1.5, 1.1, -0.05), 5)
        self.assertEqual(len(scores), 5)
        self.assertGreaterEqual(scores[0]["p"], scores[-1]["p"])
        self.assertIn(scores[0]["score"], {"1-1", "1-0", "0-0", "2-1"})

    def test_reweighting_hits_the_target_exactly(self):
        target = [0.55, 0.25, 0.20]
        matrix = grid.reweight_to_outcomes(grid.score_matrix(1.4, 1.2), target)
        for expected, actual in zip(target, grid.outcome_probabilities(matrix)):
            self.assertAlmostEqual(expected, actual, places=9)

    def test_reweighting_keeps_the_shape_inside_a_group(self):
        matrix = grid.score_matrix(1.4, 1.2)
        adjusted = grid.reweight_to_outcomes(matrix, [0.5, 0.3, 0.2])
        # 2-1 ve 3-1 ayni grupta: oranlari degismemeli
        self.assertAlmostEqual(matrix[2][1] / matrix[3][1],
                               adjusted[2][1] / adjusted[3][1], places=9)

    def test_solver_moves_towards_the_target(self):
        lam, mu = grid.solve_lambdas([0.60, 0.24, 0.16], rho=0.0,
                                     target_total=2.8, start=(1.2, 1.2))
        self.assertGreater(lam, mu)
        self.assertAlmostEqual(lam + mu, 2.8, delta=0.35)

    def test_grid_for_targets_is_self_consistent(self):
        target = [0.42, 0.27, 0.31]
        markets, lam, mu = grid.grid_for_targets(target, rho=-0.05,
                                                 target_total=2.9)
        self.assertAlmostEqual(markets["p_home"], target[0], places=9)
        self.assertAlmostEqual(markets["p_draw"], target[1], places=9)
        self.assertAlmostEqual(markets["p_away"], target[2], places=9)
        self.assertGreater(lam, 0)
        self.assertGreater(mu, 0)


# --------------------------------------------------------------------------
# features -- sizinti testleri
# --------------------------------------------------------------------------

def _dates_in(payload, found=None):
    """Demetin icindeki butun `date` alanlari (fikstur tarihi haric)."""
    found = [] if found is None else found
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "date" and isinstance(value, str) and value:
                found.append(value)
            elif key != "fixture":
                _dates_in(value, found)
            else:
                # fikstur blogu macin kendi tarihini tasir, dogal olarak
                for sub_key, sub_value in value.items():
                    if sub_key != "date":
                        _dates_in(sub_value, found)
    elif isinstance(payload, list):
        for item in payload:
            _dates_in(item, found)
    return found


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            match("2024-08-01", "A", "B", 1, 0),
            match("2024-08-08", "B", "A", 2, 2),
            match("2024-08-15", "A", "C", 0, 3),
            match("2024-08-22", "A", "B", 4, 0),   # degerlendirilen gun
            match("2024-08-29", "C", "A", 1, 1),
        ]
        self.history = features.History(self.rows)

    def test_same_day_and_later_matches_are_excluded(self):
        seen = self.history.team_matches("A", before="2024-08-22")
        dates = [item["date"] for item in seen]
        self.assertEqual(dates, ["2024-08-15", "2024-08-08", "2024-08-01"])

    def test_venue_filter(self):
        home_only = self.history.team_matches("A", "2024-08-22", venue="home")
        self.assertTrue(all(item["home"] == "A" for item in home_only))
        away_only = self.history.team_matches("A", "2024-08-22", venue="away")
        self.assertEqual([item["date"] for item in away_only], ["2024-08-08"])

    def test_head_to_head_only_counts_the_pair(self):
        meetings = self.history.head_to_head("A", "B", "2024-08-22")
        self.assertEqual(len(meetings), 2)

    def test_table_stops_at_the_cutoff(self):
        table = self.history.table("x.1", "2024-25", "2024-08-22")
        rows = {row["team"]: row for row in table}
        self.assertEqual(rows["A"]["played"], 3)
        self.assertEqual(rows["A"]["points"], 4)     # G, B, M
        self.assertEqual(rows["C"]["points"], 3)
        self.assertEqual(table[0]["position"], 1)

    def test_form_summary_reads_newest_first(self):
        summary = features.form_summary(
            self.history.team_matches("A", "2024-08-22"), "A")
        self.assertEqual(summary["form"], "MBG")
        self.assertEqual(summary["played"], 3)
        self.assertAlmostEqual(summary["points_per_game"], 4 / 3)
        self.assertEqual(summary["recent"][0]["opponent"], "C")

    def test_form_summary_handles_no_history(self):
        summary = features.form_summary([], "A")
        self.assertEqual(summary["played"], 0)
        self.assertIsNone(summary["points_per_game"])

    def test_rest_days(self):
        self.assertEqual(features.rest_days(self.history, "A", "2024-08-22"), 7)
        self.assertIsNone(features.rest_days(self.history, "A", "2024-08-01"))

    def test_bundle_never_reaches_the_match_day_or_later(self):
        model = ratings.fit_dixon_coles(self.history.before("2024-08-22"),
                                        as_of="2024-08-22", iterations=20)
        bundle = features.build(self.history, self.rows[3], model)
        dates = _dates_in(bundle)
        self.assertTrue(dates)
        self.assertTrue(all(date < "2024-08-22" for date in dates),
                        "sizinti: %s" % sorted(dates))
        self.assertEqual(bundle["home"]["form"]["played"], 3)
        self.assertEqual(bundle["fixture"]["home"], "A")

    def test_bundle_does_not_carry_the_actual_score(self):
        model = ratings.fit_dixon_coles(self.history.before("2024-08-22"),
                                        as_of="2024-08-22", iterations=20)
        bundle = features.build(self.history, self.rows[3], model)
        seen = (bundle["home"]["form"]["recent"]
                + bundle["away"]["form"]["recent"]
                + bundle["head_to_head"]["meetings"])
        self.assertTrue(seen)
        self.assertNotIn("4-0", [entry["score"] for entry in seen])


# --------------------------------------------------------------------------
# jev
# --------------------------------------------------------------------------

class JevAnswerTests(unittest.TestCase):
    def test_three_nouls_are_normalised(self):
        result = jev.normalize_answers({
            "home_win": {"type": "noul", "noul": 0.6},
            "draw": {"type": "noul", "noul": 0.3},
            "away_win": {"type": "noul", "noul": 0.3},
        })
        self.assertAlmostEqual(sum(result["probabilities"]), 1.0)
        self.assertAlmostEqual(result["probabilities"][0], 0.5)

    def test_missing_answers_give_no_distribution(self):
        result = jev.normalize_answers({"home_win": {"type": "noul", "noul": 0.6}})
        self.assertIsNone(result["probabilities"])

    def test_all_zero_answers_give_no_distribution(self):
        result = jev.normalize_answers({
            "home_win": {"type": "noul", "noul": 0.0},
            "draw": {"type": "noul", "noul": 0.0},
            "away_win": {"type": "noul", "noul": 0.0},
        })
        self.assertIsNone(result["probabilities"])

    def test_score_position_uses_the_top_level(self):
        answer = {"type": "score", "score": 1.0,
                  "probabilities": {"0": 0.3, "1": 0.4, "2": 0.3}}
        self.assertAlmostEqual(jev._score_position(answer), 0.5)

    def test_total_goals_interpolates_between_anchors(self):
        self.assertAlmostEqual(jev.total_goals_from_position(0.0), 0.85)
        self.assertAlmostEqual(jev.total_goals_from_position(0.5), 2.50)
        self.assertAlmostEqual(jev.total_goals_from_position(1.0), 4.80)
        self.assertIsNone(jev.total_goals_from_position(None))

    def test_wrong_types_are_ignored(self):
        self.assertIsNone(jev._noul({"home_win": {"type": "score", "score": 1}},
                                    "home_win"))


class JevStateTests(unittest.TestCase):
    def setUp(self):
        rows = [match("2024-08-01", "A", "B", 1, 0),
                match("2024-08-08", "B", "A", 2, 2),
                match("2024-08-15", "A", "C", 0, 3)]
        self.history = features.History(rows)
        self.model = ratings.fit_dixon_coles(rows, as_of="2024-08-22",
                                             iterations=30)
        self.bundle = features.build(self.history,
                                     match("2024-08-22", "A", "B"), self.model)

    def test_state_carries_the_model_and_the_form(self):
        state = jev.build_state(self.bundle, notes="kaleci sakat")
        self.assertEqual(state["match"]["home"], "A")
        self.assertIn("p_home_win", state["model"])
        self.assertEqual(state["context"], "kaleci sakat")
        self.assertIn("form_string_newest_first", state["home"])

    def test_fingerprint_changes_with_the_state(self):
        first = jev.state_fingerprint(jev.build_state(self.bundle), "jev-latest")
        second = jev.state_fingerprint(jev.build_state(self.bundle, notes="x"),
                                       "jev-latest")
        self.assertNotEqual(first, second)
        self.assertEqual(
            first,
            jev.state_fingerprint(jev.build_state(self.bundle), "jev-latest"))

    def test_mock_is_deterministic_and_well_formed(self):
        state = jev.build_state(self.bundle)
        first = jev.mock_answers(state)
        self.assertEqual(first, jev.mock_answers(state))
        result = jev.normalize_answers(first)
        self.assertAlmostEqual(sum(result["probabilities"]), 1.0)
        self.assertTrue(0.0 <= result["blind_spot"] <= 1.0)

    def test_ask_uses_the_cache_instead_of_calling_twice(self):
        calls = []

        def fake_caller(state, api_key, questions=None, model=None):
            calls.append(state)
            return {"home_win": {"type": "noul", "noul": 0.5},
                    "draw": {"type": "noul", "noul": 0.25},
                    "away_win": {"type": "noul", "noul": 0.25}}

        with tempfile.TemporaryDirectory() as folder:
            cache = jev.Cache(os.path.join(folder, "cache.json"))
            state = jev.build_state(self.bundle)
            jev.ask(state, "key", cache=cache, caller=fake_caller)
            jev.ask(state, "key", cache=cache, caller=fake_caller)
            self.assertEqual(len(calls), 1)
            cache.save()
            reopened = jev.Cache(os.path.join(folder, "cache.json"))
            jev.ask(state, "key", cache=reopened, caller=fake_caller)
            self.assertEqual(len(calls), 1)
            self.assertEqual(reopened.hits, 1)


# --------------------------------------------------------------------------
# blend
# --------------------------------------------------------------------------

BASELINE = {"p_home": 0.50, "p_draw": 0.25, "p_away": 0.25,
            "expected_total": 2.70, "lambda_home": 1.5, "lambda_away": 1.2}


class BlendTests(unittest.TestCase):
    def test_pool_endpoints(self):
        first, second = [0.5, 0.3, 0.2], [0.2, 0.3, 0.5]
        self.assertEqual(blend.log_pool(first, second, 0.0), first)
        self.assertEqual(blend.log_pool(first, second, 1.0), second)
        middle = blend.log_pool(first, second, 0.5)
        self.assertAlmostEqual(sum(middle), 1.0)
        self.assertAlmostEqual(middle[0], middle[2])

    def test_temperature_flattens_and_sharpens(self):
        sharp = blend.temper([0.7, 0.2, 0.1], 0.5)
        flat = blend.temper([0.7, 0.2, 0.1], 2.0)
        self.assertGreater(sharp[0], 0.7)
        self.assertLess(flat[0], 0.7)
        self.assertAlmostEqual(sum(flat), 1.0)

    def test_blind_spot_opens_and_closes_the_gate(self):
        params = {"weight": 0.4, "gate": 0.5, "max_weight": 0.75}
        closed = blend.effective_weight(params, 0.0)
        middle = blend.effective_weight(params, 0.5)
        open_gate = blend.effective_weight(params, 1.0)
        self.assertLess(closed, middle)
        self.assertLess(middle, open_gate)
        self.assertAlmostEqual(middle, 0.4)
        self.assertEqual(blend.effective_weight(params, None), 0.4)

    def test_weight_never_exceeds_the_ceiling(self):
        params = {"weight": 0.9, "gate": 1.0, "max_weight": 0.75}
        self.assertLessEqual(blend.effective_weight(params, 1.0), 0.75)

    def test_without_jev_the_baseline_passes_through(self):
        merged = blend.combine(BASELINE, None)
        self.assertFalse(merged["used_jev"])
        self.assertEqual(merged["jev_weight"], 0.0)
        self.assertAlmostEqual(merged["probabilities"][0], 0.50, places=6)

    def test_empty_jev_answer_falls_back_to_the_baseline(self):
        merged = blend.combine(BASELINE, {"probabilities": None})
        self.assertFalse(merged["used_jev"])

    def test_jev_moves_the_line_but_not_all_the_way(self):
        merged = blend.combine(
            BASELINE,
            {"probabilities": [0.2, 0.3, 0.5], "blind_spot": 0.5,
             "expected_total": 3.4},
            {"weight": 0.3, "gate": 0.5, "temperature": 1.0})
        self.assertTrue(merged["used_jev"])
        self.assertLess(merged["probabilities"][0], 0.50)
        self.assertGreater(merged["probabilities"][0], 0.20)
        self.assertGreater(merged["expected_total"], 2.70)
        self.assertLess(merged["expected_total"], 3.40)

    def test_zero_weight_ignores_jev_entirely(self):
        merged = blend.combine(
            BASELINE, {"probabilities": [0.05, 0.05, 0.90], "blind_spot": 1.0},
            {"weight": 0.0, "gate": 0.5})
        self.assertAlmostEqual(merged["probabilities"][0], 0.50, places=6)


class PredictTests(unittest.TestCase):
    def setUp(self):
        rows = [match("2024-08-01", "A", "B", 1, 0),
                match("2024-08-08", "B", "A", 2, 2),
                match("2024-08-15", "A", "C", 3, 0)]
        self.history = features.History(rows)
        self.model = ratings.fit_dixon_coles(rows, as_of="2024-08-22",
                                             iterations=40)
        self.bundle = features.build(self.history,
                                     match("2024-08-22", "A", "B"), self.model)

    def test_record_is_internally_consistent(self):
        record = blend.predict(self.bundle, None, rho=self.model["rho"])
        probabilities = record["probabilities"]
        self.assertAlmostEqual(
            probabilities["home"] + probabilities["draw"] + probabilities["away"],
            1.0, places=9)
        self.assertAlmostEqual(
            record["double_chance"]["1X"],
            probabilities["home"] + probabilities["draw"], places=9)
        self.assertAlmostEqual(record["odds_fair"]["home"],
                               1.0 / probabilities["home"], places=6)
        best = max(probabilities, key=probabilities.get)
        self.assertEqual(record["pick"], {"home": "1", "draw": "X", "away": "2"}[best])
        self.assertEqual(record["score"], record["scores"][0]["score"])

    def test_over_under_ladder_is_monotone(self):
        record = blend.predict(self.bundle, None, rho=self.model["rho"])
        ladder = record["over_under"]
        self.assertGreater(ladder["over_1_5"], ladder["over_2_5"])
        self.assertGreater(ladder["over_2_5"], ladder["over_3_5"])

    def test_unknown_team_is_flagged(self):
        fixture = match("2024-08-22", "A", "Yeni Takim")
        bundle = features.build(self.history, fixture, self.model)
        record = blend.predict(bundle, None, rho=self.model["rho"])
        self.assertEqual(record["unknown_teams"], ["away"])


# --------------------------------------------------------------------------
# backtest
# --------------------------------------------------------------------------

class MetricTests(unittest.TestCase):
    def test_log_loss_of_a_certain_correct_call_is_zero(self):
        self.assertAlmostEqual(backtest.log_loss([1.0, 0.0, 0.0], 0), 0.0)

    def test_log_loss_of_the_flat_call(self):
        self.assertAlmostEqual(backtest.log_loss([1 / 3] * 3, 1),
                               backtest.UNIFORM_LOG_LOSS)

    def test_brier_bounds(self):
        self.assertAlmostEqual(backtest.brier([1.0, 0.0, 0.0], 0), 0.0)
        self.assertAlmostEqual(backtest.brier([0.0, 0.0, 1.0], 0), 2.0)

    def test_rps_punishes_distance_in_the_ordering(self):
        near = backtest.ranked_probability_score([0.6, 0.25, 0.15], 1)
        far = backtest.ranked_probability_score([0.6, 0.25, 0.15], 2)
        self.assertLess(near, far)

    def test_outcome_index(self):
        self.assertEqual(backtest.outcome_index(match("d", "A", "B", 2, 1)), 0)
        self.assertEqual(backtest.outcome_index(match("d", "A", "B", 1, 1)), 1)
        self.assertEqual(backtest.outcome_index(match("d", "A", "B", 0, 1)), 2)

    def test_evaluate_reports_rates_and_skill(self):
        rows = [{"baseline": [0.6, 0.25, 0.15], "baseline_total": 2.7,
                 "outcome": 0},
                {"baseline": [0.2, 0.3, 0.5], "baseline_total": 2.7,
                 "outcome": 2}]
        metrics = backtest.evaluate(rows, {"weight": 0.0}, use_jev=False)
        self.assertEqual(metrics["matches"], 2)
        self.assertEqual(metrics["accuracy"], 1.0)
        self.assertGreater(metrics["skill_vs_uniform"], 0.0)
        self.assertEqual(metrics["outcome_rates"], [0.5, 0.0, 0.5])

    def test_empty_evaluation_is_not_an_error(self):
        self.assertEqual(backtest.evaluate([])["matches"], 0)

    def test_market_metrics_read_the_odds(self):
        rows = [{"baseline": [0.4, 0.3, 0.3], "baseline_total": 2.7,
                 "outcome": 0, "odds": [2.0, 3.5, 4.0]}]
        market = backtest.market_metrics(rows)
        self.assertEqual(market["matches"], 1)
        self.assertGreater(market["accuracy"], 0.0)

    def test_calibration_table_counts_every_leg(self):
        rows = [{"baseline": [0.5, 0.3, 0.2], "baseline_total": 2.7,
                 "outcome": 0}]
        table = backtest.calibration_table(rows, {"weight": 0.0}, use_jev=False)
        self.assertEqual(sum(entry["count"] for entry in table), 3)
        self.assertGreaterEqual(backtest.calibration_error(
            rows, {"weight": 0.0}, use_jev=False), 0.0)


class BlendFittingTests(unittest.TestCase):
    def _rows(self, jev_probabilities, count=200, seed=3):
        rng = random.Random(seed)
        rows = []
        for index in range(count):
            outcome = rng.choices([0, 1, 2], weights=[0.45, 0.25, 0.30])[0]
            rows.append({
                "baseline": [0.45, 0.25, 0.30],
                "baseline_total": 2.7,
                "outcome": outcome,
                "jev": {"probabilities": jev_probabilities(outcome, rng),
                        "blind_spot": 0.5, "expected_total": 2.7},
            })
        return rows

    def test_noise_is_refused(self):
        def noisy(_outcome, rng):
            values = [rng.uniform(0.2, 0.5) for _ in range(3)]
            total = sum(values)
            return [value / total for value in values]

        fitted = backtest.fit_blend(self._rows(noisy))
        self.assertFalse(fitted["significant"])
        self.assertEqual(fitted["params"]["weight"], 0.0)

    def test_real_signal_is_accepted(self):
        def informed(outcome, _rng):
            probabilities = [0.15, 0.15, 0.15]
            probabilities[outcome] = 0.70
            return probabilities

        fitted = backtest.fit_blend(self._rows(informed))
        self.assertTrue(fitted["significant"])
        self.assertGreater(fitted["params"]["weight"], 0.0)
        self.assertLess(fitted["accepted_log_loss"], fitted["baseline_log_loss"])

    def test_no_jev_rows_means_nothing_to_fit(self):
        self.assertIsNone(backtest.fit_blend([{"baseline": [0.4, 0.3, 0.3],
                                               "outcome": 0}]))


class WalkForwardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matches, _, _ = synthetic_league(seed=11, rounds=20)

    def test_predictions_beat_the_flat_call(self):
        rows = backtest.walk_forward(
            self.matches, start="2023-11-01", refit_days=60, iterations=120,
            min_train=120)
        self.assertGreater(len(rows), 50)
        metrics = backtest.evaluate(rows, {"weight": 0.0}, use_jev=False)
        self.assertLess(metrics["log_loss"], backtest.UNIFORM_LOG_LOSS)
        self.assertTrue(all(abs(sum(row["baseline"]) - 1.0) < 1e-6 for row in rows))

    def test_too_little_history_means_no_rows(self):
        rows = backtest.walk_forward(self.matches, start="2023-08-01",
                                     end="2023-08-02", min_train=10_000)
        self.assertEqual(rows, [])

    def test_mock_jev_rows_carry_answers(self):
        rows = backtest.walk_forward(
            self.matches, start="2023-11-01", end="2023-11-30", refit_days=60,
            iterations=60, min_train=120, use_jev=True, mock=True, jev_limit=5)
        with_jev = [row for row in rows if "jev" in row]
        self.assertEqual(len(with_jev), 5)
        self.assertAlmostEqual(sum(with_jev[0]["jev"]["probabilities"]), 1.0)


# --------------------------------------------------------------------------
# cli -- bulten ayristirma
# --------------------------------------------------------------------------

class BulletinTests(unittest.TestCase):
    TODAY = dt.date(2026, 9, 18)

    def test_plain_pairs(self):
        fixtures = cli.parse_bulletin("Galatasaray - Fenerbahçe\n",
                                      today=self.TODAY)
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0]["home"], "Galatasaray")
        self.assertEqual(fixtures[0]["away"], "Fenerbahçe")
        self.assertEqual(fixtures[0]["date"], "2026-09-18")

    def test_iso_date_and_time(self):
        fixtures = cli.parse_bulletin("2026-09-20 19:00 Beşiktaş - Trabzonspor",
                                      today=self.TODAY)
        self.assertEqual(fixtures[0]["date"], "2026-09-20")
        self.assertEqual(fixtures[0]["time"], "19:00")

    def test_iddaa_style_line(self):
        fixtures = cli.parse_bulletin("1234 20/09 22:00 Konyaspor - Rizespor",
                                      today=self.TODAY)
        self.assertEqual(fixtures[0]["date"], "2026-09-20")
        self.assertEqual(fixtures[0]["home"], "Konyaspor")
        self.assertEqual(fixtures[0]["away"], "Rizespor")

    def test_alternative_separators(self):
        text = "A vs B\nC – D\nE x F"
        self.assertEqual(len(cli.parse_bulletin(text, today=self.TODAY)), 3)

    def test_comments_and_noise_are_skipped(self):
        text = "# yorum\n\nsadece metin\nA - B\n"
        fixtures = cli.parse_bulletin(text, today=self.TODAY)
        self.assertEqual(len(fixtures), 1)

    def test_day_month_without_year_picks_the_near_future(self):
        self.assertEqual(cli._normalize_date("02/01", self.TODAY), "2027-01-02")
        self.assertEqual(cli._normalize_date("20/09", self.TODAY), "2026-09-20")
        self.assertEqual(cli._normalize_date("20/09/25", self.TODAY), "2025-09-20")

    def test_impossible_dates_are_rejected(self):
        self.assertIsNone(cli._normalize_date("32/13", self.TODAY))

    def test_json_bulletin(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "bulten.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"fixtures": [
                    {"date": "2026-09-20", "home": "A", "away": "B",
                     "time": "19:00"},
                    {"team1": "C", "team2": "D"},
                    {"home": "eksik"},
                ]}, handle)
            fixtures = cli.load_bulletin(path, default_date="2026-09-21")
            self.assertEqual(len(fixtures), 2)
            self.assertEqual(fixtures[0]["time"], "19:00")
            self.assertEqual(fixtures[1]["date"], "2026-09-21")


class ParserTests(unittest.TestCase):
    def test_subcommands_exist(self):
        parser = cli.build_parser()
        for command in ("fetch", "train", "backtest", "predict"):
            args = parser.parse_args([command])
            self.assertTrue(callable(args.func))

    def test_predict_defaults(self):
        args = cli.build_parser().parse_args(["predict", "--league", "tr.1"])
        self.assertEqual(args.league, ["tr.1"])
        self.assertEqual(args.days, 7)
        self.assertFalse(args.mock)


if __name__ == "__main__":
    unittest.main()
