import json
import sys
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


if __name__ == "__main__":
    unittest.main()
