import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request
from werkzeug.exceptions import NotFound

import scanner as scanner_generator
from fno_trade_analyzer import build_symbol_report
from resilience import BoundedTTLCache, UpstreamUnavailableError, call_with_resilience


DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
CACHE_SECONDS = int(os.getenv("REPORT_CACHE_SECONDS", "3600"))
TICKER_SEARCH_CACHE_SECONDS = 86400
REPORT_CACHE_MAX_SIZE = int(os.getenv("REPORT_CACHE_MAX_SIZE", "128"))
TICKER_SEARCH_CACHE_MAX_SIZE = int(os.getenv("TICKER_SEARCH_CACHE_MAX_SIZE", "256"))
REFRESH_COOLDOWN_SECONDS = int(os.getenv("REFRESH_COOLDOWN_SECONDS", "900"))
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
CURATED_TICKERS = (
    {"symbol": "ARVSMART", "name": "Arvind SmartSpaces Limited"},
)
ALLOWED_ORIGINS = {
    "https://arvind2221994.github.io",
    "http://127.0.0.1:5000",
    "http://localhost:5000",
}

app = Flask(__name__, static_folder=str(DOCS_DIR), static_url_path="")
report_cache = BoundedTTLCache(REPORT_CACHE_MAX_SIZE, CACHE_SECONDS)
ticker_search_cache = BoundedTTLCache(
    TICKER_SEARCH_CACHE_MAX_SIZE,
    TICKER_SEARCH_CACHE_SECONDS,
)
analysis_lock = threading.Lock()
health_lock = threading.Lock()
refresh_lock = threading.Lock()
last_refresh_started_at = 0.0
refresh_state = {
    "status": "idle",
    "started_at": None,
    "completed_at": None,
    "error": None,
}
dependency_health = {
    name: {
        "status": "unknown",
        "last_success_at": None,
        "last_failure_at": None,
        "error": None,
    }
    for name in ("analysis", "nse", "screener", "yahoo", "google_news")
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def record_dependency(name, status, error=None, succeeded=None):
    observed_at = utc_now()
    with health_lock:
        state = dependency_health[name]
        state["status"] = status
        state["error"] = error
        if succeeded is True or (succeeded is None and status == "healthy"):
            state["last_success_at"] = observed_at
        if status != "healthy":
            state["last_failure_at"] = observed_at


def record_report_health(report):
    record_dependency("analysis", "healthy")
    record_dependency("nse", "healthy")
    record_dependency("screener", "healthy")

    news_errors = report.get("news", {}).get("errors", [])
    if news_errors:
        news_articles = report.get("news", {}).get("articles", [])
        record_dependency(
            "google_news",
            "degraded" if news_articles else "unhealthy",
            "; ".join(news_errors),
            succeeded=bool(news_articles),
        )
    else:
        record_dependency("google_news", "healthy")


def record_analysis_failure(error):
    message = "Analysis dependency is temporarily unavailable."
    record_dependency("analysis", "unhealthy", message)
    source = getattr(error, "source", "").casefold()
    if "screener" in source:
        record_dependency("screener", "unhealthy", "Screener is temporarily unavailable.")
    elif "nse" in source:
        record_dependency("nse", "unhealthy", "NSE is temporarily unavailable.")
    elif "yahoo" in source:
        record_dependency("yahoo", "unhealthy", "Yahoo Finance is temporarily unavailable.")


def run_scanner_refresh():
    try:
        scanner_generator.main()
    except Exception:
        app.logger.exception("Scanner refresh failed")
        with refresh_lock:
            refresh_state.update({
                "status": "failed",
                "completed_at": utc_now(),
                "error": "Scanner refresh failed. Please try again later.",
            })
    else:
        with refresh_lock:
            refresh_state.update({
                "status": "succeeded",
                "completed_at": utc_now(),
                "error": None,
            })


def normalize_search_text(value):
    return "".join(character for character in value.casefold() if character.isalnum())


def search_tickers(query):
    normalized_query = normalize_search_text(query)
    local_matches = [
        ticker for ticker in CURATED_TICKERS
        if normalized_query in normalize_search_text(ticker["symbol"])
        or normalized_query in normalize_search_text(ticker["name"])
    ]

    try:
        def request_tickers():
            response = requests.get(
                YAHOO_SEARCH_URL,
                params={"q": query, "quotesCount": 10, "newsCount": 0},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            response.raise_for_status()
            return response

        response = call_with_resilience(
            "Yahoo Finance",
            request_tickers,
        )
        record_dependency("yahoo", "healthy")
        remote_matches = []
        for quote in response.json().get("quotes", []):
            yahoo_symbol = quote.get("symbol", "")
            if quote.get("exchange") != "NSI" and not yahoo_symbol.endswith(".NS"):
                continue
            symbol = yahoo_symbol.removesuffix(".NS").upper()
            if not symbol:
                continue
            remote_matches.append({
                "symbol": symbol,
                "name": quote.get("longname") or quote.get("shortname") or symbol,
            })
    except (requests.RequestException, UpstreamUnavailableError, ValueError):
        record_dependency(
            "yahoo",
            "unhealthy",
            "Yahoo Finance is temporarily unavailable.",
        )
        remote_matches = []

    matches = []
    seen = set()
    for ticker in [*local_matches, *remote_matches]:
        if ticker["symbol"] not in seen:
            seen.add(ticker["symbol"])
            matches.append(ticker)
    return matches[:8]


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/health")
def health():
    with health_lock:
        dependencies = {
            name: dict(state) for name, state in dependency_health.items()
        }
    statuses = {state["status"] for state in dependencies.values()}
    status = "degraded" if statuses & {"degraded", "unhealthy"} else "ok"
    return jsonify({
        "status": status,
        "last_success_at": dependencies["analysis"]["last_success_at"],
        "dependencies": dependencies,
    })


@app.get("/api/tickers")
def ticker_suggestions():
    query = request.args.get("q", "").strip()
    if len(query) < 2 or len(query) > 80:
        return jsonify({"suggestions": []})

    cache_key = query.casefold()
    cached = ticker_search_cache.get(cache_key)
    if cached is not None:
        return jsonify({"suggestions": cached})

    suggestions = search_tickers(query)
    ticker_search_cache.set(cache_key, suggestions)
    return jsonify({"suggestions": suggestions})


@app.get("/api/scanner-data")
def scanner_data():
    try:
        response = app.send_static_file("data/latest.json")
        response.headers["Cache-Control"] = "no-store"
        return response
    except (FileNotFoundError, NotFound):
        return jsonify({"error": "Scanner data is not available yet."}), 503


@app.get("/api/refresh")
def refresh_status():
    with refresh_lock:
        return jsonify(dict(refresh_state))


@app.post("/api/refresh")
def refresh_scanner_data():
    global last_refresh_started_at

    now = time.time()
    with refresh_lock:
        if refresh_state["status"] == "running":
            return jsonify({**refresh_state, "message": "A scanner refresh is already running."}), 409

        retry_after = max(0, int(REFRESH_COOLDOWN_SECONDS - (now - last_refresh_started_at)))
        if retry_after:
            return jsonify({
                **refresh_state,
                "message": "Scanner data was refreshed recently.",
                "retry_after_seconds": retry_after,
            }), 429

        last_refresh_started_at = now
        refresh_state.update({
            "status": "running",
            "started_at": utc_now(),
            "completed_at": None,
            "error": None,
        })
        thread = threading.Thread(
            target=run_scanner_refresh,
            name="scanner-refresh",
            daemon=True,
        )
        thread.start()

    return jsonify(dict(refresh_state)), 202


@app.get("/api/analyze/<symbol>")
def analyze(symbol):
    clean_symbol = symbol.strip().upper()
    mode = request.args.get("mode", "bullish").strip().lower()
    if mode not in {"bullish", "bearish"}:
        return jsonify({"error": "Setup mode must be bullish or bearish."}), 400
    cache_key = (clean_symbol, mode)
    cached = report_cache.get(cache_key)
    if cached is not None:
        return jsonify({**cached, "cached": True})

    try:
        with analysis_lock:
            cached = report_cache.get(cache_key)
            if cached is not None:
                return jsonify({**cached, "cached": True})
            report = build_symbol_report(clean_symbol, mode)
            report_cache.set(cache_key, report)
            record_report_health(report)
        return jsonify({**report, "cached": False})
    except ValueError as error:
        message = str(error)
        if message == "Enter a valid NSE ticker symbol":
            return jsonify({"error": message}), 400
        record_analysis_failure(error)
        return jsonify({"error": "The requested NSE ticker could not be analyzed."}), 404
    except Exception as error:
        record_analysis_failure(error)
        app.logger.exception("Analysis failed for %s", clean_symbol)
        return jsonify({"error": "Analysis is temporarily unavailable. Please try again later."}), 502


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)