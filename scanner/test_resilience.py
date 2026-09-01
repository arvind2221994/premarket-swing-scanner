import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fno_trade_analyzer
import resilience
import web_app
from resilience import BoundedTTLCache, CircuitBreaker, KeyedLockPool, UpstreamUnavailableError


class BoundedTTLCacheTests(unittest.TestCase):
    def test_evicts_least_recently_used_entry(self):
        cache = BoundedTTLCache(max_size=2, ttl_seconds=60)
        cache.set("first", 1, now=0)
        cache.set("second", 2, now=1)
        self.assertEqual(cache.get("first", now=2), 1)

        cache.set("third", 3, now=3)

        self.assertIsNone(cache.get("second", now=4))
        self.assertEqual(cache.get("first", now=4), 1)
        self.assertEqual(cache.get("third", now=4), 3)

    def test_removes_expired_entry(self):
        cache = BoundedTTLCache(max_size=2, ttl_seconds=10)
        cache.set("ticker", {"score": 75}, now=5)

        self.assertIsNone(cache.get("ticker", now=15))


class KeyedLockPoolTests(unittest.TestCase):
    def test_same_key_is_serialized_and_different_keys_are_independent(self):
        pool = KeyedLockPool()
        first_entered = threading.Event()
        release_first = threading.Event()
        same_key_entered = threading.Event()
        other_key_entered = threading.Event()

        def hold_first():
            with pool.acquire("TCS"):
                first_entered.set()
                release_first.wait(timeout=1)

        def enter(key, event):
            with pool.acquire(key):
                event.set()

        first = threading.Thread(target=hold_first)
        duplicate = threading.Thread(target=enter, args=("TCS", same_key_entered))
        independent = threading.Thread(target=enter, args=("INFY", other_key_entered))
        first.start()
        self.assertTrue(first_entered.wait(timeout=1))
        duplicate.start()
        independent.start()

        self.assertTrue(other_key_entered.wait(timeout=1))
        self.assertFalse(same_key_entered.wait(timeout=0.05))
        release_first.set()
        self.assertTrue(same_key_entered.wait(timeout=1))
        first.join()
        duplicate.join()
        independent.join()
        self.assertEqual(pool._items, {})


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=30)
        self.breaker_patch = patch.object(resilience, "circuit_breaker", self.breaker)
        self.sleep_patch = patch.object(resilience.time, "sleep")
        self.breaker_patch.start()
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()
        self.breaker_patch.stop()

    def test_retries_before_success(self):
        attempts = []

        def operation():
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("temporary")
            return "ok"

        result = resilience.call_with_resilience("NSE", operation, retries=3)

        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 3)

    def test_opens_circuit_after_repeated_failed_operations(self):
        attempts = []

        def operation():
            attempts.append(1)
            raise ConnectionError("temporary")

        for _ in range(2):
            with self.assertRaises(UpstreamUnavailableError):
                resilience.call_with_resilience("Yahoo Finance", operation, retries=1)

        with self.assertRaises(UpstreamUnavailableError):
            resilience.call_with_resilience("Yahoo Finance", operation, retries=1)
        self.assertEqual(len(attempts), 2)


