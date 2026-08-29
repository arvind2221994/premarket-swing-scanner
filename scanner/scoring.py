def clamp(value, low=0, high=100):
    return max(low, min(high, value))


def score_futures(price_change_pct, oi_change_pct, mode="bullish"):
    if mode == "bearish":
        if price_change_pct < 0 and oi_change_pct > 0:
            return 90, "Short build-up"
        if price_change_pct < 0 and oi_change_pct < 0:
            return 70, "Long unwinding"
        if price_change_pct > 0 and oi_change_pct < 0:
            return 35, "Short covering opposes bearish setup"
        if price_change_pct > 0 and oi_change_pct > 0:
            return 20, "Long build-up opposes bearish setup"
        return 50, "Neutral futures positioning"
    if price_change_pct > 0 and oi_change_pct > 0:
        return 90, "Long build-up"
    if price_change_pct > 0 and oi_change_pct < 0:
        return 70, "Short covering"
    if price_change_pct < 0 and oi_change_pct > 0:
        return 20, "Short build-up"
    if price_change_pct < 0 and oi_change_pct < 0:
        return 35, "Long unwinding"
    return 50, "Neutral futures positioning"


def score_pcr(pcr, mode="bullish"):
    if pcr is None:
        return 50, "PCR unavailable"
    if mode == "bearish":
        if pcr < 0.7:
            return 85, "Call-heavy positioning supports bearish setup"
        if pcr < 0.9:
            return 70, "PCR leans bearish"
        if pcr <= 1.3:
            return 45, "Balanced PCR"
        return 25, "Put-heavy positioning opposes bearish setup"
    if 0.9 <= pcr <= 1.3:
        return 85, "Healthy bullish PCR"
    if 1.3 < pcr <= 1.5:
        return 70, "Bullish but slightly crowded PCR"
    if pcr > 1.5:
        return 45, "Overcrowded PCR"
    if pcr < 0.7:
        return 25, "Weak PCR"
    return 55, "Neutral PCR"


def score_price_trend(close, dma20, dma50, ret_5d, mode="bullish"):
    score = 0
    reasons = []
    bearish = mode == "bearish"

    if (close < dma20) if bearish else (close > dma20):
        score += 35
        reasons.append(f"Price {'below' if bearish else 'above'} 20 DMA")

    if (close < dma50) if bearish else (close > dma50):
        score += 35
        reasons.append(f"Price {'below' if bearish else 'above'} 50 DMA")

    if (ret_5d < 0) if bearish else (ret_5d > 0):
        score += 30
        reasons.append(f"{'Negative' if bearish else 'Positive'} 5-day momentum")

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


def score_global_cues(global_data, mode="bullish"):
    if mode not in {"bullish", "bearish"}:
        raise ValueError("mode must be bullish or bearish")
    score = 50
    reasons = []
    bearish = mode == "bearish"

    def cue_reason(label, direction):
        if not bearish:
            return f"{label} {direction}"
        impact = "supports" if direction == "negative" else "opposes"
        return f"{label} {direction} {impact} bearish setup"

    nasdaq = global_data.get("nasdaq_change_pct")
    spx = global_data.get("spx_change_pct")
    dow = global_data.get("dow_change_pct")
    gift = global_data.get("gift_nifty_change_pct")

    if nasdaq is not None:
        if nasdaq > 1:
            score += 12
            reasons.append(cue_reason("NASDAQ strongly", "positive"))
        elif nasdaq < -1:
            score -= 12
            reasons.append(cue_reason("NASDAQ strongly", "negative"))

    if spx is not None:
        if spx > 1:
            score += 10
            reasons.append(cue_reason("S&P 500", "positive"))
        elif spx < -1:
            score -= 10
            reasons.append(cue_reason("S&P 500", "negative"))

    if dow is not None:
        if dow > 1:
            score += 8
            reasons.append(cue_reason("Dow Jones", "positive"))
        elif dow < -1:
            score -= 8
            reasons.append(cue_reason("Dow Jones", "negative"))

    if gift is not None:
        if gift > 0.5:
            score += 15
            reasons.append(cue_reason("GIFT Nifty", "positive"))
        elif gift < -0.5:
            score -= 15
            reasons.append(cue_reason("GIFT Nifty", "negative"))

    score = clamp(score)
    return (100 - score if bearish else score), reasons


