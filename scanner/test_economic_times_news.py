import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import economic_times_news
from resilience import UpstreamUnavailableError


class EconomicTimesNewsTests(unittest.TestCase):
    def test_builds_topic_url(self):
        self.assertEqual(
            economic_times_news.build_topic_url("Reliance Industries"),
            "https://economictimes.indiatimes.com/topic/reliance-industries",
        )

    def test_parses_json_ld_and_deduplicates_anchor(self):
        content = """
        <html><body>
          <script type="application/ld+json">
          {
            "@type": "ItemList",
            "itemListElement": [{
              "item": {
                "@type": "NewsArticle",
                "headline": "Reliance shares rise after quarterly results",
                "url": "https://economictimes.indiatimes.com/markets/stocks/news/reliance-results/articleshow/123.cms",
                "datePublished": "2026-08-27T10:30:00+05:30"
              }
            }]
          }
          </script>
          <a href="/markets/stocks/news/reliance-results/articleshow/123.cms">
            Reliance shares rise after quarterly results
          </a>
          <a href="https://example.com/markets/unrelated">External article must be ignored</a>
        </body></html>
        """

        articles = economic_times_news.parse_topic_page(
            content,
            cutoff=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["publisher"], "The Economic Times")
        self.assertEqual(articles[0]["scope"], "India")
        self.assertIsNotNone(articles[0]["published_at"])

    def test_uses_same_domain_anchor_when_structured_data_is_absent(self):
        content = """
        <a href="/industry/banking/finance/latest-banking-sector-development/articleshow/456.cms">
          Latest banking sector development affects major lenders
        </a>
        """

        articles = economic_times_news.parse_topic_page(content)

        self.assertEqual(len(articles), 1)
        self.assertTrue(articles[0]["url"].startswith(economic_times_news.BASE_URL))
        self.assertIsNone(articles[0]["published_at"])

    @patch("economic_times_news.parse_topic_page")
    @patch("economic_times_news.call_with_resilience")
    def test_fetch_returns_standalone_result_contract(self, request, parse):
        request.return_value = Mock(text="<html></html>")
        parse.return_value = [{"title": "Market headline"}]

        result = economic_times_news.fetch_economic_times_news("TCS", days=3, limit=5)

        self.assertEqual(result["articles"], [{"title": "Market headline"}])
        self.assertEqual(result["sources"], "The Economic Times")
        self.assertEqual(result["lookback_days"], 3)
        self.assertEqual(result["errors"], [])

    @patch(
        "economic_times_news.call_with_resilience",
        side_effect=UpstreamUnavailableError("The Economic Times"),
    )
    def test_fetch_sanitizes_upstream_failure(self, _):
        result = economic_times_news.fetch_economic_times_news("TCS")

        self.assertEqual(result["articles"], [])
        self.assertEqual(
            result["errors"],
            ["The Economic Times news is temporarily unavailable."],
        )


if __name__ == "__main__":
    unittest.main()
