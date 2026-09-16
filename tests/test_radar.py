"""radar.py birim testleri:  python -m unittest discover -s tests -v"""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline"))

import radar  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "arxiv_sample.xml")
UTC = dt.timezone.utc


def load_fixture():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return handle.read()


class AtomParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.papers = radar.parse_atom(load_fixture())

    def test_parses_every_entry(self):
        self.assertEqual(len(self.papers), 3)

    def test_id_drops_url_prefix_and_version(self):
        self.assertEqual([p["id"] for p in self.papers],
                         ["2609.01234", "2609.00987", "2608.55555"])

    def test_title_whitespace_is_collapsed(self):
        self.assertEqual(self.papers[0]["title"],
                         "Tool-Calling Agents with Verified Retrieval")

    def test_abstract_is_stripped_and_collapsed(self):
        self.assertTrue(self.papers[0]["abstract"].startswith("We present a scheduler"))
        self.assertNotIn("\n", self.papers[0]["abstract"])

    def test_authors_capped_at_six(self):
        self.assertEqual(len(self.papers[0]["authors"]), radar.MAX_AUTHORS)
        self.assertEqual(self.papers[0]["authors"][0], "Ada Lovelace")

    def test_categories_collected_in_order(self):
        self.assertEqual(self.papers[0]["categories"], ["cs.AI", "cs.CL"])

    def test_links_upgraded_to_https(self):
        self.assertEqual(self.papers[0]["url"], "https://arxiv.org/abs/2609.01234v1")
        self.assertEqual(self.papers[0]["pdf"], "https://arxiv.org/pdf/2609.01234v1")

    def test_missing_pdf_link_falls_back(self):
        # Ucuncu kayitta pdf linki yok.
        self.assertEqual(self.papers[2]["pdf"], "https://arxiv.org/pdf/2608.55555")

    def test_primary_category(self):
        self.assertEqual(self.papers[1]["primary_category"], "cs.SD")


class TimeWindowTests(unittest.TestCase):
    def setUp(self):
        self.papers = radar.parse_atom(load_fixture())
        self.now = dt.datetime(2026, 9, 16, 6, 0, tzinfo=UTC)

    def test_keeps_only_last_48_hours(self):
        recent = radar.filter_recent(self.papers, self.now, hours=48)
        self.assertEqual([p["id"] for p in recent], ["2609.01234", "2609.00987"])

    def test_narrow_window_excludes_older_entry(self):
        recent = radar.filter_recent(self.papers, self.now, hours=3)
        self.assertEqual([p["id"] for p in recent], ["2609.01234"])

    def test_entry_without_timestamp_is_dropped(self):
        broken = [{"id": "x", "published": ""}]
        self.assertEqual(radar.filter_recent(broken, self.now), [])

    def test_parse_timestamp_handles_z_and_offset(self):
        self.assertEqual(radar.parse_timestamp("2026-09-16T05:30:00Z"),
                         dt.datetime(2026, 9, 16, 5, 30, tzinfo=UTC))
        self.assertEqual(radar.parse_timestamp("2026-09-16T01:30:00-04:00"),
                         dt.datetime(2026, 9, 16, 5, 30, tzinfo=UTC))


