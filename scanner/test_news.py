import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import news
from resilience import UpstreamUnavailableError


class CompanyNewsFallbackTests(unittest.TestCase):
    def test_empty_feed_falls_back_without_retrying_parse_failure(self):
        empty_response = Mock(content=b"<rss><channel></channel></rss>")
        empty_response.raise_for_status.return_value = None
        resilience_calls = []

        def resilient_call(source, operation):
            resilience_calls.append(source)
            return operation()

        with (
            patch.object(news, "call_with_resilience", side_effect=resilient_call),
            patch.object(news.requests, "get", return_value=empty_response),
        ):
            result = news.fetch_company_news("QUIET")

        self.assertEqual(len(resilience_calls), 4)
        self.assertEqual(result["articles"], [])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["event_risk"]["status"], "clear")
        self.assertEqual(result["sources"], "")
        self.assertEqual(result["provider_status"], {
            "google_news": "healthy",
            "bing_news": "healthy",
        })

    def test_bing_ignores_undated_articles(self):
        feed = b"""<rss><channel><item>
            <title>Old undated material event</title>
            <link>https://example.com/old-event</link>
        </item></channel></rss>"""

        articles = news.parse_news_feed(
            feed,
            "International",
            datetime.now(timezone.utc),
            require_published=True,
        )

        self.assertEqual(articles, [])

    def test_uses_bing_rss_when_google_news_is_unavailable(self):
        published = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        fallback_feed = f"""<?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0"><channel><item>
                <title>TCS wins material contract</title>
                <link>https://example.com/tcs-contract</link>
                <pubDate>{published}</pubDate>
                <source>Bing Publisher</source>
            </item></channel></rss>"""
        fallback_response = Mock(content=fallback_feed.encode("utf-8"))
        fallback_response.raise_for_status.return_value = None

        def resilient_call(source, operation):
            if source.startswith("Google News"):
                raise UpstreamUnavailableError(source)
            return operation()

        with (
            patch.object(news, "call_with_resilience", side_effect=resilient_call),
            patch.object(news.requests, "get", return_value=fallback_response) as request,
        ):
            result = news.fetch_company_news("TCS", "Tata Consultancy Services")

        self.assertTrue(result["articles"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["sources"], "Bing News RSS")
        self.assertTrue(all("bing.com/news/search" in call.args[0] for call in request.call_args_list))


if __name__ == "__main__":
    unittest.main()