"""Regression tests for the alert-level fixes and the D5 EMA flag.

One test class per issue so a failure names the behaviour that regressed:
effective ATH, units, direction, notes, data checks, missing MAs, quality gate,
broken anchor, completed closes, and the D5 flag.
"""

import unittest
from datetime import date, datetime, timezone

import pandas as pd

from modules import portfolio
from modules.calculations import (
    RANK_AL1,
    RANK_AL2,
    RANK_AL3,
    RANK_BROKEN,
    RANK_DEEP,
    RANK_UNIT_CHECK,
    FundamentalsCheck,
    assess_fundamentals,
    bar_is_complete,
    classify_row,
    classify_zone,
    d5_flag,
    declared_unit_factor,
    effective_ath,
    established_eps_unit,
    exchange_for,
    note_issues,
    unit_factor,
    units_mismatch,
    validate_manual_levels,
)
from modules.market_data import (
    LiveMarketData,
    TickerSnapshot,
    _close_series,
    completed_closes,
    d5_view,
    fetch_alpha_vantage_eps,
    fetch_batch,
    fetch_ticker,
    moving_averages,
)
from modules.models import D5View, MacroSwingRow, PortfolioRow, StaticEntry
from workbook.context import SheetContext
from workbook.styles import eps_text, money
from workbook.summary import (
    GROUP_BROKEN,
    GROUP_BUY,
    GROUP_CHECK,
    GROUP_KNIFE,
    summary_group,
)

NO_MACRO = frozenset()


def stock(**fields):
    """A STOCK row with anchors 100 to 200 unless overridden."""
    base = dict(asset="Co", ticker="CO", ccy="USD", current=150.0,
                macro_lo=100.0, macro_hi=200.0, rtype="STOCK")
    base.update(fields)
    return PortfolioRow.from_dict(base)


class EffectiveAthTest(unittest.TestCase):
    """Issue 1: a stored ATH below the highest close is superseded."""

    def test_effective_ath_is_the_larger_value(self):
        self.assertEqual(effective_ath(288.35, 343.0), 343.0)
        self.assertEqual(effective_ath(288.35, 250.0), 288.35)
        self.assertEqual(effective_ath(288.35, None), 288.35)

    def test_superseded_ath_lifts_levels_and_is_reported(self):
        row = stock(ticker="AAPL", current=342.56, macro_lo=123.88, macro_hi=288.35)
        live = LiveMarketData(max_close={"AAPL": 345.0})
        messages = []
        rows, updates = portfolio.apply_effective_ath([row], live, warn=messages.append)
        self.assertEqual(rows[0].macro_hi, 345.0)
        self.assertEqual(updates[rows[0].identity(0)], (288.35, 345.0))
        self.assertTrue(any("AAPL" in m and "288.35" in m for m in messages))
        zone = classify_row(rows[0])
        self.assertGreater(zone.al1, 225.52)

    def test_implausible_jump_is_ignored(self):
        row = stock(ticker="X", macro_hi=45.88)
        live = LiveMarketData(max_close={"X": 4588.0})
        rows, updates = portfolio.apply_effective_ath([row], live, warn=lambda *_: None)
        self.assertEqual(rows[0].macro_hi, 45.88)
        self.assertEqual(updates, {})


class UnitsTest(unittest.TestCase):
    """Issue 2: pence versus pounds."""

    def test_unit_factor(self):
        self.assertEqual(unit_factor("GBp", "GBP"), 0.01)
        self.assertEqual(unit_factor("GBP", "GBp"), 100)
        self.assertEqual(unit_factor("GBp", "GBX"), 1)
        self.assertEqual(unit_factor("USD", "USD"), 1)
        self.assertIsNone(unit_factor("USD", "GBP"))
        self.assertIsNone(unit_factor("CAD", "USD"))
        # Readings, not currencies: TNX yield and DXY index level.
        self.assertEqual(unit_factor("USD", "PCT"), 1)
        self.assertEqual(unit_factor("USD", "IDX"), 1)

    def test_fgrd_style_mismatch_is_a_unit_check(self):
        self.assertTrue(units_mismatch(3894, 36.46))
        zone = classify_zone(3894, 21.22, 45.88)
        self.assertEqual(zone.rank, RANK_UNIT_CHECK)

    def test_a_genuine_crash_is_not_a_unit_check(self):
        self.assertFalse(units_mismatch(2.79, 83.2))   # QS, about 30x

    def test_live_values_convert_per_row_and_raw_data_is_untouched(self):
        row = stock(ticker="FG.L", ccy="GBP")
        live = LiveMarketData(
            prices={"FG.L": 3894.0},
            moving_averages={"FG.L": (4000.0, 3900.0, 3800.0, None, 3700.0)},
            currency={"FG.L": "GBp"},
            max_close={"FG.L": 4500.0},
            d5={"FG.L": D5View(3900.0, 3850.0, 3800.0, 3820.0, "2026-11-20", "LSE")},
            analyst={"FG.L": ("Buy", 5, 5000.0)},
        )
        self.assertEqual(live.price_for(row), 38.94)
        self.assertEqual(live.moving_averages_for(row)[0], 40.0)
        self.assertIsNone(live.moving_averages_for(row)[3])
        self.assertEqual(live.max_close_for(row), 45.0)
        self.assertEqual(live.d5_for(row).close, 39.0)
        self.assertEqual(live.analyst_for(row)[2], 50.0)
        # Raw provider values stay in pence for any other row on this ticker.
        self.assertEqual(live.prices["FG.L"], 3894.0)