class NormalizationTests(unittest.TestCase):
    def test_noul_taken_as_is(self):
        answers = {"practical": {"type": "noul", "noul": 0.73}}
        signals, _, _, _ = radar.normalize_answers(answers)
        self.assertAlmostEqual(signals["practical"], 0.73)

    def test_score_divided_by_max_level(self):
        # Uc seviyeli rubrik: en buyuk indeks 2, yani score / 2.
        answers = {
            "novelty": {
                "type": "score",
                "score": 1.0,
                "confidence": 0.8,
                "probabilities": {"0": 0.2, "1": 0.6, "2": 0.2},
            }
        }
        signals, _, _, novelty_confidence = radar.normalize_answers(answers)
        self.assertAlmostEqual(signals["novelty"], 0.5)
        self.assertAlmostEqual(novelty_confidence, 0.8)

    def test_fractional_score_is_supported(self):
        answers = {
            "evidence": {
                "type": "score",
                "score": 1.7,
                "confidence": 0.5,
                "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8},
            }
        }
        signals, _, _, _ = radar.normalize_answers(answers)
        self.assertAlmostEqual(signals["evidence"], 0.85)

    def test_max_level_read_from_legend_when_probabilities_missing(self):
        answers = {
            "novelty": {
                "type": "score",
                "score": 2.0,
                "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
            }
        }
        signals, _, _, _ = radar.normalize_answers(answers)
        self.assertAlmostEqual(signals["novelty"], 2.0 / 3.0)

    def test_single_level_rubric_does_not_divide_by_zero(self):
        answers = {"novelty": {"type": "score", "score": 0.0, "probabilities": {"0": 1.0}}}
        signals, _, _, _ = radar.normalize_answers(answers)
        self.assertEqual(signals["novelty"], 0.0)

    def test_values_are_clamped_to_unit_range(self):
        answers = {
            "code": {"type": "noul", "noul": 1.4},
            "hype": {"type": "noul", "noul": -0.2},
        }
        signals, _, _, _ = radar.normalize_answers(answers)
        self.assertEqual(signals["code"], 1.0)
        self.assertEqual(signals["hype"], 0.0)

    def test_choice_topic_and_confidence(self):
        answers = {
            "topic": {
                "type": "choice",
                "choice": "Retrieval and RAG",
                "confidence": 0.62,
                "probabilities": {"Retrieval and RAG": 0.62},
            }
        }
        _, topic, topic_confidence, _ = radar.normalize_answers(answers)
        self.assertEqual(topic, "Retrieval and RAG")
        self.assertAlmostEqual(topic_confidence, 0.62)

    def test_missing_answers_fall_back_safely(self):
        signals, topic, topic_confidence, novelty_confidence = radar.normalize_answers({})
        self.assertEqual(signals, {})
        self.assertEqual(topic, "Other")
        self.assertEqual(topic_confidence, 0.0)
        self.assertEqual(novelty_confidence, 0.0)

    def test_noul_answers_carry_no_confidence(self):
        # Tasarim geregi noul yanitlari confidence tasimaz; sadece score ve
        # choice tasir. Bu test o varsayimi sabitler.
        answers = radar.mock_answers({"id": "2609.01234"})
        self.assertNotIn("confidence", answers["practical"])
        self.assertIn("confidence", answers["novelty"])
        self.assertIn("confidence", answers["topic"])

    def test_mock_answers_normalize_into_every_signal(self):
        signals, topic, _, _ = radar.normalize_answers(radar.mock_answers({"id": "2609.01234"}))
        self.assertEqual(sorted(signals), sorted(radar.SIGNAL_KEYS))
        for value in signals.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertIn(topic, radar.TOPICS)


class QuestionShapeTests(unittest.TestCase):
    """Sorularin tel uzerindeki bicimi (criteria alani) bozulmasin."""

    def test_score_criteria_is_an_ordered_list(self):
        self.assertIsInstance(radar.QUESTIONS["novelty"]["criteria"], list)
        self.assertEqual(len(radar.QUESTIONS["novelty"]["criteria"]), 3)

    def test_choice_criteria_is_a_label_map(self):
        criteria = radar.QUESTIONS["topic"]["criteria"]
        self.assertIsInstance(criteria, dict)
        self.assertEqual(list(criteria), radar.TOPICS)

    def test_noul_has_no_criteria(self):
        self.assertNotIn("criteria", radar.QUESTIONS["practical"])

    def test_every_question_has_a_type_and_instructions(self):
        for name, question in radar.QUESTIONS.items():
            self.assertIn(question["type"], {"noul", "choice", "score"}, name)
            self.assertTrue(question["instructions"].strip(), name)

    def test_state_truncates_abstract_and_omits_everything_else(self):
        state = radar.build_state({
            "title": "T",
            "abstract": "x" * 5000,
            "categories": ["cs.AI"],
            "authors": ["A"],
        })
        self.assertEqual(sorted(state), ["paper", "reader"])
        self.assertEqual(sorted(state["paper"]), ["abstract", "categories", "title"])
        self.assertEqual(len(state["paper"]["abstract"]), radar.ABSTRACT_LIMIT)
        self.assertEqual(state["reader"], radar.READER)