class ApiErrorSanitizationTests(unittest.TestCase):
    def setUp(self):
        web_app.report_cache = BoundedTTLCache(max_size=2, ttl_seconds=60)
        web_app.analysis_locks = KeyedLockPool()
        for state in web_app.dependency_health.values():
            state.update({
                "status": "unknown",
                "last_success_at": None,
                "last_failure_at": None,
                "error": None,
            })
        self.client = web_app.app.test_client()

    def test_analysis_and_health_hide_upstream_exception(self):
        secret = "https://provider.example/private?token=secret-value"
        with patch.object(web_app, "build_symbol_report", side_effect=RuntimeError(secret)):
            response = self.client.get("/api/analyze/TEST")

        self.assertEqual(response.status_code, 502)
        self.assertNotIn(secret, response.get_data(as_text=True))
        health_body = json.dumps(self.client.get("/health").get_json())
        self.assertNotIn(secret, health_body)
        self.assertNotIn("secret-value", health_body)

    def test_invalid_ticker_keeps_safe_validation_message(self):
        with patch.object(
            web_app,
            "build_symbol_report",
            side_effect=ValueError("Enter a valid NSE ticker symbol"),
        ):
            response = self.client.get("/api/analyze/INVALID_SYMBOL")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Enter a valid NSE ticker symbol")

    def test_setup_modes_use_separate_report_cache_entries(self):
        def report(symbol, mode):
            return {"symbol": symbol, "setup_mode": mode}

        with patch.object(web_app, "build_symbol_report", side_effect=report) as builder:
            bullish = self.client.get("/api/analyze/TEST?mode=bullish")
            bearish = self.client.get("/api/analyze/TEST?mode=bearish")
            cached_bullish = self.client.get("/api/analyze/TEST?mode=bullish")

        self.assertEqual(bullish.get_json()["setup_mode"], "bullish")
        self.assertEqual(bearish.get_json()["setup_mode"], "bearish")
        self.assertTrue(cached_bullish.get_json()["cached"])
        self.assertEqual(builder.call_count, 2)

    def test_different_symbols_are_analyzed_concurrently(self):
        both_entered = threading.Event()
        entered = []
        entered_lock = threading.Lock()
        overlapped = []
        responses = []

        def report(symbol, mode):
            with entered_lock:
                entered.append(symbol)
                if len(entered) == 2:
                    both_entered.set()
            overlapped.append(both_entered.wait(timeout=0.2))
            return {"symbol": symbol, "setup_mode": mode}

        def request_report(symbol):
            with web_app.app.test_client() as client:
                responses.append(client.get(f"/api/analyze/{symbol}"))

        with patch.object(web_app, "build_symbol_report", side_effect=report):
            first = threading.Thread(target=request_report, args=("TCS",))
            second = threading.Thread(target=request_report, args=("INFY",))
            first.start()
            second.start()
            first.join()
            second.join()

        self.assertTrue(both_entered.is_set())
        self.assertCountEqual(entered, ["TCS", "INFY"])
        self.assertEqual(overlapped, [True, True])
        self.assertTrue(all(response.status_code == 200 for response in responses))

    def test_duplicate_report_requests_share_one_build(self):
        build_started = threading.Event()
        release_build = threading.Event()
        responses = []

        def report(symbol, mode):
            build_started.set()
            release_build.wait(timeout=1)
            return {"symbol": symbol, "setup_mode": mode}

        def request_report():
            with web_app.app.test_client() as client:
                responses.append(client.get("/api/analyze/TCS"))

        with patch.object(web_app, "build_symbol_report", side_effect=report) as builder:
            first = threading.Thread(target=request_report)
            second = threading.Thread(target=request_report)
            first.start()
            self.assertTrue(build_started.wait(timeout=1))
            second.start()
            release_build.set()
            first.join()
            second.join()

        self.assertEqual(builder.call_count, 1)
        self.assertEqual(sorted(response.get_json()["cached"] for response in responses), [False, True])


