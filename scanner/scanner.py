import gzip
import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz
import requests

from fno_trade_analyzer import (
    analyze_cash,
    analyze_fno,
    download_bhavcopy,
    fetch_fno_ban_snapshot,
    fno_ban_status_from_snapshot,
    load_cached_company_news,
    load_fundamental_analysis,
    load_recent_fno_frames,
)
from scoring import calculate_stock_score
from global_cues import fetch_global_cues
from market_context import add_sector_relative_strength, build_market_context
from backtest_score_buckets import run_backtest
from resilience import UpstreamUnavailableError


DEFAULT_SYMBOLS = ("RELIANCE", "ICICIBANK", "TCS")
DEFAULT_UNIVERSE_SIZE = 15
LATEST_DATA_PATH = Path(__file__).resolve().parent.parent / "docs" / "data" / "latest.json"
CASH_HISTORY_CACHE_PATH = LATEST_DATA_PATH.with_name("cash_history.json.gz")
MIN_CASH_TURNOVER_CRORE = float(os.getenv("MIN_CASH_TURNOVER_CRORE", "25"))
MIN_FUTURES_VOLUME = int(os.getenv("MIN_FUTURES_VOLUME", "250"))
ENRICHMENT_WORKERS = int(os.getenv("SCANNER_ENRICHMENT_WORKERS", "4"))
CASH_DOWNLOAD_WORKERS = int(os.getenv("CASH_DOWNLOAD_WORKERS", "3"))
CASH_SEED_MAX_SYMBOLS = int(os.getenv("CASH_SEED_MAX_SYMBOLS", "60"))
FUNDAMENTAL_SEED_SECONDS = min(
    int(os.getenv("FUNDAMENTALS_CACHE_SECONDS", "86400")),
    86400,
)
def sanitize_json_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json_value(item) for item in value]
    return value


def configured_symbols():
    value = os.getenv("SCANNER_SYMBOLS", "")
    return tuple(dict.fromkeys(symbol.strip().upper() for symbol in value.split(",") if symbol.strip()))