class SeenTests(unittest.TestCase):
    def test_roundtrip_and_trim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seen.json")
            kept = radar.save_seen(["a", "b", "c"], path, limit=2)
            self.assertEqual(kept, ["a", "b"])
            self.assertEqual(radar.load_seen(path), ["a", "b"])

    def test_missing_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(radar.load_seen(os.path.join(tmp, "nope.json")), [])

    def test_corrupt_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seen.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(radar.load_seen(path), [])


class IndexAndPruneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.today = dt.date(2026, 9, 16)

    def tearDown(self):
        self._tmp.cleanup()

    def make_day(self, date_str, count):
        papers = [{"id": "%s-%d" % (date_str, i)} for i in range(count)]
        radar.write_day_file(self.data_dir, date_str, papers, "2026-09-16T05:00:00Z")

    def test_index_lists_newest_first_with_counts_and_paths(self):
        self.make_day("2026-09-14", 2)
        self.make_day("2026-09-16", 5)
        self.make_day("2026-09-15", 0)

        index = radar.rebuild_index(self.data_dir, "2026-09-16T05:00:00Z", today=self.today)

        self.assertEqual([f["date"] for f in index["files"]],
                         ["2026-09-16", "2026-09-15", "2026-09-14"])
        self.assertEqual([f["count"] for f in index["files"]], [5, 0, 2])
        self.assertEqual(index["files"][0]["path"], "data/radar-2026-09-16.json")
        self.assertEqual(index["updated_at"], "2026-09-16T05:00:00Z")

    def test_index_is_written_to_disk(self):
        self.make_day("2026-09-16", 1)
        radar.rebuild_index(self.data_dir, "2026-09-16T05:00:00Z", today=self.today)
        with open(os.path.join(self.data_dir, "index.json"), encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["files"][0]["date"], "2026-09-16")

    def test_index_ignores_unrelated_files(self):
        self.make_day("2026-09-16", 1)
        radar.save_seen(["a"], os.path.join(self.data_dir, "seen.json"))
        index = radar.rebuild_index(self.data_dir, "t", today=self.today)
        self.assertEqual(len(index["files"]), 1)

    def test_prune_removes_files_older_than_retention(self):
        self.make_day("2026-09-16", 1)   # bugun
        self.make_day("2026-08-17", 1)   # 30 gun once -> kalir
        self.make_day("2026-07-17", 1)   # 61 gun once -> silinir
        self.make_day("2026-01-01", 1)   # cok eski -> silinir

        removed = radar.prune_old_files(self.data_dir, self.today, retention_days=60)

        self.assertEqual(sorted(removed), ["2026-01-01", "2026-07-17"])
        remaining = sorted(n for n in os.listdir(self.data_dir) if n.startswith("radar-"))
        self.assertEqual(remaining, ["radar-2026-08-17.json", "radar-2026-09-16.json"])

    def test_boundary_day_is_kept(self):
        # Tam 60 gun once olan dosya sinirda kalir, silinmez.
        self.make_day((self.today - dt.timedelta(days=60)).isoformat(), 1)
        removed = radar.prune_old_files(self.data_dir, self.today, retention_days=60)
        self.assertEqual(removed, [])

    def test_pruned_days_leave_the_index(self):
        self.make_day("2026-09-16", 1)
        self.make_day("2026-07-17", 1)
        radar.prune_old_files(self.data_dir, self.today, retention_days=60)
        index = radar.rebuild_index(self.data_dir, "t", today=self.today)
        self.assertEqual([f["date"] for f in index["files"]], ["2026-09-16"])

    def test_day_file_format_envelope(self):
        radar.write_day_file(self.data_dir, "2026-09-16", [{"id": "a"}],
                             "2026-09-16T05:00:00Z", demo=True)
        with open(os.path.join(self.data_dir, "radar-2026-09-16.json"), encoding="utf-8") as h:
            payload = json.load(h)
        self.assertEqual(payload["format"], "arxiv-radar/1")
        self.assertEqual(payload["generated_at"], "2026-09-16T05:00:00Z")
        self.assertIs(payload["demo"], True)
        self.assertEqual(payload["papers"], [{"id": "a"}])


