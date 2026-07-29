import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from modules import portfolio
from modules.config import RunConfig
from modules.data_store import load_portfolio_rows
from modules.models import PortfolioRow
from modules.market_data import LiveMarketData, TickerSnapshot


def make_config(data_dir, **overrides):
    defaults = dict(
        run_dt=datetime(2026, 7, 28, 9, 30),
        output_path=str(Path(data_dir) / "out.xlsx"),
        data_dir=str(data_dir),
        live_refresh=True,
        excluded_tickers=frozenset(),
        add_tickers=(),
        remove_items=(),
        av_key="",
        av_sleep=0.0,
    )
    defaults.update(overrides)
    return RunConfig(**defaults)


def snapshot(ticker="ABC", price=99.0, name="Provider Name"):
    return TickerSnapshot(
        ticker=ticker,
        name=name,
        currency="USD",
        price=price,
        bear_low=1.0,
        cycle_high=200.0,
        moving_averages=(1.0, 2.0, 3.0, 4.0, 5.0),
        fundamentals=(11.1, 9.9, 0.5, 0.2),
        analyst=("Buy", 7, 150.0),
    )


def write_portfolio(data_dir, rows):
    path = Path(data_dir) / "portfolio_rows.json"
    path.write_text(json.dumps(rows, indent=2))
    return path


class LiveOverlayTest(unittest.TestCase):
    def test_overlay_preserves_anchors_notes_and_extra_fields(self):
        row = PortfolioRow.from_dict({
            "asset": "First", "ticker": "ABC", "ccy": "USD", "current": 10,
            "macro_lo": 5, "macro_hi": 20, "notes": "keep note",
            "conviction": "high",
        })
        live = LiveMarketData(
            prices={"ABC": 42.0},
            fundamentals={"ABC": (12.0, 8.0, 0.4, 0.15)},
        )

        updated = portfolio.apply_live_overlay([row], live)[0].to_dict()

        self.assertEqual(updated["current"], 42.0)
        self.assertEqual(updated["ttm_pe"], 12.0)
        self.assertEqual(updated["macro_lo"], 5)
        self.assertEqual(updated["macro_hi"], 20)
        self.assertEqual(updated["notes"], "keep note")
        self.assertEqual(updated["conviction"], "high")

    def test_overlay_keeps_static_value_when_live_field_is_none(self):
        row = PortfolioRow.from_dict({
            "asset": "ETF", "ticker": "XYZ", "ccy": "USD", "current": 10,
            "ttm_pe": None, "de": 0.9,
        })
        live = LiveMarketData(
            prices={"XYZ": 11.0},
            fundamentals={"XYZ": (None, None, None, None)},
        )

        updated = portfolio.apply_live_overlay([row], live)[0].to_dict()

        self.assertEqual(updated["current"], 11.0)
        self.assertIsNone(updated["ttm_pe"])
        self.assertEqual(updated["de"], 0.9)


