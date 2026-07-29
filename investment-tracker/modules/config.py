"""Runtime settings, paths, and the injectable run clock.

Importing this module must not read data files or contact any provider. It only
resolves where things live and which run-time switches are active.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime


PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(PACKAGE_ROOT)

WORKBOOK_VERSION = "v21"
OUTPUT_PATTERN = "SIPP_Alert_Levels_{version}_{ts}.xlsx"


def build_parser():
    """Return the CLI parser. Kept separate so tests can inspect it."""
    parser = argparse.ArgumentParser(description="SIPP/ISA Dashboard generator")
    parser.add_argument(
        "--exclude", nargs="+", metavar="TICKER", default=[],
        help="Tickers to omit from this run, e.g. --exclude NKE INTC",
    )
    parser.add_argument(
        "--add", nargs="+", metavar="TICKER", default=[],
        help="Extra tickers to fetch live and append to Watchlist, e.g. --add ORCL ADBE",
    )
    parser.add_argument(
        "--remove", nargs="+", metavar="ASSET_OR_TICKER", default=[],
        help="Persistently remove portfolio rows by ticker or exact asset name, "
             "e.g. --remove SPCX SpaceX",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Disable live market refresh and build from local JSON fallback data.",
    )
    parser.add_argument(
        "--output", metavar="PATH",
        help="Write the workbook to this exact path instead of a timestamped filename.",
    )
    parser.add_argument(
        "--run-date", metavar="ISO_DATETIME",
        help="Use a deterministic run date/time for output text and default filename, "
             "e.g. 2026-07-28T09:30.",
    )
    parser.add_argument(
        "--data-dir", metavar="PATH",
        help="Read/write JSON sidecar data from this folder instead of ./workbook/data.",
    )
    return parser


@dataclass(frozen=True)
class RunConfig:
    """Everything a run needs to know before any data is loaded."""

    run_dt: datetime
    output_path: str
    data_dir: str
    live_refresh: bool
    excluded_tickers: frozenset
    add_tickers: tuple
    remove_items: tuple
    av_key: str
    av_sleep: float

    # ── Derived paths ────────────────────────────────────────────────────────
    @property
    def portfolio_rows_path(self):
        return os.path.join(self.data_dir, "portfolio_rows.json")

    @property
    def macro_swing_path(self):
        return os.path.join(self.data_dir, "macro_swing_data.json")

    @property
    def analyst_data_path(self):
        return os.path.join(self.data_dir, "analyst_data.json")

    @property
    def ma_data_path(self):
        return os.path.join(self.data_dir, "ma_data.json")

    @property
    def market_sources_path(self):
        return os.path.join(self.data_dir, "market_sources.json")

    # ── Derived run labels ───────────────────────────────────────────────────
    @property
    def run_date(self):
        return f"{self.run_dt.day} {self.run_dt.strftime('%b %Y')}"

    @property
    def run_timestamp(self):
        return self.run_dt.strftime("%d %b %Y %H:%M")


def from_args(args, now=None):
    """Build a RunConfig from parsed CLI args and the ambient clock."""
    if args.run_date:
        try:
            run_dt = datetime.fromisoformat(args.run_date)
        except ValueError as ex:
            raise SystemExit(
                f"--run-date must be ISO format, e.g. 2026-07-28T09:30: {ex}"
            ) from ex
    else:
        run_dt = now or datetime.now()

    data_dir = args.data_dir or os.path.join(REPO_ROOT, "workbook", "data")
    default_name = OUTPUT_PATTERN.format(
        version=WORKBOOK_VERSION, ts=run_dt.strftime("%d%m%y-%H%M")
    )
    output_path = args.output or os.path.join(REPO_ROOT, default_name)

    return RunConfig(
        run_dt=run_dt,
        output_path=output_path,
        data_dir=data_dir,
        live_refresh=not args.offline,
        excluded_tickers=frozenset(t.upper() for t in args.exclude),
        add_tickers=tuple(t.upper() for t in args.add),
        remove_items=tuple(t.upper() for t in args.remove),
        # Alpha Vantage is an optional independent EPS source. Free tier is
        # 25 calls/day at 5/min, hence the 13s default between calls.
        av_key=os.environ.get("AV_KEY", ""),
        av_sleep=float(os.environ.get("AV_SLEEP", "13")),
    )
