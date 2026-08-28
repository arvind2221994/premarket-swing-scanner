import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fundamentals import fetch_screener_data
from fno_trade_analyzer import _option_oi_profile, analyze_cash, build_trade_plan
import backtest_score_buckets
import global_cues
from fallback import _history_frame, _parse_archive_rows, _trend_snapshot
from market_context import (
    SECTOR_TICKERS,
    assess_long_swing_environment,
    build_market_context,
    recover_unavailable_nse_trends,
    recover_unavailable_sector_trends,
)
from news import classify_news_article
from resilience import UpstreamUnavailableError
from scoring import calculate_stock_score, score_detailed_report

scanner_spec = importlib.util.spec_from_file_location(
    "scanner_job", Path(__file__).resolve().parent / "scanner.py"
)
scanner_job = importlib.util.module_from_spec(scanner_spec)
scanner_spec.loader.exec_module(scanner_job)
add_sector_relative_strength = scanner_job.add_sector_relative_strength
select_liquid_fno_symbols = scanner_job.select_liquid_fno_symbols


class FundamentalDataTests(unittest.TestCase):
    @patch("fundamentals.call_with_resilience")
    def test_derives_debt_to_equity_from_latest_balance_sheet(self, request):
        response = Mock(status_code=200)
        response.text = """
            <h1>Example Ltd</h1>
            <ul id="top-ratios">
              <li><span class="name">Current Price</span><span class="number">200</span></li>
              <li><span class="name">Book Value</span><span class="number">100</span></li>
            </ul>
            <section id="balance-sheet">
              <table class="data-table"><tbody>
                <tr><td>Equity Capital</td><td>40</td><td>50</td></tr>
                <tr><td>Reserves</td><td>120</td><td>150</td></tr>
                <tr><td>Borrowings +</td><td>80</td><td>100</td></tr>
              </tbody></table>
            </section>
        """
        request.return_value = response

        metrics = fetch_screener_data("EXAMPLE")

        self.assertEqual(metrics["de_ratio"], 0.5)


