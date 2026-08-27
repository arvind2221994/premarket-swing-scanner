import os
import threading
import time
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
    return jsonify({"status": "ok"})


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
        return jsonify({**report, "cached": False})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        app.logger.exception("Analysis failed for %s", clean_symbol)
        return jsonify({"error": f"Analysis failed for {clean_symbol}: {error}"}), 502


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)