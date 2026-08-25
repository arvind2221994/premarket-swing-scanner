import json
from datetime import datetime, timezone

import requests
import yfinance as yf

from global_cues import fetch_global_cues
from scoring import score_global_cues


NSE_ALL_INDICES_URL = "https://www.nseindia.com/api/allIndices"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.nseindia.com/market-data/live-market-indices",
}
SECTOR_TICKERS = {
    "Nifty Bank": "^NSEBANK",
    "Nifty IT": "^CNXIT",
    "Nifty Auto": "^CNXAUTO",
    "Nifty Pharma": "^CNXPHARMA",
    "Nifty FMCG": "^CNXFMCG",
    "Nifty Metal": "^CNXMETAL",
    "Nifty Energy": "^CNXENERGY",
    "Nifty Realty": "^CNXREALTY",
}


def classify_trend(close, sma20, sma50):
    if close is None or sma20 is None or sma50 is None:
        return "unavailable"
    if close > sma20 > sma50:
        return "bullish"
    if close < sma20 < sma50:
        return "bearish"
    return "mixed"


def fetch_yahoo_trend(ticker):
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        data = yf.download(
            ticker,
            period="6mo",
            interval="1d",
            progress=False,
            multi_level_index=False,
            auto_adjust=False,
        )
        closes = data["Close"].dropna() if data is not None else None
        if closes is None or len(closes) < 50:
            raise ValueError("Fewer than 50 daily closes returned")

        close = float(closes.iloc[-1])
        previous_close = float(closes.iloc[-2])
        sma20 = float(closes.tail(20).mean())
        sma50 = float(closes.tail(50).mean())
        return {
            "ticker": ticker,
            "close": round(close, 4),
            "daily_change_pct": round((close / previous_close - 1) * 100, 2),
            "sma20": round(sma20, 4),
            "sma50": round(sma50, 4),
            "trend": classify_trend(close, sma20, sma50),
            "observed_at": closes.index[-1].isoformat(),
            "fetched_at": fetched_at,
            "source": "Yahoo Finance",
        }
    except Exception as error:
        return {
            "ticker": ticker,
            "close": None,
            "daily_change_pct": None,
            "sma20": None,
            "sma50": None,
            "trend": "unavailable",
            "observed_at": None,
            "fetched_at": fetched_at,
            "source": "Yahoo Finance",
            "error": str(error),
        }


def _as_int(value):
    return int(str(value).replace(",", ""))


def fetch_market_breadth():
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        with requests.Session() as session:
            session.get("https://www.nseindia.com/", headers=NSE_HEADERS, timeout=10)
            response = session.get(
                NSE_ALL_INDICES_URL,
                headers=NSE_HEADERS,
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()

        rows = payload.get("data", [])
        selected = next(
            (row for row in rows if row.get("index") == "NIFTY 500"),
            None,
        ) or next(
            (row for row in rows if row.get("index") == "NIFTY 50"),
            None,
        )
        if selected is None:
            raise ValueError("NIFTY 500 and NIFTY 50 breadth were not returned")

        advances = _as_int(selected["advances"])
        declines = _as_int(selected["declines"])
        unchanged = _as_int(selected.get("unchanged", 0))
        decided = advances + declines
        ratio = advances / declines if declines > 0 else None
        return {
            "universe": selected["index"],
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "advance_decline_ratio": round(ratio, 2) if ratio is not None else None,
            "advance_pct": round(advances / decided * 100, 1) if decided else None,
            "observed_at": selected.get("lastUpdateTime") or payload.get("timestamp"),
            "fetched_at": fetched_at,
            "source": "NSE India allIndices",
        }
    except Exception as error:
        return {
            "universe": None,
            "advances": None,
            "declines": None,
            "unchanged": None,
            "advance_decline_ratio": None,
            "advance_pct": None,
            "observed_at": None,
            "fetched_at": fetched_at,
            "source": "NSE India allIndices",
            "error": str(error),
        }


def build_market_context():
    global_cues = fetch_global_cues()
    global_score, global_reasons = score_global_cues(global_cues)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nifty_50": fetch_yahoo_trend("^NSEI"),
        "india_vix": fetch_yahoo_trend("^INDIAVIX"),
        "market_breadth": fetch_market_breadth(),
        "sector_indices": {
            name: fetch_yahoo_trend(ticker)
            for name, ticker in SECTOR_TICKERS.items()
        },
        "macro": {
            "usd_inr": fetch_yahoo_trend("INR=X"),
            "wti_crude": fetch_yahoo_trend("CL=F"),
        },
        "global_cues": {
            **global_cues,
            "score": global_score,
            "reasons": global_reasons,
        },
        "interpretation_note": (
            "USD/INR and crude are context signals only; apply them when the stock or "
            "sector has material currency, import-cost, or commodity-price exposure."
        ),
    }


def main():
    print(json.dumps(build_market_context(), indent=2))


if __name__ == "__main__":
    main()