#!/usr/bin/env python3
"""arXiv Radar: yeni arXiv makalelerini TypeSafe Jev ile degerlendirir.

Sadece standart kutuphane kullanir. Cikti site/data/ altina yazilir ve
GitHub Pages tarafindan statik olarak okunur.

Kullanim:
    python pipeline/radar.py                # gercek tarama (TYPESAFE_API_KEY gerekir)
    python pipeline/radar.py --limit 3      # ilk 3 makale (ucuz deneme)
    python pipeline/radar.py --mock         # API'siz sahte veri (yerel test)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# --------------------------------------------------------------------------
# Ayarlar -- degistirmek isteyeceginiz her sey burada.
# --------------------------------------------------------------------------

CATEGORIES = ["cs.AI", "cs.CL", "cs.LG", "cs.SD"]
WINDOW_HOURS = 48          # Son kac saatteki makaleler alinsin
MAX_RESULTS = 100          # Her kategori icin arXiv'den cekilecek kayit sayisi
MAX_PAPERS = 120           # Gunde degerlendirilecek en fazla makale (maliyet siniri)
WORKERS = 4                # Ayni anda kac TypeSafe istegi
SEEN_LIMIT = 5000          # seen.json icinde tutulacak ID sayisi
RETENTION_DAYS = 60        # Bundan eski gunluk dosyalar silinir
ABSTRACT_LIMIT = 1500      # Jev'e gonderilen ozetin karakter siniri
MAX_AUTHORS = 6

ARXIV_ENDPOINT = "https://export.arxiv.org/api/query"
USER_AGENT = "arxiv-radar/1.0 (+https://github.com/haydarsahin0/Jev)"
# urllib kendiliginden Accept gondermez; arXiv bunu 406 Not Acceptable ile
# reddeder. Kabul edilen turleri acikca belirtiyoruz.
ARXIV_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/atom+xml,text/xml;q=0.9,*/*;q=0.8",
}
ARXIV_ATTEMPTS = 3         # Gecici arXiv hatalarinda toplam deneme
ARXIV_RETRY_WAIT = 3.0     # arXiv kurallari geregi denemeler arasi en az bekleme
# arXiv yuk altinda 429 yerine 406 Not Acceptable dondurebiliyor: olcumlerde
# tek basina calisan bir kategori sorgusu ayni kosu icinde 406 verdi. Bu
# yuzden 406 da gecici kabul edilip yeniden deneniyor.
ARXIV_RETRY_CODES = (406, 429)

TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = "jev-latest"
TYPESAFE_TIMEOUT = 60
MAX_ATTEMPTS = 5           # 429/5xx durumunda toplam deneme sayisi

READER = (
    "A student developer building web products with React and Supabase, "
    "voice/TTS pipelines (ElevenLabs, Fish Audio), automated social-media "
    "content and apps on top of LLM APIs (Claude, Gemini)."
)

TOPICS = [
    "LLM agents and tool use",
    "Retrieval and RAG",
    "Speech, voice and audio",
    "Evaluation, safety and reliability",
    "Efficiency and cost",
    "Multimodal (vision, video)",
    "Training and alignment methods",
    "Other",
]

NOVELTY_LEVELS = [
    "Incremental: a small tweak or re-benchmark of known methods",
    "Solid: a new method or finding within an established line of work",
    "Distinctive: a new idea, capability or result that changes how people "
    "approach the problem",
]

EVIDENCE_LEVELS = [
    "No or minimal evaluation described",
    "Evaluation on a few benchmarks or small experiments",
    "Broad evaluation: several benchmarks, strong baselines, ablations or "
    "real-world tests",
]

# TypeSafe System One soru sozlugu. Tel uzerindeki alan adi "criteria"dir:
# score icin sirali seviye listesi, choice icin {etiket: aciklama} sozlugu.
QUESTIONS = {
    "novelty": {
        "type": "score",
        "instructions": "Based only on `paper.abstract`, how new is the contribution?",
        "criteria": NOVELTY_LEVELS,
    },
    "evidence": {
        "type": "score",
        "instructions": "How strong is the evaluation described in `paper.abstract`?",
        "criteria": EVIDENCE_LEVELS,
    },
    "practical": {
        "type": "noul",
        "instructions": (
            "Does `paper.abstract` describe a method, tool or finding that a "
            "software developer could apply in a real product within a few months?"
        ),
    },
    "relevance": {
        "type": "noul",
        "instructions": (
            "Would `reader` find this paper directly useful for the kind of "
            "products they build?"
        ),
    },
    "code": {
        "type": "noul",
        "instructions": (
            "Does `paper.abstract` state that code, models or data are publicly released?"
        ),
    },
    "hype": {
        "type": "noul",
        "instructions": (
            "Does `paper.abstract` rely mainly on broad claims without "
            "concrete, quantitative results?"
        ),
    },
    "topic": {
        "type": "choice",
        "instructions": "What is the main topic of `paper`?",
        "criteria": {name: None for name in TOPICS},
    },
}

SIGNAL_KEYS = ["novelty", "evidence", "practical", "relevance", "code", "hype"]

ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "site", "data")
SEEN_PATH = os.path.join(DATA_DIR, "seen.json")
INDEX_PATH = os.path.join(DATA_DIR, "index.json")

DAY_FILE_RE = re.compile(r"^radar-(\d{4}-\d{2}-\d{2})\.json$")


def log(message):
    """Tek satirlik log. API anahtari asla buraya girmez."""
    print(message, flush=True)


# --------------------------------------------------------------------------
# arXiv
# --------------------------------------------------------------------------

def build_arxiv_url(category, max_results=MAX_RESULTS):
    """Tek bir kategori icin arXiv sorgu URL'i (en yeniler once).

    Kategorileri "cat:cs.AI OR cat:cs.CL ..." seklinde tek sorguda birlestirmek
    arXiv'den 406 Not Acceptable donduruyor (hangi baslik gonderilirse
    gonderilsin); tek kategorili sorgu ise sorunsuz calisiyor. Bu yuzden her
    kategori ayri istekle cekilip yerelde birlestiriliyor.
    """
    query = urllib.parse.urlencode(
        {
            "search_query": "cat:" + category,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
    )
    return ARXIV_ENDPOINT + "?" + query


def fetch_categories(categories=CATEGORIES, sleep=time.sleep):
    """Her kategoriyi ayri istekle ceker, birlestirir ve ID'ye gore tekillestirir.

    Istekler arasinda arXiv'in istedigi gibi beklenir. Bir kategori basarisiz
    olursa digerleri yine islenir; hepsi basarisizsa hata firlatilir.
    """
    papers = []
    ids = set()
    errors = []

    for index, category in enumerate(categories):
        if index:
            sleep(ARXIV_RETRY_WAIT)
        try:
            xml_text = fetch_arxiv(build_arxiv_url(category), sleep=sleep)
            entries = parse_atom(xml_text)
        except Exception as error:
            errors.append((category, error))
            log("  ! %s alinamadi: %s" % (category, error))
            continue
        fresh = 0
        for paper in entries:
            if paper["id"] not in ids:
                ids.add(paper["id"])
                papers.append(paper)
                fresh += 1
        log("  %s: %d kayit (%d yeni)" % (category, len(entries), fresh))

    if errors and len(errors) == len(categories):
        raise RuntimeError("hicbir kategori alinamadi: %s" % errors[0][1])

    papers.sort(key=lambda p: p.get("published", ""), reverse=True)
    return papers


def fetch_arxiv(url, timeout=60, attempts=ARXIV_ATTEMPTS, sleep=time.sleep):
    """arXiv'den Atom yanitini alir.

    Tarama basina tek bir basarili istek yapilir; yalnizca gecici hatalarda
    (429/5xx, ag hatasi) yeniden denenir ve denemeler arasinda arXiv'in
    istedigi gibi en az birkac saniye beklenir.
    """
    last_error = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers=dict(ARXIV_HEADERS))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            last_error = "HTTP Error %s: %s" % (error.code, error.reason)
            if not (error.code in ARXIV_RETRY_CODES or 500 <= error.code < 600):
                raise RuntimeError(last_error) from None
            wait = _parse_retry_after(error.headers) or _retry_delay(attempt)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = "%s: %s" % (type(error).__name__, error)
            wait = _retry_delay(attempt)

        if attempt == attempts - 1:
            raise RuntimeError(last_error)
        sleep(max(wait, ARXIV_RETRY_WAIT))

    raise RuntimeError(last_error or "bilinmeyen hata")


def _text(node):
    if node is None or node.text is None:
        return ""
    return " ".join(node.text.split())


def _https(url):
    """http:// adreslerini https://'e yukseltir."""
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def short_id(raw_id):
    """`http://arxiv.org/abs/2509.01234v2` -> `2509.01234` (surumsuz)."""
    tail = raw_id.rsplit("/abs/", 1)[-1]
    return re.sub(r"v\d+$", "", tail)


