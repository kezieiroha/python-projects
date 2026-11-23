"""Portfolio assembly and mutation.

Owns the order in which rows are loaded, mutated, overlaid with live data, and
persisted. Nothing here touches openpyxl, and nothing here calls a provider
directly: live data arrives as an already-normalized ``LiveMarketData``.

Ordering matters and is deliberate:

1. Portfolio rows load from the single persisted collection.
2. ``--remove`` deletions are saved before any live overlay.
3. ``--add`` inserts and added-source refreshes are saved before any live overlay,
   with prices already in each row's stored unit.
4. Live values stay keyed by ticker in the provider's currency. Each row reads
   them converted into its own unit, or withheld when the currency cannot be
   converted, so rows sharing a ticker in different units are each correct.
5. The live overlay is applied to the in-memory rows only.
6. The caller then applies the effective ATH, again in memory only.

Nothing from steps 4 to 6 is ever written back to the JSON.
"""

from __future__ import annotations

from modules.calculations import (
    declared_unit_factor,
    effective_ath,
    is_known_unit,
    live_unit_factor,
    note_issues,
    same_declared_unit,
    scale_amount,
    unit_factor,
)
from modules.data_store import (
    load_portfolio_rows,
    save_portfolio_rows,
)
from modules.market_data import fetch_ticker
from modules.models import Direction, PortfolioRow, RowType

#: A cycle high more than this multiple of the stored ATH is treated as a
#: probable unit or data error rather than a genuine new high. A pence/pounds
#: error is 100x; genuine rallies past a stale ATH have reached about 3.5x
#: (LUNR), so the limit sits well between the two.
ATH_JUMP_LIMIT = 20.0


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


def report_unit_conversions(rows, live, warn=print):
    """Report, per row, any live values converted or withheld for its unit.

    Live data stays keyed by ticker in the provider's currency; each row reads
    it through ``LiveMarketData.*_for(row)``, which converts into ``row.ccy``.
    Two rows can share a ticker in different units (the same London line in
    pounds and in pence), so the decision is made for each row, never once per
    ticker.

    A pence/pounds difference is scaled. A genuine currency difference (a CAD
    quote on a row stored as USD), or a provider that reports no currency at
    all, cannot be resolved, so that row's live price, moving averages, D5,
    cycle high and analyst target are withheld and it keeps its stored
    values. The warning names the row, both units and the stored fallback
    price. Nothing wrong is shown, so the run continues.
    """
    for row in rows:
        if row.is_section or not live.has_amounts(row.ticker):
            continue
        provider_ccy = live.currency.get(row.ticker)
        factor = live.unit_factor_for(row)
        label = f"{row.ticker} ({row.asset})"
        if factor is None and provider_ccy is None:
            warn(f"[units] {label}: provider reported no currency; live prices "
                 f"withheld, using stored price {row.current}.")
        elif factor is None:
            warn(f"[units] {label}: provider quotes {provider_ccy} but the row is "
                 f"stored as {row.ccy}; live prices withheld, using stored price "
                 f"{row.current}. Check ccy and anchors in portfolio_rows.json.")
        elif factor != 1:
            warn(f"[units] {label}: provider quotes {provider_ccy}, row stored as "
                 f"{row.ccy}; live values scaled by {factor}.")


def verified_ttm_pe(row, current, live):
    """TTM P/E from Alpha Vantage verified EPS, or None to keep the provider P/E.

    Returns a (recompute, value) pair. ``recompute`` is False when the provider
    P/E must be kept: no verified EPS, no price, or units that are not both
    known and convertible. Both units are checked explicitly first because
    ``unit_factor`` returns 1 for an unknown unit, which here would silently
    divide a pence price by a pounds EPS (a 100x P/E).

    A verified negative EPS always clears the P/E (value None): a loss-maker
    has no meaningful P/E, and the sign does not depend on the unit.
    """
    eps = live.eps.get(row.ticker)
    if not (eps and eps[1] and current):
        return False, None
    value = eps[0]
    if value <= 0:
        return True, None
    eps_unit = live.eps_currency.get(row.ticker)
    if not (is_known_unit(row.ccy) and is_known_unit(eps_unit)):
        return False, None
    factor = unit_factor(row.ccy, eps_unit)   # price in row unit -> EPS unit
    if factor is None:
        return False, None
    return True, round(current * factor / value, 1)


