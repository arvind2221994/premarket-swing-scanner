import argparse
import io
import re
import zipfile
from datetime import date, timedelta

import pandas as pd
import requests

from fundamentals import calculate_fundamental_score, fetch_screener_data
from news import fetch_company_news
from resilience import UpstreamUnavailableError, call_with_resilience


ARCHIVE_URL = (
    "https://nsearchives.nseindia.com/content/{segment}/"
    "BhavCopy_NSE_{code}_0_0_0_{trade_date}_F_0000.csv.zip"
)
HEADERS = {"User-Agent": "Mozilla/5.0"}
FNO_BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"


def download_bhavcopy(session, segment, trade_date):
    code = segment.upper()
    url = ARCHIVE_URL.format(
        segment=segment.lower(),
        code=code,
        trade_date=trade_date.strftime("%Y%m%d"),
    )
    def request_bhavcopy():
        response = session.get(url, headers=HEADERS, timeout=20)
        if response.status_code != 404:
            response.raise_for_status()
        return response

    response = call_with_resilience("NSE", request_bhavcopy)

    if response.status_code == 404:
        return None

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        return pd.read_csv(archive.open(archive.namelist()[0]))


def load_recent_symbol_history(session, symbol, sessions=50, lookback_days=90):
    rows = []
    cursor = date.today()

    for days_back in range(lookback_days):
        if len(rows) >= sessions:
            break

        trade_date = cursor - timedelta(days=days_back)
        if trade_date.weekday() >= 5:
            continue

        frame = download_bhavcopy(session, "cm", trade_date)
        if frame is None:
            continue

        matches = frame[
            (frame["TckrSymb"] == symbol) & (frame["SctySrs"] == "EQ")
        ]
        if not matches.empty:
            rows.append(matches.iloc[0])

    if len(rows) < 20:
        raise ValueError(f"Only {len(rows)} cash sessions found for {symbol}")

    return pd.DataFrame(rows).sort_values("TradDt").reset_index(drop=True)


def load_recent_fno_frames(session, latest_cash_date, sessions=2):
    frames = []

    for days_back in range(10):
        trade_date = latest_cash_date - timedelta(days=days_back)
        if trade_date.weekday() >= 5:
            continue

        frame = download_bhavcopy(session, "fo", trade_date)
        if frame is not None:
            frames.append(frame)
        if len(frames) >= sessions:
            break

    return frames


def fetch_fno_ban_status(session, symbol):
    try:
        def request_ban_status():
            response = session.get(FNO_BAN_URL, headers=HEADERS, timeout=20)
            response.raise_for_status()
            return response

        response = call_with_resilience("NSE", request_ban_status)
        text = response.text.strip()
    except (requests.RequestException, UpstreamUnavailableError):
        return {
            "is_banned": None,
            "trade_date": None,
            "mwpl_utilization_pct": None,
            "source": "NSE F&O security ban file",
            "note": "Ban status is temporarily unavailable.",
        }
    date_match = re.search(r"Trade Date\s+(\d{1,2}-[A-Z]{3}-\d{4})", text, re.I)
    trade_date = None
    if date_match:
        trade_date = pd.to_datetime(date_match.group(1)).date().isoformat()

    lines = [line.strip() for line in text.splitlines()[1:] if line.strip()]
    banned_symbols = {
        value.strip().upper()
        for line in lines
        for value in line.split(",")
        if value.strip() and value.strip().upper() != "NIL"
    }
    return {
        "is_banned": symbol in banned_symbols,
        "trade_date": trade_date,
        "mwpl_utilization_pct": None,
        "source": "NSE F&O security ban file",
        "note": "Exact MWPL utilization is not published in this source.",
    }