def parse_atom(xml_text):
    """arXiv Atom yanitini makale sozluklerine cevirir."""
    root = ET.fromstring(xml_text)
    papers = []
    for entry in root.findall(ATOM_NS + "entry"):
        raw_id = _text(entry.find(ATOM_NS + "id"))
        if not raw_id:
            continue
        paper_id = short_id(raw_id)

        authors = []
        for author in entry.findall(ATOM_NS + "author"):
            name = _text(author.find(ATOM_NS + "name"))
            if name:
                authors.append(name)

        categories = []
        for category in entry.findall(ATOM_NS + "category"):
            term = category.get("term")
            if term and term not in categories:
                categories.append(term)

        url = ""
        pdf = ""
        for link in entry.findall(ATOM_NS + "link"):
            href = link.get("href") or ""
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf = _https(href)
            elif link.get("rel") == "alternate":
                url = _https(href)

        if not url:
            url = "https://arxiv.org/abs/" + paper_id
        if not pdf:
            pdf = "https://arxiv.org/pdf/" + paper_id

        papers.append(
            {
                "id": paper_id,
                "title": _text(entry.find(ATOM_NS + "title")),
                "abstract": _text(entry.find(ATOM_NS + "summary")),
                "authors": authors[:MAX_AUTHORS],
                "published": _text(entry.find(ATOM_NS + "published")),
                "updated": _text(entry.find(ATOM_NS + "updated")),
                "categories": categories,
                "primary_category": (
                    entry.find(ARXIV_NS + "primary_category").get("term")
                    if entry.find(ARXIV_NS + "primary_category") is not None
                    else (categories[0] if categories else "")
                ),
                "url": url,
                "pdf": pdf,
            }
        )
    return papers


