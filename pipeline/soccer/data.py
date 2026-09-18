"""Mac verisi: indirme, ayristirma, takim adi eslestirme ve depolama.

Kanonik mac kaydi (tek bicim, butun saglayicilar buna cevirir):

    {"date": "2024-08-09", "league": "tr.1", "season": "2024-25",
     "home": "Galatasaray", "away": "Hatayspor", "hg": 2, "ag": 1}

`hg`/`ag` yoksa kayit oynanmamis bir fikstürdür (bülten satiri).
Sadece standart kutuphane kullanilir.
"""

from __future__ import annotations

import csv
import datetime as dt
import difflib
import io
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request

USER_AGENT = "jev-soccer/1.0 (+https://github.com/haydarsahin0/Jev)"
HTTP_TIMEOUT = 60
HTTP_ATTEMPTS = 4
HTTP_BACKOFF = 2.0

# openfootball/football.json: ucretsiz, anahtarsiz, sezon basina tek JSON.
OPENFOOTBALL_URL = (
    "https://raw.githubusercontent.com/openfootball/football.json"
    "/master/{season}/{league}.json"
)

# football-data.co.uk: ayni lig kodlari degil; oran sutunlari da tasir.
# Egress politikasi bu alan adini engelleyebilir, o yuzden ikincil saglayici.
FOOTBALL_DATA_URL = "https://www.football-data.co.uk/mmz4281/{code}/{div}.csv"

# Lig kodu -> (gorunen ad, football-data.co.uk karsiligi)
LEAGUES = {
    "tr.1": ("Türkiye Süper Lig", "T1"),
    "en.1": ("İngiltere Premier Lig", "E0"),
    "en.2": ("İngiltere Championship", "E1"),
    "es.1": ("İspanya LaLiga", "SP1"),
    "de.1": ("Almanya Bundesliga", "D1"),
    "it.1": ("İtalya Serie A", "I1"),
    "fr.1": ("Fransa Ligue 1", "F1"),
    "nl.1": ("Hollanda Eredivisie", "N1"),
    "pt.1": ("Portekiz Primeira Liga", "P1"),
    "be.1": ("Belçika Pro League", "B1"),
    "gr.1": ("Yunanistan Super League", "G1"),
    "sc.1": ("İskoçya Premiership", "SC0"),
}

# Bultenlerde sik gecen kisaltmalar. Anahtarlar normalize edilmis hallerdir.
ALIASES = {
    "gsaray": "Galatasaray",
    "g saray": "Galatasaray",
    "cimbom": "Galatasaray",
    "fbahce": "Fenerbahçe",
    "f bahce": "Fenerbahçe",
    "bjk": "Beşiktaş",
    "besiktas jk": "Beşiktaş",
    "ts": "Trabzonspor",
    "trabzon": "Trabzonspor",
    "basaksehir": "İstanbul Başakşehir",
    "medipol basaksehir": "İstanbul Başakşehir",
    "rams basaksehir": "İstanbul Başakşehir",
    "man utd": "Manchester United",
    "man united": "Manchester United",
    "man city": "Manchester City",
    "spurs": "Tottenham Hotspur",
    "wolves": "Wolverhampton Wanderers",
    "inter": "Internazionale",
    "psg": "Paris Saint-Germain",
    "atletico": "Atlético Madrid",
    "barca": "Barcelona",
    "bayern": "Bayern München",
    "dortmund": "Borussia Dortmund",
}

# Kulup adlarinda tasiyici bilgi olmayan ekler. "spor" BILEREK yok: Konyaspor
# ile Konya ayri seylerdir.
NOISE_TOKENS = {
    "fc", "afc", "cf", "sc", "sk", "ac", "as", "ss", "ssc", "bk", "if", "jk",
    "sv", "tsv", "vfl", "vfb", "fk", "cd", "ud", "rc", "club", "calcio",
    "kulubu", "kulubu", "spor kulubu", "the",
}

_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^0-9a-z ]+")


def log(message):
    print(message, flush=True)