class MarketContextTests(unittest.TestCase):
    @patch("market_context.fetch_market_breadth")
    @patch("market_context.fetch_yahoo_trends")
    def test_fetches_market_trends_in_one_batch(self, fetch_trends, fetch_breadth):
        tickers = ["^NSEI", "^INDIAVIX", *SECTOR_TICKERS.values(), "INR=X", "CL=F"]
        fetch_trends.return_value = {
            ticker: {"ticker": ticker, "trend": "mixed", "close": 1}
            for ticker in tickers
        }
        fetch_breadth.return_value = {"advance_pct": 50}

        context = build_market_context({
            "nasdaq_change_pct": 0,
            "spx_change_pct": 0,
            "dow_change_pct": 0,
            "gift_nifty_change_pct": 0,
        })

        fetch_trends.assert_called_once_with(tickers)
        self.assertEqual(context["macro"]["usd_inr"]["ticker"], "INR=X")
        self.assertEqual(context["macro"]["wti_crude"]["ticker"], "CL=F")

    def test_sparse_sector_evidence_does_not_make_environment_supportive(self):
        sectors = {
            f"Sector {index}": {"trend": "bullish" if index < 2 else "unavailable"}
            for index in range(8)
        }

        environment = assess_long_swing_environment(
            {"trend": "mixed"},
            {"close": 12},
            {"advance_pct": 43},
            sectors,
            62,
        )

        self.assertEqual(environment["condition"], "mixed")
        self.assertEqual(environment["score"], 1)
        self.assertEqual(environment["available_sector_count"], 2)
        self.assertFalse(environment["sector_coverage_sufficient"])
        self.assertNotIn("More tracked sectors are bullish than bearish", environment["reasons"])

    def test_sector_direction_counts_when_half_are_available(self):
        sectors = {
            f"Sector {index}": {
                "trend": "bullish" if index < 3 else "mixed" if index == 3 else "unavailable"
            }
            for index in range(8)
        }

        environment = assess_long_swing_environment(
            {"trend": "mixed"}, {"close": None}, {"advance_pct": None}, sectors, 50
        )

        self.assertEqual(environment["score"], 1)
        self.assertTrue(environment["sector_coverage_sufficient"])

    @patch("market_context.fetch_nse_sector_trends")
    def test_recovers_only_unavailable_yahoo_sectors(self, fetch_fallback):
        trends = {
            ticker: {"trend": "mixed", "source": "Yahoo Finance"}
            for ticker in SECTOR_TICKERS.values()
        }
        trends[SECTOR_TICKERS["Nifty IT"]] = {
            "trend": "unavailable",
            "source": "Yahoo Finance",
        }
        fetch_fallback.return_value = {
            "Nifty IT": {"trend": "bullish", "source": "NSE India index archives"}
        }

        recovered = recover_unavailable_sector_trends(trends)

        fetch_fallback.assert_called_once_with(["Nifty IT"])
        self.assertEqual(recovered[SECTOR_TICKERS["Nifty IT"]]["trend"], "bullish")
        self.assertEqual(
            recovered[SECTOR_TICKERS["Nifty Bank"]]["source"], "Yahoo Finance"
        )

    def test_nse_fallback_builds_compatible_trend_snapshot(self):
        observations = [
            (pd.Timestamp("2026-01-01").date() + pd.Timedelta(days=index), 100 + index)
            for index in range(50)
        ]

        snapshot = _trend_snapshot("NIFTY IT", observations, "2026-08-28T00:00:00Z")

        self.assertEqual(snapshot["trend"], "bullish")
        self.assertEqual(snapshot["source"], "NSE India index archives")
        self.assertEqual(snapshot["close"], 149)
        self.assertIsNotNone(snapshot["sma50"])

    def test_nse_fallback_parses_official_archive_columns(self):
        trade_date = pd.Timestamp("2026-08-27").date()
        content = (
            "Index Name,Index Date,Open Index Value,Closing Index Value\n"
            'NIFTY IT,27-08-2026,"42,000.00","42,500.50"\n'
            'NIFTY BANK,27-08-2026,"50,000.00","50,100.00"\n'
        )

        rows = _parse_archive_rows(content, {"NIFTY IT"}, trade_date)

        self.assertEqual(rows, {"NIFTY IT": (trade_date, 42500.5)})

    @patch("market_context.fetch_nse_sector_trends")
    def test_preserves_fallback_failure_details(self, fetch_fallback):
        trends = {
            ticker: {"trend": "mixed", "source": "Yahoo Finance"}
            for ticker in SECTOR_TICKERS.values()
        }
        ticker = SECTOR_TICKERS["Nifty IT"]
        trends[ticker] = {"trend": "unavailable", "source": "Yahoo Finance"}
        fetch_fallback.return_value = {
            "Nifty IT": {
                "trend": "unavailable",
                "source": "NSE India index archives",
                "error": "Fewer than 50 NSE index closes were available.",
            }
        }

        recover_unavailable_sector_trends(trends)

        self.assertEqual(trends[ticker]["fallback_source"], "NSE India index archives")
        self.assertIn("Fewer than 50", trends[ticker]["fallback_error"])

    @patch("market_context.fetch_stooq_trend")
    @patch("market_context.fetch_frankfurter_usdinr_trend")
    def test_recovers_unavailable_market_signals(self, fetch_fx, fetch_crude):
        from market_context import recover_unavailable_market_trends

        trends = {
            ticker: {"trend": "unavailable", "source": "Yahoo Finance"}
            for ticker in ("INR=X", "CL=F")
        }
        fetch_fx.return_value = {"trend": "bearish", "source": "Frankfurter"}
        fetch_crude.return_value = {"trend": "mixed", "source": "Stooq"}

        recover_unavailable_market_trends(trends)

        self.assertEqual(trends["INR=X"]["source"], "Frankfurter")
        self.assertEqual(trends["CL=F"]["source"], "Stooq")

    @patch("market_context.fetch_nse_index_trends")
    def test_recovers_indian_indices_and_sectors_in_one_nse_pass(self, fetch_nse):
        trends = {
            ticker: {"trend": "mixed", "source": "Yahoo Finance"}
            for ticker in ("^NSEI", "^INDIAVIX", *SECTOR_TICKERS.values())
        }
        trends["^NSEI"]["trend"] = "unavailable"
        sector_ticker = SECTOR_TICKERS["Nifty IT"]
        trends[sector_ticker]["trend"] = "unavailable"
        fetch_nse.return_value = {
            "^NSEI": {"trend": "bullish", "source": "NSE"},
            sector_ticker: {"trend": "bullish", "source": "NSE"},
        }

        recover_unavailable_nse_trends(trends)

        fetch_nse.assert_called_once()
        requested = fetch_nse.call_args.args[0]
        self.assertEqual(set(requested), {"^NSEI", sector_ticker})

    def test_nse_history_parser_accepts_api_field_names(self):
        frame = _history_frame([{
            "CH_TIMESTAMP": "27-08-2026",
            "CH_OPENING_PRICE": "1,000.00",
            "CH_TRADE_HIGH_PRICE": "1,020.00",
            "CH_TRADE_LOW_PRICE": "990.00",
            "CH_CLOSING_PRICE": "1,010.00",
            "CH_TOT_TRADED_QTY": "50,000",
        }], index_history=False)

        self.assertEqual(float(frame["Close"].iloc[0]), 1010)
        self.assertEqual(float(frame["Volume"].iloc[0]), 50000)


