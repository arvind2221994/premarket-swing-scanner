import json
import os
from datetime import date, datetime, timedelta

import pandas as pd
import pytz
import requests

from fno_trade_analyzer import (
    analyze_cash,
    analyze_fno,
    download_bhavcopy,
    fetch_fno_ban_status,
    load_recent_fno_frames,
)
from scoring import calculate_stock_score
from global_cues import fetch_global_cues


DEFAULT_SYMBOLS = ("RELIANCE", "ICICIBANK", "TCS")


def configured_symbols():
    value = os.getenv("SCANNER_SYMBOLS", ",".join(DEFAULT_SYMBOLS))
    return tuple(dict.fromkeys(symbol.strip().upper() for symbol in value.split(",") if symbol.strip()))


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
    symbols = configured_symbols()
    if not symbols:
        raise ValueError("SCANNER_SYMBOLS must contain at least one NSE symbol")

    with requests.Session() as session:
        histories = load_cash_histories(session, symbols)
        if not histories:
            raise RuntimeError("No live NSE cash histories were available")

        latest_cash_date = max(
            pd.to_datetime(history.iloc[-1]["TradDt"]).date()
            for history in histories.values()
        )
        fno_frames = load_recent_fno_frames(session, latest_cash_date)
        if not fno_frames:
            raise RuntimeError("No live NSE F&O histories were available")
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
            stocks.append({
                "symbol": symbol,
                "futures_price_change_pct": fno["futures_price_change"],
                "futures_oi_change_pct": fno["oi_change_pct"],
                "pcr": fno["pcr"],
                "close": cash["close"],
                "dma20": cash["sma20"],
                "dma50": cash["sma50"],
                "return_5d": cash["return_5d"],
                "volume": float(volumes.iloc[-1]),
                "avg_volume": float(volumes.iloc[-21:-1].mean()),
                "in_fo_ban": ban_status["is_banned"] is True,
            })

    if not stocks:
        raise RuntimeError("No symbols had complete live NSE cash and F&O data")
    return stocks, data_as_of


def main():
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)

    global_cues = fetch_global_cues()
    stocks, data_as_of = load_stock_universe()

    scored = []

    for stock in stocks:
        result = calculate_stock_score(stock, global_cues)
        scored.append(result)

    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)

    output = {
        "generated_at_ist": now.strftime("%Y-%m-%d %H:%M:%S"),
        "data_as_of": data_as_of.isoformat(),
        "scanner_type": "pre_market_swing_scanner",
        "disclaimer": "Educational scanner only. Not financial advice.",
        "global_cues": global_cues,
        "top_3": ranked[:3],
        "all_results": ranked
    }

    os.makedirs("docs/data", exist_ok=True)

    with open("docs/data/latest.json", "w") as f:
        json.dump(output, f, indent=2)

    print("Generated docs/data/latest.json")


if __name__ == "__main__":
    main()