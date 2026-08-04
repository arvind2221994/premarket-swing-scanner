import json
import os
from datetime import datetime
import pytz

from scoring import calculate_stock_score
from global_cues import fetch_global_cues


def load_stock_universe():
    """
    Replace this with real NSE F&O data later.
    For MVP, this mock data validates the scoring and GitHub Pages pipeline.
    """

    return [
        {
            "symbol": "RELIANCE",
            "futures_price_change_pct": 1.1,
            "futures_oi_change_pct": 4.2,
            "pcr": 1.18,
            "close": 2850,
            "dma20": 2800,
            "dma50": 2710,
            "return_5d": 2.4,
            "volume": 15000000,
            "avg_volume": 10000000,
            "in_fo_ban": False,
            "event_risk": False
        },
        {
            "symbol": "ICICIBANK",
            "futures_price_change_pct": 0.8,
            "futures_oi_change_pct": 2.5,
            "pcr": 1.08,
            "close": 1230,
            "dma20": 1205,
            "dma50": 1180,
            "return_5d": 1.7,
            "volume": 18000000,
            "avg_volume": 16000000,
            "in_fo_ban": False,
            "event_risk": False
        },
        {
            "symbol": "TCS",
            "futures_price_change_pct": 0.4,
            "futures_oi_change_pct": -1.5,
            "pcr": 0.92,
            "close": 4100,
            "dma20": 4140,
            "dma50": 4050,
            "return_5d": -0.4,
            "volume": 3000000,
            "avg_volume": 3500000,
            "in_fo_ban": False,
            "event_risk": False
        }
    ]


def main():
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)

    global_cues = fetch_global_cues()
    stocks = load_stock_universe()

    scored = []

    for stock in stocks:
        result = calculate_stock_score(stock, global_cues)
        scored.append(result)

    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)

    output = {
        "generated_at_ist": now.strftime("%Y-%m-%d %H:%M:%S"),
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