class EvaluateTests(unittest.TestCase):
    def test_mock_evaluation_emits_every_output_field(self):
        paper = radar.parse_atom(load_fixture())[0]
        record = radar.evaluate(paper, api_key="", mock=True)
        self.assertEqual(
            sorted(record),
            sorted(["id", "title", "abstract", "authors", "published", "categories",
                    "url", "pdf", "signals", "topic", "topic_confidence",
                    "novelty_confidence"]),
        )
        self.assertEqual(sorted(record["signals"]), sorted(radar.SIGNAL_KEYS))
        self.assertLessEqual(len(record["authors"]), radar.MAX_AUTHORS)

    def test_mock_is_deterministic_for_the_same_id(self):
        paper = {"id": "2609.01234", "title": "t", "abstract": "a"}
        self.assertEqual(radar.evaluate(paper, "", mock=True),
                         radar.evaluate(paper, "", mock=True))

    def test_one_failing_paper_does_not_stop_the_run(self):
        papers = [{"id": "good-1", "title": "a", "abstract": "x"},
                  {"id": "boom", "title": "b", "abstract": "y"},
                  {"id": "good-2", "title": "c", "abstract": "z"}]

        real_evaluate = radar.evaluate

        def flaky(paper, api_key, mock=False):
            if paper["id"] == "boom":
                raise RuntimeError("HTTP 500")
            return real_evaluate(paper, api_key, mock=True)

        radar.evaluate = flaky
        try:
            results, failures = radar.evaluate_all(papers, "", mock=True, workers=2)
        finally:
            radar.evaluate = real_evaluate

        self.assertEqual(failures, 1)
        self.assertEqual([r["id"] for r in results], ["good-1", "good-2"])

    def test_results_keep_input_order(self):
        papers = [{"id": "a", "title": "", "abstract": ""},
                  {"id": "b", "title": "", "abstract": ""},
                  {"id": "c", "title": "", "abstract": ""}]
        results, _ = radar.evaluate_all(papers, "", mock=True, workers=3)
        self.assertEqual([r["id"] for r in results], ["a", "b", "c"])


class RetryTests(unittest.TestCase):
    def test_backoff_grows_and_respects_retry_after(self):
        self.assertLess(radar._retry_delay(0), radar._retry_delay(3))
        self.assertEqual(radar._retry_delay(0, retry_after=7), 7.0)
        self.assertEqual(radar._retry_delay(0, retry_after=999), 60.0)

    def test_retry_after_ms_header_is_read(self):
        self.assertAlmostEqual(radar._parse_retry_after({"retry-after-ms": "2500"}), 2.5)

    def test_retry_after_seconds_header_is_read(self):
        self.assertAlmostEqual(radar._parse_retry_after({"retry-after": "3"}), 3.0)

    def test_missing_or_bad_headers_return_none(self):
        self.assertIsNone(radar._parse_retry_after({}))
        self.assertIsNone(radar._parse_retry_after({"retry-after": "soon"}))
        self.assertIsNone(radar._parse_retry_after(None))


class UrlTests(unittest.TestCase):
    def test_query_is_single_category_and_sorts_by_date(self):
        url = radar.build_arxiv_url("cs.AI")
        self.assertTrue(url.startswith(radar.ARXIV_ENDPOINT + "?"))
        self.assertIn("cat%3Acs.AI", url)
        self.assertIn("sortBy=submittedDate", url)
        self.assertIn("sortOrder=descending", url)
        self.assertIn("max_results=%d" % radar.MAX_RESULTS, url)

    def test_query_never_contains_or(self):
        # OR'lu sorgular arXiv'den 406 donduruyor; her kategori ayri cekilir.
        for category in radar.CATEGORIES:
            url = radar.build_arxiv_url(category)
            self.assertNotIn("OR", url)
            self.assertEqual(url.count("cat%3A"), 1)

    def test_short_id_strips_version(self):
        self.assertEqual(radar.short_id("http://arxiv.org/abs/2609.01234v12"), "2609.01234")
        self.assertEqual(radar.short_id("2609.01234"), "2609.01234")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# TypeSafe cagrisi -- yerel bir taklit sunucuya karsi. Gercek API'ye cikilmaz,