class YahooFallbackTests(unittest.TestCase):
    @patch("global_cues.fetch_stooq_change_snapshot")
    @patch("global_cues.call_with_resilience", side_effect=global_cues.UpstreamUnavailableError("Yahoo"))
    def test_global_cue_uses_stooq_when_yahoo_fails(self, _, fetch_stooq):
        fetch_stooq.return_value = {
            "change_pct": 1.25,
            "source": "Stooq",
            "observed_at": "2026-08-27",
            "fetched_at": "2026-08-28",
        }

        snapshot = global_cues.get_change_snapshot("^GSPC")

        self.assertEqual(snapshot["change_pct"], 1.25)
        self.assertEqual(snapshot["source"], "Stooq")

    @patch("backtest_score_buckets.fetch_stooq_history")
    @patch("backtest_score_buckets.fetch_nse_history")
    @patch("backtest_score_buckets.call_with_resilience", side_effect=UpstreamUnavailableError("Yahoo"))
    def test_backtest_uses_nse_history_when_yahoo_fails(
        self, _, fetch_nse, fetch_stooq
    ):
        dates = pd.date_range("2026-01-01", periods=50)
        fetch_nse.return_value = pd.DataFrame({
            "Open": range(50), "High": range(1, 51), "Low": range(50),
            "Close": range(1, 51), "Volume": [100] * 50,
        }, index=dates)

        history = backtest_score_buckets.download_history(
            "TEST.NS", "2026-01-01", "2026-04-01"
        )

        self.assertEqual(len(history), 50)
        fetch_stooq.assert_not_called()