def parse_timestamp(value):
    """arXiv ISO-8601 zaman damgasini timezone-aware datetime'a cevirir."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def filter_recent(papers, now, hours=WINDOW_HOURS):
    """Son `hours` saat icinde yayinlanmis makaleleri dondurur."""
    cutoff = now - dt.timedelta(hours=hours)
    recent = []
    for paper in papers:
        stamp = parse_timestamp(paper.get("published"))
        if stamp is not None and stamp >= cutoff:
            recent.append(paper)
    return recent


# --------------------------------------------------------------------------
# Tekrar onleme (seen.json)
# --------------------------------------------------------------------------

def load_seen(path=SEEN_PATH):
    data = read_json(path)
    if isinstance(data, dict):
        ids = data.get("ids")
    else:
        ids = data
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str)]


def save_seen(ids, path=SEEN_PATH, limit=SEEN_LIMIT):
    """En yeni ID'ler basta olacak sekilde son `limit` kaydi saklar."""
    trimmed = ids[:limit]
    write_json(path, {"ids": trimmed})
    return trimmed


# --------------------------------------------------------------------------
# TypeSafe / Jev
# --------------------------------------------------------------------------

def build_state(paper, reader=READER):
    """Jev'e gonderilen state. Tam metin degil, sadece gereken alanlar."""
    return {
        "paper": {
            "title": paper.get("title", ""),
            "abstract": (paper.get("abstract") or "")[:ABSTRACT_LIMIT],
            "categories": paper.get("categories", []),
        },
        "reader": reader,
    }


def _retry_delay(attempt, retry_after=None):
    """Ustel geri cekilme + jitter. Retry-After basligi varsa ona uyar."""
    if retry_after is not None:
        return min(float(retry_after), 60.0)
    return min(2.0 ** attempt, 30.0) + random.uniform(0, 0.5)


