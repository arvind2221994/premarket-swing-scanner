import csv
import io
from datetime import date, datetime, timedelta, timezone

import requests
import pandas as pd

from resilience import UpstreamUnavailableError, call_with_resilience


NSE_INDEX_ARCHIVE_URL = (
    "https://archives.nseindia.com/content/indices/ind_close_all_{trade_date}.csv"
)
NSE_SECTOR_INDEX_NAMES = {
    "Nifty Bank": "NIFTY BANK",
    "Nifty IT": "NIFTY IT",
    "Nifty Auto": "NIFTY AUTO",
    "Nifty Pharma": "NIFTY PHARMA",
    "Nifty FMCG": "NIFTY FMCG",
    "Nifty Metal": "NIFTY METAL",
    "Nifty Energy": "NIFTY ENERGY",
    "Nifty Realty": "NIFTY REALTY",
}
NSE_MARKET_INDEX_NAMES = {
    "^NSEI": "NIFTY 50",
    "^INDIAVIX": "INDIA VIX",
}
STOOQ_SYMBOLS = {
    "^IXIC": "^NDQ",
    "^GSPC": "^SPX",
    "^DJI": "^DJI",
    "CL=F": "CL.F",
}
STOOQ_HISTORY_URL = "https://stooq.com/q/d/l/"
FRANKFURTER_HISTORY_URL = "https://api.frankfurter.app/{start}..{end}"
NSE_EQUITY_HISTORY_URL = "https://www.nseindia.com/api/historical/cm/equity"
NSE_INDEX_HISTORY_URL = "https://www.nseindia.com/api/historical/indicesHistory"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/csv,*/*",
    "Referer": "https://www.nseindia.com/market-data/live-equity-market",
}


def _normalize_column(value):
    return " ".join(
        str(value or "").replace("\ufeff", "").replace("_", " ").strip().split()
    ).casefold()


def _parse_archive_rows(content, requested_indices, trade_date):
    rows = {}
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        normalized = {_normalize_column(key): value for key, value in row.items()}
        index_name = str(normalized.get("index name", "")).strip().upper()
        if index_name not in requested_indices:
            continue
        close_text = str(normalized.get("closing index value", "")).replace(",", "").strip()
        if close_text:
            rows[index_name] = (trade_date, float(close_text))
    return rows


def _trend_snapshot(index_name, observations, fetched_at, source="NSE India index archives"):
    if len(observations) < 50:
        return {
            "ticker": index_name,
            "close": None,
            "daily_change_pct": None,
            "return_5d_pct": None,
            "sma20": None,
            "sma50": None,
            "trend": "unavailable",
            "observed_at": None,
            "fetched_at": fetched_at,
            "source": source,
            "error": f"Fewer than 50 {source} closes were available.",
        }

    ordered = sorted(observations, key=lambda item: item[0])
    closes = [close for _, close in ordered]
    close = closes[-1]
    previous_close = closes[-2]
    five_session_close = closes[-6]
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    trend = "bullish" if close > sma20 > sma50 else "bearish" if close < sma20 < sma50 else "mixed"
    return {
        "ticker": index_name,
        "close": round(close, 4),
        "daily_change_pct": round((close / previous_close - 1) * 100, 2),
        "return_5d_pct": round((close / five_session_close - 1) * 100, 2),
        "sma20": round(sma20, 4),
        "sma50": round(sma50, 4),
        "trend": trend,
        "observed_at": ordered[-1][0].isoformat(),
        "fetched_at": fetched_at,
        "source": source,
    }


def fetch_nse_index_trends(requested, sessions=50, lookback_days=100):
    if not requested:
        return {}

    fetched_at = datetime.now(timezone.utc).isoformat()
    observations = {index_name: [] for index_name in requested.values()}
    with requests.Session() as session:
        for days_back in range(lookback_days):
            if all(len(values) >= sessions for values in observations.values()):
                break
            trade_date = date.today() - timedelta(days=days_back)
            if trade_date.weekday() >= 5:
                continue

            def request_archive():
                response = session.get(
                    NSE_INDEX_ARCHIVE_URL.format(
                        trade_date=trade_date.strftime("%d%m%Y")
                    ),
                    headers=NSE_HEADERS,
                    timeout=10,
                )
                if response.status_code != 404:
                    response.raise_for_status()
                return response

            try:
                response = call_with_resilience("NSE index archives", request_archive, retries=2)
            except UpstreamUnavailableError:
                continue
            if response.status_code == 404:
                continue

            archive_rows = _parse_archive_rows(
                response.text, set(observations), trade_date
            )
            for index_name, observation in archive_rows.items():
                if len(observations[index_name]) < sessions:
                    observations[index_name].append(observation)

    return {
        sector: _trend_snapshot(index_name, observations[index_name], fetched_at)
        for sector, index_name in requested.items()
    }


def fetch_nse_sector_trends(sector_names, sessions=50, lookback_days=100):
    requested = {
        sector: NSE_SECTOR_INDEX_NAMES[sector]
        for sector in sector_names
        if sector in NSE_SECTOR_INDEX_NAMES
    }
    return fetch_nse_index_trends(requested, sessions, lookback_days)


def fetch_nse_market_trends(tickers, sessions=50, lookback_days=100):
    requested = {
        ticker: NSE_MARKET_INDEX_NAMES[ticker]
        for ticker in tickers
        if ticker in NSE_MARKET_INDEX_NAMES
    }
    return fetch_nse_index_trends(requested, sessions, lookback_days)


def fetch_stooq_history(ticker, start=None, end=None):
    symbol = STOOQ_SYMBOLS.get(ticker)
    if symbol is None:
        return pd.DataFrame()

    params = {"s": symbol.lower(), "i": "d"}
    if start:
        params["d1"] = str(start).replace("-", "")
    if end:
        params["d2"] = str(end).replace("-", "")

    def request_history():
        response = requests.get(
            STOOQ_HISTORY_URL,
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        response.raise_for_status()
        if "No data" in response.text:
            raise ValueError("Stooq returned no data")
        return response

    try:
        response = call_with_resilience("Stooq", request_history, retries=2)
        data = pd.read_csv(io.StringIO(response.text), parse_dates=["Date"])
        data = data.set_index("Date").sort_index()
        required = {"Open", "High", "Low", "Close"}
        if data.empty or not required.issubset(data.columns):
            return pd.DataFrame()
        if "Volume" not in data:
            data["Volume"] = 0
        return data
    except (UpstreamUnavailableError, KeyError, TypeError, ValueError, pd.errors.ParserError):
        return pd.DataFrame()


def fetch_stooq_change_snapshot(ticker):
    fetched_at = datetime.now(timezone.utc).isoformat()
    end = date.today() + timedelta(days=1)
    data = fetch_stooq_history(ticker, end - timedelta(days=14), end)
    if len(data) < 2:
        return {
            "change_pct": None,
            "observed_at": None,
            "fetched_at": fetched_at,
            "source": "Stooq",
            "error": "Stooq market data is temporarily unavailable.",
        }
    previous_close = float(data["Close"].iloc[-2])
    latest_close = float(data["Close"].iloc[-1])
    return {
        "change_pct": round((latest_close / previous_close - 1) * 100, 2),
        "observed_at": data.index[-1].isoformat(),
        "fetched_at": fetched_at,
        "source": "Stooq",
    }


def fetch_stooq_trend(ticker):
    fetched_at = datetime.now(timezone.utc).isoformat()
    end = date.today() + timedelta(days=1)
    data = fetch_stooq_history(ticker, end - timedelta(days=100), end)
    if data.empty or "Close" not in data:
        return _trend_snapshot(ticker, [], fetched_at, "Stooq")
    observations = [(index.date(), float(close)) for index, close in data["Close"].items()]
    return _trend_snapshot(ticker, observations, fetched_at, "Stooq")


def fetch_frankfurter_usdinr_trend(lookback_days=100):
    fetched_at = datetime.now(timezone.utc).isoformat()
    end = date.today()
    start = end - timedelta(days=lookback_days)

    def request_history():
        response = requests.get(
            FRANKFURTER_HISTORY_URL.format(start=start.isoformat(), end=end.isoformat()),
            params={"from": "USD", "to": "INR"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        response.raise_for_status()
        return response

    try:
        response = call_with_resilience("Frankfurter", request_history, retries=2)
        rates = response.json().get("rates", {})
        observations = [
            (date.fromisoformat(day), float(values["INR"]))
            for day, values in rates.items()
            if values.get("INR") is not None
        ]
    except (UpstreamUnavailableError, KeyError, TypeError, ValueError):
        observations = []
    return _trend_snapshot("USD/INR", observations, fetched_at, "Frankfurter")


def _history_frame(rows, index_history):
    normalized_rows = []
    for row in rows:
        normalized = {_normalize_column(key): value for key, value in row.items()}
        if index_history:
            values = {
                "Date": normalized.get("eod timestamp") or normalized.get("historical date"),
                "Open": normalized.get("eod open index val") or normalized.get("open"),
                "High": normalized.get("eod high index val") or normalized.get("high"),
                "Low": normalized.get("eod low index val") or normalized.get("low"),
                "Close": normalized.get("eod close index val") or normalized.get("close"),
                "Volume": normalized.get("eod traded quantity") or 0,
            }
        else:
            values = {
                "Date": normalized.get("ch timestamp") or normalized.get("date"),
                "Open": normalized.get("ch opening price") or normalized.get("open"),
                "High": normalized.get("ch trade high price") or normalized.get("high"),
                "Low": normalized.get("ch trade low price") or normalized.get("low"),
                "Close": normalized.get("ch closing price") or normalized.get("close"),
                "Volume": normalized.get("ch tot traded qty") or normalized.get("volume"),
            }
        if values["Date"] and all(values[name] is not None for name in ("Open", "High", "Low", "Close")):
            normalized_rows.append(values)

    if not normalized_rows:
        return pd.DataFrame()
    frame = pd.DataFrame(normalized_rows)
    frame["Date"] = pd.to_datetime(frame["Date"], dayfirst=True, errors="coerce")
    for column in ("Open", "High", "Low", "Close", "Volume"):
        frame[column] = pd.to_numeric(
            frame[column].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        )
    return frame.dropna(subset=["Date", "Open", "High", "Low", "Close"]).set_index("Date").sort_index()


def fetch_nse_history(ticker, start, end):
    index_history = ticker == "^NSEI"
    if not index_history and not ticker.endswith(".NS"):
        return pd.DataFrame()

    start_date = date.fromisoformat(str(start)[:10])
    end_date = date.fromisoformat(str(end)[:10]) - timedelta(days=1)
    if end_date < start_date:
        return pd.DataFrame()

    frames = []
    with requests.Session() as session:
        cursor = start_date
        while cursor <= end_date:
            chunk_end = min(cursor + timedelta(days=364), end_date)

            def request_history():
                session.get("https://www.nseindia.com/", headers=NSE_HEADERS, timeout=10)
                if index_history:
                    url = NSE_INDEX_HISTORY_URL
                    params = {
                        "indexType": "NIFTY 50",
                        "from": cursor.strftime("%d-%m-%Y"),
                        "to": chunk_end.strftime("%d-%m-%Y"),
                    }
                else:
                    url = NSE_EQUITY_HISTORY_URL
                    params = {
                        "symbol": ticker.removesuffix(".NS"),
                        "series": '["EQ"]',
                        "from": cursor.strftime("%d-%m-%Y"),
                        "to": chunk_end.strftime("%d-%m-%Y"),
                    }
                response = session.get(url, params=params, headers=NSE_HEADERS, timeout=15)
                response.raise_for_status()
                return response

            try:
                response = call_with_resilience("NSE historical data", request_history, retries=2)
                frame = _history_frame(response.json().get("data", []), index_history)
                if not frame.empty:
                    frames.append(frame)
            except (UpstreamUnavailableError, KeyError, TypeError, ValueError):
                pass
            cursor = chunk_end + timedelta(days=1)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).sort_index().loc[lambda frame: ~frame.index.duplicated(keep="last")]