def load_previous_snapshot(path=LATEST_DATA_PATH):
    try:
        with Path(path).open(encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def load_cash_history_seed(path=CASH_HISTORY_CACHE_PATH):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as file:
            payload = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if payload.get("schema_version") != 1:
        return {}
    return {
        symbol: pd.DataFrame(rows)
        for symbol, rows in payload.get("histories", {}).items()
        if isinstance(rows, list) and rows
    }


def write_cash_history_seed(histories, path=CASH_HISTORY_CACHE_PATH):
    payload = {
        "schema_version": 1,
        "histories": {
            symbol: json.loads(frame.to_json(orient="records", date_format="iso"))
            for symbol, frame in sorted(histories.items())
        },
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    content = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    try:
        temporary_path.write_bytes(gzip.compress(content, mtime=0))
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def reusable_fundamentals(snapshot, now=None):
    if not isinstance(snapshot, dict):
        return {}
    now = now or datetime.now(pytz.utc)
    reusable = {}
    for row in snapshot.get("all_results", []):
        if not isinstance(row, dict) or not row.get("symbol"):
            continue
        updated_at = row.get("fundamentals_updated_at")
        try:
            observed_at = datetime.fromisoformat(updated_at)
        except (TypeError, ValueError):
            continue
        if observed_at.tzinfo is None:
            observed_at = pytz.utc.localize(observed_at)
        if (now - observed_at.astimezone(pytz.utc)).total_seconds() > FUNDAMENTAL_SEED_SECONDS:
            continue
        fundamental_score = row.get("fundamental_score_raw")
        if not isinstance(fundamental_score, (int, float)):
            continue
        reusable[row["symbol"]] = {
            "company_name": row.get("company_name") or row["symbol"],
            "fundamental_score": fundamental_score,
            "fundamentals_updated_at": updated_at,
        }
    return reusable


def reusable_market_inputs(snapshot):
    if not isinstance(snapshot, dict):
        return None, None
    data_as_of = snapshot.get("data_as_of")
    rows = snapshot.get("market_inputs")
    if not data_as_of or not isinstance(rows, list) or not rows:
        return None, None
    if not all(
        isinstance(row, dict)
        and row.get("symbol")
        and "liquidity_filter_pass" in row
        for row in rows
    ):
        return None, None
    return [dict(row) for row in rows], data_as_of


def market_input_snapshot(stocks):
    enrichment_keys = {
        "company_name",
        "event_risk",
        "event_risk_status",
        "event_categories",
        "fundamental_score",
        "fundamentals_updated_at",
    }
    return [
        {key: value for key, value in stock.items() if key not in enrichment_keys}
        for stock in stocks
    ]


def enrich_stocks(stocks, fundamental_seed=None):
    fundamental_seed = fundamental_seed or {}
    with ThreadPoolExecutor(max_workers=ENRICHMENT_WORKERS) as executor:
        news_futures = {
            stock["symbol"]: executor.submit(
                load_cached_company_news, stock["symbol"], 7, 8
            )
            for stock in stocks
        }
        fundamental_futures = {
            stock["symbol"]: executor.submit(load_fundamental_analysis, stock["symbol"])
            for stock in stocks
            if stock["symbol"] not in fundamental_seed
        }
        for stock in stocks:
            symbol = stock["symbol"]
            news = news_futures[symbol].result()
            seeded = fundamental_seed.get(symbol)
            if seeded is not None:
                company_name = seeded["company_name"]
                fundamental_score = seeded["fundamental_score"]
                fundamentals_updated_at = seeded["fundamentals_updated_at"]
            else:
                fundamental_analysis = fundamental_futures[symbol].result()
                fundamental_result = fundamental_analysis["assessment"]
                company_name = (
                    fundamental_analysis["metrics"].get("name")
                    if fundamental_analysis["metrics"] else symbol
                )
                fundamental_score = (
                    fundamental_result["score"] if fundamental_result else None
                )
                fundamentals_updated_at = datetime.now(pytz.utc).isoformat()
            stock.update({
                "company_name": company_name,
                "event_risk": news["event_risk"]["detected"],
                "event_risk_status": news["event_risk"]["status"],
                "event_categories": news["event_risk"]["categories"],
                "fundamental_score": fundamental_score,
                "fundamentals_updated_at": fundamentals_updated_at,
            })
    return stocks


def add_previous_session_changes(results_by_mode, previous_snapshot):
    previous_modes = (
        previous_snapshot.get("all_results_by_mode", {})
        if isinstance(previous_snapshot, dict) else {}
    )
    if not isinstance(previous_modes, dict):
        previous_modes = {}
    for mode, results in results_by_mode.items():
        previous_results = previous_modes.get(mode, [])
        if not isinstance(previous_results, list):
            previous_results = []
        previous_by_symbol = {
            row["symbol"]: row
            for row in previous_results
            if isinstance(row, dict) and row.get("symbol")
        }
        for row in results:
            previous = previous_by_symbol.get(row["symbol"])
            previous_date = previous.get("data_as_of") if previous else None
            current_date = row.get("data_as_of")
            previous_score = previous.get("score") if previous else None
            if (
                not previous_date
                or not current_date
                or previous_date >= current_date
                or not isinstance(previous_score, (int, float))
            ):
                row.update({
                    "previous_score": None,
                    "score_change": None,
                    "new_event_categories": [],
                    "new_risk": False,
                    "entry_condition_reached": False,
                })
                continue
            previous_categories = set(previous.get("event_categories") or [])
            new_categories = sorted(set(row.get("event_categories") or []) - previous_categories)
            newly_banned = row.get("in_fo_ban") is True and previous.get("in_fo_ban") is False
            row.update({
                "previous_score": previous_score,
                "score_change": round(row["score"] - previous_score, 1),
                "new_event_categories": new_categories,
                "new_risk": bool(new_categories or newly_banned),
                "entry_condition_reached": (
                    row.get("entry_condition_met") is True
                    and previous.get("entry_condition_met") is False
                ),
            })
    return results_by_mode


def select_liquid_fno_symbols(frame, limit=DEFAULT_UNIVERSE_SIZE):
    futures = frame[frame["FinInstrmTp"] == "STF"].copy()
    if futures.empty:
        return ()
    futures["XpryDt"] = pd.to_datetime(futures["XpryDt"])
    nearest = futures.groupby("TckrSymb")["XpryDt"].transform("min")
    front = futures[futures["XpryDt"] == nearest].copy()
    front["TtlTrfVal"] = pd.to_numeric(front["TtlTrfVal"], errors="coerce").fillna(0)
    front["TtlTradgVol"] = pd.to_numeric(front["TtlTradgVol"], errors="coerce").fillna(0)
    ranked = front.sort_values(
        ["TtlTrfVal", "TtlTradgVol"], ascending=False
    )
    return tuple(ranked["TckrSymb"].drop_duplicates().head(limit))


def load_cash_histories(session, symbols, sessions=50, lookback_days=90,
                        seed_histories=None):
    seed_histories = seed_histories or {}
    rows = {
        symbol: frame.to_dict("records")
        for symbol in symbols
        if (frame := seed_histories.get(symbol)) is not None
    }
    rows.update({symbol: rows.get(symbol, []) for symbol in symbols})
    cursor = date.today()
    candidate_dates = [
        cursor - timedelta(days=days_back)
        for days_back in range(lookback_days)
        if (cursor - timedelta(days=days_back)).weekday() < 5
    ]
    symbols_by_date = {trade_date: set() for trade_date in candidate_dates}
    for symbol, symbol_rows in rows.items():
        needed_dates = candidate_dates
        if len(symbol_rows) >= sessions:
            latest_seed_date = pd.to_datetime(
                pd.DataFrame(symbol_rows)["TradDt"]
            ).max().date()
            needed_dates = [value for value in candidate_dates if value > latest_seed_date]
        for trade_date in needed_dates:
            symbols_by_date[trade_date].add(symbol)
    symbols_by_date = {
        trade_date: needed_symbols
        for trade_date, needed_symbols in symbols_by_date.items()
        if needed_symbols
    }

    worker_state = threading.local()

    def fetch_date(trade_date):
        worker_session = getattr(worker_state, "session", None)
        if worker_session is None:
            worker_session = requests.Session()
            worker_state.session = worker_session
        try:
            frame = download_bhavcopy(worker_session, "cm", trade_date)
        except UpstreamUnavailableError:
            return trade_date, None
        if frame is None:
            return trade_date, None
        needed_symbols = symbols_by_date[trade_date]
        return trade_date, frame[
            frame["TckrSymb"].isin(needed_symbols) & (frame["SctySrs"] == "EQ")
        ].copy()

    with ThreadPoolExecutor(max_workers=CASH_DOWNLOAD_WORKERS) as executor:
        downloaded = list(executor.map(fetch_date, symbols_by_date))

    for _, frame in sorted(downloaded, key=lambda item: item[0], reverse=True):
        if frame is None:
            continue
        for _, row in frame.iterrows():
            rows[row["TckrSymb"]].append(row.to_dict())

    histories = {}
    for symbol, symbol_rows in rows.items():
        frame = pd.DataFrame(symbol_rows)
        if not frame.empty:
            frame = frame.drop_duplicates(subset=["TradDt"], keep="last")
            frame = frame.sort_values("TradDt").tail(sessions).reset_index(drop=True)
        if len(frame) < sessions:
            print(f"Skipping {symbol}: only {len(frame)} cash sessions found")
            continue
        histories[symbol] = frame
    return histories


def load_stock_universe(fundamental_seed=None, enrichment_callback=None,
                        cash_seed_path=CASH_HISTORY_CACHE_PATH,
                        market_seed=None, market_seed_date=None):
    fundamental_seed = fundamental_seed or {}
    with requests.Session() as session:
        fno_frames = load_recent_fno_frames(session, date.today())
        if not fno_frames:
            raise RuntimeError("No live NSE F&O histories were available")
        latest_fno_date = pd.to_datetime(fno_frames[0]["TradDt"].iloc[0]).date()
        if market_seed and market_seed_date == latest_fno_date.isoformat():
            stocks = [dict(stock) for stock in market_seed]
            ban_snapshot = fetch_fno_ban_snapshot(session)
            for stock in stocks:
                stock["in_fo_ban"] = fno_ban_status_from_snapshot(
                    ban_snapshot, stock["symbol"]
                )["is_banned"]
            if enrichment_callback is not None:
                enrichment_callback()
            enrich_stocks(
                [stock for stock in stocks if stock.get("liquidity_filter_pass")],
                fundamental_seed,
            )
            return stocks, latest_fno_date
        configured = configured_symbols()
        universe_size = int(os.getenv("SCANNER_UNIVERSE_SIZE", str(DEFAULT_UNIVERSE_SIZE)))
        symbols = configured or select_liquid_fno_symbols(fno_frames[0], universe_size)
        if not symbols:
            symbols = DEFAULT_SYMBOLS
        seed_histories = load_cash_history_seed(cash_seed_path)
        histories = load_cash_histories(
            session,
            symbols,
            seed_histories=seed_histories,
        )
        if not histories:
            raise RuntimeError("No live NSE cash histories were available")
        retained_histories = {**seed_histories, **histories}
        retained_histories = dict(sorted(
            retained_histories.items(),
            key=lambda item: pd.to_datetime(item[1]["TradDt"]).max(),
            reverse=True,
        )[:CASH_SEED_MAX_SYMBOLS])
        write_cash_history_seed(retained_histories, cash_seed_path)

        latest_cash_date = max(
            pd.to_datetime(history.iloc[-1]["TradDt"]).date()
            for history in histories.values()
        )
        data_as_of = min(latest_cash_date, latest_fno_date)
        ban_snapshot = fetch_fno_ban_snapshot(session)

        stocks = []
        for symbol, history in histories.items():
            cash = analyze_cash(history)
            ban_status = fno_ban_status_from_snapshot(ban_snapshot, symbol)
            fno = analyze_fno(symbol, fno_frames, ban_status)
            if fno is None or fno["pcr"] is None or fno["oi_change_pct"] is None:
                print(f"Skipping {symbol}: complete live F&O data was not available")
                continue

            volumes = history["TtlTradgVol"].astype(float)
            liquidity_filter_pass = (
                cash["average_traded_value_crore"] >= MIN_CASH_TURNOVER_CRORE
                and fno["futures_volume"] >= MIN_FUTURES_VOLUME
            )
            symbol_cash_date = pd.to_datetime(history.iloc[-1]["TradDt"]).date()
            stocks.append({
                "symbol": symbol,
                "data_as_of": min(symbol_cash_date, latest_fno_date).isoformat(),
                "futures_price_change_pct": fno["futures_price_change"],
                "futures_oi_change_pct": fno["oi_change_pct"],
                "pcr": fno["pcr"],
                "close": cash["close"],
                "dma20": cash["sma20"],
                "dma50": cash["sma50"],
                "return_5d": cash["return_5d"],
                "volume": float(volumes.iloc[-1]),
                "avg_volume": float(volumes.iloc[-21:-1].mean()),
                "in_fo_ban": ban_status["is_banned"],
                "gap_pct": cash["gap_pct"],
                "gap_atr": cash["gap_atr"],
                "atr14": cash["atr14"],
                "session_move_atr": cash["session_move_atr"],
                "distance_from_breakout_atr": cash["distance_from_breakout_atr"],
                "distance_from_sma20_atr": cash["distance_from_sma20_atr"],
                "prior_twenty_day_low": cash["prior_twenty_day_low"],
                "liquidity_tier": cash["liquidity_tier"],
                "estimated_slippage_bps": cash["estimated_slippage_bps"],
                "liquidity_filter_pass": liquidity_filter_pass,
                "cash_turnover_crore": cash["average_traded_value_crore"],
                "futures_volume": fno["futures_volume"],
                "call_oi_wall": fno["call_oi_wall"],
                "put_oi_wall": fno["put_oi_wall"],
            })

    if enrichment_callback is not None:
        enrichment_callback()
    enrich_stocks(
        [stock for stock in stocks if stock["liquidity_filter_pass"]],
        fundamental_seed,
    )

    if not stocks:
        raise RuntimeError("No symbols had complete live NSE cash and F&O data")
    return stocks, data_as_of


def main(output_path=LATEST_DATA_PATH, progress_callback=None,
         reuse_cached_fundamentals=False):
    def report_progress(stage):
        if progress_callback is not None:
            progress_callback(stage)

    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    previous_snapshot = load_previous_snapshot(output_path)
    fundamental_seed = (
        reusable_fundamentals(previous_snapshot)
        if reuse_cached_fundamentals else {}
    )
    market_seed, market_seed_date = (
        reusable_market_inputs(previous_snapshot)
        if reuse_cached_fundamentals else (None, None)
    )

    report_progress("Fetching global market cues")
    global_cues = fetch_global_cues()
    report_progress("Loading NSE prices and derivatives")
    stocks, data_as_of = load_stock_universe(
        fundamental_seed=fundamental_seed,
        enrichment_callback=lambda: report_progress("Fetching news and fundamentals"),
        market_seed=market_seed,
        market_seed_date=market_seed_date,
    )
    market_context = build_market_context(global_cues)
    add_sector_relative_strength(stocks, market_context)
    eligible_stocks = [stock for stock in stocks if stock["liquidity_filter_pass"]]
    if not eligible_stocks:
        raise RuntimeError("No symbols passed the configured liquidity filters")

    report_progress("Scoring trade setups")
    bullish_ranked = sorted(
        (calculate_stock_score(stock, global_cues, "bullish") for stock in eligible_stocks),
        key=lambda result: result["score"],
        reverse=True,
    )
    bearish_ranked = sorted(
        (calculate_stock_score(stock, global_cues, "bearish") for stock in eligible_stocks),
        key=lambda result: result["score"],
        reverse=True,
    )
    results_by_mode = add_previous_session_changes(
        {"bullish": bullish_ranked, "bearish": bearish_ranked},
        previous_snapshot,
    )

    backtest_limit = int(os.getenv("BACKTEST_SYMBOL_LIMIT", "5"))
    backtest_symbols = [stock["symbol"] for stock in eligible_stocks[:backtest_limit]]
    report_progress("Running historical calibration")
    try:
        backtest = run_backtest(
            backtest_symbols,
            os.getenv("BACKTEST_START", "2021-01-01"),
            (data_as_of + timedelta(days=1)).isoformat(),
            int(os.getenv("BACKTEST_HORIZON_SESSIONS", "10")),
        )
    except UpstreamUnavailableError:
        backtest = {
            "error": "Historical calibration is temporarily unavailable.",
            "symbols": backtest_symbols,
            "score_scope": "cash_technical_heuristic_only",
        }

    output = {
        "generated_at_ist": now.strftime("%Y-%m-%d %H:%M:%S"),
        "data_as_of": data_as_of.isoformat(),
        "scanner_type": "pre_market_swing_scanner",
        "disclaimer": "Educational scanner only. Not financial advice.",
        "global_cues": global_cues,
        "market_context": market_context,
        "market_inputs": market_input_snapshot(stocks),
        "backtest": backtest,
        "universe": {
            "source": "NSE front-month single-stock futures ranked by traded value",
            "configured_override": bool(configured_symbols()),
            "scanned_count": len(stocks),
            "eligible_count": len(eligible_stocks),
            "liquidity_excluded_count": len(stocks) - len(eligible_stocks),
            "liquidity_filters": {
                "minimum_cash_turnover_crore": MIN_CASH_TURNOVER_CRORE,
                "minimum_futures_volume": MIN_FUTURES_VOLUME,
            },
        },
        "top_3": bullish_ranked[:3],
        "top_setups": {
            "bullish": bullish_ranked[:3],
            "bearish": bearish_ranked[:3],
        },
        "all_results": bullish_ranked,
        "all_results_by_mode": {
            "bullish": results_by_mode["bullish"],
            "bearish": results_by_mode["bearish"],
        },
    }

    report_progress("Publishing results")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(sanitize_json_value(output), file, indent=2, allow_nan=False)
    os.replace(temporary_path, output_path)

    print(f"Generated {output_path}")


if __name__ == "__main__":
    main()