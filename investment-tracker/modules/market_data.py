"""Provider integration and market-data normalization.

This module owns every call to yfinance and Alpha Vantage. Callers receive
normalized values and never see provider payload shapes.

Both the batch refresh and the ``--add`` refresh use the same download windows
and the same moving-average computation, so a ticker cannot end up with
different W20/W50/M20/M50/D200 values depending on which path fetched it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ── Download windows shared by every fetch path ──────────────────────────────
# Daily 2Y    -> D200 SMA and weekly EMAs (resampled to Friday close)
# Hourly 5D   -> current price (avoids stale daily-close and .info caching)
# Monthly 10Y -> monthly EMAs
DAILY_WINDOW = {"period": "2y", "interval": "1d"}
HOURLY_WINDOW = {"period": "5d", "interval": "60m"}
MONTHLY_WINDOW = {"period": "10y", "interval": "1mo"}

# Anchor derivation for --add needs a longer daily history than the MA windows.
ANCHOR_WINDOW = {"period": "5y", "interval": "1d"}
BEAR_WINDOW_START = "2022-09-01"
BEAR_WINDOW_END = "2022-12-31"


@dataclass
class LiveMarketData:
    """Opportunistically populated live values. Empty means use JSON fallback."""

    prices: dict = field(default_factory=dict)
    moving_averages: dict = field(default_factory=dict)
    fundamentals: dict = field(default_factory=dict)
    eps: dict = field(default_factory=dict)
    analyst: dict = field(default_factory=dict)

    def has_row_overlay(self):
        """True when there is anything worth overlaying onto portfolio rows."""
        return bool(self.prices or self.fundamentals)


# ─────────────────────────────────────────────────────────────────────────────
# Value normalization
# ─────────────────────────────────────────────────────────────────────────────
def to_float(value):
    try:
        return None if value is None else float(value)
    except Exception:
        return None


def rounded(value):
    """Round to 4dp for sub-$1 assets, 2dp otherwise. None on NaN or failure."""
    if value is None:
        return None
    try:
        f = float(value)
        return None if f != f else round(f, 4 if abs(f) < 1 else 2)
    except Exception:
        return None


def positive_pe(value):
    value = to_float(value)
    if value is None or value <= 0:
        return None
    return round(value, 1)


def debt_to_equity(info):
    """Return D/E as a decimal ratio from provider info."""
    de_raw = to_float(info.get("debtToEquity"))
    if de_raw is None:
        total_debt = to_float(info.get("totalDebt"))
        book_value = to_float(info.get("bookValue"))
        shares = to_float(
            info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        )
        if total_debt is not None and book_value and shares and (book_value * shares) > 0:
            de_raw = (total_debt / (book_value * shares)) * 100
    return round(de_raw / 100, 2) if de_raw is not None else None


def net_margin(info):
    """Return net margin as a decimal ratio from provider info."""
    margin = to_float(info.get("profitMargins"))
    if margin is None:
        net_income = to_float(info.get("netIncomeToCommon"))
        revenue = to_float(info.get("totalRevenue"))
        if net_income is not None and revenue and revenue > 0:
            margin = net_income / revenue
    return round(margin, 4) if margin is not None else None


def fundamentals_from_info(info):
    """Return (ttm_pe, fwd_pe, de, margin) from a provider info payload."""
    return (
        positive_pe(info.get("trailingPE")),
        positive_pe(info.get("forwardPE")),
        debt_to_equity(info),
        net_margin(info),
    )


def analyst_from_info(info, rec_map, default_consensus=None):
    """Return (consensus, n_analysts, mean_target) or None when unavailable."""
    consensus = rec_map.get((info.get("recommendationKey") or "").lower())
    if consensus is None:
        consensus = default_consensus
    if not consensus:
        return None
    n_analysts = info.get("numberOfAnalystOpinions")
    mean_target = info.get("targetMeanPrice")
    if default_consensus is None and not (n_analysts and mean_target):
        return None
    return (
        consensus,
        int(n_analysts or 0),
        round(float(mean_target), 2) if mean_target else None,
    )


def realtime_price(info):
    """Return the freshest price available on a provider info payload."""
    return rounded(
        info.get("regularMarketPrice")
        or info.get("currentPrice")
        or info.get("navPrice")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Moving averages — the single implementation used by every fetch path
# ─────────────────────────────────────────────────────────────────────────────
def moving_averages(daily_close, monthly_close):
    """Return (w20, w50, m20, m50, d200) from daily and monthly close series.

    Guard thresholds mirror the batch refresh: a value is only produced once the
    series is long enough for the average to mean anything.
    """
    d200 = (
        rounded(daily_close.rolling(200).mean().iloc[-1])
        if len(daily_close) >= 50 else None
    )

    weekly = daily_close.resample("W-FRI").last().dropna()
    w20 = (
        rounded(weekly.ewm(span=20, adjust=False).mean().iloc[-1])
        if len(weekly) >= 10 else None
    )
    w50 = (
        rounded(weekly.ewm(span=50, adjust=False).mean().iloc[-1])
        if len(weekly) >= 20 else None
    )

    m20 = (
        rounded(monthly_close.ewm(span=20, adjust=False).mean().iloc[-1])
        if len(monthly_close) >= 10 else None
    )
    m50 = (
        rounded(monthly_close.ewm(span=50, adjust=False).mean().iloc[-1])
        if len(monthly_close) >= 20 else None
    )

    return (w20, w50, m20, m50, d200)


def fib_anchors(history):
    """Derive (bear_low, cycle_high) from a long daily OHLC history.

    Both anchors come from intraday extremes so they are the same kind of price.
    Falls back to the full history when the ticker post-dates the 2022 window.
    """
    highs = history["High"].dropna()
    cycle_high = round(float(highs.max()), 2) if len(highs) else None

    bear_window = history.loc[BEAR_WINDOW_START:BEAR_WINDOW_END]
    source = bear_window if not bear_window.empty else history
    lows = source["Low"].dropna()
    bear_low = round(float(lows.min()), 2) if len(lows) else None

    return bear_low, cycle_high


# ─────────────────────────────────────────────────────────────────────────────
# yfinance fetch paths
# ─────────────────────────────────────────────────────────────────────────────
def _close_series(raw, symbol, multi):
    return (raw[symbol]["Close"] if multi else raw["Close"]).dropna()


def fetch_batch(tickers, aliases, rec_map, warn=print, client=None):
    """Refresh prices, MAs, fundamentals, EPS, and analyst data for many tickers.

    ``client`` defaults to the real ``yfinance`` module. Passing a stub exposing
    ``download`` and ``Ticker`` lets the orchestration be tested without network.
    """
    live = LiveMarketData()
    if not tickers:
        return live

    import contextlib
    import io

    yf = client
    if yf is None:
        try:
            import yfinance as yf
        except ImportError:
            warn("[Live Refresh] yfinance not installed.")
            warn("[Live Refresh] Run:  pip3 install yfinance pandas  "
                 "then re-run the script.")
            return live

    def symbol_for(ticker):
        return aliases.get(ticker, ticker)

    try:
        symbols = [symbol_for(t) for t in tickers]

        def download(label, window):
            warn(f"[Live Refresh] Fetching {label} ({len(symbols)} tickers)...")
            with contextlib.redirect_stdout(io.StringIO()):
                return yf.download(
                    symbols, auto_adjust=True, progress=False,
                    group_by="ticker", **window
                )

        raw_daily = download("daily data", DAILY_WINDOW)
        raw_hourly = download("hourly prices", HOURLY_WINDOW)
        raw_monthly = download("monthly data", MONTHLY_WINDOW)
        multi = len(symbols) > 1

        for ticker in tickers:
            symbol = symbol_for(ticker)
            try:
                daily = _close_series(raw_daily, symbol, multi)
                hourly = _close_series(raw_hourly, symbol, multi)
                monthly = _close_series(raw_monthly, symbol, multi)
                if daily.empty:
                    warn(f"  [skip] {ticker}: no daily data returned")
                    continue

                price_source = hourly if not hourly.empty else daily
                live.prices[ticker] = rounded(price_source.iloc[-1])
                live.moving_averages[ticker] = moving_averages(daily, monthly)
            except Exception as ex:
                warn(f"  [warn] {ticker}: {ex}")

        warn(
            f"[Live Refresh] Fetching fundamentals + analyst data "
            f"({len(tickers)} tickers)..."
        )
        for ticker in tickers:
            try:
                info = yf.Ticker(symbol_for(ticker)).info
                live.fundamentals[ticker] = fundamentals_from_info(info)

                # yfinance trailingEps is useful but is not Alpha Vantage
                # independent verification, so provenance is recorded as False.
                ttm_eps = to_float(info.get("trailingEps"))
                if ttm_eps is not None:
                    live.eps[ticker] = (round(ttm_eps, 2), False)

                price = realtime_price(info)
                if price is not None:
                    live.prices[ticker] = price

                analyst = analyst_from_info(info, rec_map)
                if analyst:
                    live.analyst[ticker] = analyst
            except Exception as ex:
                warn(f"  [warn] {ticker} fundamentals: {ex}")

        warn(
            f"[Live Refresh] Done — {len(live.prices)} prices | "
            f"{len(live.moving_averages)} MA sets | "
            f"{len(live.fundamentals)} fundamentals | "
            f"{len(live.analyst)} analyst sets."
        )
    except Exception as ex:
        warn(f"[Live Refresh] Failed ({ex}) — falling back to static JSON values.")

    return live


@dataclass
class TickerSnapshot:
    """Everything ``--add`` needs about a newly tracked ticker."""

    ticker: str
    name: str
    currency: str
    price: float
    bear_low: float
    cycle_high: float
    moving_averages: tuple
    fundamentals: tuple
    analyst: tuple


def fetch_ticker(ticker, rec_map, warn=print, client=None):
    """Fetch one ticker for ``--add``, reusing the shared MA computation.

    Price and moving averages come from the same windows the batch refresh uses.
    Only Fib anchor derivation is specific to ``--add``, because portfolio rows carry
    curated historical anchors instead of deriving them.

    ``client`` defaults to the real ``yfinance`` module; pass a stub to test.
    """
    import contextlib
    import io

    yf = client
    if yf is None:
        try:
            import yfinance as yf
        except ImportError:
            warn(f"[--add {ticker}] yfinance not installed.")
            return None

    try:
        handle = yf.Ticker(ticker)
        info = handle.info

        with contextlib.redirect_stdout(io.StringIO()):
            raw_daily = yf.download(ticker, auto_adjust=True, progress=False, **DAILY_WINDOW)
            raw_hourly = yf.download(ticker, auto_adjust=True, progress=False, **HOURLY_WINDOW)
            raw_monthly = yf.download(ticker, auto_adjust=True, progress=False, **MONTHLY_WINDOW)
            anchor_history = handle.history(**ANCHOR_WINDOW)

        daily = _close_series(raw_daily, ticker, multi=False)
        hourly = _close_series(raw_hourly, ticker, multi=False)
        monthly = _close_series(raw_monthly, ticker, multi=False)
        if daily.empty or anchor_history.empty:
            warn(f"[--add {ticker}] No price history — skipping.")
            return None

        price = realtime_price(info)
        if price is None:
            price_source = hourly if not hourly.empty else daily
            price = rounded(price_source.iloc[-1])

        bear_low, cycle_high = fib_anchors(anchor_history)
        if bear_low is None or cycle_high is None:
            warn(f"[--add {ticker}] Could not derive Fib anchors — skipping.")
            return None

        return TickerSnapshot(
            ticker=ticker,
            name=info.get("longName") or info.get("shortName") or ticker,
            currency=info.get("currency", "USD"),
            price=price,
            bear_low=bear_low,
            cycle_high=cycle_high,
            moving_averages=moving_averages(daily, monthly),
            fundamentals=fundamentals_from_info(info),
            analyst=analyst_from_info(info, rec_map, default_consensus="Hold"),
        )
    except Exception as ex:
        warn(f"[--add {ticker}] Failed: {ex}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Alpha Vantage — independent TTM EPS used to verify yfinance P/E ratios
# ─────────────────────────────────────────────────────────────────────────────
def alpha_vantage_targets(us_list, uk_map, daily_call_budget=25):
    """Return (targets, dropped_count) within the daily free-tier call budget.

    The free tier allows 25 calls/day. Fetching the full tracked list mostly
    returns rate-limit notices once the budget is spent, so the list is capped
    and the caller reports what was dropped.
    """
    targets = list(us_list) + list(uk_map.keys())
    if daily_call_budget and len(targets) > daily_call_budget:
        return targets[:daily_call_budget], len(targets) - daily_call_budget
    return targets, 0


def fetch_alpha_vantage_eps(targets, uk_map, api_key, sleep_seconds, warn=print,
                            http=None, sleeper=None):
    """Return {ticker: (ttm_eps, True)} for tickers Alpha Vantage can verify.

    ``http`` defaults to ``requests`` and ``sleeper`` to ``time.sleep``; both are
    injectable so rate-limit and payload handling can be tested without network
    or real delays.
    """
    results = {}
    if not (targets and api_key):
        return results

    import time

    if http is None:
        try:
            import requests as http
        except ImportError:
            warn("[Alpha Vantage EPS] requests library not installed. "
                 "Run:  pip3 install requests")
            return results
    if sleeper is None:
        sleeper = time.sleep

    warn(
        f"[Alpha Vantage EPS] Fetching {len(targets)} tickers "
        f"(sleep={sleep_seconds}s/call)..."
    )
    try:
        for idx, ticker in enumerate(targets):
            symbol = uk_map.get(ticker, ticker)   # remap .L → .LON for UK stocks
            try:
                response = http.get(
                    "https://www.alphavantage.co/query",
                    params={"function": "OVERVIEW", "symbol": symbol, "apikey": api_key},
                    timeout=15,
                )
                payload = response.json()

                if "Note" in payload or "Information" in payload or "Symbol" not in payload:
                    message = payload.get("Note") or payload.get("Information") or "no data"
                    warn(f"  [AV skip] {ticker}: {message[:80]}")
                else:
                    # DilutedEPSTTM is the authoritative TTM EPS field.
                    eps = to_float(payload.get("DilutedEPSTTM") or payload.get("EPS"))
                    if eps is not None:
                        results[ticker] = (round(eps, 2), True)
                        warn(f"  [AV] {ticker}: TTM EPS=${round(eps, 2)}")
            except Exception as ex:
                warn(f"  [AV warn] {ticker}: {ex}")

            if idx < len(targets) - 1:
                sleeper(sleep_seconds)

        warn(f"[Alpha Vantage EPS] Done — {len(results)} EPS values fetched.")
    except Exception as ex:
        warn(f"[Alpha Vantage EPS] Failed ({ex})")

    return results
