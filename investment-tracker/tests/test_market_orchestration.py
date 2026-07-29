"""Provider orchestration, driven through the injectable client seam.

`test_market_data.py` covers the pure helpers. This module covers the code that
calls the provider: symbol aliasing, per-ticker failure isolation, the .info
price override, EPS provenance, and Alpha Vantage rate-limit handling — none of
which the offline workbook tests touch.

No network: `fetch_batch`, `fetch_ticker`, and `fetch_alpha_vantage_eps` all
accept a stub client.
"""

import unittest

import pandas as pd

from modules.market_data import (
    fetch_alpha_vantage_eps,
    fetch_batch,
    fetch_ticker,
)

REC_MAP = {"buy": "Buy", "strong_buy": "Strong Buy"}


def _frame(values, freq, columns=("Close",)):
    idx = pd.date_range("2020-01-31", periods=len(values), freq=freq)
    return pd.DataFrame({c: values for c in columns}, index=idx)


def _grouped(symbols, values, freq):
    """Mimic yfinance group_by='ticker' output for multiple symbols."""
    idx = pd.date_range("2020-01-31", periods=len(values), freq=freq)
    return pd.concat(
        {s: pd.DataFrame({"Close": values}, index=idx) for s in symbols}, axis=1
    )


class StubClient:
    """Minimal stand-in for the yfinance module."""

    def __init__(self, symbols, info_by_symbol=None, fail_info=(), daily_len=400):
        self.symbols = symbols
        self.info_by_symbol = info_by_symbol or {}
        self.fail_info = set(fail_info)
        self.daily = [float(i) for i in range(1, daily_len + 1)]
        self.monthly = [float(i) for i in range(1, 61)]
        self.download_calls = []

    def download(self, tickers, **kwargs):
        self.download_calls.append((tickers, kwargs.get("interval")))
        syms = tickers if isinstance(tickers, list) else [tickers]
        multi = isinstance(tickers, list) and len(tickers) > 1
        values = self.monthly if kwargs.get("interval") == "1mo" else self.daily
        if multi:
            return _grouped(syms, values, "D" if kwargs.get("interval") != "1mo" else "ME")
        return _frame(values, "D" if kwargs.get("interval") != "1mo" else "ME")

    def Ticker(self, symbol):  # noqa: N802 — mirrors the yfinance API
        outer = self

        class _Handle:
            @property
            def info(self):
                if symbol in outer.fail_info:
                    raise RuntimeError(f"provider blew up for {symbol}")
                return outer.info_by_symbol.get(symbol, {})

            def history(self, **_kwargs):
                idx = pd.date_range("2022-09-01", periods=200, freq="D")
                return pd.DataFrame(
                    {"High": [200.0] * 200, "Low": [50.0] * 200, "Close": [100.0] * 200},
                    index=idx,
                )

        return _Handle()


class FetchBatchTest(unittest.TestCase):
    def test_aliases_are_applied_to_provider_symbols(self):
        client = StubClient(["BP.LON"])
        fetch_batch(["BP.L"], {"BP.L": "BP.LON"}, REC_MAP,
                    warn=lambda *_: None, client=client)
        requested = client.download_calls[0][0]
        self.assertEqual(requested, ["BP.LON"])

    def test_results_are_keyed_by_script_ticker_not_provider_symbol(self):
        client = StubClient(["BP.LON"])
        live = fetch_batch(["BP.L"], {"BP.L": "BP.LON"}, REC_MAP,
                           warn=lambda *_: None, client=client)
        self.assertIn("BP.L", live.prices)
        self.assertNotIn("BP.LON", live.prices)

    def test_info_price_overrides_the_bar_close(self):
        client = StubClient(["AAPL"], {"AAPL": {"regularMarketPrice": 999.5}})
        live = fetch_batch(["AAPL"], {}, REC_MAP, warn=lambda *_: None, client=client)
        self.assertEqual(live.prices["AAPL"], 999.5)

    def test_moving_averages_are_populated(self):
        client = StubClient(["AAPL"])
        live = fetch_batch(["AAPL"], {}, REC_MAP, warn=lambda *_: None, client=client)
        mas = live.moving_averages["AAPL"]
        self.assertEqual(len(mas), 5)
        self.assertTrue(all(v is not None for v in mas))

    def test_yfinance_eps_is_recorded_as_unverified(self):
        client = StubClient(["AAPL"], {"AAPL": {"trailingEps": 6.42}})
        live = fetch_batch(["AAPL"], {}, REC_MAP, warn=lambda *_: None, client=client)
        eps, verified = live.eps["AAPL"]
        self.assertEqual(eps, 6.42)
        self.assertFalse(verified, "yfinance EPS must never claim AV verification")

    def test_analyst_needs_both_count_and_target(self):
        client = StubClient(
            ["A", "B"],
            {
                "A": {"recommendationKey": "buy", "numberOfAnalystOpinions": 12,
                      "targetMeanPrice": 210.0},
                "B": {"recommendationKey": "buy"},   # no count, no target
            },
        )
        live = fetch_batch(["A", "B"], {}, REC_MAP, warn=lambda *_: None, client=client)
        self.assertEqual(live.analyst["A"], ("Buy", 12, 210.0))
        self.assertNotIn("B", live.analyst)

    def test_one_bad_ticker_does_not_abort_the_batch(self):
        client = StubClient(["GOOD", "BAD"], {"GOOD": {"trailingEps": 1.0}},
                            fail_info=["BAD"])
        warnings = []
        live = fetch_batch(["GOOD", "BAD"], {}, REC_MAP,
                           warn=warnings.append, client=client)
        self.assertIn("GOOD", live.fundamentals)
        self.assertNotIn("BAD", live.fundamentals)
        self.assertTrue(any("BAD" in str(w) for w in warnings))

    def test_empty_ticker_list_short_circuits(self):
        live = fetch_batch([], {}, REC_MAP, warn=lambda *_: None, client=StubClient([]))
        self.assertFalse(live.has_row_overlay())