# --------------------------------------------------------------------------
# Takim adlari
# --------------------------------------------------------------------------

def fold(text):
    """Aksanlari ve noktalamayi atip kiyaslanabilir bir anahtar uretir.

    Turkce'ye ozel: 'ı' -> 'i', 'ş' -> 's', 'ğ' -> 'g'. unicodedata tek
    basina 'ı' harfini duserdi, o yuzden once elle eslenir.
    """
    if not text:
        return ""
    lowered = text.replace("I", "ı").replace("İ", "i").lower()
    for source, target in (("ı", "i"), ("ş", "s"), ("ğ", "g"), ("ç", "c"),
                           ("ö", "o"), ("ü", "u"), ("ø", "o"), ("æ", "ae"),
                           ("ß", "ss")):
        lowered = lowered.replace(source, target)
    stripped = unicodedata.normalize("NFKD", lowered)
    stripped = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    stripped = _NON_WORD_RE.sub(" ", stripped)
    return _WS_RE.sub(" ", stripped).strip()


def team_key(name):
    """Fuzzy eslestirmede kullanilan anahtar: gurultu ekleri atilmis hali."""
    folded = fold(name)
    if not folded:
        return ""
    tokens = [token for token in folded.split(" ") if token not in NOISE_TOKENS]
    if not tokens:                      # adin tamami ek ise (ornegin "FC")
        tokens = folded.split(" ")
    return " ".join(tokens)


class TeamIndex:
    """Bilinen takim adlari uzerinde en yakin eslesmeyi bulur."""

    def __init__(self, names):
        self.names = sorted(set(names))
        self._by_key = {}
        for name in self.names:
            self._by_key.setdefault(team_key(name), name)

    def resolve(self, raw, cutoff=0.78):
        """Serbest metin bir takim adini bilinen ada cevirir.

        Sirayla: birebir anahtar, takma ad tablosu, alt dize, difflib.
        Bulamazsa None doner -- sessizce yanlis takima baglamaktansa
        kullaniciya sormak dogrusudur.
        """
        if not raw:
            return None
        key = team_key(raw)
        if key in self._by_key:
            return self._by_key[key]

        alias = ALIASES.get(key) or ALIASES.get(fold(raw))
        if alias:
            alias_key = team_key(alias)
            if alias_key in self._by_key:
                return self._by_key[alias_key]
            key = alias_key

        # Alt dize: "Besiktas" -> "Besiktas JK"
        contained = [
            name for candidate_key, name in self._by_key.items()
            if candidate_key and (candidate_key.startswith(key + " ")
                                  or candidate_key == key
                                  or key.startswith(candidate_key + " "))
        ]
        if len(contained) == 1:
            return contained[0]

        matches = difflib.get_close_matches(key, list(self._by_key), n=1,
                                            cutoff=cutoff)
        if matches:
            return self._by_key[matches[0]]
        return None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_get(url, attempts=HTTP_ATTEMPTS, sleep=time.sleep, opener=None):
    """GET; gecici hatalarda ustel geri cekilme ile yeniden dener."""
    last_error = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
        )
        try:
            open_url = opener or urllib.request.urlopen
            with open_url(request, timeout=HTTP_TIMEOUT) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            last_error = "HTTP %s" % error.code
            if error.code == 404 or not (error.code == 429
                                         or 500 <= error.code < 600):
                raise RuntimeError("%s: %s" % (last_error, url)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = type(error).__name__
        if attempt == attempts - 1:
            break
        sleep(HTTP_BACKOFF ** attempt)
    raise RuntimeError("%s: %s" % (last_error or "bilinmeyen hata", url))


# --------------------------------------------------------------------------
# Sezon yardimcilari
# --------------------------------------------------------------------------

def season_label(start_year):
    """2024 -> '2024-25'."""
    return "%d-%02d" % (start_year, (start_year + 1) % 100)


def season_code(start_year):
    """football-data.co.uk sezon kodu: 2024 -> '2425'."""
    return "%02d%02d" % (start_year % 100, (start_year + 1) % 100)


def current_season_start(today=None):
    """Avrupa sezonu temmuzda baslar kabul edilir."""
    today = today or dt.date.today()
    return today.year if today.month >= 7 else today.year - 1


def recent_seasons(count, today=None):
    start = current_season_start(today)
    return [start - offset for offset in range(count)][::-1]


# --------------------------------------------------------------------------
# Saglayici: openfootball
# --------------------------------------------------------------------------

def _score_pair(value):
    """openfootball'da skor dict, liste veya None olabilir; hepsini karsilar."""
    if isinstance(value, dict):
        value = value.get("ft")
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(item, int) for item in value)):
        return int(value[0]), int(value[1])
    return None


