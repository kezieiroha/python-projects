"""Prepared inputs handed to sheet renderers.

Renderers receive this and nothing else. They never load JSON, call a provider,
or reach back into CLI state.

The lookup helpers return typed views rather than bare tuples, so a renderer
reads ``mas.d200`` and ``analyst.consensus`` instead of indexing by position.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from modules.models import AnalystView, EpsView, MovingAverages


@dataclass(frozen=True)
class SheetContext:
    run_date: str
    data_source: str
    excluded_tickers: frozenset = frozenset()
    ma_data: dict = field(default_factory=dict)
    analyst_data: dict = field(default_factory=dict)
    eps_data: dict = field(default_factory=dict)
    macro_tickers: frozenset = frozenset()
    sentiment_tickers: frozenset = frozenset()

    def moving_averages_for(self, ticker):
        """Always returns a MovingAverages; missing entries are all-None."""
        return MovingAverages.from_sequence(self.ma_data.get(ticker))

    def analyst_for(self, ticker):
        """Returns an AnalystView, or None when no consensus is available."""
        return AnalystView.from_sequence(self.analyst_data.get(ticker))

    def eps_for(self, ticker):
        """Returns an EpsView, or None when no live/verified EPS was fetched."""
        return EpsView.from_sequence(self.eps_data.get(ticker))
