from datetime import datetime, timezone

import requests
import yfinance as yf

from fallback import fetch_stooq_change_snapshot
from resilience import UpstreamUnavailableError, call_with_resilience


NSE_IX_MARKET_WATCH_URL = "https://www.nseix.com/api/streamer-market-watch/"


def get_change_snapshot(ticker):
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        def download_snapshot():
            data = yf.download(
                ticker,
                period="5d",
                interval="1d",
                progress=False,
                multi_level_index=False,
            )
            if data is None or len(data) < 2:
                raise ValueError("Insufficient Yahoo Finance history")
            return data

        data = call_with_resilience("Yahoo Finance", download_snapshot)

        prev_close = float(data["Close"].iloc[-2])
        latest_close = float(data["Close"].iloc[-1])

        return {
            "change_pct": round(((latest_close - prev_close) / prev_close) * 100, 2),
            "observed_at": data.index[-1].isoformat(),
            "fetched_at": fetched_at,
            "source": "Yahoo Finance",
        }
    except (UpstreamUnavailableError, KeyError, TypeError, ValueError, IndexError):
        unavailable = {
            "change_pct": None,
            "observed_at": None,
            "fetched_at": fetched_at,
            "source": "Yahoo Finance",
            "error": "Yahoo Finance data is temporarily unavailable.",
        }
        fallback = fetch_stooq_change_snapshot(ticker)
        if fallback.get("change_pct") is not None:
            return fallback
        unavailable["fallback_source"] = fallback.get("source")
        unavailable["fallback_error"] = fallback.get("error")
        return unavailable


def get_change_pct(ticker):
    return get_change_snapshot(ticker)["change_pct"]


def select_gift_nifty_quote(payload):
    contracts = []

    for market_watch in payload.get("MBP_data_Market_Watch", []):
        for quote in market_watch.get("token_data", []):
            if (
                quote.get("SYMBOL") == "NIFTY"
                and quote.get("INSTRUMENTTYPE") == "FUTIDX"
            ):
                contracts.append(quote)

    if not contracts:
        return None

    return max(contracts, key=lambda quote: float(quote.get("VOLUME", 0)))


def parse_gift_nifty_change_pct(payload):
    quote = select_gift_nifty_quote(payload)
    return round(float(quote["PERCHANGE"]), 2) if quote is not None else None


def get_gift_nifty_snapshot():
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        def request_gift_nifty():
            response = requests.get(
                NSE_IX_MARKET_WATCH_URL,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseix.com/"},
                timeout=10,
            )
            response.raise_for_status()
            return response

        response = call_with_resilience(
            "NSE",
            request_gift_nifty,
        )
        quote = select_gift_nifty_quote(response.json())
        return {
            "change_pct": (
                round(float(quote["PERCHANGE"]), 2) if quote is not None else None
            ),
            "observed_at": quote.get("LTT") if quote is not None else None,
            "fetched_at": fetched_at,
            "source": "NSE International Exchange",
        }
    except (requests.RequestException, UpstreamUnavailableError, KeyError, TypeError, ValueError):
        return {
            "change_pct": None,
            "observed_at": None,
            "fetched_at": fetched_at,
            "source": "NSE International Exchange",
            "error": "NSE market data is temporarily unavailable.",
        }


def get_gift_nifty_change_pct():
    return get_gift_nifty_snapshot()["change_pct"]


def fetch_global_cues():
    snapshots = {
        "nasdaq": get_change_snapshot("^IXIC"),
        "spx": get_change_snapshot("^GSPC"),
        "dow": get_change_snapshot("^DJI"),
        "gift_nifty": get_gift_nifty_snapshot(),
    }
    return {
        "nasdaq_change_pct": snapshots["nasdaq"]["change_pct"],
        "spx_change_pct": snapshots["spx"]["change_pct"],
        "dow_change_pct": snapshots["dow"]["change_pct"],
        "gift_nifty_change_pct": snapshots["gift_nifty"]["change_pct"],
        "source_metadata": snapshots,
    }