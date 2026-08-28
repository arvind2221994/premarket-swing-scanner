import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from resilience import UpstreamUnavailableError, call_with_resilience


BASE_URL = "https://economictimes.indiatimes.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}
ARTICLE_PATH_PREFIXES = ("/markets/", "/industry/", "/news/", "/wealth/")


def build_topic_url(query):
    slug = re.sub(r"[^a-z0-9]+", "-", query.casefold()).strip("-")
    if not slug:
        raise ValueError("Enter a company, ticker, or market topic")
    return f"{BASE_URL}/topic/{slug}"


def _parse_datetime(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _valid_article_url(value):
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"economictimes.indiatimes.com", "m.economictimes.com"}
        and parsed.path.startswith(ARTICLE_PATH_PREFIXES)
    )


def _article(title, url, published_at=None):
    return {
        "title": " ".join(str(title).split()),
        "url": url,
        "publisher": "The Economic Times",
        "published_at": published_at.isoformat() if published_at else None,
        "scope": "India",
    }


def _json_ld_articles(soup):
    articles = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        pending = payload if isinstance(payload, list) else [payload]
        while pending:
            item = pending.pop()
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                pending.extend(graph)
            elements = item.get("itemListElement")
            if isinstance(elements, list):
                pending.extend(
                    element.get("item", element) if isinstance(element, dict) else element
                    for element in elements
                )
            title = item.get("headline") or item.get("name")
            url = item.get("url")
            if isinstance(url, dict):
                url = url.get("@id")
            url = urljoin(BASE_URL, str(url or ""))
            if title and _valid_article_url(url):
                articles.append(_article(title, url, _parse_datetime(item.get("datePublished"))))
    return articles


def parse_topic_page(content, cutoff=None, limit=20):
    soup = BeautifulSoup(content, "html.parser")
    articles = _json_ld_articles(soup)
    for anchor in soup.select("a[href]"):
        title = anchor.get("title") or anchor.get_text(" ", strip=True)
        url = urljoin(BASE_URL, anchor.get("href", ""))
        if len(title) >= 20 and _valid_article_url(url):
            articles.append(_article(title, url))

    unique = {}
    for article in articles:
        key = article["url"].split("?", 1)[0]
        published = _parse_datetime(article["published_at"])
        if cutoff and published and published < cutoff:
            continue
        if key not in unique or (article["published_at"] and not unique[key]["published_at"]):
            unique[key] = {**article, "url": key}

    return sorted(
        unique.values(),
        key=lambda article: article["published_at"] or "",
        reverse=True,
    )[:limit]


def fetch_economic_times_news(query, days=7, limit=20):
    if days < 1 or limit < 1:
        raise ValueError("days and limit must be positive")
    url = build_topic_url(query)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    def request_topic():
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        return response

    try:
        response = call_with_resilience("The Economic Times", request_topic)
        articles = parse_topic_page(response.text, cutoff=cutoff, limit=limit)
        errors = []
    except (requests.RequestException, UpstreamUnavailableError, ValueError):
        articles = []
        errors = ["The Economic Times news is temporarily unavailable."]

    return {
        "query": query,
        "articles": articles,
        "errors": errors,
        "lookback_days": days,
        "sources": "The Economic Times",
        "topic_url": url,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Economic Times headline metadata for a company or market topic."
    )
    parser.add_argument("query", help="Company, ticker, or market topic")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(fetch_economic_times_news(args.query, args.days, args.limit), indent=2))


if __name__ == "__main__":
    main()