class AnalysisSourceCacheTests(unittest.TestCase):
    def setUp(self):
        fno_trade_analyzer.cash_history_cache = BoundedTTLCache(8, 60)
        fno_trade_analyzer.fno_frames_cache = BoundedTTLCache(8, 60)
        fno_trade_analyzer.ban_list_cache = BoundedTTLCache(8, 60)
        fno_trade_analyzer.fundamentals_cache = BoundedTTLCache(8, 60)
        fno_trade_analyzer.company_news_cache = BoundedTTLCache(8, 60)
        fno_trade_analyzer.global_cues_cache = BoundedTTLCache(8, 60)
        fno_trade_analyzer.source_locks = KeyedLockPool()

    def test_each_source_is_cached_by_its_data_identity(self):
        latest_date = date(2026, 8, 28)
        history = pd.DataFrame([{"TradDt": latest_date.isoformat()}])
        frames = [pd.DataFrame([{"TradDt": latest_date.isoformat()}])]
        snapshot = {
            "banned_symbols": {"TCS"},
            "trade_date": latest_date.isoformat(),
            "note": "Current ban list.",
        }

        with (
            patch.object(fno_trade_analyzer, "load_recent_symbol_history", return_value=history) as cash_loader,
            patch.object(fno_trade_analyzer, "load_recent_fno_frames", return_value=frames) as fno_loader,
            patch.object(fno_trade_analyzer, "fetch_fno_ban_snapshot", return_value=snapshot) as ban_loader,
            patch.object(fno_trade_analyzer, "fetch_screener_data", return_value={"name": "TCS"}) as fundamentals_loader,
            patch.object(fno_trade_analyzer, "fetch_company_news", return_value={"articles": []}) as news_loader,
            patch.object(fno_trade_analyzer, "fetch_global_cues", return_value={"spx_change_pct": 1}) as cues_loader,
        ):
            for _ in range(2):
                fno_trade_analyzer.load_cached_symbol_history("TCS")
                fno_trade_analyzer.load_cached_fno_frames(latest_date)
                fno_trade_analyzer.load_cached_ban_status("TCS", latest_date)
                fno_trade_analyzer.load_cached_fundamentals("TCS")
                fno_trade_analyzer.load_cached_company_news("TCS")
                fno_trade_analyzer.load_cached_global_cues()

        for loader in (
            cash_loader,
            fno_loader,
            ban_loader,
            fundamentals_loader,
            news_loader,
            cues_loader,
        ):
            loader.assert_called_once()

    def test_fundamental_failure_is_isolated_from_report(self):
        with patch.object(
            fno_trade_analyzer,
            "load_cached_fundamentals",
            side_effect=RuntimeError("unsupported fundamentals"),
        ):
            result = fno_trade_analyzer.load_fundamental_analysis("SILVERAXIS")

        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["metrics"])
        self.assertIsNone(result["assessment"])

    def test_report_loads_independent_sources_concurrently(self):
        latest_date = date(2026, 8, 28)
        history = pd.DataFrame([{"TradDt": latest_date.isoformat()}])
        all_started = threading.Barrier(5)

        def concurrent_result(value):
            all_started.wait(timeout=1)
            return value

        with (
            patch.object(fno_trade_analyzer, "load_cached_symbol_history", return_value=history),
            patch.object(fno_trade_analyzer, "load_cached_fno_frames", side_effect=lambda *_: concurrent_result([])),
            patch.object(fno_trade_analyzer, "load_cached_ban_status", side_effect=lambda *_: concurrent_result({"is_banned": False})),
            patch.object(fno_trade_analyzer, "load_cached_fundamentals", side_effect=lambda *_: concurrent_result({})),
            patch.object(fno_trade_analyzer, "load_cached_company_news", side_effect=lambda *_: concurrent_result({"event_risk": {}})),
            patch.object(fno_trade_analyzer, "load_cached_global_cues", side_effect=lambda: concurrent_result({})),
            patch.object(fno_trade_analyzer, "analyze_cash", return_value={}),
            patch.object(fno_trade_analyzer, "analyze_fno", return_value=None),
            patch.object(fno_trade_analyzer, "calculate_fundamental_score", return_value={"score": None, "tags": []}),
            patch.object(fno_trade_analyzer, "score_detailed_report", return_value={"score": 50, "calculation": {}}),
            patch.object(fno_trade_analyzer, "build_pros_cons", return_value=([], [])),
            patch.object(fno_trade_analyzer, "build_trade_plan", return_value=None),
        ):
            report = fno_trade_analyzer.build_symbol_report("TCS")

        self.assertEqual(report["symbol"], "TCS")
        self.assertEqual(report["data_through"], latest_date.isoformat())
        self.assertFalse(report["fundamentals_available"])
        self.assertEqual(report["fundamentals_status"], "insufficient_data")


class RefreshApiTests(unittest.TestCase):
    def setUp(self):
        self.client = web_app.app.test_client()
        web_app.last_refresh_started_at = 0.0
        web_app.refresh_state.update({
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "error": None,
        })

    def test_starts_one_background_refresh(self):
        with patch.object(web_app.threading, "Thread") as thread:
            started = self.client.post("/api/refresh")
            duplicate = self.client.post("/api/refresh")

        self.assertEqual(started.status_code, 202)
        self.assertEqual(started.get_json()["status"], "running")
        self.assertEqual(thread.call_args.kwargs["target"], web_app.run_scanner_refresh)
        self.assertTrue(thread.call_args.kwargs["daemon"])
        thread.return_value.start.assert_called_once_with()
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.get_json()["message"], "A scanner refresh is already running.")

    def test_returns_current_refresh_status(self):
        web_app.refresh_state.update({
            "status": "succeeded",
            "started_at": "2026-08-28T10:00:00+00:00",
            "completed_at": "2026-08-28T10:05:00+00:00",
            "error": None,
        })

        response = self.client.get("/api/refresh")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), web_app.refresh_state)

    def test_refresh_worker_generates_data_and_marks_success(self):
        with patch.object(web_app.scanner_generator, "main") as generate:
            web_app.run_scanner_refresh()

        generate.assert_called_once_with()
        self.assertEqual(web_app.refresh_state["status"], "succeeded")
        self.assertIsNotNone(web_app.refresh_state["completed_at"])
        self.assertIsNone(web_app.refresh_state["error"])

    def test_rate_limits_recent_refresh(self):
        web_app.last_refresh_started_at = time.time()

        response = self.client.post("/api/refresh")

        self.assertEqual(response.status_code, 429)
        self.assertGreater(response.get_json()["retry_after_seconds"], 0)

    def test_refresh_failure_is_sanitized(self):
        secret = "https://provider.example/private?token=secret-value"
        with patch.object(web_app.scanner_generator, "main", side_effect=RuntimeError(secret)):
            web_app.run_scanner_refresh()

        response = self.client.get("/api/refresh")
        self.assertEqual(response.get_json()["status"], "failed")
        self.assertNotIn(secret, response.get_data(as_text=True))