class AddedRowRefreshTest(unittest.TestCase):
    """The --add refresh path, reachable now that the fetch is injectable."""

    def test_refresh_keeps_same_ticker_rows_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_portfolio(tmp, [
                {"asset": "First", "ticker": "ABC", "ccy": "USD", "current": 10,
                 "macro_lo": 5, "macro_hi": 20, "notes": "n1",
                 "rtype": "STOCK", "manual": None, "source": "added"},
                {"asset": "Second", "ticker": "ABC", "ccy": "USD", "current": 12,
                 "macro_lo": 6, "macro_hi": 24, "notes": "n2",
                 "rtype": "STOCK", "manual": None, "source": "added"},
            ])
            cfg = make_config(tmp)
            live = LiveMarketData()
            rows = load_portfolio_rows(cfg.portfolio_rows_path)

            refreshed, _changed = portfolio.refresh_portfolio_rows(
                cfg, live, rows, {}, warn=lambda *_: None,
                fetch=lambda ticker, rec_map, warn=None: snapshot(ticker),
            )

            rows = [row.to_dict() for row in refreshed]
            self.assertEqual([r["asset"] for r in rows], ["First", "Second"])
            # Price and fundamentals refresh; identity, anchors, notes do not.
            self.assertEqual([r["current"] for r in rows], [99.0, 99.0])
            self.assertEqual([r["macro_lo"] for r in rows], [5, 6])
            self.assertEqual([r["notes"] for r in rows], ["n1", "n2"])

            persisted = [r.to_dict() for r in load_portfolio_rows(cfg.portfolio_rows_path)]
            self.assertEqual([r["asset"] for r in persisted], ["First", "Second"])

    def test_new_add_ticker_is_appended_with_generated_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_portfolio(tmp, [])
            cfg = make_config(tmp, add_tickers=("NEW",))
            live = LiveMarketData()

            refreshed, _changed = portfolio.refresh_portfolio_rows(
                cfg, live, [], {}, warn=lambda *_: None,
                fetch=lambda ticker, rec_map, warn=None: snapshot(ticker, name="New Co"),
            )

            rows = [row.to_dict() for row in refreshed]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["asset"], "New Co")
            self.assertEqual(rows[0]["source"], "added")
            self.assertIn("[Auto-added via --add NEW]", rows[0]["notes"])
            # The snapshot is registered so the run header reflects the fetch.
            self.assertEqual(live.prices["NEW"], 99.0)

    def test_offline_run_keeps_persisted_rows_without_fetching(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_portfolio(tmp, [
                {"asset": "First", "ticker": "ABC", "ccy": "USD", "current": 10,
                 "macro_lo": 5, "macro_hi": 20, "notes": "n1",
                 "rtype": "STOCK", "manual": None, "source": "added"},
            ])
            cfg = make_config(tmp, live_refresh=False)
            rows = load_portfolio_rows(cfg.portfolio_rows_path)

            def explode(*_args, **_kwargs):
                raise AssertionError("offline run must not fetch")

            refreshed, _changed = portfolio.refresh_portfolio_rows(
                cfg, LiveMarketData(), rows, {}, warn=lambda *_: None, fetch=explode,
            )

            rows = [row.to_dict() for row in refreshed]
            self.assertEqual([r["asset"] for r in rows], ["First"])

    def test_failed_fetch_leaves_existing_row_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_portfolio(tmp, [
                {"asset": "First", "ticker": "ABC", "ccy": "USD", "current": 10,
                 "macro_lo": 5, "macro_hi": 20, "notes": "n1",
                 "rtype": "STOCK", "manual": None, "source": "added"},
            ])
            cfg = make_config(tmp)
            rows = load_portfolio_rows(cfg.portfolio_rows_path)

            refreshed, _changed = portfolio.refresh_portfolio_rows(
                cfg, LiveMarketData(), rows, {}, warn=lambda *_: None,
                fetch=lambda ticker, rec_map, warn=None: None,
            )

            rows = [row.to_dict() for row in refreshed]
            self.assertEqual([r["asset"] for r in rows], ["First"])
            self.assertEqual(rows[0]["current"], 10)

    def test_build_renders_source_added_row_even_when_ticker_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_portfolio(tmp, [
                {"asset": "Base Placeholder", "ticker": "TBC", "ccy": "USD",
                 "current": 1, "macro_lo": 1, "macro_hi": 2,
                 "notes": "base", "rtype": "STOCK", "manual": None},
                {"asset": "Added Placeholder", "ticker": "TBC", "ccy": "USD",
                 "current": 3, "macro_lo": 1, "macro_hi": 4,
                 "notes": "added", "rtype": "STOCK", "manual": None,
                 "source": "added"},
            ])
            cfg = make_config(tmp, live_refresh=False)

            rows = portfolio.build(cfg, LiveMarketData(), {}, warn=lambda *_: None)

            self.assertEqual(
                [row.asset for row in rows],
                ["Base Placeholder", "Added Placeholder"],
            )


class PersistenceChurnTest(unittest.TestCase):
    """The tracked JSON must not be rewritten when nothing actually changed."""

    def _added_row(self, current=10):
        return {"asset": "SpaceX", "ticker": "SPCX", "ccy": "USD",
                "current": current, "macro_lo": 5, "macro_hi": 20, "notes": "n",
                "rtype": "STOCK", "manual": None, "source": "added"}

    def test_unchanged_refresh_leaves_the_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_portfolio(tmp, [self._added_row(current=99.0)])
            before = path.read_bytes()
            mtime = path.stat().st_mtime_ns
            cfg = make_config(tmp)

            # Snapshot carries exactly the values already on disk.
            same = TickerSnapshot(
                ticker="SPCX", name="SpaceX", currency="USD", price=99.0,
                bear_low=5, cycle_high=20,
                moving_averages=(1.0, 2.0, 3.0, 4.0, 5.0),
                fundamentals=(11.1, 9.9, 0.5, 0.2), analyst=None,
            )
            rows = [PortfolioRow.from_dict(self._added_row(current=99.0))]
            rows = [rows[0].replace(ttm_pe=11.1, fwd_pe=9.9, de=0.5, margin=0.2)]

            _rows, changed = portfolio.refresh_portfolio_rows(
                cfg, LiveMarketData(), rows, {}, warn=lambda *_: None,
                fetch=lambda *_a, **_k: same,
            )

            self.assertFalse(changed)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_changed_refresh_does_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_portfolio(tmp, [self._added_row(current=10)])
            cfg = make_config(tmp)
            rows = [PortfolioRow.from_dict(self._added_row(current=10))]

            _rows, changed = portfolio.refresh_portfolio_rows(
                cfg, LiveMarketData(), rows, {}, warn=lambda *_: None,
                fetch=lambda *_a, **_k: snapshot("SPCX", price=42.0),
            )

            self.assertTrue(changed)
            self.assertEqual(json.loads(path.read_text())[0]["current"], 42.0)


class NoMutationTest(unittest.TestCase):
    """Assembly steps return new lists; the caller's list is never mutated."""

    def test_refresh_does_not_mutate_the_input_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_portfolio(tmp, [])
            cfg = make_config(tmp, add_tickers=("NEW",))
            original = [PortfolioRow(asset="Existing", ticker="EXI", current=1)]
            snapshot_of = list(original)

            returned, _changed = portfolio.refresh_portfolio_rows(
                cfg, LiveMarketData(), original, {}, warn=lambda *_: None,
                fetch=lambda ticker, *_a, **_k: snapshot(ticker, name="New Co"),
            )

            self.assertEqual(original, snapshot_of, "input list was mutated")
            self.assertEqual(len(returned), 2)
            self.assertIsNot(returned, original)

    def test_removals_do_not_mutate_the_input_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "portfolio_rows.json"
            path.write_text("[]")
            original = [
                PortfolioRow(asset="Keep", ticker="KEEP"),
                PortfolioRow(asset="Drop", ticker="DROP"),
            ]
            before = list(original)

            kept, removed = portfolio.apply_removals(
                original, ("DROP",), path, warn=lambda *_: None
            )

            self.assertEqual(original, before, "input list was mutated")
            self.assertEqual(removed, 1)
            self.assertEqual([r.ticker for r in kept], ["KEEP"])


if __name__ == "__main__":
    unittest.main()
