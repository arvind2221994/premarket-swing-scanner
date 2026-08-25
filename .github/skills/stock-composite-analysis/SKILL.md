---
name: stock-composite-analysis
description: 'Run a complete live NSE swing-trade analysis for any user-provided stock ticker using cash price trend, volume, available futures and options positioning, Screener.in fundamentals, and global market cues. Use when asked whether an NSE stock is a worthy swing trade, to analyze a ticker, check a trade setup, or produce a composite stock assessment.'
argument-hint: '<NSE ticker, for example OPTIEMUS or RELIANCE>'
user-invocable: true
disable-model-invocation: false
---

# Stock Composite Analysis

Analyze one NSE equity ticker with the repository's live-data scripts and turn the results into a concise, risk-aware swing-trade assessment.

## Inputs

- Require one NSE ticker from the user.
- Normalize it with `strip().upper()` semantics.
- Accept only a plausible ticker containing letters, digits, `&`, or `-`. Ask for clarification instead of placing other user input in a shell command.

## Procedure

1. Run from the repository root:

   ```powershell
   python scanner/fno_trade_analyzer.py <TICKER>
   ```

   This is the primary analysis. It downloads recent official NSE cash bhavcopies, detects available NSE derivatives, evaluates trend and volume, and calls `fetch_screener_data` plus `calculate_fundamental_score` from `scanner/fundamentals.py`.

2. Fetch the current Indian market regime and global-market overlay:

   ```powershell
   python scanner/market_context.py
   ```

   Use the per-source `observed_at` and `fetched_at` values to identify stale or
   cross-session data. The report includes Nifty trend versus SMA20/SMA50, India
   VIX, NSE market breadth, major sector-index trends, USD/INR, WTI crude, and
   the existing US-index and GIFT Nifty cues.

3. If a command fails, report the exact failed data source or dependency. Do not replace missing live values with estimates. A missing GIFT Nifty or US-index value lowers confidence but does not invalidate the NSE cash analysis.

4. Do not run `scanner/scanner.py` for a ticker assessment. Its stock universe is mock data and does not contain the requested live ticker.

5. Treat the analyzer score as the repository's primary heuristic. Do not interpret it as a probability of success. Keep global cues as a separate overlay; do not silently recalculate or blend a new composite formula.

6. When calibration evidence is requested, run a representative multi-stock backtest:

   ```powershell
   python scanner/backtest_score_buckets.py RELIANCE TCS HDFCBANK INFY --start 2021-01-01 --horizon 10
   ```

   Report the sample size, win rate, expectancy, average maximum drawdown, and
   false-breakout rate for each score bucket and Nifty regime. This script tests
   only the cash technical heuristic because point-in-time fundamentals and
   historical F&O inputs are not available. Never describe its results as a
   backtest of the full live composite.

## Interpretation

- `FAVORABLE SETUP` (score at least 75): technically worthy of consideration, subject to entry quality and risks.
- `WATCH FOR CONFIRMATION` (60 to 74.9): wait for breakout, retest, volume, or trend confirmation.
- `AVOID NEW ENTRY` (below 60): current evidence does not support a new swing position.
- No F&O contract means this is a cash-only setup. Do not infer PCR, futures positioning, or F&O conviction.
- For F&O stocks, use the two aligned sessions to report futures OI trend and near-ATM PCR change. Interpret the call/put OI walls only as positioning concentrations, not guaranteed resistance or support.
- Treat the next-expiry OI share as a rollover proxy, not an exchange-certified rollover percentage. Report futures volume, traded value, transactions, expiry volume share, and basis before assigning conviction.
- Report the official F&O ban-file date separately from the bhavcopy date. `MWPL utilization: N/A` means the ban source does not publish an exact percentage; do not estimate it.
- A large one-day gain or extreme volume can validate momentum while also making an immediate entry vulnerable to chasing. Explicitly distinguish setup quality from entry quality.
- Weak fundamentals or expensive multiples reduce conviction and favor a shorter holding period, tighter risk control, and smaller sizing; they do not automatically negate strong swing momentum.
- Report fundamental data completeness and the actual P/E, P/B, ROCE, ROE, and debt/equity values. When the fundamental score is unavailable, state that it was excluded and the remaining composite weights were normalized.
- Market regime and global cues are supportive, neutral, or adverse context. Do not let them override stock-specific evidence without explaining why.
- Use the matching sector-index trend when the stock's sector is clear. Treat USD/INR and crude as relevant only when the company or sector has material currency, import-cost, or commodity-price exposure.
- Compare source observation dates before interpreting cues together. Explicitly lower confidence when values represent different sessions.
- Check the printed `Data through` date. State clearly when the latest NSE session is stale or when the market has not yet produced a newer completed session.

## Response Format

Return these sections with actual values from the scripts:

1. **Verdict**: `WORTH CONSIDERING`, `WAIT FOR CONFIRMATION`, or `AVOID FOR NOW`, plus one sentence explaining whether an entry now would be a chase.
2. **Composite evidence**: heuristic score, data-through date, close, 1-day and 5-day returns, SMA20, SMA50, volume ratio, and fundamental metrics, completeness, profile, score, and tags.
3. **F&O positioning**: aligned session dates, futures OI trend, near-ATM current/previous PCR and change, ATM call/put OI walls, contract liquidity, next-expiry OI-share rollover proxy, futures basis, and dated ban/MWPL status. Label unavailable values.
4. **Market overlay**: Nifty trend versus SMA20/SMA50, India VIX and daily change, breadth, matching sector trend, relevant USD/INR or crude context, NASDAQ, S&P 500, Dow, GIFT Nifty, and the global-cues score. Include observation times and label missing or cross-session values.
5. **Trade conditions**: derive a nearby confirmation or invalidation reference from reported technical levels. Do not invent a precise target, stop-loss, support, or resistance that the scripts did not calculate.
6. **Risks and confidence**: mention absent F&O confirmation, weak fundamentals, extreme recent movement, stale/missing data, and event risk when applicable. The scripts do not fetch corporate events, so explicitly label event risk as unchecked.

End with a short statement that the result is educational analysis, not personalized financial advice. Never present the output as a guarantee or direct instruction to buy or sell.