class DirectionTest(unittest.TestCase):
    """Issue 3: up-direction manual rows."""

    def test_vix_at_35_89_is_al2(self):
        zone = classify_zone(35.89, manual=(20.0, 30.0, 40.0), is_manual=True,
                             direction="up")
        self.assertEqual(zone.rank, RANK_AL2)

    def test_tnx_at_4_91_is_al3(self):
        zone = classify_zone(4.91, manual=(3.5, 4.0, 4.5), is_manual=True,
                             direction="up")
        self.assertEqual(zone.rank, RANK_AL3)

    def test_down_manual_row_is_unchanged(self):
        zone = classify_zone(21.51, manual=(30.0, 20.0, 10.0), is_manual=True)
        self.assertEqual(zone.rank, RANK_AL1)

    def test_level_order_must_match_direction(self):
        bad = PortfolioRow.from_dict({"ticker": "VIX", "rtype": "MANUAL",
                                      "direction": "up", "manual": [40, 30, 20]})
        good = PortfolioRow.from_dict({"ticker": "VIX", "rtype": "MANUAL",
                                       "direction": "up", "manual": [20, 30, 40]})
        self.assertIsNotNone(validate_manual_levels(bad))
        self.assertIsNone(validate_manual_levels(good))


class NoteIssuesTest(unittest.TestCase):
    """Issue 4: notes must not quote prices or statuses."""

    def test_prices_levels_and_statuses_are_found(self):
        text = "AL1 HIT. Current $125.75 vs 1121.5p. D200=$259.53."
        found = note_issues(text)
        self.assertIn("$125.75", found)
        self.assertIn("1121.5p", found)
        self.assertIn("D200=", found)
        self.assertIn("HIT", found)

    def test_company_figures_are_not_prices(self):
        text = "Revenue $34B+. $110B buybacks. USDC volume $1T to $4T. 960MW. 5% GDP."
        self.assertEqual(note_issues(text), [])

    def test_added_row_note_has_no_prices(self):
        snap = TickerSnapshot("NEW", "New Co", "USD", 99.0, 1.0, 200.0,
                              (1.0, 2.0, 3.0, 4.0, 5.0), (11.1, 9.9, 0.5, 0.2),
                              ("Buy", 7, 150.0))
        row = portfolio.build_added_row(snap)
        self.assertEqual(row.notes, "[Auto-added via --add NEW]")
        self.assertEqual(note_issues(row.notes), [])


class DataCheckTest(unittest.TestCase):
    """Issue 5: implausible fundamentals."""

    def test_nvda_style_forward_pe_collapse(self):
        check = assess_fundamentals(stock(ttm_pe=14.0, fwd_pe=8.0), 9.31, NO_MACRO)
        self.assertTrue(check.needs_data_check)
        self.assertIn("fwd_pe", check.suspect)

    def test_mstr_style_forward_pe_below_5(self):
        check = assess_fundamentals(stock(fwd_pe=4.0), -52.33, NO_MACRO)
        self.assertIn("fwd_pe", check.suspect)
        self.assertIn("NEGATIVE EPS", check.red)

    def test_margin_above_90_percent(self):
        check = assess_fundamentals(stock(margin=0.979), 0.78, NO_MACRO)
        self.assertIn("margin", check.suspect)
        self.assertNotIn("LOSS-MAKING", check.red)

    def test_margin_sign_disagrees_with_eps(self):
        check = assess_fundamentals(stock(margin=0.67), -1.20, NO_MACRO)
        self.assertIn("margin", check.suspect)

    def test_healthy_row_is_clean(self):
        check = assess_fundamentals(stock(ttm_pe=25.0, fwd_pe=20.0, de=0.3,
                                          margin=0.4), 19.0, NO_MACRO)
        self.assertEqual(check, FundamentalsCheck())