def apply_live_overlay(rows, live):
    """Overlay live price and fundamentals onto rows.

    Historical Fib anchors, notes, and identity fields are never changed here.
    The price is converted into each row's own unit; a row whose currency
    cannot be converted keeps its stored price. Fundamentals ratios are
    dimensionless and only override when the live value is not None, which
    preserves the deliberate N/A intent on ETF and pre-IPO rows.

    TTM P/E is recomputed from verified EPS only when ``verified_ttm_pe``
    allows it; otherwise the provider P/E stands.
    """
    if not live.has_row_overlay():
        return rows

    updated = []
    for row in rows:
        changes = {}

        # Rows with no price and no entry in the fetch list (CRCL, TBC) simply
        # never appear in live.prices, so they are left alone.
        price = live.price_for(row)
        if price is not None:
            changes["current"] = price

        if row.ticker in live.fundamentals:
            ttm_pe, fwd_pe, de, margin = live.fundamentals[row.ticker]
            for name, value in (
                ("ttm_pe", ttm_pe), ("fwd_pe", fwd_pe),
                ("de", de), ("margin", margin),
            ):
                if value is not None:
                    changes[name] = value

        recompute, pe = verified_ttm_pe(row, changes.get("current", row.current), live)
        if recompute:
            changes["ttm_pe"] = pe

        updated.append(row.replace(**changes) if changes else row)
    return updated


def apply_effective_ath(rows, live, warn=print):
    """Lift stale stored ATHs to the cycle high since the dated anchor low.

    Effective ATH = max(stored ATH, highest completed-session intraday High on
    or after the row's ``macro_lo_date``), converted into the row's unit
    (``LiveMarketData.cycle_high_for``). This is the same definition
    ``fib_anchors`` uses for ``--add`` and the Fib Methodology sheet uses for
    swing anchors, so a peak before the low can never be promoted (LUNR's
    February 2023 spike against an April 2025 low).

    A row without a verified low date keeps its curated ATH: there is no
    automatic promotion. When live highs are available, one line reports how
    many such rows were skipped. Applied in memory only, so Fib levels, status
    and upside use the effective ATH on every sheet while the JSON stays as
    curated.

    Returns (rows, updates). ``updates`` maps each lifted row's identity,
    ``row.identity(index)`` with ``index`` its position in this full portfolio
    list, to (stored_ath, effective_ath). Keying by row rather than ticker
    means only the rows actually lifted are highlighted, even when another row
    sharing the ticker kept its stored ATH. Each lift is reported so the JSON
    can be updated by hand.

    Manual rows and up-direction rows are skipped: their levels are not derived
    from an ATH. A row whose currency cannot be converted, or whose history
    does not reach its low date, has no cycle high and keeps its stored ATH.
    """
    updates, reported, out = {}, set(), []
    undated = 0
    for index, row in enumerate(rows):
        ticker = _ticker_of(row)
        eligible = (not row.is_section and not row.is_manual and row.has_fib_anchors
                    and row.direction == Direction.DOWN)
        if eligible and not row.macro_lo_date and row.ticker in live.highs:
            undated += 1
        cycle_high = live.cycle_high_for(row) if eligible else None
        if cycle_high is None or cycle_high <= row.macro_hi:
            out.append(row)
            continue

        if cycle_high > ATH_JUMP_LIMIT * row.macro_hi:
            warn(f"[ATH] {ticker}: cycle high {cycle_high} is more than "
                 f"{ATH_JUMP_LIMIT:g}x the stored ATH {row.macro_hi}; ignored. "
                 f"Check units.")
            out.append(row)
            continue

        new_hi = effective_ath(row.macro_hi, cycle_high)
        if (ticker, row.ccy) not in reported:
            reported.add((ticker, row.ccy))
            warn(f"[ATH] {ticker} ({row.ccy}): stored cycle ATH {row.macro_hi} "
                 f"superseded by the highest High {new_hi} since the low on "
                 f"{row.macro_lo_date}. Update macro_hi in portfolio_rows.json.")
        lifted = row.replace(macro_hi=new_hi)
        updates[lifted.identity(index)] = (row.macro_hi, new_hi)
        out.append(lifted)
    if undated:
        warn(f"[ATH] {undated} row(s) have no macro_lo_date; stored cycle ATH "
             f"kept and automatic promotion skipped until the low is dated.")
    return out, updates


def _live_in_unit(store, live, ticker, ccy):
    """A raw live amount for ``ticker`` converted into ``ccy``, or None.

    Same strict rule as the rows: an unknown provider currency is None.
    """
    factor = live_unit_factor(live.currency.get(ticker), ccy)
    if factor is None or store.get(ticker) is None:
        return None
    return scale_amount(store[ticker], factor)


