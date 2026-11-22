"""Prepared inputs handed to sheet renderers.

Renderers receive this and nothing else. They never load JSON, call a provider,
or reach back into CLI state.

The lookup helpers return typed views rather than bare tuples, so a renderer
reads ``mas.d200`` and ``analyst.consensus`` instead of indexing by position.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from modules.calculations import EXCHANGE_ORDER, assess_fundamentals, resolve_eps
from modules.market_data import LiveMarketData
from modules.models import AnalystView, EpsView, MovingAverages


@dataclass(frozen=True)
class SheetContext:
    """Everything a renderer needs beyond the portfolio rows themselves.

    ma_data       ticker -> static fallback MA tuple, in the row's own unit
    analyst_data  ticker -> static fallback analyst tuple, in the row's unit
    live          raw LiveMarketData, keyed by ticker in provider units
    ath_updates   ticker -> (stored ATH, effective ATH) where superseded
    d5_basis      exchange label -> date of the completed session used

    Every price-like lookup takes the row, not the ticker, because rows that
    share a ticker can store different units. Live values are converted into
    ``row.ccy`` by ``LiveMarketData``; a row whose currency cannot be converted
    falls back to the static value, or to None where there is none.
    """

    run_date: str
    data_source: str
    excluded_tickers: frozenset = frozenset()
    ma_data: dict = field(default_factory=dict)
    analyst_data: dict = field(default_factory=dict)
    live: LiveMarketData = field(default_factory=LiveMarketData)
    macro_tickers: frozenset = frozenset()
    sentiment_tickers: frozenset = frozenset()
    ath_updates: dict = field(default_factory=dict)
    d5_basis: dict = field(default_factory=dict)

    def moving_averages_for(self, row):
        """Always returns a MovingAverages; missing entries are all-None."""
        live = self.live.moving_averages_for(row)
        return MovingAverages.from_sequence(
            live if live is not None else self.ma_data.get(row.ticker))

    def analyst_for(self, row):
        """Returns an AnalystView, or None when no consensus is available."""
        live = self.live.analyst_for(row)
        return AnalystView.from_sequence(
            live if live is not None else self.analyst_data.get(row.ticker))

    def eps_for(self, ticker):
        """Returns an EpsView, or None when no live/verified EPS was fetched."""
        return EpsView.from_sequence(self.live.eps.get(ticker))

    def d5_for(self, row):
        """Returns a D5View in the row's unit, or None without usable history."""
        return self.live.d5_for(row)

    def max_close_for(self, row):
        """Highest close since the anchor low in the row's unit, or None."""
        return self.live.max_close_for(row)

    def resolved_eps(self, row):
        """Returns (trailing_eps, verified) for a row, using the shared rule."""
        return resolve_eps(row, self.eps_for(row.ticker))

    def eps_unit_for(self, row):
        """The unit of the EPS value ``resolved_eps`` returns, or None.

        Provider EPS (Alpha Vantage or yfinance) is in its reporting unit,
        known only where established. EPS derived from the row's price and P/E
        is in the row's own unit.
        """
        view = self.eps_for(row.ticker)
        if view is not None and view.trailing_eps is not None:
            return self.live.eps_currency.get(row.ticker)
        return row.ccy

    def fundamentals_for(self, row):
        """Returns the FundamentalsCheck for a row, identical on every sheet."""
        eps, _verified = self.resolved_eps(row)
        return assess_fundamentals(row, eps, self.macro_tickers)

    def is_intraday(self, row):
        """True when the row's Current price was taken during an open session."""
        view = self.d5_for(row)
        return bool(view and view.intraday)

    def d5_basis_text(self):
        """One line naming the completed session each exchange's D5 uses."""
        if not self.d5_basis:
            return "D5 basis: no daily history (offline run)"
        parts = [
            f"{exchange} close {self.d5_basis[exchange]}"
            for exchange in EXCHANGE_ORDER if exchange in self.d5_basis
        ]
        return "D5 basis (last completed session): " + " | ".join(parts)
