"""Komut satiri: fetch / train / backtest / predict.

    python -m pipeline.soccer fetch    --league tr.1 --seasons 6
    python -m pipeline.soccer train    --league tr.1 --search
    python -m pipeline.soccer backtest --league tr.1 --from 2024-08-01 --with-jev --mock
    python -m pipeline.soccer predict  --league tr.1 --days 7 --mock
    python -m pipeline.soccer predict  --league tr.1 --bulletin bulten.txt
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

from . import backtest as backtest_module
from . import blend as blend_module
from . import data as data_module
from . import features as features_module
from . import jev as jev_module
from . import ratings as ratings_module

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(ROOT, "site", "data", "football")
CACHE_NAME = "jev-cache.json"


def log(message):
    print(message, flush=True)


def today_iso():
    return dt.date.today().isoformat()


def model_path(data_dir, league):
    return os.path.join(data_dir, "model-%s.json" % league)


def load_model(data_dir, league):
    path = model_path(data_dir, league)
    if not os.path.exists(path):
        raise SystemExit(
            "model yok: %s\nonce 'python -m pipeline.soccer train --league %s' calistirin"
            % (path, league))
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

def command_fetch(args):
    provider = data_module.PROVIDERS[args.provider]
    years = data_module.recent_seasons(args.seasons)
    total_new = 0

    for league in args.league:
        existing = data_module.load_league(args.data_dir, league)
        collected = []
        for year in years:
            label = data_module.season_label(year)
            try:
                records = provider(league, year)
            except RuntimeError as error:
                log("  %s %s: atlandi (%s)" % (league, label, error))
                continue
            collected = data_module.merge_records(collected, records)
            log("  %s %s: %d kayit" % (league, label, len(records)))
        if not collected:
            log("%s: hic veri alinamadi" % league)
            continue
        merged = data_module.merge_records(existing, collected)
        total_new += len(merged) - len(existing)
        path = data_module.save_league(args.data_dir, league, merged,
                                       source=args.provider)
        played = data_module.played_only(merged)
        fixtures = data_module.fixtures_only(merged)
        log("%s -> %s (%d oynanmis, %d fikstur)"
            % (league, os.path.relpath(path, ROOT), len(played), len(fixtures)))
    log("toplam %d yeni kayit" % max(total_new, 0))
    return 0


# --------------------------------------------------------------------------
# train
# --------------------------------------------------------------------------

def command_train(args):
    for league in args.league:
        records = data_module.load_league(args.data_dir, league)
        played = data_module.played_only(records)
        if len(played) < args.min_matches:
            log("%s: yeterli mac yok (%d < %d), once fetch calistirin"
                % (league, len(played), args.min_matches))
            continue

        as_of = args.as_of or today_iso()
        decay, ridge = args.decay, args.ridge
        search = None

        if args.search:
            start = args.backtest_from or _default_backtest_start(played)
            log("%s: hiperparametre aramasi (%s sonrasi)" % (league, start))
            search = backtest_module.search_hyperparameters(
                played, start, args.backtest_to,
                decays=args.decays, ridges=args.ridges,
                refit_days=args.refit_days, iterations=args.search_iterations,
                progress=log)
            decay = search[0]["decay"]
            ridge = search[0]["ridge"]
            log("  secilen: decay=%.5f ridge=%.4f" % (decay, ridge))

        log("%s: Dixon-Coles kestirimi (%d mac, %s itibariyla)"
            % (league, len(played), as_of))
        dc = ratings_module.fit_dixon_coles(
            played, as_of=as_of, decay=decay, ridge=ridge,
            iterations=args.iterations, verbose=args.verbose)
        elo_ratings, _ = ratings_module.run_elo(played)

        payload = {
            "format": "jev-soccer-model/1",
            "league": league,
            "league_name": data_module.LEAGUES.get(league, (league, None))[0],
            "trained_at": dt.datetime.now(dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "as_of": as_of,
            "dc": dc,
            "elo": {
                "k": ratings_module.ELO_K,
                "home_advantage": ratings_module.ELO_HOME,
                "ratings": elo_ratings,
            },
            "blend": dict(blend_module.DEFAULT_PARAMS),
        }
        if search:
            payload["hyperparameter_search"] = search[:5]

        if not args.no_backtest:
            start = args.backtest_from or _default_backtest_start(played)
            log("%s: geriye donuk test (%s sonrasi)" % (league, start))
            rows = backtest_module.walk_forward(
                played, start, args.backtest_to, refit_days=args.refit_days,
                decay=decay, ridge=ridge, iterations=args.iterations,
                progress=None)
            temperature, _ = backtest_module.fit_baseline_temperature(rows)
            payload["blend"]["baseline_temperature"] = temperature
            metrics = backtest_module.evaluate(
                rows, payload["blend"], use_jev=False, label="taban")
            metrics["calibration"] = backtest_module.calibration_table(
                rows, payload["blend"], use_jev=False)
            metrics["calibration_error"] = backtest_module.calibration_error(
                rows, payload["blend"], use_jev=False)
            metrics["from"] = start
            metrics["to"] = args.backtest_to
            payload["backtest"] = metrics
            _print_metrics(metrics)

        path = model_path(args.data_dir, league)
        write_json(path, payload)
        log("%s -> %s (ev avantaji %.3f, rho %+.3f)"
            % (league, os.path.relpath(path, ROOT), dc["home_advantage"], dc["rho"]))
    return 0


def _default_backtest_start(played):
    """Son ~2 sezon: hem yeterince mac, hem makul sure."""
    dates = sorted(match["date"] for match in played)
    if not dates:
        return today_iso()
    latest = dt.date.fromisoformat(dates[-1])
    return (latest - dt.timedelta(days=730)).isoformat()


def _print_metrics(metrics):
    if not metrics.get("matches"):
        log("  degerlendirilecek mac yok")
        return
    log("  %d mac | log-loss %.4f (duz dagilim %.4f, kazanc %%%.1f)"
        % (metrics["matches"], metrics["log_loss"], metrics["uniform_log_loss"],
           100 * metrics["skill_vs_uniform"]))
    log("  RPS %.4f | Brier %.4f | isabet %%%.1f | kalibrasyon hatasi %.4f"
        % (metrics["rps"], metrics["brier"], 100 * metrics["accuracy"],
           metrics.get("calibration_error", float("nan"))))
    market = metrics.get("market")
    if market:
        log("  piyasa (kapanis orani) ayni maclarda: log-loss %.4f | isabet %%%.1f"
            % (market["log_loss"], 100 * market["accuracy"]))


# --------------------------------------------------------------------------
# backtest
# --------------------------------------------------------------------------

def command_backtest(args):
    api_key = _api_key(required=args.with_jev and not args.mock)
    for league in args.league:
        played = data_module.played_only(
            data_module.load_league(args.data_dir, league))
        if not played:
            log("%s: veri yok" % league)
            continue
        start = getattr(args, "from") or _default_backtest_start(played)
        cache = jev_module.Cache(os.path.join(args.data_dir, CACHE_NAME))

        log("%s: %s .. %s" % (league, start, args.to or "son"))
        rows = backtest_module.walk_forward(
            played, start, args.to, refit_days=args.refit_days,
            decay=args.decay, ridge=args.ridge, iterations=args.iterations,
            api_key=api_key, use_jev=args.with_jev, mock=args.mock,
            cache=cache, jev_limit=args.limit, notes=args.notes,
            progress=log if args.verbose else None)
        if not rows:
            log("  degerlendirilecek mac yok")
            continue

        temperature, _ = backtest_module.fit_baseline_temperature(rows)
        base_params = {"weight": 0.0, "baseline_temperature": temperature}
        baseline_metrics = backtest_module.evaluate(
            rows, base_params, use_jev=False, label="taban")
        baseline_metrics["calibration_error"] = backtest_module.calibration_error(
            rows, base_params, use_jev=False)
        log(" taban (yalniz istatistik model):")
        _print_metrics(baseline_metrics)

        report = {
            "format": "jev-soccer-backtest/1",
            "league": league,
            "from": start,
            "to": args.to,
            "baseline_temperature": temperature,
            "baseline": baseline_metrics,
            "calibration": backtest_module.calibration_table(
                rows, base_params, use_jev=False),
        }

        if args.with_jev:
            fitted = backtest_module.fit_blend(rows, temperature)
            if not fitted:
                log(" Jev'li satir yok (butun cagrilar basarisiz olmus olabilir)")
            else:
                searched = fitted["searched_params"]
                params = fitted["params"]
                metrics = backtest_module.evaluate(
                    rows, searched, use_jev=True, label="taban+Jev")
                metrics["calibration_error"] = backtest_module.calibration_error(
                    rows, searched, use_jev=True)
                log(" taban + Jev (aranan harman: w=%.2f kapi=%.2f T=%.2f):"
                    % (searched["weight"], searched["gate"],
                       searched["temperature"]))
                _print_metrics(metrics)
                log(" Jev'li %d macta mac basina log-loss kazanci: %+.4f ± %.4f"
                    % (fitted["matches"], fitted["gain"], fitted["gain_stderr"]))
                if fitted["significant"]:
                    log(" kazanc esigi gecti -> Jev agirligi %.2f kabul edildi"
                        % params["weight"])
                else:
                    log(" kazanc gurultuden ayirt edilemiyor -> Jev agirligi 0'a "
                        "cekildi (daha cok mac gerekiyor)")
                report["blend"] = params
                report["searched_blend"] = searched
                report["with_jev"] = metrics
                report["jev_matches"] = fitted["matches"]
                report["gain"] = fitted["gain"]
                report["gain_stderr"] = fitted["gain_stderr"]
                report["significant"] = fitted["significant"]
                if args.save:
                    _save_blend(args.data_dir, league, params, metrics)
                    log(" harman parametreleri model dosyasina yazildi")
            if cache.hits or cache.misses:
                log(" Jev onbellegi: %d isabet, %d cagri"
                    % (cache.hits, cache.misses))

        if args.out:
            write_json(args.out, report)
            log(" rapor -> %s" % args.out)
        if args.rows_out:
            write_json(args.rows_out, {"format": "jev-soccer-rows/1",
                                       "league": league, "rows": rows})
            log(" satirlar -> %s" % args.rows_out)
    return 0


def _save_blend(data_dir, league, params, metrics):
    payload = load_model(data_dir, league)
    payload["blend"] = params
    payload["blend_backtest"] = {
        key: metrics[key] for key in
        ("matches", "log_loss", "rps", "brier", "accuracy", "calibration_error")
        if key in metrics
    }
    write_json(model_path(data_dir, league), payload)


# --------------------------------------------------------------------------
# predict
# --------------------------------------------------------------------------

BULLETIN_SEPARATORS = (" - ", " – ", " — ", " vs ", " v ", " x ")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})|(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
LEADING_CODE_RE = re.compile(r"^\s*\d{2,6}\s+")


def parse_bulletin(text, default_date=None, today=None):
    """Bulten metnini fikstur satirlarina cevirir.

    Kabul edilen bicimler:
        Galatasaray - Fenerbahce
        2026-09-20 Galatasaray - Fenerbahce
        1234 20/09 22:00 Galatasaray - Fenerbahce      (iddaa benzeri)
    """
    today = today or dt.date.today()
    fixtures = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = LEADING_CODE_RE.sub("", line)

        date = default_date
        match = DATE_RE.search(line)
        if match:
            date = _normalize_date(match.group(0), today) or date
            line = (line[:match.start()] + " " + line[match.end():]).strip()
        time_match = TIME_RE.search(line)
        kickoff = None
        if time_match:
            kickoff = time_match.group(0)
            line = (line[:time_match.start()] + " " + line[time_match.end():]).strip()

        pair = None
        for separator in BULLETIN_SEPARATORS:
            if separator in line:
                left, _, right = line.partition(separator)
                pair = (left.strip(" -–—\t"), right.strip(" -–—\t"))
                break
        if not pair or not pair[0] or not pair[1]:
            continue
        fixture = {"home": pair[0], "away": pair[1],
                   "date": date or today.isoformat()}
        if kickoff:
            fixture["time"] = kickoff
        fixtures.append(fixture)
    return fixtures


def _normalize_date(token, today):
    token = token.strip()
    try:
        return dt.date.fromisoformat(token).isoformat()
    except ValueError:
        pass
    parts = re.split(r"[./]", token)
    try:
        day, month = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None
    year = today.year
    if len(parts) > 2:
        year = int(parts[2])
        if year < 100:
            year += 2000
    else:
        # Yil yazilmamissa en yakin gelecegi sec.
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            return None
        if (candidate - today).days < -180:
            year += 1
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def load_bulletin(path, default_date=None):
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if path.lower().endswith(".json"):
        payload = json.loads(text)
        rows = payload.get("fixtures") if isinstance(payload, dict) else payload
        fixtures = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            home = row.get("home") or row.get("team1")
            away = row.get("away") or row.get("team2")
            if not home or not away:
                continue
            fixture = {"home": home, "away": away,
                       "date": row.get("date") or default_date or today_iso()}
            if row.get("time"):
                fixture["time"] = row["time"]
            fixtures.append(fixture)
        return fixtures
    return parse_bulletin(text, default_date)


def command_predict(args):
    api_key = _api_key(required=not args.mock and not args.no_jev)
    league = args.league[0]
    payload = load_model(args.data_dir, league)
    dc = payload["dc"]
    elo_ratings = payload.get("elo", {}).get("ratings", {})
    params = dict(blend_module.DEFAULT_PARAMS, **payload.get("blend", {}))

    records = data_module.load_league(args.data_dir, league)
    history = features_module.History(data_module.played_only(records))

    fixtures, unresolved = _collect_fixtures(args, records, dc, league)
    for name in unresolved:
        log("  ? eslestirilemedi, atlandi: %s" % name)
    if not fixtures:
        log("tahmin edilecek mac yok")
        upcoming = data_module.fixtures_only(records)
        if upcoming:
            dates = sorted(item["date"] for item in upcoming)
            log("  depodaki fikstür araligi: %s .. %s (%d mac)"
                % (dates[0], dates[-1], len(upcoming)))
            log("  --date ile bu araliktan bir gun secin ya da bulteninizi "
                "--bulten dosyasi olarak verin")
        else:
            log("  bu ligde hic oynanmamis fikstür yok; once "
                "'fetch' calistirin ya da --bulletin kullanin")
        return 1

    cache = jev_module.Cache(os.path.join(args.data_dir, CACHE_NAME))
    predictions = []
    failures = 0
    for fixture in fixtures:
        bundle = features_module.build(history, fixture, dc,
                                       elo_ratings=elo_ratings)
        result = None
        if not args.no_jev:
            state = jev_module.build_state(bundle, notes=args.notes)
            try:
                result, _ = jev_module.ask(state, api_key, model=args.model,
                                           mock=args.mock, cache=cache)
            except RuntimeError as error:
                failures += 1
                log("  Jev hatasi (%s - %s): %s -- taban kullanildi"
                    % (fixture["home"], fixture["away"], error))
        predictions.append(blend_module.predict(bundle, result, params,
                                                rho=dc.get("rho", 0.0)))
    cache.save()

    _print_predictions(predictions, args)
    if failures:
        log("(%d macta Jev cagrisi basarisiz oldu)" % failures)

    out = args.out or os.path.join(args.data_dir, "bulten-%s.json" % today_iso())
    write_json(out, {
        "format": "jev-soccer-bulletin/1",
        "league": league,
        "league_name": payload.get("league_name"),
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "model_as_of": payload.get("as_of"),
        "demo": bool(args.mock),
        "jev_used": not args.no_jev,
        "blend": params,
        "backtest": payload.get("backtest", {}).get("log_loss"),
        "matches": predictions,
    })
    log("\n-> %s" % os.path.relpath(out, ROOT))
    return 0


def _collect_fixtures(args, records, dc, league):
    """Bulten dosyasindan ya da depodaki fikstürlerden maclari toplar."""
    known = sorted(dc.get("teams", {}))
    index = data_module.TeamIndex(known)
    unresolved = []

    if args.bulletin:
        raw = load_bulletin(args.bulletin, args.date)
        fixtures = []
        for row in raw:
            home = index.resolve(row["home"])
            away = index.resolve(row["away"])
            if not home or not away:
                unresolved.append("%s - %s" % (row["home"], row["away"]))
                continue
            fixtures.append({"date": row["date"], "time": row.get("time"),
                             "league": league, "season": _season_for(records, row["date"]),
                             "home": home, "away": away})
        return fixtures, unresolved

    start = args.date or today_iso()
    end = (dt.date.fromisoformat(start)
           + dt.timedelta(days=args.days)).isoformat()
    fixtures = data_module.fixtures_only(records, since=start, until=end)
    return fixtures[:args.limit] if args.limit else fixtures, unresolved


def _season_for(records, date):
    year = data_module.current_season_start(dt.date.fromisoformat(date))
    return data_module.season_label(year)


def _print_predictions(predictions, args):
    log("")
    log("%-10s %-22s %-22s  %5s %5s %5s  %-4s %-5s  %-6s %-5s"
        % ("tarih", "ev", "deplasman", "1", "X", "2", "skor", "top", "AÜ2.5", "KG"))
    log("-" * 104)
    for item in predictions:
        probabilities = item["probabilities"]
        log("%-10s %-22s %-22s  %5.2f %5.2f %5.2f  %-4s %5.2f  %6.2f %5.2f"
            % (item["date"], _cut(item["home"], 22), _cut(item["away"], 22),
               probabilities["home"], probabilities["draw"], probabilities["away"],
               item["score"], item["expected_total"],
               item["over_under"]["over_2_5"], item["btts"]))
    log("")
    if args.detail:
        for item in predictions:
            _print_detail(item)


def _cut(text, width):
    return text if len(text) <= width else text[:width - 1] + "…"


def _print_detail(item):
    log("%s  %s - %s" % (item["date"], item["home"], item["away"]))
    base = item["baseline"]
    log("   taban     : 1 %.3f  X %.3f  2 %.3f  (toplam %.2f)"
        % (base["home"], base["draw"], base["away"], base["expected_total"]))
    jev = item["jev"]
    if jev["used"] and jev["probabilities"]:
        log("   Jev       : 1 %.3f  X %.3f  2 %.3f  (agirlik %.2f, kor nokta %.2f)"
            % (jev["probabilities"][0], jev["probabilities"][1],
               jev["probabilities"][2], jev["weight"],
               jev["blind_spot"] if jev["blind_spot"] is not None else float("nan")))
    else:
        log("   Jev       : kullanilmadi")
    probabilities = item["probabilities"]
    log("   sonuc     : 1 %.3f  X %.3f  2 %.3f  -> %s"
        % (probabilities["home"], probabilities["draw"], probabilities["away"],
           item["pick"]))
    log("   adil oran : 1 %.2f  X %.2f  2 %.2f"
        % (item["odds_fair"]["home"], item["odds_fair"]["draw"],
           item["odds_fair"]["away"]))
    log("   skorlar   : " + "  ".join(
        "%s %%%.0f" % (entry["score"], 100 * entry["p"])
        for entry in item["scores"]))
    log("")


# --------------------------------------------------------------------------
# Argumanlar
# --------------------------------------------------------------------------

def _api_key(required):
    key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
    if required and not key:
        raise SystemExit(
            "TYPESAFE_API_KEY tanimli degil. Anahtarsiz denemek icin --mock, "
            "Jev'i hic cagirmamak icin --no-jev kullanin.")
    return key


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.soccer",
        description="Gecmis maclarla egitilen skor modeli + Jev'in tipli karari")
    parser.add_argument("--data-dir", default=DATA_DIR,
                        help="veri klasoru (varsayilan site/data/football)")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="lig verisini indir")
    fetch.add_argument("--league", nargs="+", default=["tr.1"],
                       choices=sorted(data_module.LEAGUES))
    fetch.add_argument("--seasons", type=int, default=6)
    fetch.add_argument("--provider", default="openfootball",
                       choices=sorted(data_module.PROVIDERS))
    fetch.set_defaults(func=command_fetch)

    train = sub.add_parser("train", help="modeli egit ve kalibre et")
    train.add_argument("--league", nargs="+", default=["tr.1"])
    train.add_argument("--as-of", default=None)
    train.add_argument("--decay", type=float, default=ratings_module.DEFAULT_DECAY)
    train.add_argument("--ridge", type=float, default=ratings_module.DEFAULT_RIDGE)
    train.add_argument("--iterations", type=int, default=300)
    train.add_argument("--min-matches", type=int, default=150)
    train.add_argument("--refit-days", type=int, default=21)
    train.add_argument("--backtest-from", default=None)
    train.add_argument("--backtest-to", default=None)
    train.add_argument("--no-backtest", action="store_true")
    train.add_argument("--search", action="store_true",
                       help="decay/ridge icin walk-forward izgara aramasi (yavas)")
    train.add_argument("--decays", type=float, nargs="+",
                       default=[0.0018, 0.0030, 0.0050])
    train.add_argument("--ridges", type=float, nargs="+",
                       default=[0.001, 0.003, 0.010])
    train.add_argument("--search-iterations", type=int, default=150)
    train.set_defaults(func=command_train)

    back = sub.add_parser("backtest", help="ileri yuruyen degerlendirme")
    back.add_argument("--league", nargs="+", default=["tr.1"])
    back.add_argument("--from", dest="from", default=None)
    back.add_argument("--to", default=None)
    back.add_argument("--refit-days", type=int, default=21)
    back.add_argument("--decay", type=float, default=ratings_module.DEFAULT_DECAY)
    back.add_argument("--ridge", type=float, default=ratings_module.DEFAULT_RIDGE)
    back.add_argument("--iterations", type=int, default=250)
    back.add_argument("--with-jev", action="store_true",
                      help="Jev'i de cagir ve harman agirligini ogren (ucretli)")
    back.add_argument("--limit", type=int, default=None,
                      help="en fazla kac macta Jev cagrilsin")
    back.add_argument("--mock", action="store_true")
    back.add_argument("--notes", default=None)
    back.add_argument("--save", action="store_true",
                      help="ogrenilen harmani model dosyasina yaz")
    back.add_argument("--out", default=None)
    back.add_argument("--rows-out", default=None)
    back.add_argument("--verbose", action="store_true")
    back.set_defaults(func=command_backtest)

    predict = sub.add_parser("predict", help="bultendeki maclari tahmin et")
    predict.add_argument("--league", nargs=1, default=["tr.1"])
    predict.add_argument("--bulletin", default=None,
                         help="bulten dosyasi (.txt, .csv veya .json)")
    predict.add_argument("--date", default=None)
    predict.add_argument("--days", type=int, default=7)
    predict.add_argument("--limit", type=int, default=None)
    predict.add_argument("--notes", default=None,
                         help="Jev'e verilecek serbest baglam (sakatlik, motivasyon)")
    predict.add_argument("--model", default="jev-latest")
    predict.add_argument("--mock", action="store_true")
    predict.add_argument("--no-jev", action="store_true",
                         help="yalnizca istatistik model")
    predict.add_argument("--detail", action="store_true")
    predict.add_argument("--out", default=None)
    predict.set_defaults(func=command_predict)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if isinstance(getattr(args, "league", None), str):
        args.league = [args.league]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