def _parse_retry_after(headers):
    if headers is None:
        return None
    value = headers.get("retry-after-ms")
    if value:
        try:
            return float(value) / 1000.0
        except ValueError:
            pass
    value = headers.get("retry-after")
    if value:
        try:
            return float(value)
        except ValueError:
            return None
    return None


def call_jev(state, api_key, questions=None, endpoint=TYPESAFE_ENDPOINT,
             model=TYPESAFE_MODEL, max_attempts=MAX_ATTEMPTS, sleep=time.sleep):
    """POST /v1/systemone. `answers` sozlugunu dondurur.

    429 ve 5xx (529 dahil) yanitlarinda ustel geri cekilme ile yeniden dener.
    API anahtari yalnizca Authorization basliginda kullanilir, loglanmaz.
    """
    payload = {
        "state": state,
        "model": model,
        "questions": questions if questions is not None else QUESTIONS,
    }
    body = json.dumps(payload).encode("utf-8")
    last_error = None

    for attempt in range(max_attempts):
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TYPESAFE_TIMEOUT) as response:
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


def normalize_answers(answers):
    """Jev yanitini 0-1 arasi sinyallere cevirir.

    noul  -> `noul` alani zaten 0-1 olasilik, oldugu gibi alinir (guven degeri
             tasimaz, tasarim geregi).
    score -> `score` seviyeler uzerinde olasilik agirlikli konumdur; en buyuk
             seviye indeksine, yani (seviye_sayisi - 1)'e bolunerek 0-1'e cekilir.
    """
    signals = {}
    topic = "Other"
    topic_confidence = 0.0
    novelty_confidence = 0.0

    for key in SIGNAL_KEYS:
        answer = answers.get(key)
        if not isinstance(answer, dict):
            continue
        kind = answer.get("type")
        if kind == "noul":
            value = answer.get("noul")
            if isinstance(value, (int, float)):
                signals[key] = _clamp01(float(value))
        elif kind == "score":
            value = answer.get("score")
            if isinstance(value, (int, float)):
                signals[key] = _clamp01(float(value) / _max_level(answer))
            if key == "novelty":
                novelty_confidence = _confidence(answer)

    choice = answers.get("topic")
    if isinstance(choice, dict):
        name = choice.get("choice")
        if isinstance(name, str) and name:
            topic = name
        topic_confidence = _confidence(choice)

    return signals, topic, topic_confidence, novelty_confidence


def _max_level(answer):
    """Score yanitinda en buyuk seviye indeksi = seviye_sayisi - 1."""
    probabilities = answer.get("probabilities")
    levels = []
    if isinstance(probabilities, dict):
        for key in probabilities:
            try:
                levels.append(int(key))
            except (TypeError, ValueError):
                continue
    if not levels:
        legend = answer.get("legend")
        if isinstance(legend, dict):
            for key in legend:
                try:
                    levels.append(int(key))
                except (TypeError, ValueError):
                    continue
    top = max(levels) if levels else 0
    return top if top > 0 else 1


def _confidence(answer):
    value = answer.get("confidence")
    if isinstance(value, (int, float)):
        return _clamp01(float(value))
    return 0.0


def _clamp01(value):
    if value != value:  # NaN
        return 0.0
    return max(0.0, min(1.0, value))


def mock_answers(paper):
    """Anahtarsiz yerel test icin makale ID'sinden turetilmis sahte yanit."""
    digest = hashlib.sha256(paper["id"].encode("utf-8")).digest()

    def unit(index):
        return digest[index] / 255.0

    top_level = len(NOVELTY_LEVELS) - 1
    return {
        "novelty": {
            "type": "score",
            "score": round(unit(0) * top_level, 3),
            "confidence": round(0.5 + unit(1) * 0.5, 3),
            "probabilities": {str(i): 1.0 / len(NOVELTY_LEVELS)
                              for i in range(len(NOVELTY_LEVELS))},
        },
        "evidence": {
            "type": "score",
            "score": round(unit(2) * (len(EVIDENCE_LEVELS) - 1), 3),
            "confidence": round(0.5 + unit(3) * 0.5, 3),
            "probabilities": {str(i): 1.0 / len(EVIDENCE_LEVELS)
                              for i in range(len(EVIDENCE_LEVELS))},
        },
        "practical": {"type": "noul", "noul": round(unit(4), 3)},
        "relevance": {"type": "noul", "noul": round(unit(5), 3)},
        "code": {"type": "noul", "noul": round(unit(6), 3)},
        "hype": {"type": "noul", "noul": round(unit(7) * 0.6, 3)},
        "topic": {
            "type": "choice",
            "choice": TOPICS[digest[8] % len(TOPICS)],
            "confidence": round(0.4 + unit(9) * 0.6, 3),
            "probabilities": {name: 1.0 / len(TOPICS) for name in TOPICS},
        },
    }