class UniverseTests(unittest.TestCase):
    def test_selects_front_expiry_symbols_by_traded_value(self):
        frame = pd.DataFrame([
            {"FinInstrmTp": "STF", "TckrSymb": "LOW", "XpryDt": "2026-09-01", "TtlTrfVal": 10, "TtlTradgVol": 100},
            {"FinInstrmTp": "STF", "TckrSymb": "HIGH", "XpryDt": "2026-09-01", "TtlTrfVal": 100, "TtlTradgVol": 50},
            {"FinInstrmTp": "STF", "TckrSymb": "HIGH", "XpryDt": "2026-10-01", "TtlTrfVal": 1000, "TtlTradgVol": 500},
        ])

        self.assertEqual(select_liquid_fno_symbols(frame, 2), ("HIGH", "LOW"))

    def test_adds_sector_and_benchmark_relative_strength(self):
        stocks = [{"symbol": "TCS", "return_5d": 5}, {"symbol": "UNKNOWN", "return_5d": 1}]
        context = {
            "nifty_50": {"return_5d_pct": 0.5},
            "sector_indices": {"Nifty IT": {"return_5d_pct": 2}},
        }

        add_sector_relative_strength(stocks, context)

        self.assertEqual(stocks[0]["sector"], "Nifty IT")
        self.assertEqual(stocks[0]["sector_relative_strength_pct"], 3)
        self.assertEqual(stocks[1]["sector"], "Nifty 50 benchmark")
        self.assertEqual(stocks[1]["sector_relative_strength_pct"], 0.5)


class MarketMicrostructureTests(unittest.TestCase):
    def cash_history(self, gap_open=100):
        rows = []
        for index in range(51):
            close = 100 + index * 0.1
            rows.append({
                "OpnPric": close,
                "ClsPric": close,
                "HghPric": close + 1,
                "LwPric": close - 1,
                "TtlTradgVol": 100000,
            })
        rows[-1]["OpnPric"] = gap_open
        rows[-1]["ClsPric"] = gap_open
        rows[-1]["HghPric"] = gap_open + 1
        rows[-1]["LwPric"] = gap_open - 1
        return pd.DataFrame(rows)

    def test_detects_extended_gap_and_estimates_liquidity(self):
        cash = analyze_cash(self.cash_history(gap_open=110))

        self.assertTrue(cash["gap_up_extended"])
        self.assertGreater(cash["average_traded_value_crore"], 0)
        self.assertIn(cash["liquidity_tier"], {"high", "good", "moderate", "low"})

    def test_extended_gap_and_low_liquidity_block_entry(self):
        cash = analyze_cash(self.cash_history(gap_open=110))
        cash["liquidity_tier"] = "high"
        plan = build_trade_plan(cash, 90)
        self.assertFalse(plan["entry_valid"])
        self.assertEqual(plan["status"], "Wait for pullback")

        cash["gap_up_extended"] = False
        cash["liquidity_tier"] = "low"
        self.assertFalse(build_trade_plan(cash, 90)["entry_valid"])

    def test_builds_sorted_call_put_wall_profile(self):
        options = pd.DataFrame([
            {"StrkPric": 100, "OptnTp": "CE", "OpnIntrst": 200},
            {"StrkPric": 100, "OptnTp": "PE", "OpnIntrst": 300},
            {"StrkPric": 110, "OptnTp": "CE", "OpnIntrst": 500},
        ])

        profile = _option_oi_profile(options)

        self.assertEqual([row["strike"] for row in profile], [100.0, 110.0])
        self.assertEqual(profile[0]["put_oi"], 300)
        self.assertEqual(profile[1]["call_oi"], 500)

    def test_bearish_plan_uses_upper_stop_and_lower_targets(self):
        cash = {
            "atr14": 2,
            "prior_twenty_day_low": 90,
            "close": 88,
            "volume_ratio": 1.5,
            "gap_down_extended": False,
            "liquidity_tier": "high",
            "recent_swing_high": 94,
        }

        plan = build_trade_plan(cash, 85, "bearish")

        self.assertEqual(plan["direction"], "short")
        self.assertGreater(plan["stop_loss"], plan["entry_reference"])
        self.assertLess(plan["targets"][0], plan["entry_reference"])
        self.assertLess(plan["targets"][1], plan["targets"][0])


