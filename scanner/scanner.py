import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz
import requests

from fno_trade_analyzer import (
    analyze_cash,
    analyze_fno,
    download_bhavcopy,
    fetch_fno_ban_status,
    load_fundamental_analysis,
    load_recent_fno_frames,
)
from news import fetch_company_news
from scoring import calculate_stock_score
from global_cues import fetch_global_cues
from market_context import add_sector_relative_strength, build_market_context
from backtest_score_buckets import run_backtest
from resilience import UpstreamUnavailableError


DEFAULT_SYMBOLS = ("RELIANCE", "ICICIBANK", "TCS")
DEFAULT_UNIVERSE_SIZE = 15
LATEST_DATA_PATH = Path(__file__).resolve().parent.parent / "docs" / "data" / "latest.json"
MIN_CASH_TURNOVER_CRORE = float(os.getenv("MIN_CASH_TURNOVER_CRORE", "25"))
MIN_FUTURES_VOLUME = int(os.getenv("MIN_FUTURES_VOLUME", "250"))
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


def load_cash_histories(session, symbols, sessions=50, lookback_days=90):
    rows = {symbol: [] for symbol in symbols}
    cursor = date.today()

    for days_back in range(lookback_days):
        if all(len(symbol_rows) >= sessions for symbol_rows in rows.values()):
            break

        trade_date = cursor - timedelta(days=days_back)
        if trade_date.weekday() >= 5:
            continue

        frame = download_bhavcopy(session, "cm", trade_date)
        if frame is None:
            continue

        matches = frame[
            frame["TckrSymb"].isin(symbols) & (frame["SctySrs"] == "EQ")
        ]
        for _, row in matches.iterrows():
            if len(rows[row["TckrSymb"]]) < sessions:
                rows[row["TckrSymb"]].append(row)

    histories = {}
    for symbol, symbol_rows in rows.items():
        if len(symbol_rows) < sessions:
            print(f"Skipping {symbol}: only {len(symbol_rows)} cash sessions found")
            continue
        histories[symbol] = (
            pd.DataFrame(symbol_rows).sort_values("TradDt").reset_index(drop=True)
        )
    return histories


def load_stock_universe():
    with requests.Session() as session:
        fno_frames = load_recent_fno_frames(session, date.today())
        if not fno_frames:
            raise RuntimeError("No live NSE F&O histories were available")
        configured = configured_symbols()
        universe_size = int(os.getenv("SCANNER_UNIVERSE_SIZE", str(DEFAULT_UNIVERSE_SIZE)))
        symbols = configured or select_liquid_fno_symbols(fno_frames[0], universe_size)
        if not symbols:
            symbols = DEFAULT_SYMBOLS
        histories = load_cash_histories(session, symbols)
        if not histories:
            raise RuntimeError("No live NSE cash histories were available")

        latest_cash_date = max(
            pd.to_datetime(history.iloc[-1]["TradDt"]).date()
            for history in histories.values()
        )
        latest_fno_date = pd.to_datetime(fno_frames[0]["TradDt"].iloc[0]).date()
        data_as_of = min(latest_cash_date, latest_fno_date)

        stocks = []
        for symbol, history in histories.items():
            cash = analyze_cash(history)
            ban_status = fetch_fno_ban_status(session, symbol)
            fno = analyze_fno(symbol, fno_frames, ban_status)
            if fno is None or fno["pcr"] is None or fno["oi_change_pct"] is None:
                print(f"Skipping {symbol}: complete live F&O data was not available")
                continue

            volumes = history["TtlTradgVol"].astype(float)
            news = fetch_company_news(symbol, days=7, limit=8)
            fundamental_analysis = load_fundamental_analysis(symbol)
            fundamental_result = fundamental_analysis["assessment"]
            liquidity_filter_pass = (
                cash["average_traded_value_crore"] >= MIN_CASH_TURNOVER_CRORE
                and fno["futures_volume"] >= MIN_FUTURES_VOLUME
            )
            symbol_cash_date = pd.to_datetime(history.iloc[-1]["TradDt"]).date()
            stocks.append({
                "symbol": symbol,
                "company_name": (
                    fundamental_analysis["metrics"].get("name")
                    if fundamental_analysis["metrics"] else symbol
                ),
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
                "event_risk": news["event_risk"]["detected"],
                "event_risk_status": news["event_risk"]["status"],
                "event_categories": news["event_risk"]["categories"],
                "fundamental_score": (
                    fundamental_result["score"] if fundamental_result else None
                ),
            })

    if not stocks:
        raise RuntimeError("No symbols had complete live NSE cash and F&O data")
    return stocks, data_as_of


def main(output_path=LATEST_DATA_PATH, progress_callback=None):
    def report_progress(stage):
        if progress_callback is not None:
            progress_callback(stage)

    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    previous_snapshot = load_previous_snapshot(output_path)

    report_progress("Fetching global market cues")
    global_cues = fetch_global_cues()
    report_progress("Loading NSE prices and derivatives")
    stocks, data_as_of = load_stock_universe()
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