def analyze_cash(history):
    closes = history["ClsPric"].astype(float)
    highs = history["HghPric"].astype(float)
    lows = history["LwPric"].astype(float)
    volumes = history["TtlTradgVol"].astype(float)
    latest = history.iloc[-1]
    open_price = float(latest.get("OpnPric", latest["ClsPric"]))
    close = float(latest["ClsPric"])
    previous_close = float(history.iloc[-2]["ClsPric"])
    sma20 = float(closes.tail(20).mean())
    sma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else None
    return_1d = (close / previous_close - 1) * 100
    return_5d = (close / float(closes.iloc[-6]) - 1) * 100
    average_volume = float(volumes.iloc[-21:-1].mean())
    volume_ratio = float(latest["TtlTradgVol"]) / average_volume
    prior_twenty_day_high = float(highs.iloc[-21:-1].max())
    prior_twenty_day_low = float(lows.iloc[-21:-1].min())
    ten_day_low = float(lows.tail(10).min())
    twenty_day_low = float(lows.tail(20).min())

    previous_closes = closes.shift(1)
    true_ranges = pd.concat(
        [
            highs - lows,
            (highs - previous_closes).abs(),
            (lows - previous_closes).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr14 = float(true_ranges.tail(14).mean())
    gap_pct = (open_price / previous_close - 1) * 100
    gap_atr = (open_price - previous_close) / atr14 if atr14 > 0 else None
    gap_up_extended = gap_atr is not None and gap_atr >= 1.25
    traded_values = closes * volumes
    average_traded_value_crore = float(traded_values.tail(20).mean() / 10_000_000)
    if average_traded_value_crore >= 500:
        liquidity_tier, estimated_slippage_bps = "high", 4
    elif average_traded_value_crore >= 100:
        liquidity_tier, estimated_slippage_bps = "good", 8
    elif average_traded_value_crore >= 25:
        liquidity_tier, estimated_slippage_bps = "moderate", 15
    else:
        liquidity_tier, estimated_slippage_bps = "low", 30

    recent_swing_low = None
    pivot_search_start = max(1, len(history) - 21)
    for index in range(len(history) - 2, pivot_search_start - 1, -1):
        if lows.iloc[index] < lows.iloc[index - 1] and lows.iloc[index] <= lows.iloc[index + 1]:
            recent_swing_low = float(lows.iloc[index])
            break
    if recent_swing_low is None:
        recent_swing_low = float(lows.iloc[-11:-1].min())
    recent_swing_high = float(highs.iloc[-11:-1].max())

    distance_from_sma20_atr = (close - sma20) / atr14 if atr14 > 0 else None
    distance_from_breakout_atr = (
        (close - prior_twenty_day_high) / atr14 if atr14 > 0 else None
    )

    score = 0
    signals = []
    if close > sma20:
        score += 25
        signals.append("Price is above the 20-session average")
    else:
        signals.append("Price is below the 20-session average")
    if sma50 is not None and close > sma50:
        score += 20
        signals.append("Price is above the 50-session average")
    if sma50 is not None and sma20 > sma50:
        score += 15
        signals.append("20-session average is above the 50-session average")
    if return_5d > 0:
        score += 15
        signals.append("Five-session momentum is positive")
    if volume_ratio >= 1.2:
        score += 15
        signals.append("Volume is at least 1.2x its 20-session average")
    elif volume_ratio >= 0.8:
        score += 8
        signals.append("Volume is near its 20-session average")
    if close >= prior_twenty_day_high * 0.98:
        score += 10
        signals.append("Price is within 2% of its prior 20-session high")

    return {
        "score": score,
        "open": open_price,
        "close": close,
        "return_1d": return_1d,
        "return_5d": return_5d,
        "sma20": sma20,
        "sma50": sma50,
        "volume_ratio": volume_ratio,
        "prior_twenty_day_high": prior_twenty_day_high,
        "prior_twenty_day_low": prior_twenty_day_low,
        "ten_day_low": ten_day_low,
        "twenty_day_low": twenty_day_low,
        "atr14": atr14,
        "gap_pct": gap_pct,
        "gap_atr": gap_atr,
        "gap_up_extended": gap_up_extended,
        "gap_down_extended": gap_atr is not None and gap_atr <= -1.25,
        "average_traded_value_crore": average_traded_value_crore,
        "liquidity_tier": liquidity_tier,
        "estimated_slippage_bps": estimated_slippage_bps,
        "recent_swing_low": recent_swing_low,
        "recent_swing_high": recent_swing_high,
        "distance_from_sma20_atr": distance_from_sma20_atr,
        "distance_from_breakout_atr": distance_from_breakout_atr,
        "signals": signals,
    }


def _symbol_contracts(frame, symbol):
    rows = frame[frame["TckrSymb"] == symbol].copy()
    if rows.empty:
        return rows
    rows["XpryDt"] = pd.to_datetime(rows["XpryDt"])
    return rows


def _matching_future(rows, expiry):
    futures = rows[
        (rows["FinInstrmTp"] == "STF") & (rows["XpryDt"] == expiry)
    ]
    return futures.iloc[0] if not futures.empty else None


def _near_atm_options(rows, expiry, spot, strike_count=5):
    options = rows[
        (rows["FinInstrmTp"] == "STO") & (rows["XpryDt"] == expiry)
    ].copy()
    if options.empty:
        return options, None

    options["StrkPric"] = options["StrkPric"].astype(float)
    strikes = sorted(options["StrkPric"].dropna().unique())
    atm_index = min(range(len(strikes)), key=lambda index: abs(strikes[index] - spot))
    selected_strikes = strikes[
        max(0, atm_index - strike_count) : atm_index + strike_count + 1
    ]
    return options[options["StrkPric"].isin(selected_strikes)], strikes[atm_index]


def _pcr_and_walls(options):
    if options.empty:
        return None, None, None
    call_rows = options[options["OptnTp"] == "CE"]
    put_rows = options[options["OptnTp"] == "PE"]
    call_oi = call_rows["OpnIntrst"].astype(float).sum()
    put_oi = put_rows["OpnIntrst"].astype(float).sum()
    pcr = put_oi / call_oi if call_oi > 0 else None
    call_wall = (
        float(call_rows.loc[call_rows["OpnIntrst"].astype(float).idxmax(), "StrkPric"])
        if not call_rows.empty
        else None
    )
    put_wall = (
        float(put_rows.loc[put_rows["OpnIntrst"].astype(float).idxmax(), "StrkPric"])
        if not put_rows.empty
        else None
    )
    return pcr, call_wall, put_wall


def _option_oi_profile(options):
    if options.empty:
        return []
    profile = []
    for strike, rows in options.groupby("StrkPric"):
        call_oi = rows.loc[rows["OptnTp"] == "CE", "OpnIntrst"].astype(float).sum()
        put_oi = rows.loc[rows["OptnTp"] == "PE", "OpnIntrst"].astype(float).sum()
        profile.append({
            "strike": float(strike),
            "call_oi": float(call_oi),
            "put_oi": float(put_oi),
        })
    return sorted(profile, key=lambda row: row["strike"])


def _rollover_proxy(rows, previous_rows, expiries):
    if len(expiries) < 2:
        return None, None

    def next_expiry_share(source):
        futures = source[
            (source["FinInstrmTp"] == "STF") & source["XpryDt"].isin(expiries[:2])
        ]
        oi_by_expiry = futures.groupby("XpryDt")["OpnIntrst"].sum().astype(float)
        total = oi_by_expiry.sum()
        return float(oi_by_expiry.get(expiries[1], 0.0) / total * 100) if total > 0 else None

    current_share = next_expiry_share(rows)
    previous_share = next_expiry_share(previous_rows) if not previous_rows.empty else None
    change = (
        current_share - previous_share
        if current_share is not None and previous_share is not None
        else None
    )
    return current_share, change


def analyze_fno(symbol, frames, ban_status=None):
    if not frames:
        return None

    symbol_rows = _symbol_contracts(frames[0], symbol)
    if symbol_rows.empty:
        return None
    previous_rows = (
        _symbol_contracts(frames[1], symbol) if len(frames) > 1 else pd.DataFrame()
    )

    futures = symbol_rows[symbol_rows["FinInstrmTp"] == "STF"].copy()
    if futures.empty:
        return None

    expiries = sorted(futures["XpryDt"].unique())
    nearest_expiry = expiries[0]
    future = _matching_future(symbol_rows, nearest_expiry)
    previous_future = _matching_future(previous_rows, nearest_expiry)
    price_change = (
        (float(future["ClsPric"]) / float(future["PrvsClsgPric"]) - 1) * 100
    )
    current_oi = float(future["OpnIntrst"])
    previous_oi = float(previous_future["OpnIntrst"]) if previous_future is not None else None
    oi_change_pct = (
        (current_oi / previous_oi - 1) * 100
        if previous_oi is not None and previous_oi > 0
        else None
    )
    classification_oi_change = (
        oi_change_pct
        if oi_change_pct is not None
        else (
            float(future["ChngInOpnIntrst"])
            / max(current_oi - float(future["ChngInOpnIntrst"]), 1)
            * 100
        )
    )

    if price_change > 0 and classification_oi_change > 0:
        build_up, futures_score = "Long build-up", 85
    elif price_change > 0:
        build_up, futures_score = "Short covering", 70
    elif classification_oi_change > 0:
        build_up, futures_score = "Short build-up", 25
    else:
        build_up, futures_score = "Long unwinding", 40

    spot = float(future["UndrlygPric"])
    near_options, atm_strike = _near_atm_options(
        symbol_rows, nearest_expiry, spot
    )
    previous_near_options, _ = _near_atm_options(
        previous_rows, nearest_expiry, spot
    ) if not previous_rows.empty else (pd.DataFrame(), None)
    pcr, call_oi_wall, put_oi_wall = _pcr_and_walls(near_options)
    option_oi_profile = _option_oi_profile(near_options)
    previous_pcr, _, _ = _pcr_and_walls(previous_near_options)
    pcr_change = (
        pcr - previous_pcr
        if pcr is not None and previous_pcr is not None
        else None
    )
    if pcr_change is None:
        pcr_trend = "unavailable"
    elif pcr_change > 0.05:
        pcr_trend = "rising"
    elif pcr_change < -0.05:
        pcr_trend = "falling"
    else:
        pcr_trend = "stable"
    pcr_score = 50
    if pcr is not None:
        if 0.9 <= pcr <= 1.3:
            pcr_score = 80
        elif pcr < 0.7:
            pcr_score = 30
        elif pcr > 1.5:
            pcr_score = 45
        else:
            pcr_score = 60

    futures_volume = float(future["TtlTradgVol"])
    previous_volume = (
        float(previous_future["TtlTradgVol"])
        if previous_future is not None
        else None
    )
    futures_volume_change_pct = (
        (futures_volume / previous_volume - 1) * 100
        if previous_volume is not None and previous_volume > 0
        else None
    )
    total_futures_volume = futures["TtlTradgVol"].astype(float).sum()
    front_volume_share_pct = (
        futures_volume / total_futures_volume * 100
        if total_futures_volume > 0
        else None
    )
    rollover_share, rollover_change = _rollover_proxy(
        symbol_rows, previous_rows, expiries
    )
    basis = float(future["ClsPric"]) - spot
    futures_transactions = int(future["TtlNbOfTxsExctd"])
    futures_liquidity_tier = (
        "high" if futures_volume >= 5000 and futures_transactions >= 1000
        else "good" if futures_volume >= 1000 and futures_transactions >= 250
        else "moderate" if futures_volume >= 250 and futures_transactions >= 50
        else "low"
    )

    return {
        "score": futures_score * 0.65 + pcr_score * 0.35,
        "expiry": nearest_expiry.date().isoformat(),
        "current_session": str(frames[0]["TradDt"].iloc[0]),
        "previous_session": str(frames[1]["TradDt"].iloc[0]) if len(frames) > 1 else None,
        "futures_price_change": price_change,
        "oi_change_pct": oi_change_pct,
        "build_up": build_up,
        "pcr": pcr,
        "previous_pcr": previous_pcr,
        "pcr_change": pcr_change,
        "pcr_trend": pcr_trend,
        "atm_strike": atm_strike,
        "near_atm_strike_count": int(near_options["StrkPric"].nunique()) if not near_options.empty else 0,
        "call_oi_wall": call_oi_wall,
        "put_oi_wall": put_oi_wall,
        "option_oi_profile": option_oi_profile,
        "futures_volume": futures_volume,
        "futures_volume_change_pct": futures_volume_change_pct,
        "futures_traded_value": float(future["TtlTrfVal"]),
        "futures_transactions": futures_transactions,
        "futures_liquidity_tier": futures_liquidity_tier,
        "front_volume_share_pct": front_volume_share_pct,
        "next_expiry_oi_share_pct": rollover_share,
        "next_expiry_oi_share_change_pp": rollover_change,
        "basis": basis,
        "basis_pct": basis / spot * 100 if spot > 0 else None,
        "ban_status": ban_status,
    }


def directional_cash_score(cash, mode):
    if mode == "bullish":
        return cash["score"]
    score = 0
    score += 25 if cash["close"] < cash["sma20"] else 0
    score += 20 if cash["sma50"] is not None and cash["close"] < cash["sma50"] else 0
    score += 15 if cash["sma50"] is not None and cash["sma20"] < cash["sma50"] else 0
    score += 15 if cash["return_5d"] < 0 else 0
    score += 15 if cash["volume_ratio"] >= 1.2 else 8 if cash["volume_ratio"] >= 0.8 else 0
    score += 10 if cash["close"] <= cash["prior_twenty_day_low"] * 1.02 else 0
    return score


def directional_fno_score(fno, mode):
    if fno is None or mode == "bullish":
        return fno["score"] if fno is not None else None
    futures_score = {
        "Short build-up": 85,
        "Long unwinding": 70,
        "Short covering": 35,
        "Long build-up": 20,
    }.get(fno["build_up"], 50)
    pcr = fno["pcr"]
    pcr_score = 50 if pcr is None else 85 if pcr < 0.7 else 70 if pcr < 0.9 else 45 if pcr <= 1.3 else 25
    return futures_score * 0.65 + pcr_score * 0.35


def make_assessment(cash, fno, fundamental_score, mode="bullish"):
    cash_score = directional_cash_score(cash, mode)
    fno_score = directional_fno_score(fno, mode)
    components = [(cash_score, 0.8 if fno is None else 0.5)]
    if fno is None:
        if fundamental_score is not None and mode == "bullish":
            components.append((fundamental_score * 10, 0.2))
    else:
        components.append((fno_score, 0.35))
        if fundamental_score is not None and mode == "bullish":
            components.append((fundamental_score * 10, 0.15))

    total_weight = sum(weight for _, weight in components)
    score = sum(value * weight for value, weight in components) / total_weight

    if score >= 75:
        verdict = "FAVORABLE SETUP"
    elif score >= 60:
        verdict = "WATCH FOR CONFIRMATION"
    else:
        verdict = "AVOID NEW ENTRY"
    return round(score, 1), verdict


def build_pros_cons(cash, fno, fundamental_score, mode="bullish"):
    pros = []
    cons = []
    bullish = mode == "bullish"

    trend_supports = cash["close"] > cash["sma20"] if bullish else cash["close"] < cash["sma20"]
    (pros if trend_supports else cons).append(
        "Price is above the 20-session average"
        if cash["close"] > cash["sma20"]
        else "Price is below the 20-session average"
    )
    if cash["sma50"] is not None:
        trend_supports = cash["close"] > cash["sma50"] if bullish else cash["close"] < cash["sma50"]
        (pros if trend_supports else cons).append(
            "Price is above the 50-session average"
            if cash["close"] > cash["sma50"]
            else "Price is below the 50-session average"
        )
    momentum_supports = cash["return_5d"] > 0 if bullish else cash["return_5d"] < 0
    (pros if momentum_supports else cons).append(
        "Five-session momentum is positive"
        if cash["return_5d"] > 0
        else "Five-session momentum is negative"
    )
    (pros if cash["volume_ratio"] >= 1.2 else cons).append(
        "Cash volume is at least 1.2x its 20-session average"
        if cash["volume_ratio"] >= 1.2
        else "Cash volume lacks a 1.2x expansion confirmation"
    )
    directional_gap_extended = cash["gap_up_extended"] if bullish else cash["gap_down_extended"]
    if directional_gap_extended:
        cons.append(
            f"Opening gap is extended at {abs(cash['gap_atr']):.1f} ATR; avoid chasing"
        )
    if cash["liquidity_tier"] == "low":
        cons.append(
            f"Low cash turnover implies about {cash['estimated_slippage_bps']} bps slippage"
        )
    else:
        pros.append(
            f"Cash liquidity is {cash['liquidity_tier']} with estimated slippage near "
            f"{cash['estimated_slippage_bps']} bps"
        )

    if fno is None:
        cons.append("No listed NSE F&O contract; derivatives confirmation is unavailable")
    else:
        favorable_builds = {"Long build-up", "Short covering"} if bullish else {"Short build-up", "Long unwinding"}
        (pros if fno["build_up"] in favorable_builds else cons).append(
            f"Futures positioning shows {fno['build_up'].lower()}"
        )
        if fno["pcr"] is None:
            cons.append("Near-ATM put-call ratio is unavailable")
        elif bullish and 0.9 <= fno["pcr"] <= 1.3:
            pros.append("Near-ATM PCR is in the balanced bullish range of 0.9-1.3")
        elif not bullish and fno["pcr"] < 0.9:
            pros.append("Near-ATM PCR is call-heavy and supports the bearish setup")
        else:
            cons.append(f"Near-ATM PCR does not support the {mode} setup")
        if fno["ban_status"]["is_banned"] is True:
            cons.append("The stock is currently in the NSE F&O ban list")
        if fno["futures_liquidity_tier"] == "low":
            cons.append("Front-month futures liquidity is low")

    if fundamental_score is None and bullish:
        cons.append("Fundamental data is incomplete and excluded from the composite")
    elif bullish and fundamental_score >= 6:
        pros.append("Fundamental score is at least 6/10")
    elif bullish and fundamental_score < 5:
        cons.append("Fundamental score is below 5/10")
    elif not bullish and fundamental_score is not None and fundamental_score >= 6:
        cons.append("Strong fundamentals may oppose the bearish setup")
    elif not bullish and fundamental_score is not None and fundamental_score < 5:
        pros.append("Weak fundamentals are consistent with the bearish setup")

    return pros, cons


def build_daily_change(current_score, current_pros, current_cons, previous_date,
                       previous_cash, previous_fno, fundamental_score, mode="bullish"):
    previous_score, _ = make_assessment(
        previous_cash, previous_fno, fundamental_score, mode
    )
    previous_pros, previous_cons = build_pros_cons(
        previous_cash, previous_fno, fundamental_score, mode
    )
    return {
        "previous_date": previous_date.isoformat(),
        "previous_score": previous_score,
        "score_change": round(current_score - previous_score, 1),
        "added_positive": [signal for signal in current_pros if signal not in previous_pros],
        "removed_positive": [signal for signal in previous_pros if signal not in current_pros],
        "added_risks": [signal for signal in current_cons if signal not in previous_cons],
        "resolved_risks": [signal for signal in previous_cons if signal not in current_cons],
    }


def build_trade_plan(cash, score, mode="bullish"):
    atr = cash["atr14"]
    if atr <= 0:
        return None

    if mode == "bearish":
        breakdown_level = cash["prior_twenty_day_low"] - atr * 0.1
        entry_valid = (
            score >= 75
            and cash["close"] <= breakdown_level
            and cash["volume_ratio"] >= 1.2
            and not cash["gap_down_extended"]
            and cash["liquidity_tier"] != "low"
        )
        entry_high = min(breakdown_level, cash["close"] + atr * 0.25) if entry_valid else breakdown_level
        entry_low = cash["close"] - atr * 0.25 if entry_valid else breakdown_level - atr * 0.5
        entry_reference = entry_low
        stop_loss = max(
            entry_high + atr * 0.5,
            min(entry_reference + atr * 1.5, cash["recent_swing_high"] + atr * 0.25),
        )
        risk_per_share = stop_loss - entry_reference
        return {
            "direction": "short",
            "status": (
                "Entry valid now" if entry_valid
                else "Wait for bounce" if cash["gap_down_extended"]
                else "Wait for breakdown"
            ),
            "entry_valid": entry_valid,
            "entry_low": round(entry_low, 2),
            "entry_high": round(entry_high, 2),
            "entry_reference": round(entry_reference, 2),
            "stop_loss": round(stop_loss, 2),
            "risk_per_share": round(risk_per_share, 2),
            "targets": [
                round(entry_reference - risk_per_share * 1.5, 2),
                round(entry_reference - risk_per_share * 2.5, 2),
            ],
            "risk_reward_ratio": 2.5,
            "invalidation": (
                f"Daily close above INR {stop_loss:.2f}, or a breakdown close back above "
                f"INR {cash['prior_twenty_day_low']:.2f}."
            ),
        }

    breakout_level = cash["prior_twenty_day_high"] + atr * 0.1
    entry_valid = (
        score >= 75
        and cash["close"] >= breakout_level
        and cash["volume_ratio"] >= 1.2
        and not cash["gap_up_extended"]
        and cash["liquidity_tier"] != "low"
    )
    if entry_valid:
        entry_low = max(breakout_level, cash["close"] - atr * 0.25)
        entry_high = cash["close"] + atr * 0.25
    else:
        entry_low = breakout_level
        entry_high = breakout_level + atr * 0.5

    entry_reference = entry_high
    atr_stop = entry_reference - atr * 1.5
    structural_stop = cash["recent_swing_low"] - atr * 0.25
    stop_loss = min(max(atr_stop, structural_stop), entry_low - atr * 0.5)
    risk_per_share = entry_reference - stop_loss
    target_one = entry_reference + risk_per_share * 1.5
    target_two = entry_reference + risk_per_share * 2.5

    invalidation = (
        f"Daily close below INR {stop_loss:.2f}, or a breakout close back below "
        f"INR {cash['prior_twenty_day_high']:.2f}."
        if entry_valid
        else (
            f"Daily close below the 20-session average at INR {cash['sma20']:.2f} "
            f"before a favorable, volume-confirmed close above INR {breakout_level:.2f}."
        )
    )
    return {
        "direction": "long",
        "status": (
            "Entry valid now" if entry_valid
            else "Wait for pullback" if cash["gap_up_extended"]
            else "Wait for breakout"
        ),
        "entry_valid": entry_valid,
        "entry_low": round(entry_low, 2),
        "entry_high": round(entry_high, 2),
        "entry_reference": round(entry_reference, 2),
        "stop_loss": round(stop_loss, 2),
        "risk_per_share": round(risk_per_share, 2),
        "targets": [round(target_one, 2), round(target_two, 2)],
        "risk_reward_ratio": 2.5,
        "invalidation": invalidation,
    }


def build_symbol_report(symbol, mode="bullish"):
    clean_symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9&-]{1,20}", clean_symbol):
        raise ValueError("Enter a valid NSE ticker symbol")
    if mode not in {"bullish", "bearish"}:
        raise ValueError("Enter a valid setup mode")

    with requests.Session() as session:
        history = load_recent_symbol_history(session, clean_symbol, sessions=51)
        latest_date = pd.to_datetime(history.iloc[-1]["TradDt"]).date()
        fno_frames = load_recent_fno_frames(session, latest_date, sessions=3)
        ban_status = fetch_fno_ban_status(session, clean_symbol)

    cash = analyze_cash(history)
    fno = analyze_fno(clean_symbol, fno_frames, ban_status)
    fundamentals = fetch_screener_data(clean_symbol)
    fundamental_result = calculate_fundamental_score(fundamentals)
    fundamental_score = fundamental_result["score"]
    fundamental_tags = fundamental_result["tags"]
    news = fetch_company_news(clean_symbol, fundamentals.get("name"))
    score, verdict = make_assessment(cash, fno, fundamental_score, mode)
    pros, cons = build_pros_cons(cash, fno, fundamental_score, mode)
    event_risk = news.get("event_risk", {})
    if event_risk.get("detected"):
        labels = ", ".join(category.replace("_", " ") for category in event_risk["categories"])
        cons.append(
            f"Potentially material event headlines detected: {labels}"
        )
    trade_plan = build_trade_plan(cash, score, mode)
    daily_change = None
    if len(history) >= 51:
        previous_history = history.iloc[:-1].reset_index(drop=True)
        previous_date = pd.to_datetime(previous_history.iloc[-1]["TradDt"]).date()
        previous_cash = analyze_cash(previous_history)
        previous_fno = analyze_fno(clean_symbol, fno_frames[1:], ban_status)
        if (fno is None) == (previous_fno is None):
            daily_change = build_daily_change(
                score,
                pros,
                cons,
                previous_date,
                previous_cash,
                previous_fno,
                fundamental_score,
                mode,
            )

    cash_weight = 0.8 if fno is None else 0.5
    fno_weight = 0.35 if fno is not None else 0
    fundamental_weight = 0 if mode == "bearish" else (
        0.2 if fno is None and fundamental_score is not None
        else 0.15 if fundamental_score is not None
        else 0
    )
    cash_score = directional_cash_score(cash, mode)
    fno_score = directional_fno_score(fno, mode)
    weighted_total = (
        cash_score * cash_weight
        + (fno_score * fno_weight if fno is not None else 0)
        + (fundamental_score * 10 * fundamental_weight if fundamental_score is not None else 0)
    )
    total_weight = cash_weight + fno_weight + fundamental_weight

    return {
        "symbol": clean_symbol,
        "setup_mode": mode,
        "data_through": latest_date.isoformat(),
        "score": score,
        "verdict": verdict,
        "confidence": "High" if score >= 75 else "Moderate" if score >= 60 else "Low",
        "cash": cash,
        "fno": fno,
        "fundamentals": fundamentals,
        "fundamental_assessment": fundamental_result,
        "news": news,
        "pros": pros,
        "cons": cons,
        "daily_change": daily_change,
        "trade_plan": trade_plan,
        "calculation": {
            "cash_score": cash_score,
            "cash_weight": cash_weight,
            "fno_score": fno_score,
            "fno_weight": fno_weight,
            "fundamental_score_100": (
                fundamental_score * 10 if fundamental_score is not None else None
            ),
            "fundamental_weight": fundamental_weight,
            "weighted_total": weighted_total,
            "total_weight": total_weight,
            "formula": "weighted component total / active component weight",
        },
        "disclaimer": "Educational analysis only. Not financial advice.",
    }


def analyze_symbol(symbol):
    report = build_symbol_report(symbol)
    clean_symbol = report["symbol"]
    latest_date = report["data_through"]
    cash = report["cash"]
    fno = report["fno"]
    fundamentals = report["fundamentals"]
    fundamental_result = report["fundamental_assessment"]
    fundamental_score = fundamental_result["score"]
    fundamental_tags = fundamental_result["tags"]
    score = report["score"]
    verdict = report["verdict"]

    print("\n" + "=" * 64)
    print(f" NSE SWING TRADE CHECK: {clean_symbol} | Data through {latest_date}")
    print("=" * 64)
    print(f"Close: Rs {cash['close']:.2f} | 1D: {cash['return_1d']:+.2f}% | 5D: {cash['return_5d']:+.2f}%")
    sma50 = f"{cash['sma50']:.2f}" if cash["sma50"] is not None else "N/A"
    print(f"SMA20: {cash['sma20']:.2f} | SMA50: {sma50} | Volume: {cash['volume_ratio']:.2f}x average")
    print(
        f"Prior 20D high: {cash['prior_twenty_day_high']:.2f} | "
        f"10D low: {cash['ten_day_low']:.2f} | 20D low: {cash['twenty_day_low']:.2f}"
    )
    print(
        f"ATR14: {cash['atr14']:.2f} | Recent swing low: {cash['recent_swing_low']:.2f} | "
        f"From SMA20: {cash['distance_from_sma20_atr']:+.2f} ATR | "
        f"From breakout: {cash['distance_from_breakout_atr']:+.2f} ATR"
    )

    print("\nCash-market signals:")
    for signal in cash["signals"]:
        print(f"  - {signal}")

    print("\nF&O signals:")
    if fno is None:
        print("  - No NSE F&O contract found for this symbol")
        print("  - Treat this as a cash-market swing only; PCR and futures OI do not apply")
    else:
        def display(value, precision=2, signed=False, suffix=""):
            if value is None or pd.isna(value):
                return "N/A"
            sign = "+" if signed else ""
            return f"{value:{sign}.{precision}f}{suffix}"

        pcr = f"{fno['pcr']:.2f}" if fno["pcr"] is not None else "N/A"
        previous_pcr = f"{fno['previous_pcr']:.2f}" if fno["previous_pcr"] is not None else "N/A"
        pcr_change = f"{fno['pcr_change']:+.2f}" if fno["pcr_change"] is not None else "N/A"
        oi_change = f"{fno['oi_change_pct']:+.2f}%" if fno["oi_change_pct"] is not None else "N/A"
        print(f"  - Nearest futures expiry: {fno['expiry']}")
        print(f"  - Futures: {fno['futures_price_change']:+.2f}%")
        print(f"  - Open interest across sessions: {oi_change} ({fno['build_up']})")
        print(f"  - Near-ATM PCR: {pcr} | Previous: {previous_pcr} | Change: {pcr_change} ({fno['pcr_trend']})")
        print(
            f"  - ATM: {display(fno['atm_strike'])} | "
            f"Put OI wall: {display(fno['put_oi_wall'])} | "
            f"Call OI wall: {display(fno['call_oi_wall'])} "
            f"({fno['near_atm_strike_count']} strikes)"
        )
        volume_change = f"{fno['futures_volume_change_pct']:+.2f}%" if fno["futures_volume_change_pct"] is not None else "N/A"
        print(
            f"  - Front futures liquidity: volume {fno['futures_volume']:.0f} "
            f"({volume_change}), value Rs {fno['futures_traded_value']:.0f}, "
            f"transactions {fno['futures_transactions']}, "
            f"expiry volume share {display(fno['front_volume_share_pct'], 1, suffix='%')}"
        )
        rollover_change = f"{fno['next_expiry_oi_share_change_pp']:+.2f} pp" if fno["next_expiry_oi_share_change_pp"] is not None else "N/A"
        print(
            f"  - Rollover proxy: next-expiry OI share "
            f"{display(fno['next_expiry_oi_share_pct'], 1, suffix='%')} "
            f"| Change {rollover_change}"
        )
        print(
            f"  - Futures basis: Rs {display(fno['basis'], signed=True)} "
            f"({display(fno['basis_pct'], signed=True, suffix='%')})"
        )
        status = fno["ban_status"]
        ban_label = (
            "YES" if status["is_banned"] is True
            else "No" if status["is_banned"] is False
            else "N/A"
        )
        print(
            f"  - F&O ban: {ban_label} for {status['trade_date'] or 'N/A'} "
            f"| MWPL utilization: N/A"
        )

    print("\nFundamentals:")
    for label, field in (
        ("P/E", "pe_ratio"),
        ("P/B", "pb_ratio"),
        ("ROCE", "roce"),
        ("ROE", "roe"),
        ("Debt/equity", "de_ratio"),
    ):
        value = fundamentals.get(field)
        print(f"  - {label}: {value if value is not None else 'N/A'}")
    completeness = fundamental_result["completeness"]
    print(f"  - Data completeness: {completeness['available']}/{completeness['total']}")
    print(f"  - Scoring profile: {fundamental_result['profile']}")
    if fundamental_score is None:
        print("  - Score: N/A (insufficient data; excluded from composite)")
    else:
        print(f"  - Score: {fundamental_score:.1f}/10")
    for tag in fundamental_tags or ["Moderate / mixed fundamentals"]:
        print(f"  - {tag}")

    print("\nAssessment:")
    print(f"  {verdict} | Composite score: {score}/100")
    if fno is None:
        print("  This is not an NSE F&O trade because no derivative contract is listed.")
    print("  Use a stop-loss and position sizing; this is educational, not financial advice.")
    print("=" * 64 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze an NSE symbol using official bhavcopies and Screener.in."
    )
    parser.add_argument("symbol", nargs="?", default="HINDCOPPER")
    args = parser.parse_args()
    analyze_symbol(args.symbol)


if __name__ == "__main__":
    main()