class MissingMovingAveragesTest(unittest.TestCase):
    """Issue 6: single-ticker yfinance downloads use MultiIndex columns."""

    def test_single_ticker_multiindex_close_becomes_a_series(self):
        idx = pd.date_range("2024-01-01", periods=400, freq="D")
        columns = pd.MultiIndex.from_tuples([("Close", "IREN"), ("High", "IREN")],
                                            names=["Price", "Ticker"])
        raw = pd.DataFrame({c: [float(i) for i in range(400)] for c in columns},
                           index=idx)
        close = _close_series(raw, "IREN")
        self.assertIsInstance(close, pd.Series)
        monthly = pd.Series([float(i) for i in range(30)],
                            index=pd.date_range("2024-01-31", periods=30, freq="ME"))
        self.assertTrue(all(v is not None for v in moving_averages(close, monthly)))

    def test_one_symbol_batch_with_ticker_first_columns(self):
        """``group_by="ticker"`` with one symbol gives (Ticker, Price) columns,
        where ``raw["Close"]`` raised KeyError and history was lost."""
        idx = pd.date_range("2024-01-01", periods=400, freq="D")
        columns = pd.MultiIndex.from_tuples([("IREN", "Close"), ("IREN", "High")],
                                            names=["Ticker", "Price"])
        raw = pd.DataFrame({c: [float(i) for i in range(400)] for c in columns},
                           index=idx)
        close = _close_series(raw, "IREN")
        self.assertIsInstance(close, pd.Series)
        self.assertEqual(close.iloc[-1], 399.0)

    def test_missing_symbol_never_inherits_another_ticker(self):
        idx = pd.date_range("2024-01-01", periods=3, freq="D")
        for names, key in ((["Price", "Ticker"], ("Close", "OTHER")),
                           (["Ticker", "Price"], ("OTHER", "Close"))):
            columns = pd.MultiIndex.from_tuples([key], names=names)
            raw = pd.DataFrame({columns[0]: [121.0, 122.0, 123.0]}, index=idx)
            self.assertTrue(_close_series(raw, "MISSING").empty, names)

    def test_one_symbol_batch_returns_d5_and_moving_averages(self):
        """End to end through ``fetch_batch`` with a one-symbol batch."""
        def frame(periods, freq):
            idx = pd.date_range("2023-01-02", periods=periods, freq=freq, tz="UTC")
            columns = pd.MultiIndex.from_tuples([("IREN", "Close")],
                                                names=["Ticker", "Price"])
            return pd.DataFrame({columns[0]: [10.0 + i for i in range(periods)]},
                                index=idx)

        class Client:
            def download(self, symbols, interval="1d", **_kw):
                return frame(40, "ME") if interval == "1mo" else frame(700, "D")

            def Ticker(self, _symbol):
                return type("T", (), {"info": {"currency": "USD"}})()

        live = fetch_batch(["IREN"], {}, {}, warn=lambda *_: None, client=Client(),
                           now=datetime(2026, 11, 22, 12, tzinfo=timezone.utc))
        self.assertIn("IREN", live.d5)
        self.assertIsNotNone(live.moving_averages["IREN"][0])
        self.assertIn("IREN", live.max_close)


class QualityGateTest(unittest.TestCase):
    """Issue 7: buy-zone rows with red flags become falling knives."""

    def test_red_flag_in_buy_zone_is_a_falling_knife(self):
        check = assess_fundamentals(stock(margin=-1.6), -0.91, NO_MACRO)
        self.assertEqual(summary_group(RANK_DEEP, check), GROUP_KNIFE)

    def test_clean_buy_zone_row_is_promoted(self):
        self.assertEqual(summary_group(RANK_AL1, FundamentalsCheck()), GROUP_BUY)

    def test_data_check_row_is_held_back(self):
        check = FundamentalsCheck(data_check=("X",), suspect=frozenset({"margin"}))
        self.assertEqual(summary_group(RANK_AL2, check), GROUP_CHECK)

    def test_funds_and_macro_rows_are_exempt(self):
        etf = stock(rtype="ETF", margin=-0.5)
        macro = stock(ticker="BTC", margin=-0.5)
        self.assertEqual(assess_fundamentals(etf, -1.0, NO_MACRO), FundamentalsCheck())
        self.assertEqual(assess_fundamentals(macro, -1.0, frozenset({"BTC"})),
                         FundamentalsCheck())


