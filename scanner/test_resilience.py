import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import resilience
import web_app
from resilience import BoundedTTLCache, CircuitBreaker, UpstreamUnavailableError


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


if __name__ == "__main__":
    unittest.main()
