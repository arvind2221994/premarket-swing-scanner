# Premarket Swing Scanner

This project produces bullish and bearish NSE swing-trade assessments from cash-market history, futures and options positioning, global-market cues, liquidity, and recent event risk. The resulting scores and trade plans are deterministic calculations, not prices or recommendations supplied by NSE or another trade-plan API.

## Data sources

| Input | Source |
| --- | --- |
| Cash OHLC and volume | Official NSE capital-market (`cm`) daily bhavcopy |
| Futures price, futures open interest, options open interest | Official NSE derivatives (`fo`) daily bhavcopy |
| F&O ban status | Official NSE F&O security-ban file |
| NASDAQ, S&P 500, and Dow changes | Yahoo Finance, with Stooq fallback |
| GIFT Nifty change | NSE International Exchange market-watch endpoint |
| Company fundamentals | Screener.in |
| Potential event risk | Google News RSS, India and US editions |

The source loaders are implemented in [`scanner/fno_trade_analyzer.py`](scanner/fno_trade_analyzer.py), [`scanner/global_cues.py`](scanner/global_cues.py), [`scanner/fundamentals.py`](scanner/fundamentals.py), and [`scanner/news.py`](scanner/news.py).

## Composite score

Each component is scored on a nominal 0-100 scale. The final score is the weighted sum:

$$
S = 0.35S_{F&O} + 0.20S_{trend} + 0.10S_{volume}
  + 0.10S_{global} + 0.10S_{relative} + 0.15S_{risk}
$$

The component thresholds and weights are defined in [`scanner/scoring.py`](scanner/scoring.py). They are project heuristics, not exchange-defined measures and not calibrated probabilities of trade success.

On the single-stock analysis endpoint, sector-relative strength is not currently supplied to the scorer and therefore receives its neutral default of 50. The scheduled scanner calculates it as the stock's five-session return minus the mapped sector index's five-session return.

Fundamentals are displayed as supporting context but are not included in the composite score.

## Fundamental score