def resolve_swing_quote(swing, rows, live, warn=print):
    """(current, cycle high) for a swing anchor, in the swing's own unit.

    ``rows`` must be the stored rows captured before the live overlay
    (``build`` returns them). The overlay rows already hold live prices, so
    reading them would let a live quote rejected in step 1 back in as a
    "stored" price.

    The answer must not depend on portfolio row order, so:

    1. A usable live quote, converted directly from the provider's currency
       into ``swing.ccy``, is preferred.
    2. Otherwise stored row prices are used: rows in exactly the swing's unit
       first, else any row whose unit converts. If the preferred candidates
       disagree after conversion (for example 12 GBP and 1,300p), the price is
       ambiguous: a warning is printed and the current price is omitted.

    The cycle high comes from live history only, converted the same way, and
    follows the portfolio rule: the highest intraday High on or after the
    swing's ``macro_lo_date``, or None (stored high kept) when undated.
    """
    ticker = swing.ticker
    current = _live_in_unit(live.prices, live, ticker, swing.ccy)
    if current is None:
        priced = [r for r in rows
                  if not r.is_section and r.ticker == ticker and r.current is not None]
        exact = [r for r in priced if same_declared_unit(r.ccy, swing.ccy)]
        tier = exact or [r for r in priced
                         if declared_unit_factor(r.ccy, swing.ccy) is not None]
        values = sorted({
            scale_amount(r.current, declared_unit_factor(r.ccy, swing.ccy))
            for r in tier
        })
        if len(values) == 1:
            current = values[0]
        elif len(values) > 1:
            warn(f"[swing] {ticker}: stored prices disagree in {swing.ccy} "
                 f"({', '.join(f'{v:g}' for v in values)}); current price omitted "
                 f"on the Fib Methodology sheet.")
    return current, live.high_since(ticker, swing.ccy, swing.macro_lo_date)


def swing_key(swing):
    """Key for a resolved swing quote: ticker and unit.

    Two anchors for one ticker in different units (GBP and GBp) need their own
    converted quotes, so the ticker alone is not enough.
    """
    return (swing.ticker, swing.ccy)


def resolve_swing_quotes(swings, stored_rows, live, warn=print):
    """Map ``swing_key`` to ``resolve_swing_quote`` for the methodology sheet."""
    return {swing_key(s): resolve_swing_quote(s, stored_rows, live, warn=warn)
            for s in swings}


def report_note_issues(rows, warn=print):
    """Report every note or section header that quotes a price or a status.

    Returns the number of rows reported. Notes are free-text thesis only; the
    generator computes prices, levels and statuses on every run.
    """
    count = 0
    for row in rows:
        text = row.asset if row.is_section else row.notes
        issues = note_issues(text)
        if issues:
            count += 1
            label = "section" if row.is_section else (row.ticker or row.asset)
            shown = ", ".join(dict.fromkeys(issues))[:160]
            warn(f"[notes] {label}: contains {shown}")
    return count


def build_added_row(snapshot):
    """Turn a provider snapshot into a portfolio row.

    The note carries only the provenance tag. Status, prices and levels are
    computed on every run, so writing them into the note would make it stale
    from the next run onward.

    The low's date from ``fib_anchors`` is persisted as ``macro_lo_date`` so
    later runs can promote the cycle high by the dated rule.
    """
    ttm_pe, fwd_pe, de, margin = snapshot.fundamentals
    extra = {"source": "added"}
    if snapshot.low_date:
        extra["macro_lo_date"] = snapshot.low_date
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
        notes=f"[Auto-added via --add {snapshot.ticker}]",
        rtype=RowType.STOCK,
        extra=extra,
    )


def register_snapshot(live, snapshot):
    """Record a ``--add`` snapshot in live data so every sheet can use it."""
    live.prices[snapshot.ticker] = snapshot.price
    live.moving_averages[snapshot.ticker] = snapshot.moving_averages
    live.fundamentals[snapshot.ticker] = snapshot.fundamentals
    if snapshot.currency:
        # An unreported currency stays absent, so every reader withholds.
        live.currency[snapshot.ticker] = snapshot.currency
    if snapshot.analyst:
        live.analyst[snapshot.ticker] = snapshot.analyst
    if snapshot.d5 is not None:
        live.d5[snapshot.ticker] = snapshot.d5
    if snapshot.highs is not None:
        live.highs[snapshot.ticker] = snapshot.highs
    if snapshot.operating_income is not None:
        live.operating_income[snapshot.ticker] = snapshot.operating_income


