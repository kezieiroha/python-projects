"""Tests for market-provider data normalization helpers."""

import unittest

from modules.market_data import (
    alpha_vantage_targets,
    debt_to_equity,
    fib_anchors,
    highest_since,
    moving_averages,
    net_margin,
    positive_pe,
    shows_profit_with_operating_loss,
    to_float,
    trailing_operating_income,
)


class MarketDataTest(unittest.TestCase):
    def test_positive_pe_filters_non_positive_values(self):
        self.assertEqual(positive_pe("12.34"), 12.3)
        self.assertIsNone(positive_pe(0))
        self.assertIsNone(positive_pe(-5))
        self.assertIsNone(positive_pe(None))

    def test_debt_to_equity_primary_and_fallback(self):
        self.assertEqual(debt_to_equity({"debtToEquity": 97.9}), 0.98)
        self.assertEqual(
            debt_to_equity({
                "totalDebt": 100,
                "bookValue": 2,
                "sharesOutstanding": 50,
            }),
            1.0,
        )

    def test_net_margin_primary_and_fallback(self):
        self.assertEqual(net_margin({"profitMargins": 0.12345}), 0.1235)
        self.assertEqual(
            net_margin({"netIncomeToCommon": 25, "totalRevenue": 100}),
            0.25,
        )

    def test_to_float_rejects_bad_values(self):
        self.assertEqual(to_float("2.5"), 2.5)
        self.assertIsNone(to_float("bad"))


if __name__ == "__main__":
    unittest.main()


