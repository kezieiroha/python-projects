"""Provider integration and market-data normalization.

This module owns every call to yfinance and Alpha Vantage. Callers receive
normalized values and never see provider payload shapes.

Both the batch refresh and the ``--add`` refresh use the same download windows
and the same per-series computation (``series_metrics``), so a ticker cannot end
up with different moving averages, D5 values or highest close depending on
which path fetched it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from modules.calculations import (
    ANCHOR_START,
    bar_is_complete,
    established_eps_unit,
    exchange_for,
    live_unit_factor,
    scale_amount,
)
from modules.models import D5View


# -- Download windows shared by every fetch path ------------------------------
# Daily from the anchor-low window -> highest close since the anchor low, the
#                                     D5 EMA, and (on the last 2 years only)
#                                     D200 SMA and weekly EMAs
# Hourly 5D                        -> current price (avoids stale daily close)
# Monthly 10Y                      -> monthly EMAs
DAILY_WINDOW = {"start": ANCHOR_START, "interval": "1d"}
HOURLY_WINDOW = {"period": "5d", "interval": "60m"}
MONTHLY_WINDOW = {"period": "10y", "interval": "1mo"}

#: D200 and the weekly EMAs are computed on this trailing window, matching the
#: 2-year daily download they were originally defined on.
MA_DAILY_YEARS = 2

#: The D5 EMA span, in completed daily sessions.
D5_SPAN = 5

# Anchor derivation for --add needs a longer daily history than the MA windows.
ANCHOR_WINDOW = {"period": "5y", "interval": "1d"}
BEAR_WINDOW_START = ANCHOR_START
BEAR_WINDOW_END = "2022-12-31"


@dataclass
class LiveMarketData:
    """Opportunistically populated live values. Empty means use JSON fallback.

    All dicts are keyed by the script ticker, not the provider symbol, and hold
    raw provider values in the provider's quote currency (``currency``). They
    are never rewritten into a row's unit, because rows sharing a ticker may
    store different units (the same London line in pounds and in pence).

    Readers use the ``*_for(row)`` methods, which convert each value into that
    row's stored unit (``row.ccy``). For a currency-denominated row the
    provider currency must be known and convertible; otherwise (no currency
    reported, or a CAD quote on a USD row) they return None, so the row falls
    back to its stored values instead of mixing or guessing units. Rows whose
    unit is a reading rather than a currency (PCT yield, IDX level) take the
    live value as it is. Dimensionless values (fundamentals ratios, analyst
    consensus and count) are never withheld.

    ``eps_currency`` is the reporting unit of ``eps``, recorded only where it is
    established; a missing entry means the unit is unknown.

    ``operating_income`` holds a verified trailing operating income (four
    reported quarters summed) only for tickers where the provider showed
    positive net income alongside an operating loss. Only its sign is used,
    so it is not unit-converted.
    """

    prices: dict = field(default_factory=dict)
    moving_averages: dict = field(default_factory=dict)
    fundamentals: dict = field(default_factory=dict)
    eps: dict = field(default_factory=dict)
    eps_currency: dict = field(default_factory=dict)
    analyst: dict = field(default_factory=dict)
    currency: dict = field(default_factory=dict)
    d5: dict = field(default_factory=dict)
    max_close: dict = field(default_factory=dict)
    operating_income: dict = field(default_factory=dict)

    def has_row_overlay(self):
        """True when there is anything worth overlaying onto portfolio rows."""
        return bool(self.prices or self.fundamentals or self.eps)

    def unit_factor_for(self, row):
        """Multiplier from provider unit to ``row.ccy``, or None to withhold.

        Strict for currency rows: an unknown provider currency is None, never
        "assume the same unit", because a unitless quote could be in pence,
        pounds or another currency entirely.
        """
        return live_unit_factor(self.currency.get(row.ticker), row.ccy)

    def has_amounts(self, ticker):
        """True when any currency-denominated live value exists for ``ticker``."""
        return any(ticker in store for store in (
            self.prices, self.moving_averages, self.d5,
            self.max_close, self.analyst))

    def price_for(self, row):
        """Live price in the row's unit, or None (absent or withheld)."""
        factor = self.unit_factor_for(row)
        if factor is None or row.ticker not in self.prices:
            return None
        return scale_amount(self.prices[row.ticker], factor)

    def moving_averages_for(self, row):
        """Live MA tuple in the row's unit, or None (absent or withheld)."""
        factor = self.unit_factor_for(row)
        if factor is None or row.ticker not in self.moving_averages:
            return None
        return tuple(scale_amount(v, factor) for v in self.moving_averages[row.ticker])

    def d5_for(self, row):
        """D5View in the row's unit, or None (absent or withheld)."""
        factor = self.unit_factor_for(row)
        view = self.d5.get(row.ticker)
        if factor is None or view is None:
            return None
        return view if factor == 1 else view.scaled(factor)

    def max_close_for(self, row):
        """Highest close since the anchor low in the row's unit, or None."""
        factor = self.unit_factor_for(row)
        if factor is None:
            return None
        return scale_amount(self.max_close.get(row.ticker), factor)

    def analyst_for(self, row):
        """(consensus, count, target) with the target in the row's unit.

        Consensus and count are dimensionless and always kept; the target is
        None when the currency cannot be converted. Returns None when absent.
        """
        analyst = self.analyst.get(row.ticker)
        if analyst is None:
            return None
        consensus, n_analysts, target = analyst
        factor = self.unit_factor_for(row)
        return (consensus, n_analysts,
                None if factor is None else scale_amount(target, factor))