def parse_openfootball(payload, league, season):
    """openfootball JSON -> kanonik kayitlar (oynanmamislar dahil)."""
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    matches = payload.get("matches") if isinstance(payload, dict) else None
    if not isinstance(matches, list):
        return []

    records = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        home = (match.get("team1") or "").strip()
        away = (match.get("team2") or "").strip()
        date = (match.get("date") or "").strip()
        if not home or not away or not _valid_date(date):
            continue
        record = {
            "date": date,
            "league": league,
            "season": season,
            "home": home,
            "away": away,
        }
        time_of_day = (match.get("time") or "").strip()
        if time_of_day:
            record["time"] = time_of_day
        score = _score_pair(match.get("score"))
        if score is not None:
            record["hg"], record["ag"] = score
        records.append(record)
    return records


def fetch_openfootball(league, start_year, fetcher=http_get):
    season = season_label(start_year)
    url = OPENFOOTBALL_URL.format(season=season, league=league)
    return parse_openfootball(fetcher(url), league, season)


# --------------------------------------------------------------------------
# Saglayici: football-data.co.uk (CSV, oranlarla birlikte)
# --------------------------------------------------------------------------

def parse_football_data_csv(text, league, season):
    """football-data.co.uk CSV -> kanonik kayitlar.

    Kapanis oranlari (PSCH/PSCD/PSCA veya B365*) varsa `odds` alanina
    yazilir; backtest'te piyasa referansi olarak kullanilir.
    """
    records = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        home = (row.get("HomeTeam") or "").strip()
        away = (row.get("AwayTeam") or "").strip()
        date = _parse_uk_date(row.get("Date"))
        if not home or not away or not date:
            continue
        record = {
            "date": date,
            "league": league,
            "season": season,
            "home": home,
            "away": away,
        }
        try:
            record["hg"] = int(row["FTHG"])
            record["ag"] = int(row["FTAG"])
        except (KeyError, TypeError, ValueError):
            record.pop("hg", None)
            record.pop("ag", None)
        odds = _odds_from_row(row)
        if odds:
            record["odds"] = odds
        records.append(record)
    return records


def _odds_from_row(row):
    for prefix in ("PSC", "B365C", "PS", "B365", "BW", "Avg"):
        keys = (prefix + "H", prefix + "D", prefix + "A")
        try:
            values = [float(row[key]) for key in keys]
        except (KeyError, TypeError, ValueError):
            continue
        if all(value > 1.0 for value in values):
            return values
    return None