def evaluate(paper, api_key, mock=False):
    """Tek makaleyi degerlendirir ve site icin kayit uretir."""
    answers = mock_answers(paper) if mock else call_jev(build_state(paper), api_key)
    signals, topic, topic_confidence, novelty_confidence = normalize_answers(answers)
    return {
        "id": paper["id"],
        "title": paper.get("title", ""),
        "abstract": paper.get("abstract", ""),
        "authors": paper.get("authors", [])[:MAX_AUTHORS],
        "published": paper.get("published", ""),
        "categories": paper.get("categories", []),
        "url": paper.get("url", ""),
        "pdf": paper.get("pdf", ""),
        "signals": signals,
        "topic": topic,
        "topic_confidence": round(topic_confidence, 3),
        "novelty_confidence": round(novelty_confidence, 3),
    }


def evaluate_all(papers, api_key, mock=False, workers=WORKERS):
    """Makaleleri paralel degerlendirir. Tek bir hata tarama durdurmaz."""
    results = []
    failures = 0
    if not papers:
        return results, failures

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(evaluate, p, api_key, mock): p for p in papers}
        for future in concurrent.futures.as_completed(futures):
            paper = futures[future]
            try:
                results.append(future.result())
            except Exception as error:  # tek makale tum taramayi durdurmasin
                failures += 1
                log("  ! %s atlandi: %s" % (paper["id"], error))

    order = {p["id"]: i for i, p in enumerate(papers)}
    results.sort(key=lambda r: order.get(r["id"], 0))
    return results, failures


# --------------------------------------------------------------------------
# Dosya cikti katmani
# --------------------------------------------------------------------------

def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=False)
        handle.write("\n")


def day_filename(date_str):
    return "radar-%s.json" % date_str


def write_day_file(data_dir, date_str, papers, generated_at, demo=False):
    path = os.path.join(data_dir, day_filename(date_str))
    write_json(
        path,
        {
            "format": "arxiv-radar/1",
            "generated_at": generated_at,
            "demo": bool(demo),
            "papers": papers,
        },
    )
    return path


def prune_old_files(data_dir, today, retention_days=RETENTION_DAYS):
    """60 gunden eski gunluk dosyalari siler; silinen tarihleri dondurur."""
    cutoff = today - dt.timedelta(days=retention_days)
    removed = []
    try:
        names = os.listdir(data_dir)
    except OSError:
        return removed
    for name in names:
        match = DAY_FILE_RE.match(name)
        if not match:
            continue
        try:
            date = dt.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if date < cutoff:
            try:
                os.remove(os.path.join(data_dir, name))
            except OSError:
                continue
            removed.append(match.group(1))
    return removed


