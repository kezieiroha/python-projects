"""Command parsing and run orchestration.

Execution flow:
1. Parse arguments and resolve run configuration.
2. Read editable portfolio/static market data from JSON sidecar files.
3. Optionally refresh prices, moving averages, fundamentals, EPS, and analyst data.
4. Assemble the portfolio, applying --exclude, --add, and --remove.
5. Render the workbook sheets and save.

Importing this module has no side effects: nothing here runs until ``main`` is
called. Historical Fib anchors stay data, not code — they live under workbook/data/.
"""

from __future__ import annotations

from modules import config, portfolio
from modules.data_store import (
    load_json_object,
    load_macro_swing_data,
    load_tuple_map,
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
        return f"Full live refresh via yfinance — {cfg.run_timestamp}"
    return (
        f"Partial live refresh via yfinance ({fetched}/{len(requested)} prices) "
        f"— {cfg.run_timestamp}"
    )


def fetch_list(cfg, sources):
    """Tickers to refresh. Excluded tickers are never fetched — they are not rendered."""
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
        live.eps.update(fetch_alpha_vantage_eps(
            targets,
            sources.get("alpha_vantage_uk", {}),
            cfg.av_key,
            cfg.av_sleep,
            warn=report,
        ))

    return live


def build_context(cfg, sources, live):
    """Merge live values over static JSON fallbacks into renderer-ready inputs.

    ma_data tuple:      (W20 EMA, W50 EMA, M20 EMA, M50 EMA, D200 SMA)
    analyst_data tuple: (consensus_label, num_analysts, mean_price_target)
    eps_data tuple:     (ttm_eps, is_verified_by_alpha_vantage)
    """
    ma_data = load_tuple_map(cfg.ma_data_path, "MA data")
    ma_data.update(live.moving_averages)

    analyst_data = load_tuple_map(cfg.analyst_data_path, "analyst data")
    analyst_data.update(live.analyst)

    # EPS is intentionally not a static sidecar file. It comes from live sources
    # or is derived from price / P/E while rendering.
    eps_data = dict(live.eps)

    return SheetContext(
        run_date=cfg.run_date,
        data_source=describe_source(cfg, live, fetch_list(cfg, sources)),
        excluded_tickers=cfg.excluded_tickers,
        ma_data=ma_data,
        analyst_data=analyst_data,
        eps_data=eps_data,
        macro_tickers=frozenset(macro_tickers(sources)),
        sentiment_tickers=frozenset(sentiment_tickers(sources)),
    )


def main(argv=None, report=print):
    args = config.build_parser().parse_args(argv)
    cfg = config.from_args(args)

    # Provider ticker lists, aliases, and classifications are data, so adding or
    # removing a tracked provider never requires changing this code.
    sources = load_json_object(cfg.market_sources_path, "market sources")

    live = collect_live_data(cfg, sources, report=report)
    rows = portfolio.build(
        cfg, live, sources.get("recommendation_map", {}), warn=report
    )
    ctx = build_context(cfg, sources, live)

    workbook = build_workbook(
        rows, load_macro_swing_data(cfg.macro_swing_path), ctx
    )
    workbook.save(cfg.output_path)
    report(f"Saved: {cfg.output_path}")
    return cfg.output_path


if __name__ == "__main__":
    main()
