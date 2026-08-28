import email.utils
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

import feedparser
import requests

from resilience import UpstreamUnavailableError, call_with_resilience


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SwingScanner/1.0)"}
EDITIONS = (
    {"scope": "Local", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
    {"scope": "International", "hl": "en-US", "gl": "US", "ceid": "US:en"},
)
EVENT_KEYWORDS = {
    "earnings": ("earnings", "results", "profit", "revenue", "guidance"),
    "dividend": ("dividend", "ex-dividend", "record date"),
    "corporate_action": (
        "bonus issue", "stock split", "share split", "buyback", "rights issue",
        "merger", "acquisition", "demerger", "open offer",
    ),
    "material_event": (
        "fraud", "default", "insolvency", "bankruptcy", "regulator", "sebi",
        "order win", "contract win", "promoter stake", "management change",
    ),
}


def classify_news_article(article, symbol, company_name=None, now=None):
    title = article["title"].casefold()
    categories = [
        category
        for category, keywords in EVENT_KEYWORDS.items()
        if any(keyword in title for keyword in keywords)
    ]
    symbol_match = symbol.casefold() in title
    company_tokens = [
        token for token in (company_name or "").casefold().split()
        if len(token) >= 4
    ]
    company_match = any(token in title for token in company_tokens)
    relevance_score = 2 + (3 if symbol_match else 0) + (2 if company_match else 0)
    materiality_score = min(10, relevance_score + len(categories) * 3)
    enriched = {
        **article,
        "relevance_score": relevance_score,
        "materiality_score": materiality_score,
        "materiality": "high" if materiality_score >= 8 else "medium" if materiality_score >= 5 else "low",
        "event_categories": categories,
        "potentially_material": materiality_score >= 5 and bool(categories),
    }
    return enriched


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
        if not title or urlparse(link).scheme != "https":
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
            def request_news():
                response = requests.get(
                    f"{GOOGLE_NEWS_RSS}?{params}", headers=HEADERS, timeout=15
                )
                response.raise_for_status()
                return response

            response = call_with_resilience("Google News", request_news)
            articles.extend(
                parse_news_feed(response.content, edition["scope"], cutoff)
            )
        except (requests.RequestException, UpstreamUnavailableError, ValueError):
            errors.append(f"{edition['scope']} news is temporarily unavailable.")

    unique = {}
    for article in articles:
        key = article["title"].casefold()
        if key not in unique:
            unique[key] = article
        elif unique[key]["scope"] != article["scope"]:
            unique[key]["scope"] = "Local & International"

    classified = [
        classify_news_article(article, symbol, company_name)
        for article in unique.values()
    ]
    ranked = sorted(
        classified,
        key=lambda article: (
            article["materiality_score"],
            article["relevance_score"],
            article["published_at"] or "",
        ),
        reverse=True,
    )[:limit]
    material_articles = [article for article in ranked if article["potentially_material"]]
    categories = sorted({
        category
        for article in material_articles
        for category in article["event_categories"]
    })
    return {
        "articles": ranked,
        "event_risk": {
            "status": "unavailable" if errors and not ranked else "detected" if material_articles else "clear",
            "detected": bool(material_articles),
            "categories": categories,
            "headline_count": len(material_articles),
        },
        "errors": errors,
        "lookback_days": days,
        "sources": "Google News RSS (India and US editions)",
    }