def _parse_uk_date(value):
    """'09/08/2024' veya '09/08/24' -> '2024-08-09'."""
    value = (value or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def fetch_football_data(league, start_year, fetcher=http_get):
    entry = LEAGUES.get(league)
    if not entry or not entry[1]:
        raise RuntimeError("football-data.co.uk karsiligi yok: %s" % league)
    url = FOOTBALL_DATA_URL.format(code=season_code(start_year), div=entry[1])
    return parse_football_data_csv(fetcher(url), league, season_label(start_year))


PROVIDERS = {
    "openfootball": fetch_openfootball,
    "football-data": fetch_football_data,
}


# --------------------------------------------------------------------------
# Depolama
# --------------------------------------------------------------------------

def _valid_date(value):
    try:
        dt.date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def canonical_names(records):
    """Ayni kulubun farkli yazimlarini tek ada baglar.

    openfootball ayni takimi sezondan sezona farkli yazabiliyor ("Aston Villa"
    ve "Aston Villa FC" gibi). Duzeltilmezse model tek kulubu iki ayri takim
    sanir ve ikisinin de gecmisi yarim kalir. Anahtar `team_key`; kazanan
    yazim, en cok gecen (esitlikte en yeni, sonra en uzun) yazimdir.
    """
    stats = {}
    for record in records:
        for name in (record.get("home"), record.get("away")):
            if not name:
                continue
            entry = stats.setdefault(team_key(name), {})
            row = entry.setdefault(name, {"count": 0, "last": ""})
            row["count"] += 1
            if record.get("date", "") > row["last"]:
                row["last"] = record["date"]

    mapping = {}
    for key, spellings in stats.items():
        best = max(spellings.items(),
                   key=lambda item: (item[1]["count"], item[1]["last"],
                                     len(item[0])))[0]
        for name in spellings:
            if name != best:
                mapping[name] = best
    return mapping


def canonicalize(records):
    """Takim adlarini tek yazima cevirir; gerek yoksa listeyi oldugu gibi verir."""
    mapping = canonical_names(records)
    if not mapping:
        return records
    fixed = []
    for record in records:
        home = mapping.get(record.get("home"))
        away = mapping.get(record.get("away"))
        if home or away:
            record = dict(record)
            if home:
                record["home"] = home
            if away:
                record["away"] = away
        fixed.append(record)
    return fixed


def match_id(record):
    return "%s|%s|%s|%s" % (record.get("league"), record.get("date"),
                            team_key(record.get("home", "")),
                            team_key(record.get("away", "")))


def merge_records(existing, incoming):
    """Ayni mac icin yeni kayit eskisini gunceller; sirali liste doner.

    Oynanmamis bir fikstürün sonucu sonra gelir; skor tasiyan kayit
    skorsuzun uzerine yazar, tersi olmaz.
    """
    merged = {}
    for record in list(existing) + list(incoming):
        key = match_id(record)
        current = merged.get(key)
        if current is None:
            merged[key] = dict(record)
            continue
        if "hg" in record and "ag" in record:
            merged[key] = dict(record)
        else:
            for field, value in record.items():
                current.setdefault(field, value)
    return sorted(merged.values(), key=lambda item: (item["date"], item["home"]))


def store_path(data_dir, league):
    return os.path.join(data_dir, "matches-%s.json" % league)


def load_league(data_dir, league):
    path = store_path(data_dir, league)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return []
    matches = payload.get("matches") if isinstance(payload, dict) else payload
    if not isinstance(matches, list):
        return []
    return canonicalize(matches)


def save_league(data_dir, league, records, source="openfootball"):
    os.makedirs(data_dir, exist_ok=True)
    records = canonicalize(records)
    played = [item for item in records if "hg" in item and "ag" in item]
    payload = {
        "format": "jev-soccer-matches/1",
        "league": league,
        "league_name": LEAGUES.get(league, (league, None))[0],
        "source": source,
        "updated_at": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "played": len(played),
        "fixtures": len(records) - len(played),
        "matches": records,
    }
    path = store_path(data_dir, league)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
    os.replace(tmp, path)
    return path


def load_history(data_dir, leagues):
    """Birden cok ligi tek listede toplar (tarih sirali)."""
    records = []
    for league in leagues:
        records.extend(load_league(data_dir, league))
    records.sort(key=lambda item: (item["date"], item.get("league", ""),
                                   item.get("home", "")))
    return records


def played_only(records):
    return [item for item in records
            if isinstance(item.get("hg"), int) and isinstance(item.get("ag"), int)]


def fixtures_only(records, since=None, until=None):
    result = []
    for item in records:
        if "hg" in item and "ag" in item:
            continue
        if since and item["date"] < since:
            continue
        if until and item["date"] > until:
            continue
        result.append(item)
    return result


def teams_in(records):
    names = set()
    for record in records:
        names.add(record["home"])
        names.add(record["away"])
    return sorted(names)