def calculate_stock_score(stock, global_data, mode="bullish"):
    if mode not in {"bullish", "bearish"}:
        raise ValueError("mode must be bullish or bearish")
    futures_score, futures_reason = score_futures(
        stock["futures_price_change_pct"],
        stock["futures_oi_change_pct"],
        mode,
    )

    pcr_score, pcr_reason = score_pcr(stock["pcr"], mode)

    fno_score = futures_score * 0.60 + pcr_score * 0.40

    trend_score, trend_reasons = score_price_trend(
        stock["close"],
        stock["dma20"],
        stock["dma50"],
        stock["return_5d"],
        mode,
    )

    volume_score, volume_reason = score_volume(
        stock["volume"],
        stock["avg_volume"]
    )

    global_score, global_reasons = score_global_cues(global_data, mode)

    relative_strength = stock.get("sector_relative_strength_pct")
    if relative_strength is None:
        relative_score = 50
        relative_reason = "Sector-relative strength unavailable"
    else:
        directional_strength = -relative_strength if mode == "bearish" else relative_strength
        relative_score = clamp(50 + directional_strength * 10)
        relative_reason = (
            f"{'Underperforming' if mode == 'bearish' else 'Outperforming'} sector by "
            f"{abs(relative_strength):.1f}% over five sessions"
            if directional_strength > 0
            else f"Sector-relative momentum opposes {mode} setup"
        )

    risk_score = 100
    risk_reasons = []

    if stock.get("in_fo_ban", False):
        risk_score = 0
        risk_reasons.append("Stock is in F&O ban list")

    if stock.get("event_risk", False):
        risk_score -= 30
        risk_reasons.append("Event risk detected")

    gap_atr = stock.get("gap_atr")
    too_extended = (
        gap_atr is not None
        and ((mode == "bullish" and gap_atr >= 1.25) or (mode == "bearish" and gap_atr <= -1.25))
    )
    if too_extended:
        risk_score -= 35
        risk_reasons.append(f"Opening gap is already extended for a {mode} entry")

    if not stock.get("liquidity_filter_pass", True):
        risk_score = 0
        risk_reasons.append("Fails cash or futures liquidity filter")

    final_score = (
        fno_score * 0.35
        + trend_score * 0.20
        + volume_score * 0.10
        + global_score * 0.10
        + relative_score * 0.10
        + risk_score * 0.15
    )

    reasons = [
        futures_reason,
        pcr_reason,
        volume_reason,
        *trend_reasons,
        relative_reason,
        *global_reasons,
        *risk_reasons
    ]

    if final_score >= 75:
        recommendation = f"High confidence {mode} swing candidate"
    elif final_score >= 60:
        recommendation = f"Moderate {mode} swing candidate"
    elif final_score >= 50:
        recommendation = "Watchlist only"
    else:
        recommendation = "Avoid"

    return {
        "symbol": stock["symbol"],
        "setup_mode": mode,
        "score": round(final_score, 2),
        "recommendation": recommendation,
        "futures_price_change_pct": stock["futures_price_change_pct"],
        "futures_oi_change_pct": stock["futures_oi_change_pct"],
        "pcr": stock["pcr"],
        "return_5d": stock["return_5d"],
        "sector": stock.get("sector"),
        "sector_relative_strength_pct": relative_strength,
        "gap_pct": stock.get("gap_pct"),
        "gap_atr": gap_atr,
        "gap_extended": too_extended,
        "liquidity_tier": stock.get("liquidity_tier"),
        "estimated_slippage_bps": stock.get("estimated_slippage_bps"),
        "event_risk": stock.get("event_risk", False),
        "event_risk_status": stock.get("event_risk_status", "clear"),
        "event_categories": stock.get("event_categories", []),
        "call_oi_wall": stock.get("call_oi_wall"),
        "put_oi_wall": stock.get("put_oi_wall"),
        "reasons": reasons,
        "calculation": {
            "fno_score": round(fno_score, 2),
            "fno_weight": 0.35,
            "trend_score": round(trend_score, 2),
            "trend_weight": 0.20,
            "volume_score": round(volume_score, 2),
            "volume_weight": 0.10,
            "global_score": round(global_score, 2),
            "global_weight": 0.10,
            "relative_score": round(relative_score, 2),
            "relative_weight": 0.10,
            "risk_score": round(risk_score, 2),
            "risk_weight": 0.15,
            "weighted_total": round(final_score, 2),
            "total_weight": 1.0,
            "formula": "weighted component total",
        },
    }


def score_detailed_report(symbol, cash, fno, global_data, mode="bullish",
                          sector=None, sector_relative_strength_pct=None,
                          event_risk=False, event_risk_status="clear",
                          event_categories=None):
    volume_ratio = cash.get("volume_ratio", 1)
    stock = {
        "symbol": symbol,
        "futures_price_change_pct": fno["futures_price_change"] if fno else 0,
        "futures_oi_change_pct": fno["oi_change_pct"] if fno else 0,
        "pcr": fno["pcr"] if fno else None,
        "close": cash["close"],
        "dma20": cash["sma20"],
        "dma50": cash["sma50"],
        "return_5d": cash["return_5d"],
        "volume": volume_ratio,
        "avg_volume": 1,
        "sector": sector,
        "sector_relative_strength_pct": sector_relative_strength_pct,
        "in_fo_ban": bool(fno and fno["ban_status"]["is_banned"] is True),
        "event_risk": event_risk,
        "event_risk_status": event_risk_status,
        "event_categories": event_categories or [],
        "gap_pct": cash.get("gap_pct"),
        "gap_atr": cash.get("gap_atr"),
        "liquidity_filter_pass": (
            cash.get("liquidity_tier") != "low"
            and (not fno or fno.get("futures_liquidity_tier") != "low")
        ),
        "liquidity_tier": cash.get("liquidity_tier"),
        "estimated_slippage_bps": cash.get("estimated_slippage_bps"),
        "call_oi_wall": fno.get("call_oi_wall") if fno else None,
        "put_oi_wall": fno.get("put_oi_wall") if fno else None,
    }
    return calculate_stock_score(stock, global_data, mode)