The fundamental score is an explainable screening summary calculated by [`calculate_fundamental_score`](scanner/fundamentals.py#L143). It compresses several differently scaled accounting ratios into a 1-10 indicator and produces descriptive tags for the report. Its purpose is to flag broad valuation, profitability, and balance-sheet strengths or risks for further research. It is not an intrinsic-value estimate, credit rating, or probability of future returns.

The raw values come from Screener.in. Price-to-book is calculated when current price and positive book value are available:

$$
P/B=\frac{CurrentPrice}{BookValuePerShare}
$$

When Screener's quick debt-to-equity ratio is absent, the project derives it from the latest balance sheet:

$$
D/E=\frac{Borrowings}{EquityCapital+Reserves}
$$

### Completeness gate

The scorer first chooses a profile from Screener's broad-sector classification:

- **Industrial:** requires at least three available values among P/E, P/B, ROCE, ROE, and debt/equity.
- **Financial services:** requires at least two available values among P/E, P/B, and ROE.

If the applicable minimum is not met, the score is excluded and the report shows `Insufficient fundamental data`. Dividend yield can affect a valid score but does not count toward this completeness gate.

### Industrial profile

The score starts from a neutral baseline of 5.0 and applies every relevant adjustment:

| Signal | Condition | Adjustment |
| --- | --- | ---: |
| Deep value | P/E < 15 **and** P/B < 1.5 | +1.25 |
| Expensive valuation | P/E > 45 **or** P/B > 12 | -1.00 |
| Exceptional capital efficiency | ROCE >= 20% | +1.25 |
| Adequate capital efficiency | 12% <= ROCE < 20% | +0.50 |
| Poor capital efficiency | ROCE < 8% | -1.00 |
| Strong shareholder return | ROE >= 18% | +0.75 |
| Virtually debt-free | Debt/equity < 0.1 | +1.50 |
| Conservative debt | 0.1 <= debt/equity < 0.5 | +0.75 |
| High leverage | Debt/equity > 1.5 | -1.50 |
| Dividend bonus | Dividend yield >= 2.5% | +0.50 |

There is deliberately no ROCE adjustment from 8% up to but excluding 12%, no valuation adjustment when only one of P/E or P/B is available, and no debt adjustment from 0.5 through 1.5.

### Financial-services profile

Financial companies use a separate profile because leverage is part of a bank or lender's operating model and industrial ROCE/debt thresholds are not directly comparable. The score again begins at 5.0:

| Signal | Condition | Adjustment |
| --- | --- | ---: |
| Moderate earnings multiple | P/E < 15 | +0.75 |
| Expensive earnings multiple | P/E > 30 | -0.75 |
| Moderate book multiple | P/B < 2 | +1.00 |
| Expensive book multiple | P/B > 5 | -1.00 |
| Strong shareholder return | ROE >= 18% | +0.75 |
| Weak shareholder return | ROE < 10% | -0.75 |
| Dividend bonus | Dividend yield >= 2.5% | +0.50 |

ROCE and debt/equity do not affect this profile. After all applicable adjustments, the result is clamped and rounded:

$$
FundamentalScore=round\left(\max(1,\min(10,5+\sum Adjustments)),1\right)
$$

For command-line presentation, 8.0 or above is labelled high confidence, 5.5-7.9 moderate, and below 5.5 weak/high risk. These labels describe the heuristic fundamentals result, not confidence in a price forecast.

### Why these rules exist

The dimensions have conventional financial-analysis motivations:

- P/E and P/B provide earnings- and book-value-relative valuation checks.
- ROCE measures operating efficiency across the capital employed by an industrial business.
- ROE measures returns generated on shareholder equity and remains useful for financial companies.
- Debt/equity provides an industrial-company leverage check.
- Dividend yield adds a small shareholder-distribution signal.

The exact thresholds and adjustments in the tables are project-authored heuristics. The repository contains no cited study, fitted model, or historical calibration demonstrating that, for example, P/E 15, ROCE 20%, or a +1.25 adjustment is optimal for NSE swing trades. They should therefore be treated as transparent defaults to validate by sector and market regime, not as rules supplied by Screener.in or established by the references below.

## ATR(14)

For session $t$, True Range is:

$$
TR_t = \max\left(
H_t-L_t,
\left|H_t-C_{t-1}\right|,
\left|L_t-C_{t-1}\right|
\right)
$$

The project then calculates ATR as the arithmetic mean of the latest 14 True Range values:

$$
ATR_{14} = \frac{1}{14}\sum_{i=0}^{13}TR_{t-i}
$$

This True Range definition comes from J. Welles Wilder. The current implementation uses a rolling simple average; after initialization, Wilder's original ATR uses recursive smoothing instead. See [`analyze_cash`](scanner/fno_trade_analyzer.py#L237) for the implementation.

## Trade-plan formulas

The trade plan is calculated by [`build_trade_plan`](scanner/fno_trade_analyzer.py#L733) from NSE-derived price history and the composite score. The API returns the calculated result; no external provider supplies entry, stop, or target levels.

Definitions used below:

- $A$: ATR(14)
- $C$: latest close
- $H_{20}$: highest high in the 20 sessions preceding the latest session
- $L_{20}$: lowest low in the 20 sessions preceding the latest session
- $H_s$ and $L_s$: most recent detected swing high and swing low
- $E_l$ and $E_h$: lower and upper boundaries of the entry zone

### Bullish setup

The buffered breakout level is:

$$
B = H_{20} + 0.1A
$$

An entry is valid now only when all of these conditions hold:

- composite score is at least 75;
- $C \ge B$;
- latest volume is at least 1.2 times its preceding 20-session average;
- the opening gap is less than $1.25A$; and
- cash liquidity is not classified as low.

When entry is valid:

$$
E_l=\max(B,C-0.25A), \qquad E_h=C+0.25A
$$

While waiting for a breakout:

$$
E_l=B, \qquad E_h=B+0.5A
$$

The upper entry boundary is used as the conservative entry reference, $E=E_h$. Two stop candidates are calculated:

$$
Stop_{ATR}=E-1.5A, \qquad Stop_{structure}=L_s-0.25A
$$

The final stop is:

$$
Stop=\min\left(\max(Stop_{ATR},Stop_{structure}),E_l-0.5A\right)
$$

This selects the tighter of the ATR and structural candidates, subject to keeping the stop at least $0.5A$ below the entry zone.

### Bearish setup

The buffered breakdown level is:

$$
B = L_{20} - 0.1A
$$

Entry validity mirrors the bullish conditions: score at least 75, $C \le B$, volume at least 1.2 times average, no opening gap below $-1.25A$, and non-low liquidity.

When entry is valid:

$$
E_h=\min(B,C+0.25A), \qquad E_l=C-0.25A
$$

While waiting for a breakdown:

$$
E_h=B, \qquad E_l=B-0.5A
$$

The lower entry boundary is the short entry reference, $E=E_l$. The final stop is:

$$
Stop=\max\left(E_h+0.5A,\min(E+1.5A,H_s+0.25A)\right)
$$

This keeps the stop at least $0.5A$ above the entry zone while combining volatility and recent swing-high references.

### Targets and position size

For a long trade:

$$
R=E-Stop, \qquad T_1=E+1.5R, \qquad T_2=E+2.5R
$$

For a short trade:

$$
R=Stop-E, \qquad T_1=E-1.5R, \qquad T_2=E-2.5R
$$

Position sizing uses the smaller of risk capacity and cash affordability:

$$
RiskBudget=Capital\times\frac{RiskPercent}{100}
$$

$$
Shares=\min\left(
\left\lfloor\frac{RiskBudget}{R}\right\rfloor,
\left\lfloor\frac{Capital}{E}\right\rfloor
\right)
$$

## Formula provenance and limitations

The True Range and ATR concepts are established technical-analysis measures introduced by Wilder. Breakouts based on prior-period highs and lows, volatility-scaled stops, and reward-to-risk targets are common trading-system patterns. However, this project's exact buffers and multipliers (`0.1 ATR`, `0.25 ATR`, `0.5 ATR`, `1.5 ATR`, `1.5R`, and `2.5R`) are custom assumptions. They are not prescribed by Wilder, NSE, or the cited educational references.

These parameters should be treated as hypotheses requiring walk-forward testing with transaction costs, slippage, survivorship-bias controls, and separate out-of-sample validation before they are used for decisions.

## References

1. J. Welles Wilder Jr., *New Concepts in Technical Trading Systems*, Trend Research, 1978. Original source for True Range and Average True Range.
2. [Fidelity Learning Center: Average True Range](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/atr). Overview of ATR, True Range, and volatility-based stop usage.
3. [NSE India: All Reports](https://www.nseindia.com/all-reports). Official exchange reports and daily market files used as the raw cash and derivatives source.
4. [NSE International Exchange](https://www.nseix.com/). Official source used for the GIFT Nifty market-watch snapshot.
5. Richard Donchian, "High Finance in Copper," *Financial Analysts Journal*, 16(6), 1960, pp. 133-142. Early published discussion of rule-based breakout trading.
6. Aswath Damodaran, *Investment Valuation*, 3rd ed., Wiley, 2012. Background on relative valuation using earnings and book-value multiples.
7. Stephen H. Penman, *Financial Statement Analysis and Security Valuation*, 5th ed., McGraw-Hill, 2013. Background on profitability, return on equity, leverage, and valuation-ratio interpretation.
8. [Screener.in](https://www.screener.in/). Upstream source for the company ratios; Screener.in does not define this project's score thresholds or weights.

## Disclaimer

This software is for educational analysis only. Its scores and levels are mechanical heuristics, not financial advice or guarantees of performance.