def rebuild_index(data_dir, updated_at, retention_days=RETENTION_DAYS, today=None):
    """Diskteki gunluk dosyalari tarayip index.json'u yeniden yazar.

    Index her zaman gercekte var olan dosyalari yansitir, en yeni tarih basta.
    """
    if today is None:
        today = dt.datetime.now(dt.timezone.utc).date()
    cutoff = today - dt.timedelta(days=retention_days)

    entries = []
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        names = []

    for name in names:
        match = DAY_FILE_RE.match(name)
        if not match:
            continue
        date_str = match.group(1)
        try:
            if dt.date.fromisoformat(date_str) < cutoff:
                continue
        except ValueError:
            continue
        payload = read_json(os.path.join(data_dir, name), default={})
        papers = payload.get("papers") if isinstance(payload, dict) else None
        entries.append(
            {
                "date": date_str,
                "path": "data/" + name,
                "count": len(papers) if isinstance(papers, list) else 0,
            }
        )

    entries.sort(key=lambda entry: entry["date"], reverse=True)
    index = {"updated_at": updated_at, "files": entries}
    write_json(os.path.join(data_dir, "index.json"), index)
    return index


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="arXiv Radar tarayicisi")
    parser.add_argument("--mock", action="store_true",
                        help="API cagrisi yapmadan sahte sinyal uret (yerel test)")
    parser.add_argument("--limit", type=int, default=None,
                        help="En fazla bu kadar makale degerlendir")
    parser.add_argument("--data-dir", default=DATA_DIR,
                        help="Cikti klasoru (varsayilan: site/data)")
    parser.add_argument("--xml", default=None,
                        help="arXiv'e istek atmak yerine bu Atom XML dosyasini oku "
                             "(cevrimdisi deneme ve hata ayiklama icin)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    data_dir = args.data_dir
    seen_path = os.path.join(data_dir, "seen.json")

    now = dt.datetime.now(dt.timezone.utc)
    generated_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    today = now.date()
    date_str = today.isoformat()

    api_key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
    if not args.mock and not api_key:
        log("HATA: TYPESAFE_API_KEY tanimli degil. Yerel deneme icin --mock kullanin.")
        return 2

    if args.xml:
        log("arXiv yaniti dosyadan okunuyor: %s" % args.xml)
        try:
            with open(args.xml, "r", encoding="utf-8") as handle:
                papers = parse_atom(handle.read())
        except OSError as error:
            log("HATA: %s okunamadi: %s" % (args.xml, error))
            return 1
        except ET.ParseError as error:
            log("HATA: arXiv yaniti ayristirilamadi: %s" % error)
            return 1
    else:
        log("arXiv sorgusu (kategori basina ayri istek): %s" % ", ".join(CATEGORIES))
        try:
            papers = fetch_categories()
        except Exception as error:
            log("HATA: arXiv'e ulasilamadi: %s" % error)
            return 1
    log("  toplam %d benzersiz kayit" % len(papers))

    papers = filter_recent(papers, now)
    log("  son %d saatte: %d" % (WINDOW_HOURS, len(papers)))

    seen = load_seen(seen_path)
    seen_set = set(seen)
    fresh = [p for p in papers if p["id"] not in seen_set]
    log("  daha once gorulmemis: %d" % len(fresh))

    cap = MAX_PAPERS if args.limit is None else min(args.limit, MAX_PAPERS)
    selected = fresh[:cap]
    if len(fresh) > len(selected):
        log("  siniri asan %d makale bugun atlandi" % (len(fresh) - len(selected)))

    log("Jev degerlendirmesi: %d makale%s"
        % (len(selected), " (mock)" if args.mock else ""))
    results, failures = evaluate_all(selected, api_key, mock=args.mock)
    log("  basarili: %d, atlanan: %d" % (len(results), failures))

    # Gun icinde ikinci kez calisirsa ayni dosyaya ekleme yap, ustune yazma.
    existing = read_json(os.path.join(data_dir, day_filename(date_str)), default={})
    merged = []
    merged_ids = set()
    if isinstance(existing, dict) and isinstance(existing.get("papers"), list):
        for paper in existing["papers"]:
            if isinstance(paper, dict) and paper.get("id") not in merged_ids:
                merged.append(paper)
                merged_ids.add(paper.get("id"))
    demo = bool(args.mock) or (isinstance(existing, dict) and existing.get("demo") is True)
    for paper in results:
        if paper["id"] not in merged_ids:
            merged.append(paper)
            merged_ids.add(paper["id"])

    write_day_file(data_dir, date_str, merged, generated_at, demo=demo)
    log("  yazildi: data/%s (%d makale)" % (day_filename(date_str), len(merged)))

    # Sadece gercekten degerlendirilenleri gorulmus say; boylece bir hata
    # yuzunden atlanan makale yarin tekrar denenir.
    scored_ids = [paper["id"] for paper in results]
    save_seen(scored_ids + seen, seen_path)

    removed = prune_old_files(data_dir, today)
    if removed:
        log("  %d eski dosya silindi" % len(removed))

    index = rebuild_index(data_dir, generated_at, today=today)
    log("  index.json: %d gun" % len(index["files"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