class BrokenAnchorTest(unittest.TestCase):
    """Issue 8: price below the anchor low."""

    def test_below_anchor_low_takes_precedence_over_deep_value(self):
        zone = classify_zone(32.92, 77.22, 166.63)
        self.assertEqual(zone.rank, RANK_BROKEN)
        self.assertIn("STRUCTURE BROKEN", zone.label)

    def test_broken_rows_never_reach_buy_zones(self):
        red = assess_fundamentals(stock(margin=-1.0), -1.0, NO_MACRO)
        self.assertEqual(summary_group(RANK_BROKEN, red), GROUP_BROKEN)
        self.assertEqual(summary_group(RANK_BROKEN, FundamentalsCheck()), GROUP_BROKEN)


class CompletedClosesTest(unittest.TestCase):
    """Issue 9: an unfinished session bar is excluded."""

    RUN = datetime(2026, 11, 20, 15, 45, tzinfo=timezone.utc)   # 15:45 London

    def test_exchange_mapping(self):
        self.assertEqual(exchange_for("BP.L"), "LSE")
        self.assertEqual(exchange_for("AAPL"), "US")
        self.assertEqual(exchange_for("BTC-USD"), "CRYPTO")
        self.assertEqual(exchange_for("CL=F"), "NYMEX")
        self.assertEqual(exchange_for("GRA.TO"), "TSX")

    def test_session_completion(self):
        today = date(2026, 11, 20)
        self.assertFalse(bar_is_complete(today, "LSE", self.RUN))
        self.assertFalse(bar_is_complete(today, "US", self.RUN))
        self.assertTrue(bar_is_complete(date(2026, 11, 19), "US", self.RUN))
        after_close = datetime(2026, 11, 20, 16, 35, tzinfo=timezone.utc)
        self.assertTrue(bar_is_complete(today, "LSE", after_close))
        self.assertFalse(bar_is_complete(today, "CRYPTO", after_close))

    def test_intraday_bar_is_dropped_and_flagged(self):
        closes = pd.Series([10.0, 11.0, 12.0],
                           index=pd.to_datetime(["2026-11-18", "2026-11-19",
                                                 "2026-11-20"]))
        kept, intraday = completed_closes(closes, "AAPL", self.RUN)
        self.assertTrue(intraday)
        self.assertEqual(list(kept), [10.0, 11.0])


class D5FlagTest(unittest.TestCase):
    """New feature: D5 EMA flag."""

    def test_d5_is_an_ema_of_completed_closes(self):
        values = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 16.0]
        closes = pd.Series(values, index=pd.date_range("2026-11-10", periods=7))
        view = d5_view(closes, "AAPL", intraday=False)
        expected = pd.Series(values).ewm(span=5, adjust=False).mean().iloc[-1]
        self.assertAlmostEqual(view.d5, round(expected, 2))
        self.assertEqual(view.close, 16.0)

    def test_too_little_history_gives_none(self):
        closes = pd.Series([1.0] * 5, index=pd.date_range("2026-11-10", periods=5))
        self.assertIsNone(d5_view(closes, "AAPL", intraday=False))

    def test_flag_labels(self):
        above = D5View(11.0, 10.0, 10.5, 10.0, "2026-11-20", "US")
        crossed_up = D5View(11.0, 10.0, 9.0, 10.0, "2026-11-20", "US")
        crossed_down = D5View(9.0, 10.0, 11.0, 10.0, "2026-11-20", "US")
        self.assertEqual(d5_flag(above), ("Close > D5", True, False))
        self.assertEqual(d5_flag(crossed_up), ("CROSSED ABOVE D5", True, True))
        self.assertEqual(d5_flag(crossed_down), ("CROSSED BELOW D5", False, True))
        self.assertIsNone(d5_flag(None))


