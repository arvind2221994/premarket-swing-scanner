import email.utils
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser
import requests


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SwingScanner/1.0)"}
EDITIONS = (
    {"scope": "Local", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
    {"scope": "International", "hl": "en-US", "gl": "US", "ceid": "US:en"},
)


def _published_at(entry):
    value = entry.get("published") or entry.get("updated")
    if not value:
        return None
    parsed = email.utils.parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _publisher(entry):
    source = entry.get("source") or {}
    return source.get("title") or "Unknown publisher"


def parse_news_feed(content, scope, cutoff):
    feed = feedparser.parse(content)
    articles = []
    for entry in feed.entries:
        published = _published_at(entry)
        if published is not None and published < cutoff:
            continue
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue
        articles.append({
            "title": title,
            "url": link,
            "publisher": _publisher(entry),
            "published_at": published.isoformat() if published is not None else None,
            "scope": scope,
        })
    return articles


def fetch_company_news(symbol, company_name=None, days=7, limit=20):
    terms = [f'"{symbol}" stock']
    if company_name and company_name.upper() != symbol.upper():
        terms.insert(0, f'"{company_name}"')
    query = f"({' OR '.join(terms)}) when:{days}d"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    articles = []
    errors = []

    for edition in EDITIONS:
        params = (
            f"q={quote_plus(query)}&hl={edition['hl']}"
            f"&gl={edition['gl']}&ceid={edition['ceid']}"
        )
        try:
            response = requests.get(
                f"{GOOGLE_NEWS_RSS}?{params}", headers=HEADERS, timeout=15
            )
            response.raise_for_status()
            articles.extend(
                parse_news_feed(response.content, edition["scope"], cutoff)
            )
        except (requests.RequestException, ValueError) as error:
            errors.append(f"{edition['scope']} news unavailable: {error}")

    unique = {}
    for article in articles:
        key = article["title"].casefold()
        if key not in unique:
            unique[key] = article
        elif unique[key]["scope"] != article["scope"]:
            unique[key]["scope"] = "Local & International"

    ranked = sorted(
        unique.values(),
        key=lambda article: article["published_at"] or "",
        reverse=True,
    )[:limit]
    return {
        "articles": ranked,
        "errors": errors,
        "lookback_days": days,
        "sources": "Google News RSS (India and US editions)",
    }