"""Shared data structures.

These replace the positional tuples the script used to thread through every
layer. A field like ``macro_hi`` is reached by name, not by remembering that it
is index 5.

Importing this module must not read files or contact a provider. It is the
vocabulary the other modules speak, and nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, NamedTuple


class RowType:
    """Row kinds as stored in ``workbook/data/portfolio_rows.json``.

    The string values are the on-disk representation, so they must not change
    without migrating the data files.
    """

    SECTION = "SECTION"
    MANUAL = "MANUAL"
    STOCK = "STOCK"
    ETF = "ETF"
    ISA = "ISA"
    SIPP = "SIPP"

    #: Row kinds that represent a company whose financials should be assessed.
    EQUITY_LIKE = frozenset({STOCK, ISA, SIPP, MANUAL})


# Field order used for JSON round-trips. Kept explicit so persisted files stay
# byte-stable regardless of dataclass declaration order.
ROW_FIELDS = (
    "asset", "ticker", "ccy", "current", "macro_lo", "macro_hi",
    "ttm_pe", "fwd_pe", "de", "margin", "notes", "rtype", "manual",
)

MACRO_SWING_FIELDS = (
    "ticker", "macro_lo", "macro_hi", "current",
    "al1_display", "status", "bg", "font_color",
)


def _clean(value):
    return str(value or "").strip().upper()


@dataclass(frozen=True)
class PortfolioRow:
    """One tracked asset, section header, or manual-level row.

    ``extra`` carries any JSON keys this model does not know about, so a field
    added by hand to ``portfolio_rows.json`` survives a load/save cycle instead of
    being silently dropped.
    """

    asset: str = ""
    ticker: str = ""
    ccy: str | None = None
    current: float | None = None
    macro_lo: float | None = None
    macro_hi: float | None = None
    ttm_pe: float | None = None
    fwd_pe: float | None = None
    de: float | None = None
    margin: float | None = None
    notes: str | None = None
    rtype: str = RowType.STOCK
    manual: Any = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    # ── Construction and persistence ─────────────────────────────────────────
    @classmethod
    def from_dict(cls, data):
        """Build a row from a JSON object, retaining unknown keys."""
        known = {k: data.get(k) for k in ROW_FIELDS if k in data}
        known.setdefault("rtype", RowType.STOCK)
        known.setdefault("manual", None)
        extra = {k: v for k, v in data.items() if k not in ROW_FIELDS}
        return cls(**known, extra=extra)

    def to_dict(self):
        """Render back to a JSON object, unknown keys first so known keys win."""
        out = dict(self.extra)
        out.update({name: getattr(self, name) for name in ROW_FIELDS})
        return out

    def replace(self, **changes):
        """Return a copy with named fields changed and ``extra`` preserved."""
        return replace(self, **changes)

    # ── Identity ─────────────────────────────────────────────────────────────
    def identity(self, index=0):
        """Key that keeps same-ticker rows distinct within a run.

        Identity is derived rather than persisted on purpose. A ticker is a
        market-data lookup key, not a row identity — placeholder tickers such as
        ``TBC`` are shared by several pre-IPO assets — so the asset name and the
        row's position disambiguate them. Storing a generated id in the JSON
        would make the files less pleasant to hand-edit for no behavioural gain.
        """
        return (_clean(self.asset), _clean(self.ticker), index)

    def matches(self, items):
        """True when a CLI removal key names this row by asset or ticker."""
        if not items:
            return False
        return _clean(self.asset) in items or _clean(self.ticker) in items

    # ── Convenience predicates used by renderers ─────────────────────────────
    @property
    def is_section(self):
        return self.rtype == RowType.SECTION

    @property
    def is_etf(self):
        return self.rtype == RowType.ETF

    @property
    def is_manual(self):
        return self.rtype == RowType.MANUAL

    @property
    def has_fib_anchors(self):
        return bool(self.macro_lo and self.macro_hi)

    @property
    def na_label(self):
        """Placeholder shown when a fundamentals value is unavailable."""
        return "ETF" if self.is_etf else "N/A"


@dataclass(frozen=True)
class MacroSwingRow:
    """A curated historical example on the Fib Methodology sheet."""

    ticker: str = ""
    macro_lo: float | None = None
    macro_hi: float | None = None
    current: float | None = None
    al1_display: str | None = None
    status: str | None = None
    bg: str | None = None
    font_color: str | None = None

    @classmethod
    def from_dict(cls, data):
        return cls(**{k: data.get(k) for k in MACRO_SWING_FIELDS})


class MovingAverages(NamedTuple):
    """W20/W50/M20/M50 EMAs plus the D200 SMA.

    Tuple-shaped because ``workbook/data/ma_data.json`` stores these as arrays in this
    order, and the batch fetch produces them in the same order.
    """

    w20: float | None = None
    w50: float | None = None
    m20: float | None = None
    m50: float | None = None
    d200: float | None = None

    @classmethod
    def from_sequence(cls, values):
        if values is None:
            return cls()
        if isinstance(values, MovingAverages):
            return values
        return cls(*(list(values) + [None] * 5)[:5])


class AnalystView(NamedTuple):
    """Consensus rating, contributing analyst count, and mean price target."""

    consensus: str
    n_analysts: int = 0
    target: float | None = None

    @classmethod
    def from_sequence(cls, values):
        if values is None:
            return None
        if isinstance(values, AnalystView):
            return values
        return cls(*values)

    def upside_from(self, price):
        """Fractional upside from ``price`` to the target, or None."""
        if not price or price <= 0 or self.target is None:
            return None
        return (self.target - price) / price


class EpsView(NamedTuple):
    """Trailing EPS plus where it came from.

    Only Alpha Vantage counts as independent verification. yfinance trailing EPS
    and the price ÷ P/E fallback are both unverified, and the workbook marks them
    with a ``~`` prefix so a reader can tell the difference.
    """

    trailing_eps: float | None = None
    source: str = "derived"

    ALPHA_VANTAGE = "alpha_vantage"
    YFINANCE = "yfinance"
    DERIVED = "derived"

    @property
    def is_verified(self):
        return self.source == self.ALPHA_VANTAGE

    @classmethod
    def from_sequence(cls, values):
        """Accept ``(eps, is_verified_bool)`` as stored by the fetch paths."""
        if values is None:
            return None
        if isinstance(values, EpsView):
            return values
        eps, verified = values
        return cls(eps, cls.ALPHA_VANTAGE if verified else cls.YFINANCE)