class AlphaVantagePreferenceTest(unittest.TestCase):
    """Issue 5: verified Alpha Vantage EPS replaces the yfinance TTM P/E."""

    def test_verified_negative_eps_clears_ttm_pe(self):
        row = stock(ticker="IONQ", current=20.72, ttm_pe=12.0)
        live = LiveMarketData(prices={"IONQ": 20.72}, eps={"IONQ": (-1.5, True)})
        updated = portfolio.apply_live_overlay([row], live)[0]
        self.assertIsNone(updated.ttm_pe)

    def test_verified_positive_eps_recomputes_ttm_pe(self):
        row = stock(ticker="AAPL", current=200.0, ttm_pe=40.0)
        live = LiveMarketData(prices={"AAPL": 200.0}, eps={"AAPL": (8.0, True)},
                              eps_currency={"AAPL": "USD"})
        self.assertEqual(portfolio.apply_live_overlay([row], live)[0].ttm_pe, 25.0)

    def test_unknown_eps_unit_keeps_the_provider_pe(self):
        row = stock(ticker="BP.L", ccy="GBp", current=500.0, ttm_pe=40.0)
        live = LiveMarketData(prices={"BP.L": 500.0}, currency={"BP.L": "GBp"},
                              eps={"BP.L": (0.25, True)})
        self.assertEqual(portfolio.apply_live_overlay([row], live)[0].ttm_pe, 40.0)
        ctx = SheetContext(run_date="d", data_source="s", live=live)
        self.assertIsNone(ctx.eps_unit_for(row))
        self.assertEqual(eps_text(0.25, ctx.eps_unit_for(row), True), "0.25")

    def test_pounds_quote_and_eps_on_pence_row(self):
        """A GBP 12 quote with GBP 0.60 verified EPS on a pence row is 20x,
        not 2,000x, and the EPS cell shows GBP 0.60 in its own unit."""
        row = stock(asset="X plc", ticker="X.L", ccy="GBp", current=1100.0,
                    macro_lo=500.0, macro_hi=1500.0, ttm_pe=99.0)
        live = LiveMarketData(prices={"X.L": 12.0}, currency={"X.L": "GBP"},
                              eps={"X.L": (0.6, True)},
                              eps_currency={"X.L": "GBP"})
        updated = portfolio.apply_live_overlay([row], live)[0]
        self.assertEqual(updated.current, 1200.0)
        self.assertEqual(updated.ttm_pe, 20.0)

        from openpyxl import Workbook
        from workbook import alert_levels
        ws = Workbook().active
        ctx = SheetContext(run_date="d", data_source="s", live=live)
        alert_levels.render(ws, [updated], ctx)
        cells = [r for r in ws.iter_rows() if r[1].value == "X.L"]
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0][14].value, "GBP 0.60")

    def test_eps_unit_is_recorded_only_where_established(self):
        self.assertEqual(established_eps_unit("USD", "USD"), "USD")
        self.assertIsNone(established_eps_unit("GBp", "GBP"))
        self.assertIsNone(established_eps_unit("GBP", "GBP"))
        self.assertIsNone(established_eps_unit("CAD", "USD"))
        self.assertIsNone(established_eps_unit(None, "USD"))
        self.assertIsNone(established_eps_unit("USD", None))

    def test_alpha_vantage_unit_only_for_us_targets(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class Http:
            def get(self, _url, params=None, timeout=None):
                return Response({"Symbol": params["symbol"],
                                 "DilutedEPSTTM": "1.5", "Currency": "USD"
                                 if params["symbol"] == "AAPL" else "GBP"})

        units = {}
        out = fetch_alpha_vantage_eps(["AAPL", "BP.L"], {"BP.L": "BP.LON"}, "key",
                                      0, warn=lambda *_: None, http=Http(),
                                      sleeper=lambda _s: None, units=units)
        self.assertEqual(set(out), {"AAPL", "BP.L"})
        self.assertEqual(units, {"AAPL": "USD", "BP.L": None})


class CurrencyMismatchTest(unittest.TestCase):
    """A currency that cannot be scaled is withheld for that row only."""

    @staticmethod
    def _live():
        return LiveMarketData(
            prices={"GRA": 30.0},
            moving_averages={"GRA": (31.0, 32.0, 33.0, 34.0, 35.0)},
            currency={"GRA": "CAD"},
            max_close={"GRA": 60.0},
            d5={"GRA": D5View(30.0, 29.0, 28.0, 27.0, "2026-11-20", "TSX")},
            analyst={"GRA": ("Buy", 4, 40.0)},
            fundamentals={"GRA": (15.0, 12.0, 0.3, 0.1)},
        )

    def test_cad_quote_on_usd_row_is_withheld_and_warned(self):
        row = stock(ticker="GRA", ccy="USD", current=15.0)
        live = self._live()
        warnings = []
        portfolio.report_unit_conversions([row], live, warn=warnings.append)
        self.assertEqual(len(warnings), 1)
        for part in ("GRA", "CAD", "USD", "15.0"):
            self.assertIn(part, warnings[0])

        self.assertIsNone(live.price_for(row))
        self.assertIsNone(live.moving_averages_for(row))
        self.assertIsNone(live.d5_for(row))
        self.assertIsNone(live.max_close_for(row))
        self.assertEqual(live.analyst_for(row), ("Buy", 4, None))

        updated = portfolio.apply_live_overlay([row], live)[0]
        self.assertEqual(updated.current, 15.0)
        self.assertEqual(updated.ttm_pe, 15.0)   # dimensionless ratio kept

    def test_cad_and_usd_rows_sharing_a_ticker_in_both_orders(self):
        cad = stock(asset="Graphite CAD", ticker="GRA", ccy="CAD", current=25.0)
        usd = stock(asset="Graphite USD", ticker="GRA", ccy="USD", current=15.0)
        for rows in ([cad, usd], [usd, cad]):
            live = self._live()
            out = {r.asset: r for r in portfolio.apply_live_overlay(rows, live)}
            self.assertEqual(out["Graphite CAD"].current, 30.0)
            self.assertEqual(out["Graphite USD"].current, 15.0)
            self.assertEqual(live.moving_averages_for(cad)[0], 31.0)
            self.assertIsNone(live.moving_averages_for(usd))
            self.assertEqual(live.d5_for(cad).close, 30.0)
            self.assertIsNone(live.d5_for(usd))
            self.assertEqual(live.max_close_for(cad), 60.0)
            self.assertIsNone(live.max_close_for(usd))
            self.assertEqual(live.analyst_for(cad)[2], 40.0)
            self.assertIsNone(live.analyst_for(usd)[2])

            warnings = []
            portfolio.report_unit_conversions(rows, live, warn=warnings.append)
            self.assertEqual(len(warnings), 1)
            self.assertIn("Graphite USD", warnings[0])

    def test_pounds_and_pence_rows_sharing_a_ticker(self):
        pence = stock(asset="Pence", ticker="AB.L", ccy="GBp", current=1000.0,
                      macro_lo=500.0, macro_hi=1500.0)
        pounds = stock(asset="Pounds", ticker="AB.L", ccy="GBP", current=10.0,
                       macro_lo=5.0, macro_hi=15.0)
        for rows in ([pence, pounds], [pounds, pence]):
            live = LiveMarketData(prices={"AB.L": 1200.0}, currency={"AB.L": "GBp"},
                                  max_close={"AB.L": 1800.0})
            out = {r.asset: r for r in portfolio.apply_live_overlay(rows, live)}
            self.assertEqual(out["Pence"].current, 1200.0)
            self.assertEqual(out["Pounds"].current, 12.0)
            lifted, _updates = portfolio.apply_effective_ath(
                list(out.values()), live, warn=lambda *_: None)
            his = {r.asset: r.macro_hi for r in lifted}
            self.assertEqual(his, {"Pence": 1800.0, "Pounds": 18.0})

    def test_static_fallback_is_used_for_a_withheld_row(self):
        row = stock(ticker="GRA", ccy="USD", current=15.0)
        ctx = SheetContext(run_date="d", data_source="s", live=self._live(),
                           ma_data={"GRA": StaticEntry("USD", (1.0, 2.0, 3.0, 4.0, 5.0))})
        self.assertEqual(ctx.moving_averages_for(row).d200, 5.0)
        self.assertIsNone(ctx.d5_for(row))


class RefreshUnitsTest(unittest.TestCase):
    """``--add`` refreshes save prices in the row's stored unit."""

    def _refresh(self, row, snap, aliases=None, calls=None):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        def fetch(ticker, rec_map, warn=None, symbol=None):
            if calls is not None:
                calls.append((ticker, symbol))
            return snap

        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(add_tickers=(), live_refresh=True,
                                  portfolio_rows_path=Path(tmp) / "rows.json")
            rows, _changed = portfolio.refresh_portfolio_rows(
                cfg, LiveMarketData(), [row], {}, warn=lambda *_: None,
                fetch=fetch, aliases=aliases)
        return rows[0]

    @staticmethod
    def _snap(ticker, ccy, price):
        return TickerSnapshot(ticker, "Co", ccy, price, 1.0, 200.0,
                              (1.0, 2.0, 3.0, 4.0, 5.0), (11.1, 9.9, 0.5, 0.2),
                              ("Buy", 7, 15.0))

    def test_pounds_quote_on_pence_row_is_saved_in_pence(self):
        row = stock(ticker="AB.L", ccy="GBp", current=1100.0, source="added")
        saved = self._refresh(row, self._snap("AB.L", "GBP", 12.0))
        self.assertEqual(saved.current, 1200.0)

    def test_unconvertible_currency_keeps_stored_price(self):
        row = stock(ticker="GRA", ccy="USD", current=150.0, source="added")
        saved = self._refresh(row, self._snap("GRA", "CAD", 90.0))
        self.assertEqual(saved.current, 150.0)
        self.assertEqual(saved.ttm_pe, 11.1)

    def test_refresh_fetches_the_aliased_symbol(self):
        row = stock(ticker="BTC", ccy="USD", current=90000.0, source="added")
        calls = []
        saved = self._refresh(row, self._snap("BTC", "USD", 95000.0),
                              aliases={"BTC": "BTC-USD"}, calls=calls)
        self.assertEqual(calls, [("BTC", "BTC-USD")])
        self.assertEqual(saved.ticker, "BTC")
        self.assertEqual(saved.current, 95000.0)

    def test_fetch_ticker_queries_the_provider_symbol(self):
        seen = []

        class Handle:
            info = {"currency": "USD"}

            def history(self, **_kw):
                return pd.DataFrame()

        class Client:
            def Ticker(self, symbol):
                seen.append(symbol)
                return Handle()

            def download(self, symbol, **_kw):
                seen.append(symbol)
                return pd.DataFrame({"Close": [1.0]},
                                    index=pd.date_range("2026-01-02", periods=1))

        fetch_ticker("BTC", {}, warn=lambda *_: None, client=Client(),
                     symbol="BTC-USD")
        self.assertEqual(set(seen), {"BTC-USD"})


class DownBoundaryTest(unittest.TestCase):
    """A falling-direction price exactly on a level has hit that level."""

    def test_manual_levels_are_inclusive(self):
        levels = (100.0, 90.0, 80.0)
        for price, rank in ((100.0, RANK_AL1), (90.0, RANK_AL2), (80.0, RANK_AL3)):
            zone = classify_zone(price, manual=levels, is_manual=True)
            self.assertEqual(zone.rank, rank, price)

    def test_fib_levels_are_inclusive(self):
        base = classify_zone(150.0, 100.0, 200.0)
        for price, rank in ((base.al1, RANK_AL1), (base.al2, RANK_AL2),
                            (base.al3, RANK_AL3), (base.d4, RANK_DEEP)):
            self.assertEqual(classify_zone(price, 100.0, 200.0).rank, rank, price)

    def test_exact_anchor_low_is_not_broken(self):
        self.assertEqual(classify_zone(100.0, 100.0, 200.0).rank, RANK_DEEP)


class StaticFallbackUnitsTest(unittest.TestCase):
    """Static MAs and analyst targets are shown in each row's own unit."""

    pounds = stock(asset="Pounds", ticker="AB.L", ccy="GBP", current=12.0,
                   macro_lo=5.0, macro_hi=15.0)
    pence = stock(asset="Pence", ticker="AB.L", ccy="GBp", current=1200.0,
                  macro_lo=500.0, macro_hi=1500.0)

    def test_pounds_and_pence_rows_convert_the_same_fallback(self):
        ctx = SheetContext(
            run_date="d", data_source="s",
            ma_data={"AB.L": StaticEntry("GBP", (11.0, 10.5, 10.2, 9.9, 10.0))},
            analyst_data={"AB.L": StaticEntry("GBP", ("Buy", 6, 15.0))},
        )
        self.assertEqual(ctx.moving_averages_for(self.pounds).d200, 10.0)
        self.assertEqual(ctx.moving_averages_for(self.pence).d200, 1000.0)
        self.assertEqual(money(ctx.analyst_for(self.pounds).target, "GBP"), "GBP 15")
        self.assertEqual(money(ctx.analyst_for(self.pence).target, "GBp"), "1,500p")

    def test_entries_without_a_unit_are_withheld_with_one_warning(self):
        import json
        import tempfile
        from pathlib import Path
        from modules.data_store import load_unit_map

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analyst_data.json"
            path.write_text(json.dumps({
                "AB.L": ["Buy", 6, 15.0],                       # legacy array
                "CD.L": {"values": ["Hold", 3, 9.0]},           # object, no ccy
                "EF": {"ccy": "USD", "values": ["Buy", 9, 50.0]},
            }))
            warnings = []
            entries = load_unit_map(path, "analyst data", warn=warnings.append)

        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("analyst_data.json" in w for w in warnings))
        self.assertTrue(any("AB.L" in w for w in warnings))
        self.assertTrue(any("CD.L" in w for w in warnings))
        ctx = SheetContext(run_date="d", data_source="s", analyst_data=entries)
        withheld = ctx.analyst_for(self.pence)
        self.assertEqual((withheld.consensus, withheld.n_analysts, withheld.target),
                         ("Buy", 6, None))
        self.assertEqual(ctx.analyst_for(stock(ticker="EF")).target, 50.0)

    def test_declared_readings_only_match_the_same_reading(self):
        entry = StaticEntry("PCT", (4.0, 4.1, 4.2, 4.3, 4.4))
        ctx = SheetContext(run_date="d", data_source="s", ma_data={"TNX": entry})
        self.assertEqual(ctx.moving_averages_for(stock(ticker="TNX", ccy="PCT")).d200, 4.4)
        self.assertIsNone(ctx.moving_averages_for(stock(ticker="TNX", ccy="IDX")).d200)
        self.assertIsNone(ctx.moving_averages_for(stock(ticker="TNX", ccy="USD")).d200)
        self.assertEqual(declared_unit_factor("IDX", "IDX"), 1)
        self.assertIsNone(declared_unit_factor(None, "USD"))