# -----------------------------------------------------------------------------
# Value normalization
# -----------------------------------------------------------------------------
def to_float(value):
    try:
        return None if value is None else float(value)
    except Exception:
        return None


def rounded(value):
    """Round to 4dp for sub-1 assets, 2dp otherwise. None on NaN or failure."""
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


# -----------------------------------------------------------------------------
# Series computation: the single implementation used by every fetch path
# -----------------------------------------------------------------------------
def moving_averages(daily_close, monthly_close):
    """Return (w20, w50, m20, m50, d200) from daily and monthly close series.

    The daily series may reach back to the anchor low. D200 and the weekly EMAs
    use only the trailing MA_DAILY_YEARS so their values match the original
    2-year definition. Guard thresholds mean a value is only produced once the
    series is long enough for the average to mean anything.
    """
    if len(daily_close):
        import pandas as pd

        start = daily_close.index[-1] - pd.DateOffset(years=MA_DAILY_YEARS)
        daily_close = daily_close[daily_close.index >= start]

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


def completed_closes(daily_close, symbol, now_utc):
    """Return (closes, intraday) with any unfinished session bar removed.

    When the run happens while the symbol's exchange is still trading, the last
    daily bar is today's partial session. It is dropped so every close-based
    signal uses completed sessions only, and ``intraday`` is True so the row can
    be marked as carrying an intraday Current price.
    """
    if not len(daily_close):
        return daily_close, False
    last_date = daily_close.index[-1].date()
    if bar_is_complete(last_date, exchange_for(symbol), now_utc):
        return daily_close, False
    return daily_close.iloc[:-1], True


def d5_view(closes, symbol, intraday):
    """Build a D5View from completed daily closes, or None if too short.

    D5 is an EMA with span D5_SPAN over completed daily closes (adjust=False,
    the same convention as the weekly and monthly EMAs). Two points of the EMA
    are kept so the flag can detect a cross since the previous session.
    """
    if len(closes) < D5_SPAN + 1:
        return None
    ema = closes.ewm(span=D5_SPAN, adjust=False).mean()
    return D5View(
        close=rounded(closes.iloc[-1]),
        d5=rounded(ema.iloc[-1]),
        prev_close=rounded(closes.iloc[-2]),
        prev_d5=rounded(ema.iloc[-2]),
        session_date=closes.index[-1].date().isoformat(),
        exchange=exchange_for(symbol),
        intraday=intraday,
    )


