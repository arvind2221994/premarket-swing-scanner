import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

from fno_trade_analyzer import build_symbol_report


DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
CACHE_SECONDS = int(os.getenv("REPORT_CACHE_SECONDS", "3600"))
ALLOWED_ORIGINS = {
    "https://arvind2221994.github.io",
    "http://127.0.0.1:5000",
    "http://localhost:5000",
}

app = Flask(__name__, static_folder=str(DOCS_DIR), static_url_path="")
report_cache = {}
analysis_lock = threading.Lock()
health_lock = threading.Lock()
dependency_health = {
    name: {
        "status": "unknown",
        "last_success_at": None,
        "last_failure_at": None,
        "error": None,
    }
    for name in ("analysis", "nse", "screener", "google_news")
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
    message = str(error)
    record_dependency("analysis", "unhealthy", message)
    lowered = message.lower()
    if "screener.in" in lowered:
        record_dependency("screener", "unhealthy", message)
    elif "nsearchives.nseindia.com" in lowered:
        record_dependency("nse", "unhealthy", message)


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


@app.get("/api/analyze/<symbol>")
def analyze(symbol):
    clean_symbol = symbol.strip().upper()
    cached = report_cache.get(clean_symbol)
    now = time.time()
    if cached and now - cached["created_at"] < CACHE_SECONDS:
        return jsonify({**cached["report"], "cached": True})

    try:
        with analysis_lock:
            cached = report_cache.get(clean_symbol)
            if cached and now - cached["created_at"] < CACHE_SECONDS:
                return jsonify({**cached["report"], "cached": True})
            report = build_symbol_report(clean_symbol)
            report_cache[clean_symbol] = {"created_at": time.time(), "report": report}
            record_report_health(report)
        return jsonify({**report, "cached": False})
    except ValueError as error:
        if "Screener.in" in str(error):
            record_analysis_failure(error)
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        record_analysis_failure(error)
        app.logger.exception("Analysis failed for %s", clean_symbol)
        return jsonify({"error": f"Analysis failed for {clean_symbol}: {error}"}), 502


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)