def refreshed_price(row, snapshot):
    """The snapshot price in the row's stored unit, or the row's own price.

    The JSON must hold prices in the row's ``ccy``, because offline runs compare
    them with the stored anchors directly. A pence/pounds difference is scaled.
    The same strict rule as the live overlay applies before anything is
    written: a currency difference that cannot be scaled, or a snapshot with
    no reported currency, keeps the stored price (PCT and IDX readings excepted).
    """
    factor = live_unit_factor(snapshot.currency, row.ccy)
    if factor is None or snapshot.price is None:
        return row.current
    return snapshot.price if factor == 1 else round(snapshot.price * factor, 4)


def refresh_portfolio_rows(config, live, rows, rec_map, warn=print, fetch=fetch_ticker,
                           aliases=None):
    """Refresh source=added rows and process ``--add`` requests.

    Ticker is a provider lookup key, not a row identity. Refreshing a ticker
    overlays only price and fundamentals onto every matching row, preserving each
    row's own asset name, anchors, notes, type, and unknown JSON fields.

    ``aliases`` is ``yahoo_aliases`` from market_sources.json. The fetch uses
    the aliased provider symbol (``BTC-USD`` for ``BTC``), matching the batch
    refresh, while the portfolio ticker stays the storage and matching key.

    Returns a new list; the caller's list is never mutated. The file is only
    rewritten when a value actually changed, so an unchanged refresh leaves the
    tracked JSON alone instead of dirtying it on every run.
    """
    aliases = aliases or {}
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
            warn("[--add] --offline was used; cannot fetch new tickers. "
                 "Previously persisted tickers will still be included.")
        return rows, False

    changed = False
    for ticker in queue:
        snapshot = fetch(ticker, rec_map, warn=warn,
                         symbol=aliases.get(ticker, ticker))
        if snapshot is None:
            if ticker not in known_tickers:
                warn(f"[--add {ticker}] Not persisted because no prior row exists.")
            continue

        is_new = ticker in config.add_tickers and ticker not in known_tickers
        if is_new and not snapshot.currency:
            # Without a currency the row's unit, and so its anchors, cannot be
            # established. Defaulting to USD would persist a guess.
            warn(f"[--add {ticker}] Provider reported no currency; not "
                 f"persisted. Retry later or add the row by hand with its ccy.")
            continue

        register_snapshot(live, snapshot)
        warn(
            f"[--add {ticker}] Fetched OK: cur={snapshot.price}, "
            f"ATH={snapshot.cycle_high}, 2022 low={snapshot.bear_low}"
        )
        if not snapshot.currency:
            warn(f"[--add {ticker}] Provider reported no currency; stored "
                 f"price kept, ratios refreshed.")

        if is_new:
            rows.append(build_added_row(snapshot))
            known_tickers.add(ticker)
            changed = True
        else:
            # Refresh every row carrying this ticker, preserving each row's own
            # identity, anchors, and notes. The price is saved in the row's own
            # unit; the ratios are dimensionless and saved as they are.
            ttm_pe, fwd_pe, de, margin = snapshot.fundamentals
            for idx, row in enumerate(rows):
                if _ticker_of(row) != ticker:
                    continue
                refreshed = row.replace(
                    current=refreshed_price(row, snapshot),
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


def build(config, live, rec_map, warn=print, fetch=fetch_ticker, aliases=None):
    """Assemble the portfolio rows for rendering.

    Returns (rows, stored_rows). ``rows`` carry the live overlay. ``stored_rows``
    are the same rows just before it (after ``--remove``, ``--add`` and refresh,
    so prices are in each row's stored unit); anything that needs a stored
    fallback independent of live data, such as the swing quotes, uses them.

    ``aliases`` maps portfolio tickers to provider symbols for ``--add``.
    """
    rows = load_portfolio_rows(config.portfolio_rows_path)
    rows, removed = apply_removals(
        rows, config.remove_items, config.portfolio_rows_path, warn=warn
    )
    rows, _changed = refresh_portfolio_rows(
        config, live, rows, rec_map, warn=warn, fetch=fetch, aliases=aliases
    )
    stored_rows = list(rows)
    report_unit_conversions(stored_rows, live, warn=warn)
    rows = apply_live_overlay(stored_rows, live)

    if config.remove_items and not removed:
        warn("[--remove] No matching portfolio rows found.")

    return rows, stored_rows