def series_metrics(daily, monthly, symbol, now_utc):
    """Return (moving_averages, d5_view, max_close) for one symbol.

    ``max_close`` is the highest completed close since ANCHOR_START, used to
    supersede a stale stored cycle ATH.
    """
    closes, intraday = completed_closes(daily, symbol, now_utc)
    anchored = closes[closes.index >= ANCHOR_START]
    max_close = rounded(anchored.max()) if len(anchored) else None
    return (
        moving_averages(daily, monthly),
        d5_view(closes, symbol, intraday),
        max_close,
    )


def fib_anchors(history):
    """Derive (bear_low, cycle_high) from a long daily OHLC history.

    The low is dated first: the lowest intraday Low in the 2022 bear window, or
    in the full history when the ticker post-dates that window. The high is
    then the highest intraday High on or after that date, so a peak that
    predates the low (a 2021 high, for example) can never become the cycle
    high. Both anchors are intraday extremes, so they share a price basis.
    """
    bear_window = history.loc[BEAR_WINDOW_START:BEAR_WINDOW_END]
    source = bear_window if not bear_window.empty else history
    lows = source["Low"].dropna()
    if not len(lows):
        return None, None
    bear_low = round(float(lows.min()), 2)

    highs = history.loc[lows.idxmin():, "High"].dropna()
    cycle_high = round(float(highs.max()), 2) if len(highs) else None

    return bear_low, cycle_high


#: Reported quarters summed into a trailing operating income figure.
TTM_QUARTERS = 4


def shows_profit_with_operating_loss(info):
    """True when provider info reports a positive net margin but a negative
    operating margin.

    A cheap pre-filter from the ``.info`` payload already fetched, so the
    quarterly statement (one more provider call) is only requested for the
    few tickers where it matters.
    """
    net = to_float(info.get("profitMargins"))
    operating = to_float(info.get("operatingMargins"))
    return net is not None and net > 0 and operating is not None and operating < 0


def trailing_operating_income(quarterly):
    """Sum of the latest four reported quarterly Operating Income values.

    ``quarterly`` is an income statement with line items as rows and
    quarter-end dates as columns (yfinance ``quarterly_income_stmt``). Returns
    None unless all four latest quarters carry a value, so a partial history is
    never read as a trailing total.
    """
    if quarterly is None or "Operating Income" not in getattr(quarterly, "index", ()):
        return None
    latest = quarterly.loc["Operating Income"].sort_index(ascending=False)
    values = [to_float(v) for v in latest.iloc[:TTM_QUARTERS]]
    if len(values) < TTM_QUARTERS or any(v is None or v != v for v in values):
        return None
    return sum(values)


