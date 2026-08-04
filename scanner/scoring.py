def clamp(value, low=0, high=100):
    return max(low, min(high, value))


def score_futures(price_change_pct, oi_change_pct):
    if price_change_pct > 0 and oi_change_pct > 0:
        return 90, "Long build-up"
    if price_change_pct > 0 and oi_change_pct < 0:
        return 70, "Short covering"
    if price_change_pct < 0 and oi_change_pct > 0:
        return 20, "Short build-up"
    if price_change_pct < 0 and oi_change_pct < 0:
        return 35, "Long unwinding"
    return 50, "Neutral futures positioning"


def score_pcr(pcr):
    if 0.9 <= pcr <= 1.3:
        return 85, "Healthy bullish PCR"
    if 1.3 < pcr <= 1.5:
        return 70, "Bullish but slightly crowded PCR"
    if pcr > 1.5:
        return 45, "Overcrowded PCR"
    if pcr < 0.7:
        return 25, "Weak PCR"
    return 55, "Neutral PCR"


def score_price_trend(close, dma20, dma50, ret_5d):
    score = 0
    reasons = []

    if close > dma20:
        score += 35
        reasons.append("Price above 20 DMA")

    if close > dma50:
        score += 35
        reasons.append("Price above 50 DMA")

    if ret_5d > 0:
        score += 30
        reasons.append("Positive 5-day momentum")

    return clamp(score), reasons


def score_volume(volume, avg_volume):
    if avg_volume <= 0:
        return 50, "Volume data unavailable"

    ratio = volume / avg_volume

    if ratio >= 1.8:
        return 90, "Strong volume expansion"
    if ratio >= 1.2:
        return 75, "Volume above average"
    if ratio >= 0.8:
        return 55, "Normal volume"
    return 35, "Weak volume"


def score_global_cues(global_data):
    score = 50
    reasons = []

    nasdaq = global_data.get("nasdaq_change_pct")
    spx = global_data.get("spx_change_pct")
    gift = global_data.get("gift_nifty_change_pct")

    if nasdaq is not None:
        if nasdaq > 1:
            score += 12
            reasons.append("NASDAQ strongly positive")
        elif nasdaq < -1:
            score -= 12
            reasons.append("NASDAQ strongly negative")

    if spx is not None:
        if spx > 1:
            score += 10
            reasons.append("S&P 500 positive")
        elif spx < -1:
            score -= 10
            reasons.append("S&P 500 negative")

    if gift is not None:
        if gift > 0.5:
            score += 15
            reasons.append("GIFT Nifty positive")
        elif gift < -0.5:
            score -= 15
            reasons.append("GIFT Nifty negative")

    return clamp(score), reasons


def calculate_stock_score(stock, global_data):
    futures_score, futures_reason = score_futures(
        stock["futures_price_change_pct"],
        stock["futures_oi_change_pct"]
    )

    pcr_score, pcr_reason = score_pcr(stock["pcr"])

    fno_score = futures_score * 0.60 + pcr_score * 0.40

    trend_score, trend_reasons = score_price_trend(
        stock["close"],
        stock["dma20"],
        stock["dma50"],
        stock["return_5d"]
    )

    volume_score, volume_reason = score_volume(
        stock["volume"],
        stock["avg_volume"]
    )

    global_score, global_reasons = score_global_cues(global_data)

    risk_score = 100
    risk_reasons = []

    if stock.get("in_fo_ban", False):
        risk_score = 0
        risk_reasons.append("Stock is in F&O ban list")

    if stock.get("event_risk", False):
        risk_score -= 30
        risk_reasons.append("Event risk detected")

    final_score = (
        fno_score * 0.40
        + trend_score * 0.25
        + volume_score * 0.10
        + global_score * 0.15
        + risk_score * 0.10
    )

    reasons = [
        futures_reason,
        pcr_reason,
        volume_reason,
        *trend_reasons,
        *global_reasons,
        *risk_reasons
    ]

    if final_score >= 75:
        recommendation = "High confidence bullish swing candidate"
    elif final_score >= 60:
        recommendation = "Moderate bullish swing candidate"
    elif final_score >= 50:
        recommendation = "Watchlist only"
    else:
        recommendation = "Avoid"

    return {
        "symbol": stock["symbol"],
        "score": round(final_score, 2),
        "recommendation": recommendation,
        "pcr": stock["pcr"],
        "reasons": reasons
    }