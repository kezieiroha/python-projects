"""Tests for market-provider data normalization helpers."""

import unittest

from modules.market_data import (
    alpha_vantage_targets,
    debt_to_equity,
    fib_anchors,
    moving_averages,
    net_margin,
    positive_pe,
    to_float,
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

    def test_fib_anchors_use_intraday_extremes(self):
        import pandas as pd
        idx = pd.date_range("2022-09-01", periods=4, freq="D")
        history = pd.DataFrame(
            {"High": [10.0, 30.0, 20.0, 25.0], "Low": [5.0, 8.0, 3.0, 9.0]},
            index=idx,
        )
        self.assertEqual(fib_anchors(history), (3.0, 30.0))

    def test_alpha_vantage_targets_respect_daily_budget(self):
        targets, dropped = alpha_vantage_targets(
            [f"T{i}" for i in range(30)], {"BP.L": "BP.LON"}, daily_call_budget=25
        )
        self.assertEqual(len(targets), 25)
        self.assertEqual(dropped, 6)

    def test_alpha_vantage_targets_pass_through_when_under_budget(self):
        targets, dropped = alpha_vantage_targets(["AAPL"], {}, daily_call_budget=25)
        self.assertEqual((targets, dropped), (["AAPL"], 0))