# ama tel uzerindeki istek govdesi ve yeniden deneme davranisi dogrulanir.
# ---------------------------------------------------------------------------

import http.server  # noqa: E402
import threading    # noqa: E402


class StubHandler(http.server.BaseHTTPRequestHandler):
    """Sirayla `script` listesindeki (status, body) ciftlerini dondurur."""

    script = []
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        StubHandler.received.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(self.rfile.read(length).decode("utf-8")),
        })
        status, payload, headers = StubHandler.script.pop(0)
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class StubServerCase(unittest.TestCase):
    def setUp(self):
        StubHandler.script = []
        StubHandler.received = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), StubHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = "http://127.0.0.1:%d/v1/systemone" % self.server.server_address[1]
        self.slept = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, state=None, **kwargs):
        return radar.call_jev(
            state if state is not None else {"paper": {"title": "t"}, "reader": "r"},
            "sk-test-anahtar",
            endpoint=self.endpoint,
            sleep=self.slept.append,
            **kwargs
        )


class WireContractTests(StubServerCase):
    def ok_answer(self):
        return {
            "model": "jev-latest",
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "answers": {"practical": {"type": "noul", "noul": 0.9}},
        }

    def test_request_body_shape(self):
        StubHandler.script = [(200, self.ok_answer(), None)]
        paper = radar.parse_atom(load_fixture())[0]
        self.call(radar.build_state(paper))

        body = StubHandler.received[0]["body"]
        self.assertEqual(sorted(body), ["model", "questions", "state"])
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(sorted(body["state"]), ["paper", "reader"])
        self.assertEqual(body["state"]["reader"], radar.READER)
        self.assertEqual(sorted(body["questions"]),
                         sorted(["novelty", "evidence", "practical", "relevance",
                                 "code", "hype", "topic"]))
        self.assertEqual(body["questions"]["novelty"]["type"], "score")
        self.assertEqual(body["questions"]["novelty"]["criteria"], radar.NOVELTY_LEVELS)
        self.assertEqual(body["questions"]["topic"]["type"], "choice")
        self.assertEqual(sorted(body["questions"]["topic"]["criteria"]), sorted(radar.TOPICS))
        self.assertEqual(body["questions"]["practical"]["type"], "noul")
        self.assertNotIn("criteria", body["questions"]["practical"])

    def test_authorization_header_carries_the_key(self):
        StubHandler.script = [(200, self.ok_answer(), None)]
        self.call()
        headers = StubHandler.received[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer sk-test-anahtar")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_key_never_appears_in_the_body(self):
        StubHandler.script = [(200, self.ok_answer(), None)]
        self.call()
        self.assertNotIn("sk-test-anahtar", json.dumps(StubHandler.received[0]["body"]))

    def test_answers_are_returned(self):
        StubHandler.script = [(200, self.ok_answer(), None)]
        answers = self.call()
        self.assertEqual(answers["practical"]["noul"], 0.9)


class RetryBehaviourTests(StubServerCase):
    def ok(self):
        return (200, {"model": "jev-latest",
                      "answers": {"code": {"type": "noul", "noul": 0.5}}}, None)

    def test_429_is_retried_then_succeeds(self):
        StubHandler.script = [(429, {"error": "rate limited"}, None), self.ok()]
        answers = self.call()
        self.assertEqual(answers["code"]["noul"], 0.5)
        self.assertEqual(len(StubHandler.received), 2)
        self.assertEqual(len(self.slept), 1)

    def test_529_is_retried_then_succeeds(self):
        StubHandler.script = [(529, {"error": "overloaded"}, None), self.ok()]
        self.call()
        self.assertEqual(len(StubHandler.received), 2)

    def test_retry_after_header_is_honoured(self):
        StubHandler.script = [(429, {"error": "slow down"}, {"Retry-After": "4"}), self.ok()]
        self.call()
        self.assertEqual(self.slept, [4.0])

    def test_gives_up_after_max_attempts(self):
        StubHandler.script = [(529, {"error": "overloaded"}, None)] * 5
        with self.assertRaises(RuntimeError) as caught:
            self.call()
        self.assertIn("529", str(caught.exception))
        self.assertEqual(len(StubHandler.received), 5)
        self.assertEqual(len(self.slept), 4)

    def test_400_is_not_retried(self):
        StubHandler.script = [(400, {"error": "bad request"}, None)]
        with self.assertRaises(RuntimeError):
            self.call()
        self.assertEqual(len(StubHandler.received), 1)
        self.assertEqual(self.slept, [])

    def test_401_is_not_retried(self):
        StubHandler.script = [(401, {"error": "unauthorized"}, None)]
        with self.assertRaises(RuntimeError):
            self.call()
        self.assertEqual(len(StubHandler.received), 1)

    def test_malformed_200_is_retried_then_fails(self):
        StubHandler.script = [(200, {"model": "x"}, None)] * 5
        with self.assertRaises(RuntimeError):
            self.call()
        self.assertEqual(len(StubHandler.received), 5)

    def test_backoff_grows_between_attempts(self):
        StubHandler.script = [(529, {"e": 1}, None)] * 4 + [self.ok()]
        self.call()
        self.assertEqual(len(self.slept), 4)
        self.assertLess(self.slept[0], self.slept[-1])


class ArxivFetchHandler(http.server.BaseHTTPRequestHandler):
    script = []
    received = []

    def do_GET(self):
        ArxivFetchHandler.received.append({
            "path": self.path,
            "headers": dict(self.headers),
        })
        status, body, headers = ArxivFetchHandler.script.pop(0)
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/atom+xml")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class ArxivFetchTests(unittest.TestCase):
    """arXiv istegi -- yerel taklit sunucuya karsi.

    arXiv, Accept basligi olmayan istekleri 406 Not Acceptable ile reddeder;
    urllib bu basligi kendiliginden gondermez. Bu testler o regresyonu kilitler.
    """

    def setUp(self):
        ArxivFetchHandler.script = []
        ArxivFetchHandler.received = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), ArxivFetchHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/api/query" % self.server.server_address[1]
        self.slept = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def fetch(self, **kwargs):
        return radar.fetch_arxiv(self.url, sleep=self.slept.append, **kwargs)

    def test_accept_header_is_sent(self):
        ArxivFetchHandler.script = [(200, "<feed/>", None)]
        self.fetch()
        accept = ArxivFetchHandler.received[0]["headers"]["Accept"]
        self.assertIn("application/atom+xml", accept)
        self.assertIn("*/*", accept)

    def test_user_agent_is_descriptive(self):
        ArxivFetchHandler.script = [(200, "<feed/>", None)]
        self.fetch()
        agent = ArxivFetchHandler.received[0]["headers"]["User-Agent"]
        self.assertIn("arxiv-radar", agent)
        self.assertNotIn("Python-urllib", agent)

    def test_body_is_returned(self):
        ArxivFetchHandler.script = [(200, "<feed>ok</feed>", None)]
        self.assertEqual(self.fetch(), "<feed>ok</feed>")

    def test_406_is_retried(self):
        # arXiv yuk altinda 429 yerine 406 dondurebiliyor; olcumlerde tek
        # basina calisan bir kategori sorgusu ayni kosu icinde 406 verdi.
        ArxivFetchHandler.script = [(406, "not acceptable", None), (200, "<feed/>", None)]
        self.assertEqual(self.fetch(), "<feed/>")
        self.assertEqual(len(ArxivFetchHandler.received), 2)

    def test_400_is_not_retried(self):
        ArxivFetchHandler.script = [(400, "bad request", None)]
        with self.assertRaises(RuntimeError) as caught:
            self.fetch()
        self.assertIn("400", str(caught.exception))
        self.assertEqual(len(ArxivFetchHandler.received), 1)
        self.assertEqual(self.slept, [])

    def test_transient_5xx_is_retried(self):
        ArxivFetchHandler.script = [(503, "busy", None), (200, "<feed/>", None)]
        self.assertEqual(self.fetch(), "<feed/>")
        self.assertEqual(len(ArxivFetchHandler.received), 2)

    def test_retry_waits_at_least_the_polite_interval(self):
        ArxivFetchHandler.script = [(503, "busy", None), (200, "<feed/>", None)]
        self.fetch()
        self.assertGreaterEqual(self.slept[0], radar.ARXIV_RETRY_WAIT)

    def test_gives_up_after_attempts(self):
        ArxivFetchHandler.script = [(503, "busy", None)] * 3
        with self.assertRaises(RuntimeError):
            self.fetch()
        self.assertEqual(len(ArxivFetchHandler.received), 3)

    def test_endpoint_is_https(self):
        self.assertTrue(radar.ARXIV_ENDPOINT.startswith("https://"))
        self.assertTrue(radar.build_arxiv_url("cs.AI").startswith("https://export.arxiv.org/"))