class ScannerDataApiTests(unittest.TestCase):
    def setUp(self):
        self.client = web_app.app.test_client()
        self.original_static_folder = web_app.app.static_folder
        self.temporary_directory = tempfile.TemporaryDirectory()
        web_app.app.static_folder = self.temporary_directory.name

    def tearDown(self):
        web_app.app.static_folder = self.original_static_folder
        self.temporary_directory.cleanup()

    def test_returns_latest_scanner_snapshot_without_caching(self):
        data_directory = Path(self.temporary_directory.name) / "data"
        data_directory.mkdir()
        snapshot = {"generated_at_ist": "2026-08-28 15:45:00", "top_3": []}
        (data_directory / "latest.json").write_text(json.dumps(snapshot), encoding="utf-8")

        response = self.client.get("/api/scanner-data")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), snapshot)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response.close()

    def test_returns_service_unavailable_when_snapshot_is_missing(self):
        response = self.client.get("/api/scanner-data")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "Scanner data is not available yet."})


class EconomicTimesNewsApiTests(unittest.TestCase):
    def setUp(self):
        self.client = web_app.app.test_client()
        web_app.economic_times_cache = BoundedTTLCache(max_size=4, ttl_seconds=60)

    @patch.object(web_app, "fetch_economic_times_news")
    def test_returns_and_caches_news_for_valid_query(self, fetch_news):
        fetch_news.return_value = {
            "query": "TCS",
            "articles": [{"title": "TCS reports quarterly results"}],
            "errors": [],
            "lookback_days": 3,
            "sources": "The Economic Times",
            "topic_url": "https://economictimes.indiatimes.com/topic/tcs",
        }

        first = self.client.get("/api/news/economic-times?q=TCS&days=3&limit=5")
        second = self.client.get("/api/news/economic-times?q=TCS&days=3&limit=5")

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.get_json()["cached"])
        self.assertTrue(second.get_json()["cached"])
        fetch_news.assert_called_once_with("TCS", days=3, limit=5)

    def test_rejects_invalid_query_parameters(self):
        cases = (
            "/api/news/economic-times",
            "/api/news/economic-times?q=TCS&days=abc",
            "/api/news/economic-times?q=TCS&days=31",
            "/api/news/economic-times?q=TCS&limit=51",
        )

        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 400)

    @patch.object(web_app, "fetch_economic_times_news")
    def test_returns_sanitized_bad_gateway_when_source_fails(self, fetch_news):
        fetch_news.return_value = {
            "query": "TCS",
            "articles": [],
            "errors": ["The Economic Times news is temporarily unavailable."],
            "lookback_days": 7,
            "sources": "The Economic Times",
            "topic_url": "https://economictimes.indiatimes.com/topic/tcs",
        }

        response = self.client.get(
            "/api/news/economic-times?q=TCS",
            headers={"Origin": "https://example-client.test"},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(
            response.get_json()["errors"],
            ["The Economic Times news is temporarily unavailable."],
        )

    @patch.object(web_app, "fetch_economic_times_news")
    def test_allows_browser_calls_from_any_origin(self, fetch_news):
        fetch_news.return_value = {
            "query": "banking",
            "articles": [],
            "errors": [],
            "lookback_days": 7,
            "sources": "The Economic Times",
            "topic_url": "https://economictimes.indiatimes.com/topic/banking",
        }

        response = self.client.get(
            "/api/news/economic-times?q=banking",
            headers={"Origin": "https://example-client.test"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")


if __name__ == "__main__":
    unittest.main()