# -----------------------------------------------------------------------------
# yfinance fetch paths
# -----------------------------------------------------------------------------
def _close_series(raw, symbol):
    """Return the Close column for one symbol as a Series, whatever the layout.

    yfinance returns three column layouts depending on the call:

    - ticker-first (Ticker, Price) MultiIndex, from ``group_by="ticker"``,
      including a batch of only one symbol;
    - price-first (Price, Ticker) MultiIndex, from a single-symbol download
      without ``group_by``;
    - flat columns, from older releases.

    The layout is detected from the columns actually returned: the MultiIndex
    level holding ``symbol`` is selected away, leaving price fields. A
    one-column DataFrame is then reduced to a Series, because under pandas 3 a
    later ``float()`` on a frame raises and was swallowed into None.
    """
    columns = raw.columns
    if columns.nlevels > 1:
        for level in range(columns.nlevels):
            if symbol in columns.get_level_values(level):
                raw = raw.xs(symbol, axis=1, level=level)
                break
        else:
            # The symbol is in no level: it has no history in this download.
            # Falling through to raw["Close"] would return another
            # instrument's closes, so report it as empty instead.
            import pandas as pd
            return pd.Series(dtype=float)
    close = raw["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    return close.dropna()


def _utc_now(now):
    return now or datetime.now(timezone.utc)


def fetch_batch(tickers, aliases, rec_map, warn=print, client=None, now=None):
    """Refresh prices, MAs, D5, fundamentals, EPS, and analyst data for many tickers.

    ``client`` defaults to the real ``yfinance`` module. Passing a stub exposing
    ``download`` and ``Ticker`` lets the orchestration be tested without network.
    ``now`` (timezone-aware) decides which daily bars are completed sessions; it
    defaults to the current time.
    """
    live = LiveMarketData()
    if not tickers:
        return live

    import contextlib
    import io

    now = _utc_now(now)
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

        for ticker in tickers:
            symbol = symbol_for(ticker)
            try:
                daily = _close_series(raw_daily, symbol)
                hourly = _close_series(raw_hourly, symbol)
                monthly = _close_series(raw_monthly, symbol)
                if daily.empty:
                    warn(f"  [skip] {ticker}: no daily data returned")
                    continue

                price_source = hourly if not hourly.empty else daily
                live.prices[ticker] = rounded(price_source.iloc[-1])
                mas, d5, max_close = series_metrics(daily, monthly, symbol, now)
                live.moving_averages[ticker] = mas
                if d5 is not None:
                    live.d5[ticker] = d5
                if max_close is not None:
                    live.max_close[ticker] = max_close
            except Exception as ex:
                warn(f"  [warn] {ticker}: {ex}")

        warn(
            f"[Live Refresh] Fetching fundamentals + analyst data "
            f"({len(tickers)} tickers)..."
        )
        for ticker in tickers:
            try:
                handle = yf.Ticker(symbol_for(ticker))
                info = handle.info
                live.fundamentals[ticker] = fundamentals_from_info(info)
                if info.get("currency"):
                    live.currency[ticker] = info["currency"]

                # Earnings quality: positive net income with an operating loss
                # is confirmed against four reported quarters before it is
                # recorded (calculations.assess_fundamentals uses it).
                if shows_profit_with_operating_loss(info):
                    operating = trailing_operating_income(handle.quarterly_income_stmt)
                    if operating is not None:
                        live.operating_income[ticker] = operating

                # yfinance trailingEps is useful but is not Alpha Vantage
                # independent verification, so provenance is recorded as False.
                # Its unit is recorded only where established (see
                # calculations.established_eps_unit).
                ttm_eps = to_float(info.get("trailingEps"))
                if ttm_eps is not None:
                    live.eps[ticker] = (round(ttm_eps, 2), False)
                    unit = established_eps_unit(info.get("currency"),
                                                info.get("financialCurrency"))
                    if unit:
                        live.eps_currency[ticker] = unit

                price = realtime_price(info)
                if price is not None:
                    live.prices[ticker] = price

                analyst = analyst_from_info(info, rec_map)
                if analyst:
                    live.analyst[ticker] = analyst
            except Exception as ex:
                warn(f"  [warn] {ticker} fundamentals: {ex}")

        warn(
            f"[Live Refresh] Done: {len(live.prices)} prices | "
            f"{len(live.moving_averages)} MA sets | "
            f"{len(live.d5)} D5 sets | "
            f"{len(live.fundamentals)} fundamentals | "
            f"{len(live.analyst)} analyst sets."
        )
    except Exception as ex:
        warn(f"[Live Refresh] Failed ({ex}); falling back to static JSON values.")

    return live


@dataclass
class TickerSnapshot:
    """Everything ``--add`` needs about a newly tracked ticker."""

    ticker: str
    name: str
    currency: str | None
    price: float
    bear_low: float
    cycle_high: float
    moving_averages: tuple
    fundamentals: tuple
    analyst: tuple
    d5: D5View | None = None
    max_close: float | None = None
    operating_income: float | None = None


def fetch_ticker(ticker, rec_map, warn=print, client=None, now=None, symbol=None):
    """Fetch one ticker for ``--add``, reusing the shared series computation.

    Price, moving averages, D5 and highest close come from the same windows and
    the same ``series_metrics`` the batch refresh uses. Only Fib anchor
    derivation is specific to ``--add``, because portfolio rows carry curated
    historical anchors instead of deriving them.

    ``ticker`` is the portfolio key and is what the snapshot carries.
    ``symbol`` is the provider symbol from ``yahoo_aliases`` (for example
    ``BTC-USD`` for ``BTC``) and defaults to ``ticker``, so ``--add`` queries
    the same instrument as the batch refresh.

    ``client`` defaults to the real ``yfinance`` module; pass a stub to test.
    """
    import contextlib
    import io

    symbol = symbol or ticker
    now = _utc_now(now)
    yf = client
    if yf is None:
        try:
            import yfinance as yf
        except ImportError:
            warn(f"[--add {ticker}] yfinance not installed.")
            return None

    try:
        handle = yf.Ticker(symbol)
        info = handle.info

        with contextlib.redirect_stdout(io.StringIO()):
            raw_daily = yf.download(symbol, auto_adjust=True, progress=False, **DAILY_WINDOW)
            raw_hourly = yf.download(symbol, auto_adjust=True, progress=False, **HOURLY_WINDOW)
            raw_monthly = yf.download(symbol, auto_adjust=True, progress=False, **MONTHLY_WINDOW)
            anchor_history = handle.history(**ANCHOR_WINDOW)

        daily = _close_series(raw_daily, symbol)
        hourly = _close_series(raw_hourly, symbol)
        monthly = _close_series(raw_monthly, symbol)
        if daily.empty or anchor_history.empty:
            warn(f"[--add {ticker}] No price history; skipping.")
            return None

        price = realtime_price(info)
        if price is None:
            price_source = hourly if not hourly.empty else daily
            price = rounded(price_source.iloc[-1])

        bear_low, cycle_high = fib_anchors(anchor_history)
        if bear_low is None or cycle_high is None:
            warn(f"[--add {ticker}] Could not derive Fib anchors; skipping.")
            return None

        mas, d5, max_close = series_metrics(daily, monthly, symbol, now)
        # Same earnings-quality pre-filter and reconciliation as fetch_batch,
        # so --add rows are held to the same rule.
        operating = (trailing_operating_income(handle.quarterly_income_stmt)
                     if shows_profit_with_operating_loss(info) else None)
        return TickerSnapshot(
            ticker=ticker,
            name=info.get("longName") or info.get("shortName") or ticker,
            # No default: an unreported currency stays unknown, so the quote
            # is never persisted or shown as if it were USD.
            currency=info.get("currency"),
            price=price,
            bear_low=bear_low,
            cycle_high=cycle_high,
            moving_averages=mas,
            fundamentals=fundamentals_from_info(info),
            analyst=analyst_from_info(info, rec_map, default_consensus="Hold"),
            d5=d5,
            max_close=max_close,
            operating_income=operating,
        )
    except Exception as ex:
        warn(f"[--add {ticker}] Failed: {ex}")
        return None


# -----------------------------------------------------------------------------
# Alpha Vantage: independent TTM EPS used to verify yfinance P/E ratios
# -----------------------------------------------------------------------------
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
                            http=None, sleeper=None, units=None):
    """Return {ticker: (ttm_eps, True)} for tickers Alpha Vantage can verify.

    When ``units`` (a dict) is given, it receives ticker -> EPS unit for every
    EPS returned: the OVERVIEW ``Currency`` where its meaning for
    ``DilutedEPSTTM`` is established, otherwise None. It is established only
    for US targets whose ``Currency`` is USD; ``.LON`` targets have no known
    quote currency here, so their unit is None (unknown).

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
            symbol = uk_map.get(ticker, ticker)   # remap .L to .LON for UK stocks
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
                        if units is not None:
                            quote = None if ticker in uk_map else "USD"
                            units[ticker] = established_eps_unit(
                                quote, payload.get("Currency"))
                        warn(f"  [AV] {ticker}: TTM EPS={round(eps, 2)}")
            except Exception as ex:
                warn(f"  [AV warn] {ticker}: {ex}")

            if idx < len(targets) - 1:
                sleeper(sleep_seconds)

        warn(f"[Alpha Vantage EPS] Done: {len(results)} EPS values fetched.")
    except Exception as ex:
        warn(f"[Alpha Vantage EPS] Failed ({ex})")

    return results
