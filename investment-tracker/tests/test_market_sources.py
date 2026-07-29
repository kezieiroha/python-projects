"""Tests for data-driven market-source classification helpers."""

import unittest

from modules.market_sources import macro_tickers, sentiment_tickers


class MarketSourcesTest(unittest.TestCase):
    def test_classification_sets_come_from_config(self):
        config = {
            "macro_tickers": ["abc", "TLT"],
            "sentiment_tickers": ["vix"],
        }
        self.assertEqual(macro_tickers(config), {"ABC", "TLT"})
        self.assertEqual(sentiment_tickers(config), {"VIX"})

    def test_classification_sets_have_defaults(self):
        self.assertIn("TLT", macro_tickers({}))
        self.assertIn("TLT", sentiment_tickers({}))


if __name__ == "__main__":
    unittest.main()