class SwingQuoteTest(unittest.TestCase):
    """Fib Methodology resolves price in the swing's unit, whatever the row order."""

    swing = MacroSwingRow(ticker="AB.L", ccy="GBP", macro_lo=6.0, macro_hi=18.0)

    def _status(self, rows, live=None):
        from openpyxl import Workbook
        from workbook import methodology

        live = live or LiveMarketData()
        warnings = []
        quotes = portfolio.resolve_swing_quotes([self.swing], rows, live,
                                                warn=warnings.append)
        ws = Workbook().active
        methodology.render(ws, [self.swing],
                           SheetContext(run_date="d", data_source="s", live=live,
                                        swing_quotes=quotes))
        cells = [r for r in ws.iter_rows() if r[0].value == "AB.L"]
        return quotes["AB.L"][0], cells[0][5].value, warnings

    def _both_orders(self, rows, live=None):
        first = self._status(rows, live)
        second = self._status(list(reversed(rows)), live)
        self.assertEqual(first, second)
        return first

    def test_equivalent_prices_give_the_same_signal_in_both_orders(self):
        rows = [stock(asset="P", ticker="AB.L", ccy="GBP", current=12.0),
                stock(asset="Q", ticker="AB.L", ccy="GBp", current=1200.0)]
        current, status, _ = self._both_orders(rows)
        self.assertEqual(current, 12.0)
        self.assertIn("AL2 HIT", status)

    def test_exact_unit_row_is_preferred_over_a_conflicting_conversion(self):
        rows = [stock(asset="P", ticker="AB.L", ccy="GBP", current=12.0),
                stock(asset="Q", ticker="AB.L", ccy="GBp", current=1300.0)]
        current, status, warnings = self._both_orders(rows)
        self.assertEqual(current, 12.0)
        self.assertEqual(warnings, [])

    def test_equally_preferred_conflicting_prices_are_omitted(self):
        rows = [stock(asset="P", ticker="AB.L", ccy="GBp", current=1200.0),
                stock(asset="Q", ticker="AB.L", ccy="GBX", current=1300.0)]
        current, status, warnings = self._both_orders(rows)
        self.assertIsNone(current)
        self.assertEqual(status, "No current price this run")
        self.assertEqual(len(warnings), 1)
        self.assertIn("AB.L", warnings[0])

    def test_live_quote_is_preferred_and_converted(self):
        rows = [stock(asset="P", ticker="AB.L", ccy="GBp", current=1200.0),
                stock(asset="Q", ticker="AB.L", ccy="GBX", current=1300.0)]
        live = LiveMarketData(prices={"AB.L": 1250.0}, currency={"AB.L": "GBp"},
                              max_close={"AB.L": 2000.0})
        current, _status, warnings = self._both_orders(rows, live)
        self.assertEqual(current, 12.5)
        self.assertEqual(warnings, [])
        _cur, max_close = portfolio.resolve_swing_quote(self.swing, rows, live)
        self.assertEqual(max_close, 20.0)


