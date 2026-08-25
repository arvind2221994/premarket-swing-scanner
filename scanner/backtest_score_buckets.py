import argparse
import json
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from fno_trade_analyzer import analyze_cash


SCORE_BUCKETS = ("below_60", "60_to_74_9", "75_and_above")
REGIMES = ("bullish", "mixed", "bearish")


def classify_score(score):
    if score >= 75:
        return "75_and_above"
    if score >= 60:
        return "60_to_74_9"
    return "below_60"


def classify_regime(close, sma20, sma50):
    if close > sma20 > sma50:
        return "bullish"
    if close < sma20 < sma50:
        return "bearish"
    return "mixed"


def download_history(ticker, start, end):
    data = yf.download(
        ticker,
        start=start,
        end=end,
        interval="1d",
        auto_adjust=True,
        progress=False,
        multi_level_index=False,
    )
    if data is None or data.empty:
        raise ValueError(f"No Yahoo Finance history returned for {ticker}")
    return data.dropna(subset=["Open", "High", "Low", "Close", "Volume"])


def to_analyzer_history(data):
    return pd.DataFrame(
        {
            "ClsPric": data["Close"],
            "HghPric": data["High"],
            "LwPric": data["Low"],
            "TtlTradgVol": data["Volume"],
        },
        index=data.index,
    )


def generate_observations(symbol, stock_data, nifty_data, horizon):
    history = to_analyzer_history(stock_data)
    nifty_closes = nifty_data["Close"].reindex(history.index).ffill()
    nifty_sma20 = nifty_closes.rolling(20).mean()
    nifty_sma50 = nifty_closes.rolling(50).mean()
    observations = []

    for position in range(49, len(history) - horizon):
        signal_history = history.iloc[: position + 1]
        signal = analyze_cash(signal_history)
        entry = signal["close"]
        future = stock_data.iloc[position + 1 : position + horizon + 1]
        if future.empty or pd.isna(nifty_sma50.iloc[position]):
            continue

        exit_close = float(future["Close"].iloc[-1])
        forward_return = (exit_close / entry - 1) * 100
        max_drawdown = (float(future["Low"].min()) / entry - 1) * 100
        breakout = entry >= signal["prior_twenty_day_high"]
        false_breakout = breakout and (
            float(future["Close"].min()) < signal["prior_twenty_day_high"]
            and forward_return <= 0
        )
        regime = classify_regime(
            float(nifty_closes.iloc[position]),
            float(nifty_sma20.iloc[position]),
            float(nifty_sma50.iloc[position]),
        )
        observations.append(
            {
                "symbol": symbol,
                "date": history.index[position].date().isoformat(),
                "score": signal["score"],
                "score_bucket": classify_score(signal["score"]),
                "regime": regime,
                "forward_return_pct": forward_return,
                "max_drawdown_pct": max_drawdown,
                "win": forward_return > 0,
                "breakout": breakout,
                "false_breakout": false_breakout,
            }
        )
    return observations


def summarize_group(rows):
    if not rows:
        return None
    breakouts = [row for row in rows if row["breakout"]]
    false_breakouts = [row for row in breakouts if row["false_breakout"]]
    return {
        "observations": len(rows),
        "win_rate_pct": round(sum(row["win"] for row in rows) / len(rows) * 100, 1),
        "expectancy_pct": round(
            sum(row["forward_return_pct"] for row in rows) / len(rows), 2
        ),
        "average_max_drawdown_pct": round(
            sum(row["max_drawdown_pct"] for row in rows) / len(rows), 2
        ),
        "breakout_observations": len(breakouts),
        "false_breakout_rate_pct": (
            round(len(false_breakouts) / len(breakouts) * 100, 1)
            if breakouts
            else None
        ),
    }


def build_summary(observations):
    return {
        bucket: {
            regime: summarize_group(
                [
                    row
                    for row in observations
                    if row["score_bucket"] == bucket and row["regime"] == regime
                ]
            )
            for regime in REGIMES
        }
        for bucket in SCORE_BUCKETS
    }


def run_backtest(symbols, start, end, horizon):
    nifty = download_history("^NSEI", start, end)
    observations = []
    errors = []
    for symbol in symbols:
        ticker = f"{symbol}.NS"
        try:
            stock = download_history(ticker, start, end)
            observations.extend(
                generate_observations(symbol, stock, nifty, horizon)
            )
        except Exception as error:
            errors.append({"symbol": symbol, "error": str(error)})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score_scope": "cash_technical_heuristic_only",
        "source": "Yahoo Finance adjusted daily history",
        "symbols": symbols,
        "start": start,
        "end": end,
        "forward_horizon_sessions": horizon,
        "methodology": {
            "win": "Forward return is greater than zero at the horizon.",
            "expectancy": "Mean forward return at the horizon.",
            "drawdown": "Mean worst intraperiod low relative to signal close.",
            "false_breakout": (
                "Signal close is at or above the prior 20-day high, a later close "
                "falls below that level, and horizon return is non-positive."
            ),
            "regime": "Nifty close/SMA20/SMA50: bullish, bearish, or mixed.",
        },
        "limitations": [
            "This calibrates the cash technical score, not the live composite.",
            "Historical F&O and point-in-time fundamentals are unavailable here; "
            "using current values would introduce look-ahead bias.",
            "Results exclude costs, slippage, taxes, position sizing, and overlapping-signal effects.",
        ],
        "observation_count": len(observations),
        "errors": errors,
        "score_buckets_by_regime": build_summary(observations),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Backtest the analyzer cash-score buckets across Nifty regimes."
    )
    parser.add_argument("symbols", nargs="+", help="NSE symbols, for example RELIANCE TCS")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--horizon", type=int, default=10)
    args = parser.parse_args()
    if args.horizon < 1:
        parser.error("--horizon must be at least 1")
    symbols = [symbol.strip().upper() for symbol in args.symbols]
    print(json.dumps(run_backtest(symbols, args.start, args.end, args.horizon), indent=2))


if __name__ == "__main__":
    main()