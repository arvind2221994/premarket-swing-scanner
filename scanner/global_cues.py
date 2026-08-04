import yfinance as yf


def get_change_pct(ticker):
    try:
        data = yf.download(ticker, period="5d", interval="1d", progress=False)

        if data is None or len(data) < 2:
            return None

        prev_close = float(data["Close"].iloc[-2])
        latest_close = float(data["Close"].iloc[-1])

        return round(((latest_close - prev_close) / prev_close) * 100, 2)
    except Exception:
        return None


def fetch_global_cues():
    return {
        "nasdaq_change_pct": get_change_pct("^IXIC"),
        "spx_change_pct": get_change_pct("^GSPC"),
        "dow_change_pct": get_change_pct("^DJI"),
        "gift_nifty_change_pct": None
    }