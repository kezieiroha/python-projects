"""Command parsing and run orchestration.

Execution flow:
1. Parse arguments and resolve run configuration.
2. Read editable portfolio/static market data from JSON sidecar files.
3. Optionally refresh prices, moving averages, D5, fundamentals, EPS, and
   analyst data.
4. Assemble the portfolio, applying --exclude, --add, --remove, unit
   normalisation and the effective ATH.
5. Report notes that quote prices or statuses, and rows failing the unit check.
6. Render the workbook sheets and save. A unit-check failure exits with code 1
   after the workbook is saved, so the problem is loud but the run still
   produces output.

Importing this module has no side effects: nothing here runs until ``main`` is
called. Historical Fib anchors stay data, not code; they live under workbook/data/.
"""

from __future__ import annotations

from datetime import date

from modules import config, portfolio
from modules.calculations import RANK_UNIT_CHECK, classify_row
from modules.data_store import (
    load_json_object,
    load_macro_swing_data,
    load_unit_map,
)
from modules.market_data import (
    LiveMarketData,
    alpha_vantage_targets,
    fetch_alpha_vantage_eps,
    fetch_batch,
)
from modules.market_sources import macro_tickers, sentiment_tickers
from workbook import SheetContext, build_workbook


def describe_source(cfg, live, requested):
    """Run-status label distinguishing full live, partial live, and static runs.

    Coverage counts only the tickers that were actually requested from the batch
    fetch. ``live.prices`` also accumulates ``--add`` snapshots, so counting its
    length would inflate the numerator past the denominator and misreport a
    partial refresh as full.
    """
    if not (cfg.live_refresh and live.prices):
        return "Static JSON fallback data"

    requested = list(requested)
    fetched = sum(1 for ticker in requested if ticker in live.prices)
    if fetched >= len(requested):
        return f"Full live refresh via yfinance ({cfg.run_timestamp})"
    return (
        f"Partial live refresh via yfinance ({fetched}/{len(requested)} prices) "
        f"({cfg.run_timestamp})"
    )


def fetch_list(cfg, sources):
    """Tickers to refresh. Excluded tickers are never fetched; they are not rendered."""
    return [
        t for t in sources.get("yahoo_fetch", [])
        if t.upper() not in cfg.excluded_tickers
    ]


def collect_live_data(cfg, sources, report=print):
    """Fetch live market data when enabled, returning empty containers otherwise."""
    if not cfg.live_refresh:
        return LiveMarketData()

    live = fetch_batch(
        fetch_list(cfg, sources),
        sources.get("yahoo_aliases", {}),
        sources.get("recommendation_map", {}),
        warn=report,
        now=cfg.run_dt_utc,
    )

    if cfg.av_key:
        targets, dropped = alpha_vantage_targets(
            sources.get("alpha_vantage_us", []),
            sources.get("alpha_vantage_uk", {}),
        )
        if dropped:
            report(
                f"[Alpha Vantage EPS] Capped at {len(targets)} tickers for the daily "
                f"free-tier budget; {dropped} skipped this run."
            )
        # Verified EPS replaces the yfinance value, and so does its unit: an
        # unknown Alpha Vantage unit must not inherit the yfinance one.
        av_units = {}
        live.eps.update(fetch_alpha_vantage_eps(
            targets,
            sources.get("alpha_vantage_uk", {}),
            cfg.av_key,
            cfg.av_sleep,
            warn=report,
            units=av_units,
        ))
        for ticker, unit in av_units.items():
            if unit:
                live.eps_currency[ticker] = unit
            else:
                live.eps_currency.pop(ticker, None)

    return live


def d5_basis(live):
    """Map each exchange to the latest completed session date its D5 uses."""
    latest = {}
    for view in live.d5.values():
        if view.session_date > latest.get(view.exchange, ""):
            latest[view.exchange] = view.session_date
    return {
        exchange: date.fromisoformat(iso).strftime("%d %b %Y")
        for exchange, iso in latest.items()
    }


def build_context(cfg, sources, live, ath_updates=None, swing_quotes=None,
                  report=print):
    """Bundle static JSON fallbacks and raw live data for the renderers.

    ma_data tuple:      (W20 EMA, W50 EMA, M20 EMA, M50 EMA, D200 SMA)
    analyst_data tuple: (consensus_label, num_analysts, mean_price_target)

    Live values are not merged into the static maps here. They stay in
    ``live``, in provider units, and the context converts them per row, so a
    row whose currency cannot be converted still falls back to its static data.
    Static entries carry their own unit; ``load_unit_map`` warns for any
    without one, and their price amounts are withheld.
    EPS is intentionally not a static sidecar file: it comes from live sources
    or is derived from price / P/E while rendering.
    """
    return SheetContext(
        run_date=cfg.run_date,
        data_source=describe_source(cfg, live, fetch_list(cfg, sources)),
        excluded_tickers=cfg.excluded_tickers,
        ma_data=load_unit_map(cfg.ma_data_path, "MA data", warn=report),
        analyst_data=load_unit_map(cfg.analyst_data_path, "analyst data",
                                   warn=report),
        live=live,
        macro_tickers=frozenset(macro_tickers(sources)),
        sentiment_tickers=frozenset(sentiment_tickers(sources)),
        ath_updates=dict(ath_updates or {}),
        swing_quotes=dict(swing_quotes or {}),
        d5_basis=d5_basis(live),
    )


def unit_check_failures(rows, excluded):
    """Return rows whose price and levels are about 100x apart."""
    return [
        row for row in rows
        if not row.is_section and row.ticker not in excluded
        and classify_row(row).rank == RANK_UNIT_CHECK
    ]


def main(argv=None, report=print):
    args = config.build_parser().parse_args(argv)
    cfg = config.from_args(args)

    # Provider ticker lists, aliases, and classifications are data, so adding or
    # removing a tracked provider never requires changing this code.
    sources = load_json_object(cfg.market_sources_path, "market sources")

    live = collect_live_data(cfg, sources, report=report)
    rows = portfolio.build(
        cfg, live, sources.get("recommendation_map", {}), warn=report,
        aliases=sources.get("yahoo_aliases", {}),
    )
    rows, ath_updates = portfolio.apply_effective_ath(rows, live, warn=report)
    swings = load_macro_swing_data(cfg.macro_swing_path)
    swing_quotes = portfolio.resolve_swing_quotes(swings, rows, live, warn=report)
    ctx = build_context(cfg, sources, live, ath_updates, swing_quotes, report=report)

    flagged = portfolio.report_note_issues(rows, warn=report)
    if flagged:
        report(f"[notes] {flagged} row(s) quote prices or statuses. Notes should "
               f"hold thesis text only; the generator computes the rest.")

    workbook = build_workbook(rows, swings, ctx)
    workbook.save(cfg.output_path)
    report(f"Saved: {cfg.output_path}")

    failures = unit_check_failures(rows, cfg.excluded_tickers)
    if failures:
        report("=" * 72)
        report("ERROR: UNIT CHECK FAILED. Price and alert levels differ by ~100x:")
        for row in failures:
            report(f"  {row.ticker}: current {row.current} {row.ccy}, "
                   f"anchors {row.macro_lo} to {row.macro_hi}")
        report("Fix the row's ccy and anchors in portfolio_rows.json.")
        report("=" * 72)
        raise SystemExit(1)

    return cfg.output_path


if __name__ == "__main__":
    main()
