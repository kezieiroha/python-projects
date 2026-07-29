"""Tests for JSON row persistence and row identity preservation."""

import json
import tempfile
import unittest
from pathlib import Path

from modules.data_store import (
    load_portfolio_rows,
    load_macro_swing_data,
    save_portfolio_rows,
)
from modules.models import PortfolioRow, RowType


class DataStoreTest(unittest.TestCase):
    def test_portfolio_rows_round_trip_preserves_unknown_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "portfolio_rows.json"
            path.write_text(json.dumps([
                {
                    "asset": "Example",
                    "ticker": "EXM",
                    "ccy": "USD",
                    "current": 10,
                    "macro_lo": 5,
                    "macro_hi": 20,
                    "notes": "test",
                    "my_custom_field": "KEEP ME",
                    "conviction": "high",
                }
            ]))

            rows = load_portfolio_rows(path)
            self.assertEqual(rows[0].asset, "Example")
            self.assertEqual(rows[0].ticker, "EXM")
            self.assertEqual(rows[0].rtype, RowType.STOCK)

            save_portfolio_rows(path, rows)
            saved = json.loads(path.read_text())[0]
            self.assertEqual(saved["ticker"], "EXM")
            self.assertEqual(saved["my_custom_field"], "KEEP ME")
            self.assertEqual(saved["conviction"], "high")

    def test_portfolio_rows_are_not_deduplicated_by_ticker_on_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "portfolio_rows.json"
            rows = [
                PortfolioRow(asset="First", ticker="ABC", ccy="USD", current=1,
                             macro_lo=1, macro_hi=2, notes=""),
                PortfolioRow(asset="Second", ticker="ABC", ccy="USD", current=3,
                             macro_lo=1, macro_hi=4, notes=""),
            ]
            save_portfolio_rows(path, rows)
            loaded = load_portfolio_rows(path)
            self.assertEqual([row.asset for row in loaded], ["First", "Second"])

    def test_portfolio_rows_uppercase_ticker_on_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "portfolio_rows.json"
            save_portfolio_rows(path, [PortfolioRow(asset="A", ticker="abc")])
            self.assertEqual(json.loads(path.read_text())[0]["ticker"], "ABC")

    def test_missing_portfolio_file_fails_loudly_and_names_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "portfolio_rows.json"
            with self.assertRaises(RuntimeError) as ctx:
                load_portfolio_rows(missing)
            self.assertIn(str(missing), str(ctx.exception))

    def test_macro_swing_rows_load_as_named_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "macro.json"
            path.write_text(json.dumps([
                {"ticker": "MSTR", "macro_lo": 13.26, "macro_hi": 543.0,
                 "current": 132.36, "al1_display": "$340.64",
                 "status": "BELOW", "bg": "FFCCCC", "font_color": "880000"}
            ]))
            swing = load_macro_swing_data(path)[0]
            self.assertEqual(swing.ticker, "MSTR")
            self.assertEqual(swing.macro_hi, 543.0)
            self.assertEqual(swing.font_color, "880000")


class PortfolioRowTest(unittest.TestCase):
    def test_matches_asset_or_ticker_case_insensitively(self):
        row = PortfolioRow(asset="SpaceX", ticker="TBC", rtype=RowType.MANUAL)
        self.assertTrue(row.matches({"SPACEX"}))
        self.assertTrue(row.matches({"TBC"}))
        self.assertFalse(row.matches({"OPENAI"}))
        self.assertFalse(row.matches(set()))

    def test_identity_separates_rows_sharing_a_ticker(self):
        first = PortfolioRow(asset="SpaceX", ticker="TBC")
        second = PortfolioRow(asset="Stripe", ticker="TBC")
        self.assertNotEqual(first.identity(0), second.identity(1))
        # Same row at the same position is stable across calls.
        self.assertEqual(first.identity(0), first.identity(0))

    def test_replace_keeps_extra_fields(self):
        row = PortfolioRow.from_dict(
            {"asset": "A", "ticker": "T", "current": 1, "conviction": "high"}
        )
        updated = row.replace(current=99)
        self.assertEqual(updated.current, 99)
        self.assertEqual(updated.extra["conviction"], "high")
        self.assertEqual(updated.to_dict()["conviction"], "high")

    def test_known_fields_win_over_stale_extra_keys(self):
        row = PortfolioRow.from_dict({"asset": "A", "ticker": "T", "current": 5})
        self.assertEqual(row.to_dict()["current"], 5)

    def test_predicates(self):
        self.assertTrue(PortfolioRow(rtype=RowType.SECTION).is_section)
        self.assertTrue(PortfolioRow(rtype=RowType.ETF).is_etf)
        self.assertEqual(PortfolioRow(rtype=RowType.ETF).na_label, "ETF")
        self.assertEqual(PortfolioRow(rtype=RowType.STOCK).na_label, "N/A")
        self.assertTrue(PortfolioRow(macro_lo=1, macro_hi=2).has_fib_anchors)
        self.assertFalse(PortfolioRow(macro_lo=1).has_fib_anchors)


if __name__ == "__main__":
    unittest.main()