class SharedComputationTest(unittest.TestCase):
    """Both fetch paths share these, so a ticker cannot get divergent values."""

    def _series(self, values, freq):
        import pandas as pd
        idx = pd.date_range("2020-01-31", periods=len(values), freq=freq)
        return pd.Series(values, index=idx)

    def test_moving_averages_returns_none_until_series_is_long_enough(self):
        daily = self._series([10.0] * 5, "D")
        monthly = self._series([10.0] * 3, "ME")
        w20, w50, m20, m50, d200 = moving_averages(daily, monthly)
        self.assertEqual((w20, w50, m20, m50, d200), (None, None, None, None, None))

    def test_moving_averages_computes_from_sufficient_history(self):
        daily = self._series([float(i) for i in range(1, 401)], "D")
        monthly = self._series([float(i) for i in range(1, 61)], "ME")
        w20, w50, m20, m50, d200 = moving_averages(daily, monthly)
        for value in (w20, w50, m20, m50, d200):
            self.assertIsNotNone(value)
        self.assertAlmostEqual(d200, 300.5)

    def test_fib_anchors_ignore_a_larger_high_before_the_low(self):
        """The 30.0 peak precedes the dated 3.0 low, so it is not eligible;
        the cycle high is the highest intraday High from the low onward."""
        import pandas as pd
        idx = pd.date_range("2022-09-01", periods=4, freq="D")
        history = pd.DataFrame(
            {"High": [10.0, 30.0, 20.0, 25.0], "Low": [5.0, 8.0, 3.0, 9.0]},
            index=idx,
        )
        self.assertEqual(fib_anchors(history), (3.0, 25.0, "2022-09-03"))

    def test_fib_anchors_pre_window_peak_like_path(self):
        """A 2021 peak (PATH-style) before a 2022 low is excluded, and the
        low's date is returned for persistence."""
        import pandas as pd
        idx = pd.to_datetime(["2021-05-28", "2022-11-04", "2024-02-12", "2026-11-20"])
        history = pd.DataFrame(
            {"High": [90.0, 11.0, 27.87, 12.0], "Low": [70.0, 10.4, 25.0, 11.5]},
            index=idx,
        )
        self.assertEqual(fib_anchors(history), (10.4, 27.87, "2022-11-04"))

    def test_highest_since_starts_at_the_low_and_needs_coverage(self):
        import pandas as pd
        idx = pd.to_datetime(["2022-09-01", "2023-02-22", "2025-04-07",
                              "2026-05-28", "2026-11-20"]).tz_localize("America/New_York")
        highs = pd.Series([12.0, 81.99, 6.5, 46.75, 10.0], index=idx)
        self.assertEqual(highest_since(highs, "2025-04-07"), 46.75)
        self.assertEqual(highest_since(highs, "2022-09-01"), 81.99)
        # History that starts after the low cannot verify the maximum.
        self.assertIsNone(highest_since(highs, "2021-08-20"))
        self.assertIsNone(highest_since(highs, None))
        self.assertIsNone(highest_since(pd.Series(dtype=float), "2025-04-07"))

    @staticmethod
    def _quarters(dates, values):
        import pandas as pd
        return pd.DataFrame([values], index=["Operating Income"],
                            columns=pd.to_datetime(dates))

    def test_trailing_operating_income_needs_four_quarters(self):
        import pandas as pd
        full = self._quarters(["2026-09-30", "2026-06-30", "2026-03-31",
                               "2025-12-31", "2025-09-30"],
                              [-4.0, -3.0, -2.0, -1.0, 99.0])
        self.assertEqual(trailing_operating_income(full), -10.0)
        self.assertIsNone(trailing_operating_income(full.iloc[:, :3]))
        gap = full.copy()
        gap.iloc[0, 1] = float("nan")
        self.assertIsNone(trailing_operating_income(gap))
        self.assertIsNone(trailing_operating_income(pd.DataFrame()))

    def test_trailing_operating_income_rejects_a_missing_quarter(self):
        """Four observations spanning five quarters (March 2026 absent) are
        not a trailing total; this returned -8 before the consecutive check."""
        skipped = self._quarters(["2026-09-30", "2026-06-30", "2025-12-31",
                                  "2025-09-30"], [-2.0, -2.0, -2.0, -2.0])
        self.assertIsNone(trailing_operating_income(skipped))
        repeated = self._quarters(["2026-09-30", "2026-09-30", "2026-06-30",
                                   "2026-03-31"], [-2.0, -2.0, -2.0, -2.0])
        self.assertIsNone(trailing_operating_income(repeated))

    def test_trailing_operating_income_accepts_52_53_week_quarters(self):
        weeks = self._quarters(["2026-10-25", "2026-07-26", "2026-04-26",
                                "2026-01-25"], [-1.0, -1.0, -1.0, -1.0])
        self.assertEqual(trailing_operating_income(weeks), -4.0)

    def test_profit_with_operating_loss_prefilter(self):
        self.assertTrue(shows_profit_with_operating_loss(
            {"profitMargins": 0.67, "operatingMargins": -3.06}))
        self.assertFalse(shows_profit_with_operating_loss(
            {"profitMargins": 0.50, "operatingMargins": 0.33}))
        self.assertFalse(shows_profit_with_operating_loss(
            {"profitMargins": -0.2, "operatingMargins": -0.3}))
        self.assertFalse(shows_profit_with_operating_loss({"profitMargins": 0.5}))

    def test_prefilter_accepts_positive_eps_without_a_net_margin(self):
        """Same profit evidence as assess_fundamentals: positive EPS alone
        (no net margin reported) is enough to request the statement."""
        self.assertTrue(shows_profit_with_operating_loss(
            {"trailingEps": 1.73, "operatingMargins": -3.06}))
        self.assertTrue(shows_profit_with_operating_loss(
            {"trailingPE": 14.0, "regularMarketPrice": 23.36,
             "operatingMargins": -3.06}))
        self.assertFalse(shows_profit_with_operating_loss(
            {"trailingEps": -0.5, "operatingMargins": -3.06}))

    def test_alpha_vantage_targets_respect_daily_budget(self):
        targets, dropped = alpha_vantage_targets(
            [f"T{i}" for i in range(30)], {"BP.L": "BP.LON"}, daily_call_budget=25
        )
        self.assertEqual(len(targets), 25)
        self.assertEqual(dropped, 6)

    def test_alpha_vantage_targets_pass_through_when_under_budget(self):
        targets, dropped = alpha_vantage_targets(["AAPL"], {}, daily_call_budget=25)
        self.assertEqual((targets, dropped), (["AAPL"], 0))