class FetchCategoriesTests(unittest.TestCase):
    """Kategori basina ayri istek.

    arXiv "cat:cs.AI OR cat:cs.CL" gibi OR'lu sorgulari 406 ile reddediyor
    (olcumde arka arkaya 11 OR sorgusu 406 verdi, hemen ardindan tek
    kategorili sorgu 200 dondu). Bu yuzden her kategori ayri cekiliyor.
    """

    def setUp(self):
        self.calls = []
        self.slept = []
        self.real_fetch = radar.fetch_arxiv

    def tearDown(self):
        radar.fetch_arxiv = self.real_fetch

    def feed(self, ids):
        entries = "".join("""
          <entry>
            <id>http://arxiv.org/abs/%s v1</id>
            <published>2026-09-16T05:00:00Z</published>
            <title>T %s</title><summary>S</summary>
            <link href="http://arxiv.org/abs/%s" rel="alternate"/>
          </entry>""".replace(" v1", "v1") % (i, i, i) for i in ids)
        return ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
                + entries + '</feed>')

    def install(self, mapping):
        def fake(url, **kwargs):
            self.calls.append(url)
            for category, result in mapping.items():
                if "cat%3A" + category in url or "cat:" + category in url:
                    if isinstance(result, Exception):
                        raise result
                    return self.feed(result)
            raise AssertionError("beklenmeyen url: " + url)
        radar.fetch_arxiv = fake

    def test_one_request_per_category(self):
        self.install({"cs.AI": ["1"], "cs.CL": ["2"], "cs.LG": ["3"], "cs.SD": ["4"]})
        papers = radar.fetch_categories(sleep=self.slept.append)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(sorted(p["id"] for p in papers), ["1", "2", "3", "4"])

    def test_no_or_in_any_url(self):
        self.install({c: [] for c in radar.CATEGORIES})
        radar.fetch_categories(sleep=self.slept.append)
        for url in self.calls:
            self.assertNotIn("OR", url)
            self.assertNotIn("+OR+", url)
            self.assertEqual(url.count("cat%3A"), 1)

    def test_cross_listed_paper_appears_once(self):
        self.install({"cs.AI": ["1", "9"], "cs.CL": ["9"], "cs.LG": ["9"], "cs.SD": ["2"]})
        papers = radar.fetch_categories(sleep=self.slept.append)
        self.assertEqual(sorted(p["id"] for p in papers), ["1", "2", "9"])

    def test_waits_between_categories(self):
        self.install({c: [] for c in radar.CATEGORIES})
        radar.fetch_categories(sleep=self.slept.append)
        self.assertEqual(len(self.slept), len(radar.CATEGORIES) - 1)
        for wait in self.slept:
            self.assertGreaterEqual(wait, radar.ARXIV_RETRY_WAIT)

    def test_one_failing_category_does_not_stop_the_rest(self):
        self.install({"cs.AI": ["1"], "cs.CL": ["2"], "cs.LG": ["3"],
                      "cs.SD": RuntimeError("HTTP Error 406: Not Acceptable")})
        papers = radar.fetch_categories(sleep=self.slept.append)
        self.assertEqual(sorted(p["id"] for p in papers), ["1", "2", "3"])

    def test_all_categories_failing_raises(self):
        self.install({c: RuntimeError("HTTP Error 406") for c in radar.CATEGORIES})
        with self.assertRaises(RuntimeError):
            radar.fetch_categories(sleep=self.slept.append)

    def test_merged_papers_are_newest_first(self):
        self.install({"cs.AI": ["1"], "cs.CL": ["2"], "cs.LG": ["3"], "cs.SD": ["4"]})
        papers = radar.fetch_categories(sleep=self.slept.append)
        published = [p["published"] for p in papers]
        self.assertEqual(published, sorted(published, reverse=True))


