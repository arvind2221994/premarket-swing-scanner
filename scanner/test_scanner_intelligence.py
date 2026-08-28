import importlib.util
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fno_trade_analyzer import _option_oi_profile, analyze_cash, build_trade_plan
from news import classify_news_article
from scoring import calculate_stock_score

scanner_spec = importlib.util.spec_from_file_location(
    "scanner_job", Path(__file__).resolve().parent / "scanner.py"
)
scanner_job = importlib.util.module_from_spec(scanner_spec)
scanner_spec.loader.exec_module(scanner_job)
add_sector_relative_strength = scanner_job.add_sector_relative_strength
select_liquid_fno_symbols = scanner_job.select_liquid_fno_symbols


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