class FetchTickerTest(unittest.TestCase):
    def test_snapshot_carries_name_currency_anchors_and_mas(self):
        client = StubClient(
            ["NEW"],
            {"NEW": {"longName": "New Co", "currency": "GBp",
                     "regularMarketPrice": 123.0, "recommendationKey": "strong_buy",
                     "numberOfAnalystOpinions": 4, "targetMeanPrice": 150.0}},
        )
        snap = fetch_ticker("NEW", REC_MAP, warn=lambda *_: None, client=client)
        self.assertEqual(snap.name, "New Co")
        self.assertEqual(snap.currency, "GBp")
        self.assertEqual(snap.price, 123.0)
        self.assertEqual((snap.bear_low, snap.cycle_high), (50.0, 200.0))
        self.assertEqual(len(snap.moving_averages), 5)
        self.assertEqual(snap.analyst[0], "Strong Buy")

    def test_ticker_symbol_used_when_provider_has_no_name(self):
        client = StubClient(["ZZZ"], {"ZZZ": {}})
        snap = fetch_ticker("ZZZ", REC_MAP, warn=lambda *_: None, client=client)
        self.assertEqual(snap.name, "ZZZ")

    def test_provider_failure_returns_none_and_warns(self):
        client = StubClient(["BOOM"], fail_info=["BOOM"])
        warnings = []
        self.assertIsNone(
            fetch_ticker("BOOM", REC_MAP, warn=warnings.append, client=client)
        )
        self.assertTrue(any("BOOM" in str(w) for w in warnings))


class StubHttp:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, _url, params=None, timeout=None):
        symbol = params["symbol"]
        self.calls.append(symbol)
        payload = self.payloads.get(symbol, {})

        class _Resp:
            def json(self_inner):
                return payload

        return _Resp()


class AlphaVantageTest(unittest.TestCase):
    def test_verified_eps_is_marked_as_alpha_vantage(self):
        http = StubHttp({"AAPL": {"Symbol": "AAPL", "DilutedEPSTTM": "6.42"}})
        out = fetch_alpha_vantage_eps(["AAPL"], {}, "key", 0,
                                      warn=lambda *_: None, http=http,
                                      sleeper=lambda _s: None)
        self.assertEqual(out["AAPL"], (6.42, True))

    def test_rate_limit_note_is_skipped_not_stored(self):
        http = StubHttp({"AAPL": {"Note": "call frequency exceeded"}})
        out = fetch_alpha_vantage_eps(["AAPL"], {}, "key", 0,
                                      warn=lambda *_: None, http=http,
                                      sleeper=lambda _s: None)
        self.assertEqual(out, {})

    def test_uk_symbols_are_remapped(self):
        http = StubHttp({"BP.LON": {"Symbol": "BP.LON", "EPS": "1.5"}})
        out = fetch_alpha_vantage_eps(["BP.L"], {"BP.L": "BP.LON"}, "key", 0,
                                      warn=lambda *_: None, http=http,
                                      sleeper=lambda _s: None)
        self.assertEqual(http.calls, ["BP.LON"])
        self.assertEqual(out["BP.L"], (1.5, True))

    def test_no_sleep_after_the_final_call(self):
        http = StubHttp({t: {"Symbol": t, "EPS": "1.0"} for t in ["A", "B", "C"]})
        sleeps = []
        fetch_alpha_vantage_eps(["A", "B", "C"], {}, "key", 13,
                                warn=lambda *_: None, http=http,
                                sleeper=sleeps.append)
        self.assertEqual(sleeps, [13, 13], "must not sleep after the last target")

    def test_no_key_means_no_calls(self):
        http = StubHttp({})
        self.assertEqual(
            fetch_alpha_vantage_eps(["AAPL"], {}, "", 0, warn=lambda *_: None, http=http),
            {},
        )
        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