class IntelligenceScoringTests(unittest.TestCase):
    def stock(self):
        return {
            "symbol": "TEST",
            "futures_price_change_pct": 1,
            "futures_oi_change_pct": 1,
            "pcr": 1,
            "close": 110,
            "dma20": 100,
            "dma50": 90,
            "return_5d": 2,
            "volume": 120,
            "avg_volume": 100,
            "sector_relative_strength_pct": 2,
            "liquidity_filter_pass": True,
            "gap_atr": 0,
        }

    def test_scores_bullish_and_bearish_modes_independently(self):
        bullish = self.stock()
        self.assertGreater(
            calculate_stock_score(bullish, {}, "bullish")["score"],
            calculate_stock_score(bullish, {}, "bearish")["score"],
        )

        bearish = {
            **bullish,
            "futures_price_change_pct": -1,
            "pcr": 0.6,
            "close": 80,
            "dma20": 90,
            "dma50": 100,
            "return_5d": -2,
            "sector_relative_strength_pct": -2,
        }
        self.assertGreater(
            calculate_stock_score(bearish, {}, "bearish")["score"],
            calculate_stock_score(bearish, {}, "bullish")["score"],
        )

    def test_directional_gap_and_liquidity_reduce_score(self):
        stock = self.stock()
        baseline = calculate_stock_score(stock, {}, "bullish")["score"]
        extended = calculate_stock_score({**stock, "gap_atr": 1.5}, {}, "bullish")["score"]
        illiquid = calculate_stock_score({**stock, "liquidity_filter_pass": False}, {}, "bullish")["score"]

        self.assertLess(extended, baseline)
        self.assertLess(illiquid, baseline)

    def test_dashboard_and_detailed_report_use_identical_scoring(self):
        dashboard_stock = self.stock()
        dashboard_stock.update({
            "sector": "Nifty Test",
            "liquidity_tier": "high",
            "estimated_slippage_bps": 5,
            "event_risk": False,
        })
        cash = {
            "close": 110,
            "sma20": 100,
            "sma50": 90,
            "return_5d": 2,
            "volume_ratio": 1.2,
            "gap_pct": 0,
            "gap_atr": 0,
            "liquidity_tier": "high",
            "estimated_slippage_bps": 5,
        }
        fno = {
            "futures_price_change": 1,
            "oi_change_pct": 1,
            "pcr": 1,
            "ban_status": {"is_banned": False},
            "futures_liquidity_tier": "high",
            "call_oi_wall": 120,
            "put_oi_wall": 100,
        }
        global_cues = {"nasdaq_change_pct": 1.2}

        dashboard = calculate_stock_score(dashboard_stock, global_cues, "bullish")
        detailed = score_detailed_report(
            "TEST",
            cash,
            fno,
            global_cues,
            "bullish",
            sector="Nifty Test",
            sector_relative_strength_pct=2,
        )

        self.assertEqual(detailed["score"], dashboard["score"])
        self.assertEqual(detailed["recommendation"], dashboard["recommendation"])
        self.assertEqual(detailed["calculation"], dashboard["calculation"])

    def test_detailed_scoring_handles_missing_fno_data(self):
        cash = {
            "close": 110,
            "sma20": 100,
            "sma50": 90,
            "return_5d": 2,
            "volume_ratio": 1.2,
            "gap_pct": 0,
            "gap_atr": 0,
            "liquidity_tier": "high",
            "estimated_slippage_bps": 5,
        }

        result = score_detailed_report("TEST", cash, None, {}, "bullish")

        self.assertIsInstance(result["score"], float)
        self.assertEqual(result["pcr"], None)
        self.assertIn("PCR unavailable", result["reasons"])


class NewsIntelligenceTests(unittest.TestCase):
    def test_flags_and_ranks_material_corporate_events(self):
        material = classify_news_article(
            {"title": "TCS quarterly results and dividend record date announced"},
            "TCS",
        )
        routine = classify_news_article(
            {"title": "Broker maintains view on technology sector"},
            "TCS",
        )

        self.assertTrue(material["potentially_material"])
        self.assertIn("earnings", material["event_categories"])
        self.assertIn("dividend", material["event_categories"])
        self.assertGreater(material["materiality_score"], routine["materiality_score"])


if __name__ == "__main__":
    unittest.main()
