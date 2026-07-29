"""Portfolio assembly and mutation.

Owns the order in which rows are loaded, mutated, overlaid with live data, and
persisted. Nothing here touches openpyxl, and nothing here calls a provider
directly — live data arrives as an already-normalized ``LiveMarketData``.

Ordering matters and is deliberate:

1. Portfolio rows load from the single persisted collection.
2. ``--remove`` deletions are saved before any live overlay.
3. ``--add`` inserts and added-source refreshes are saved before any live overlay.
4. The live overlay is applied to the in-memory rows only.
"""

from __future__ import annotations

from modules.calculations import classify_zone
from modules.data_store import (
    load_portfolio_rows,
    save_portfolio_rows,
)
from modules.market_data import fetch_ticker
from modules.models import PortfolioRow, RowType


def _ticker_of(row):
    return (row.ticker or "").strip().upper()


def _is_added_source(row):
    return row.extra.get("source") == "added"


def apply_removals(rows, removals, path, warn=print):
    """Delete matching portfolio rows and persist.

    Returns a new (rows, removed_count); the caller's list is never mutated.
    """
    if not removals:
        return list(rows), 0

    requested = set(removals)
    kept = [row for row in rows if not row.matches(requested)]
    removed = len(rows) - len(kept)
    if removed:
        save_portfolio_rows(path, kept)
        warn(f"[--remove] Removed {removed} portfolio row(s) from {path}")
    return kept, removed


def apply_live_overlay(rows, live):
    """Overlay live price and fundamentals onto rows.

    Historical Fib anchors, notes, and identity fields are never changed here.
    Fundamentals only override when the live value is not None, which preserves
    the deliberate N/A intent on ETF and pre-IPO rows.
    """
    if not live.has_row_overlay():
        return rows

    updated = []
    for row in rows:
        changes = {}

        # Rows with no price and no entry in the fetch list (CRCL, TBC) simply
        # never appear in live.prices, so they are left alone.
        if row.ticker in live.prices:
            changes["current"] = live.prices[row.ticker]

        if row.ticker in live.fundamentals:
            ttm_pe, fwd_pe, de, margin = live.fundamentals[row.ticker]
            for name, value in (
                ("ttm_pe", ttm_pe), ("fwd_pe", fwd_pe),
                ("de", de), ("margin", margin),
            ):
                if value is not None:
                    changes[name] = value

        updated.append(row.replace(**changes) if changes else row)
    return updated


def build_added_row(snapshot):
    """Turn a provider snapshot into a portfolio row with a generated note."""
    zone = classify_zone(snapshot.price, snapshot.bear_low, snapshot.cycle_high)
    ttm_pe, fwd_pe, de, margin = snapshot.fundamentals
    w20, w50, m20, m50, d200 = snapshot.moving_averages

    note = (
        f"{zone.label} "
        f"Macro Fib (2022 low {snapshot.bear_low} -> ATH {snapshot.cycle_high}): "
        f"AL1={zone.al1} | AL2={zone.al2} | AL3={zone.al3} | 78.6%={zone.d4}. "
        f"D200={d200} | W20={w20} / W50={w50} / M20={m20} / M50={m50}. "
        f"TTM P/E={ttm_pe or 'N/A'} | Fwd P/E={fwd_pe or 'N/A'} | "
        f"D/E={de or 'N/A'} | "
        f"Net margin={round(margin * 100, 1) if margin else 'N/A'}%. "
        f"[Auto-added via --add {snapshot.ticker}]"
    )
    return PortfolioRow(
        asset=snapshot.name,
        ticker=snapshot.ticker,
        ccy=snapshot.currency,
        current=snapshot.price,
        macro_lo=snapshot.bear_low,
        macro_hi=snapshot.cycle_high,
        ttm_pe=ttm_pe,
        fwd_pe=fwd_pe,
        de=de,
        margin=margin,
        notes=note,
        rtype=RowType.STOCK,
        extra={"source": "added"},
    )


def register_snapshot(live, snapshot):
    """Record a ``--add`` snapshot in live data so the run header reflects it."""
    live.prices[snapshot.ticker] = snapshot.price
    live.moving_averages[snapshot.ticker] = snapshot.moving_averages
    live.fundamentals[snapshot.ticker] = snapshot.fundamentals
    if snapshot.analyst:
        live.analyst[snapshot.ticker] = snapshot.analyst


def refresh_portfolio_rows(config, live, rows, rec_map, warn=print, fetch=fetch_ticker):
    """Refresh source=added rows and process ``--add`` requests.

    Ticker is a provider lookup key, not a row identity. Refreshing a ticker
    overlays only price and fundamentals onto every matching row, preserving each
    row's own asset name, anchors, notes, type, and unknown JSON fields.

    Returns a new list; the caller's list is never mutated. The file is only
    rewritten when a value actually changed, so an unchanged refresh leaves the
    tracked JSON alone instead of dirtying it on every run.
    """
    rows = list(rows)
    known_tickers = {_ticker_of(row) for row in rows if _ticker_of(row)}

    # Refresh persisted additions first so plain runs keep generated rows current,
    # then append any newly requested tickers.
    queue = []
    for row in rows:
        if not _is_added_source(row):
            continue
        ticker = _ticker_of(row)
        if ticker and ticker not in queue:
            queue.append(ticker)
    for ticker in config.add_tickers:
        if ticker not in queue:
            queue.append(ticker)

    if not queue:
        return rows, False

    if not config.live_refresh:
        if config.add_tickers:
            warn("[--add] --offline was used — cannot fetch new tickers. "
                 "Previously persisted tickers will still be included.")
        return rows, False

    changed = False
    for ticker in queue:
        snapshot = fetch(ticker, rec_map, warn=warn)
        if snapshot is None:
            if ticker not in known_tickers:
                warn(f"[--add {ticker}] Not persisted because no prior row exists.")
            continue

        register_snapshot(live, snapshot)
        warn(
            f"[--add {ticker}] Fetched OK — cur={snapshot.price}, "
            f"ATH={snapshot.cycle_high}, 2022 low={snapshot.bear_low}"
        )

        if ticker in config.add_tickers and ticker not in known_tickers:
            rows.append(build_added_row(snapshot))
            known_tickers.add(ticker)
            changed = True
        else:
            # Refresh every row carrying this ticker, preserving each row's own
            # identity, anchors, and notes.
            ttm_pe, fwd_pe, de, margin = snapshot.fundamentals
            for idx, row in enumerate(rows):
                if _ticker_of(row) != ticker:
                    continue
                refreshed = row.replace(
                    current=snapshot.price,
                    ttm_pe=ttm_pe,
                    fwd_pe=fwd_pe,
                    de=de,
                    margin=margin,
                )
                if refreshed != row:
                    rows[idx] = refreshed
                    changed = True

    if changed:
        save_portfolio_rows(config.portfolio_rows_path, rows)
        warn(f"[--add] Persisted {len(rows)} portfolio rows "
             f"to {config.portfolio_rows_path}")

    return rows, changed


def build(config, live, rec_map, warn=print, fetch=fetch_ticker):
    """Assemble the final portfolio rows for rendering."""
    rows = load_portfolio_rows(config.portfolio_rows_path)
    rows, removed = apply_removals(
        rows, config.remove_items, config.portfolio_rows_path, warn=warn
    )
    rows, _changed = refresh_portfolio_rows(
        config, live, rows, rec_map, warn=warn, fetch=fetch
    )
    rows = apply_live_overlay(rows, live)

    if config.remove_items and not removed:
        warn("[--remove] No matching portfolio rows found.")

    return rows