class AthHighlightTest(unittest.TestCase):
    """Only the rows whose ATH was actually lifted are amber."""

    def test_highlight_follows_the_row_not_the_ticker(self):
        from openpyxl import Workbook
        from workbook import alert_levels
        from workbook.styles import CHECK_BG

        rows = [
            stock(asset="Excluded", ticker="EXC", current=150.0),
            PortfolioRow.from_dict({"asset": "SECTION", "rtype": "SECTION"}),
            stock(asset="Lifted", ticker="AB.L", ccy="GBp", current=1200.0,
                  macro_lo=500.0, macro_hi=1500.0),
            stock(asset="Kept", ticker="AB.L", ccy="USD", current=15.0,
                  macro_lo=5.0, macro_hi=18.0),
        ]
        live = LiveMarketData(currency={"AB.L": "GBp"}, max_close={"AB.L": 1800.0})
        lifted, updates = portfolio.apply_effective_ath(rows, live,
                                                        warn=lambda *_: None)
        self.assertEqual(list(updates), [lifted[2].identity(2)])

        ws = Workbook().active
        ctx = SheetContext(run_date="d", data_source="s", live=live,
                           excluded_tickers=frozenset({"EXC"}), ath_updates=updates)
        alert_levels.render(ws, lifted, ctx)
        fills = {r[0].value: r[5].fill.fgColor.rgb[-6:]
                 for r in ws.iter_rows() if r[0].value in ("Lifted", "Kept")}
        self.assertEqual(fills["Lifted"], CHECK_BG)
        self.assertNotEqual(fills["Kept"], CHECK_BG)


if __name__ == "__main__":
    unittest.main()