class PaginationTests(unittest.TestCase):
    """`since` verildiginde kategori pencereye inene kadar sayfalanir."""

    def setUp(self):
        self.real = radar.fetch_arxiv
        self.urls = []
        self.slept = []

    def tearDown(self):
        radar.fetch_arxiv = self.real

    def feed(self, stamps):
        entries = "".join('''
          <entry><id>http://arxiv.org/abs/%s</id>
          <published>%s</published><title>T</title><summary>S</summary></entry>'''
                          % (ident, when) for ident, when in stamps)
        return ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
                + entries + "</feed>")

    def install(self, pages):
        def fake(url, **kwargs):
            self.urls.append(url)
            index = len(self.urls) - 1
            return self.feed(pages[index]) if index < len(pages) else self.feed([])
        radar.fetch_arxiv = fake

    def test_single_page_when_no_window_given(self):
        self.install([[("a", "2026-09-16T05:00:00Z")]] * 4)
        radar.fetch_category("cs.AI", since=None, sleep=self.slept.append)
        self.assertEqual(len(self.urls), 1)

    def test_pages_until_window_is_passed(self):
        self.install([
            [("a", "2026-09-16T05:00:00Z")],   # pencere icinde -> devam
            [("b", "2026-09-14T05:00:00Z")],   # pencere icinde -> devam
            [("c", "2026-09-01T05:00:00Z")],   # pencere disi -> dur
        ])
        since = dt.datetime(2026, 9, 10, tzinfo=UTC)
        entries = radar.fetch_category("cs.AI", since=since, sleep=self.slept.append)
        self.assertEqual(len(self.urls), 3)
        self.assertEqual([e["id"] for e in entries], ["a", "b", "c"])

    def test_start_offset_advances_by_max_results(self):
        self.install([[("a", "2026-09-16T05:00:00Z")], [("b", "2026-09-01T05:00:00Z")]])
        radar.fetch_category("cs.AI", since=dt.datetime(2026, 9, 10, tzinfo=UTC),
                             sleep=self.slept.append)
        self.assertIn("start=0", self.urls[0])
        self.assertIn("start=%d" % radar.MAX_RESULTS, self.urls[1])

    def test_stops_on_empty_page(self):
        self.install([[("a", "2026-09-16T05:00:00Z")], []])
        radar.fetch_category("cs.AI", since=dt.datetime(2026, 1, 1, tzinfo=UTC),
                             sleep=self.slept.append)
        self.assertEqual(len(self.urls), 2)

    def test_page_cap_is_respected(self):
        self.install([[("p%d" % i, "2026-09-16T05:00:00Z")] for i in range(20)])
        radar.fetch_category("cs.AI", since=dt.datetime(2020, 1, 1, tzinfo=UTC),
                             sleep=self.slept.append, max_pages=3)
        self.assertEqual(len(self.urls), 3)

    def test_build_url_accepts_start(self):
        self.assertIn("start=300", radar.build_arxiv_url("cs.AI", start=300))
