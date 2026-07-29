import unittest
from datetime import datetime

from modules import cli, config
from modules.market_data import LiveMarketData

SOURCES = {"yahoo_fetch": ["AAPL", "NKE", "INTC"]}


def make_config(**overrides):
    args = config.build_parser().parse_args(
        ["--offline", "--run-date", "2026-07-28T09:30"]
    )
    cfg = config.from_args(args)
    return cfg if not overrides else config.RunConfig(
        **{**cfg.__dict__, **overrides}
    )


class ParserTest(unittest.TestCase):
    def test_run_date_is_injectable(self):
        args = config.build_parser().parse_args(["--run-date", "2026-07-28T09:30"])
        cfg = config.from_args(args)
        self.assertEqual(cfg.run_dt, datetime(2026, 7, 28, 9, 30))
        self.assertEqual(cfg.run_date, "28 Jul 2026")

    def test_ambient_clock_used_when_no_run_date(self):
        args = config.build_parser().parse_args([])
        cfg = config.from_args(args, now=datetime(2030, 1, 2, 3, 4))
        self.assertEqual(cfg.run_date, "2 Jan 2030")

    def test_bad_run_date_exits_with_a_useful_message(self):
        args = config.build_parser().parse_args(["--run-date", "not-a-date"])
        with self.assertRaises(SystemExit) as ctx:
            config.from_args(args)
        self.assertIn("--run-date must be ISO format", str(ctx.exception))

    def test_offline_disables_live_refresh(self):
        self.assertFalse(make_config().live_refresh)


class FetchListTest(unittest.TestCase):
    def test_excluded_tickers_are_never_fetched(self):
        cfg = make_config(excluded_tickers=frozenset({"NKE", "INTC"}))
        self.assertEqual(cli.fetch_list(cfg, SOURCES), ["AAPL"])

    def test_full_list_used_when_nothing_excluded(self):
        self.assertEqual(cli.fetch_list(make_config(), SOURCES), SOURCES["yahoo_fetch"])


REQUESTED = ["A", "B", "C"]


class DataSourceLabelTest(unittest.TestCase):
    def test_static_when_offline(self):
        label = cli.describe_source(make_config(), LiveMarketData(), REQUESTED)
        self.assertEqual(label, "Static JSON fallback data")

    def test_static_when_live_but_nothing_fetched(self):
        cfg = make_config(live_refresh=True)
        self.assertEqual(
            cli.describe_source(cfg, LiveMarketData(), REQUESTED),
            "Static JSON fallback data",
        )

    def test_full_refresh_label(self):
        cfg = make_config(live_refresh=True)
        live = LiveMarketData(prices={"A": 1, "B": 2, "C": 3})
        self.assertTrue(
            cli.describe_source(cfg, live, REQUESTED).startswith("Full live refresh")
        )

    def test_partial_refresh_reports_the_shortfall(self):
        cfg = make_config(live_refresh=True)
        live = LiveMarketData(prices={"A": 1})
        self.assertIn("Partial live refresh via yfinance (1/3 prices)",
                      cli.describe_source(cfg, live, REQUESTED))

    def test_added_row_prices_do_not_inflate_coverage(self):
        """--add snapshots also land in live.prices; they are not batch coverage."""
        cfg = make_config(live_refresh=True)
        # Two of three requested tickers fetched, plus an --add row.
        live = LiveMarketData(prices={"A": 1, "B": 2, "SPCX": 116.41})
        label = cli.describe_source(cfg, live, REQUESTED)
        self.assertIn("(2/3 prices)", label)
        self.assertTrue(label.startswith("Partial live refresh"))

    def test_coverage_cannot_exceed_the_request(self):
        cfg = make_config(live_refresh=True)
        live = LiveMarketData(prices={"A": 1, "B": 2, "C": 3, "EXTRA": 9})
        self.assertTrue(
            cli.describe_source(cfg, live, REQUESTED).startswith("Full live refresh")
        )


if __name__ == "__main__":
    unittest.main()
