"""Pure investment-tracker calculations.

These helpers intentionally have no file, network, CLI, or openpyxl dependencies.
They are shared by ticker ingestion, Alert Levels rendering, Daily Summary
rendering, and Fib Methodology rendering so the workbook cannot drift between
separate implementations.

Sections:
    Fib levels and zones      fib, fib_levels, classify_zone, zone rank constants
    Anchors and units         effective_ath, unit_factor, units_mismatch
    Fundamentals              resolve_eps, assess_fundamentals
    Sessions and D5           exchange_for, bar_is_complete, d5_flag
    Text helpers              ma_trend_text, first_sentence, note_issues
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from modules.models import Direction, RowType


# =============================================================================
# Fib levels and zones
# =============================================================================

# Zone ranks. Lower ranks are deeper retracements. The Daily Summary sorts on
# rank, so the numeric order is meaningful.
RANK_DEEP = 0          # below the 78.6% level
RANK_AL3 = 1           # AL3 hit
RANK_AL2 = 2           # AL2 hit
RANK_AL1 = 3           # AL1 hit
RANK_APPROACHING = 4   # within APPROACH_BAND of AL1
RANK_ABOVE = 5         # beyond AL1 on the safe side
RANK_BROKEN = 6        # price below the anchor low: Fib structure no longer applies
RANK_UNIT_CHECK = 8    # price and levels are about 100x apart: unit mismatch
RANK_NO_FIB = 9        # no usable levels

#: Ranks that count as "in a buy zone" before any quality gate is applied.
BUY_ZONE_RANKS = frozenset({RANK_DEEP, RANK_AL3, RANK_AL2, RANK_AL1})

#: Gap to AL1 at or below which a row is "approaching" (15%).
APPROACH_BAND = 0.15


@dataclass(frozen=True)
class ZoneResult:
    rank: int
    label: str
    status: str
    color: str
    gap_to_al1: float | None
    al1: float | None
    al2: float | None
    al3: float | None
    d4: float | None


def fib(high, low, pct):
    """Return a Fibonacci retracement level from high/low anchors."""
    return round(high - ((high - low) * pct), 2)


def upside_to_ath(current, macro_hi):
    """Return the gain required for current price to reach cycle ATH."""
    return (macro_hi - current) / current if current and macro_hi else None


def fib_levels(current, macro_lo, macro_hi, manual=None, is_manual=False,
               direction=Direction.DOWN):
    """Return AL1/AL2/AL3/D4 levels and gap-to-AL1 for a row.

    Manual rows use their stored levels and have no 78.6% level. Fib rows derive
    all four levels from the anchors.

    The gap is signed so that a negative gap always means "already in the zone",
    whichever way the row triggers:
        down rows: (current - AL1) / AL1
        up rows:   (AL1 - current) / AL1
    """
    if is_manual and manual and manual[0] is not None:
        al1, al2, al3 = manual
        d4 = None
    elif macro_lo and macro_hi:
        al1 = fib(macro_hi, macro_lo, 0.382)
        al2 = fib(macro_hi, macro_lo, 0.500)
        al3 = fib(macro_hi, macro_lo, 0.618)
        d4 = fib(macro_hi, macro_lo, 0.786)
    else:
        al1 = al2 = al3 = d4 = None

    gap = None
    if current is not None and al1:
        gap = (current - al1) / al1
        if direction == Direction.UP:
            gap = -gap
    return al1, al2, al3, d4, gap


def _zone(rank, label, status, color, gap, levels):
    """Build a ZoneResult from a rank, its wording, and the computed levels."""
    al1, al2, al3, d4 = levels
    return ZoneResult(rank, label, status, color, gap, al1, al2, al3, d4)


def classify_zone(current, macro_lo=None, macro_hi=None, manual=None,
                  is_manual=False, direction=Direction.DOWN):
    """Classify current price relative to alert levels.

    Precedence, first match wins:
        no usable levels           -> RANK_NO_FIB
        price ~100x off the levels -> RANK_UNIT_CHECK
        price below anchor low     -> RANK_BROKEN (Fib rows only)
        beyond 78.6%, AL3, AL2, AL1 in turn
        within APPROACH_BAND of AL1 -> RANK_APPROACHING
        otherwise                   -> RANK_ABOVE

    "Beyond" a level means below it for down rows and at or above it for up
    rows (for example VIX, where a higher reading is the alert).
    """
    al1, al2, al3, d4, gap = fib_levels(
        current, macro_lo, macro_hi, manual=manual, is_manual=is_manual,
        direction=direction,
    )
    levels = (al1, al2, al3, d4)
    if al1 is None or current is None:
        return _zone(RANK_NO_FIB, "No Fib / ETF", "-", "555555", None, levels)

    if units_mismatch(current, al1):
        return _zone(
            RANK_UNIT_CHECK,
            f"UNIT CHECK - price {current} is about 100x the levels (AL1 {al1}).",
            "UNIT CHECK\nPRICE vs LEVELS ~100x",
            "6A1B9A", gap, levels,
        )

    if not is_manual and macro_lo and current < macro_lo:
        return _zone(
            RANK_BROKEN,
            f"BELOW ANCHOR LOW ({macro_lo}) - STRUCTURE BROKEN.",
            "BELOW ANCHOR LOW\nSTRUCTURE BROKEN",
            "4E342E", gap, levels,
        )

    up = direction == Direction.UP

    def beyond(level):
        """True when the price has reached ``level`` in the alert direction.

        Inclusive both ways: a price exactly on a level has hit it.
        """
        if level is None:
            return False
        return current >= level if up else current <= level

    if beyond(d4):
        return _zone(RANK_DEEP, f"BELOW 78.6% ({d4}) — DEEPEST VALUE.",
                     "BELOW 78.6%\nDEEPEST VALUE", "880000", gap, levels)
    if beyond(al3):
        return _zone(RANK_AL3, f"AL3 HIT ({al3}) — BACK UP TRUCK.",
                     "AL3 HIT\nBACK UP TRUCK", "C62828", gap, levels)
    if beyond(al2):
        return _zone(RANK_AL2, f"AL2 HIT ({al2}) — STRONG BUY.",
                     "AL2 HIT\nSTRONG BUY NOW", "E65100", gap, levels)
    if beyond(al1):
        return _zone(RANK_AL1, f"AL1 HIT ({al1}) — ACCUMULATE.",
                     "AL1 HIT\nACCUMULATE NOW", "2E7D32", gap, levels)
    if gap is not None and gap <= APPROACH_BAND:
        return _zone(RANK_APPROACHING, f"APPROACHING AL1 ({al1}) — SET ALERTS.",
                     "Above AL1\nNo action yet", "555555", gap, levels)
    return _zone(RANK_ABOVE, f"ABOVE AL1 ({al1}) — WATCHING.",
                 "Above AL1\nNo action yet", "555555", gap, levels)


def classify_row(row):
    """Classify a PortfolioRow using its own anchors, manual levels and direction."""
    return classify_zone(
        row.current, row.macro_lo, row.macro_hi,
        manual=row.manual, is_manual=row.is_manual, direction=row.direction,
    )


def validate_manual_levels(row):
    """Return an error message when a manual row's levels contradict its direction.

    Down rows must list levels high to low (AL1 > AL2 > AL3); up rows low to
    high. A wrong order would silently invert the zone logic, which is exactly
    how the VIX row came to read AL3 instead of AL2.
    """
    if row.direction not in Direction.ALL:
        return f"direction must be 'up' or 'down', got {row.direction!r}"
    if not (row.is_manual and row.manual) or any(v is None for v in row.manual):
        return None
    al1, al2, al3 = row.manual
    if row.direction == Direction.UP and not (al1 < al2 < al3):
        return f"direction 'up' needs rising levels, got {list(row.manual)}"
    if row.direction == Direction.DOWN and not (al1 > al2 > al3):
        return f"direction 'down' needs falling levels, got {list(row.manual)}"
    return None


# =============================================================================
# Anchors and units
# =============================================================================

#: Anchor-low window start. The effective ATH looks at closes from here on.
ANCHOR_START = "2022-09-01"

#: Price-to-level ratio band treated as a pence-versus-pounds mismatch.
UNIT_RATIO_BAND = (50.0, 200.0)


#: Row units that are readings rather than currencies: a yield in percent
#: (TNX) and an index level (DXY). The provider still reports a currency for
#: them, but there is nothing to convert, so they never count as a mismatch.
NON_CURRENCY_UNITS = frozenset({"PCT", "IDX"})


def _unit_code(ccy):
    """Normalise a currency code so GBp and GBX (both pence) compare equal.

    Returns None for a missing value or a non-currency unit, which
    ``unit_factor`` treats as "nothing to convert".
    """
    if not ccy or ccy.upper() in NON_CURRENCY_UNITS:
        return None
    if ccy == "GBp" or ccy.upper() == "GBX":
        return "GBP_PENCE"
    return ccy.upper()


def is_known_unit(ccy):
    """True when ``ccy`` is a real currency unit, not missing or a reading.

    ``unit_factor`` returns 1 for an unknown unit (nothing to convert), so any
    calculation that must not guess, such as P/E from EPS, checks this first.
    """
    return _unit_code(ccy) is not None


#: Units whose EPS reporting unit is ambiguous. LSE lines quote in pence while
#: companies report EPS in pounds, and providers do not say which they used.
_AMBIGUOUS_EPS_UNITS = frozenset({"GBP", "GBP_PENCE"})


def established_eps_unit(quote_ccy, reporting_ccy):
    """The EPS unit when it is established, otherwise None.

    Established means both the listing's quote currency and the provider's
    reporting currency are known, they agree, and they are not sterling (where
    pence versus pounds is ambiguous). For example a US listing reporting in
    USD. Anything else is unknown: the EPS is shown without a currency symbol
    and the provider P/E is kept.
    """
    if not (is_known_unit(quote_ccy) and is_known_unit(reporting_ccy)):
        return None
    quote, reporting = _unit_code(quote_ccy), _unit_code(reporting_ccy)
    if quote != reporting or reporting in _AMBIGUOUS_EPS_UNITS:
        return None
    return reporting


def unit_factor(provider_ccy, row_ccy):
    """Multiplier converting provider prices into the row's stored unit.

    Returns 1 when the units already agree (or either side is unknown), 0.01 or
    100 for a pence/pounds difference, and None when the currencies genuinely
    differ (for example USD versus GBP), which cannot be fixed by scaling.
    """
    provider, row = _unit_code(provider_ccy), _unit_code(row_ccy)
    if provider is None or row is None or provider == row:
        return 1
    if provider == "GBP_PENCE" and row == "GBP":
        return 0.01
    if provider == "GBP" and row == "GBP_PENCE":
        return 100
    return None


def units_mismatch(price, reference):
    """True when price and a reference level differ by roughly 100x either way."""
    if not price or not reference or price <= 0 or reference <= 0:
        return False
    ratio = max(price, reference) / min(price, reference)
    lo, hi = UNIT_RATIO_BAND
    return lo <= ratio <= hi


def effective_ath(stored_ath, max_close):
    """Return max(stored ATH, highest close since the anchor low)."""
    values = [v for v in (stored_ath, max_close) if v is not None]
    return max(values) if values else None


# =============================================================================
# Fundamentals
# =============================================================================

@dataclass(frozen=True)
class FundamentalsCheck:
    """Result of the fundamentals quality gate for one row.

    red         rules broken (gate the row out of IN BUY ZONES)
    amber       warnings shown in Status but not gating
    data_check  reasons a value looks implausible and should be verified
    suspect     field names whose cell should read DATA CHECK
    """

    red: tuple = ()
    amber: tuple = ()
    data_check: tuple = ()
    suspect: frozenset = frozenset()

    @property
    def is_red(self):
        return bool(self.red)

    @property
    def needs_data_check(self):
        return bool(self.data_check)


def resolve_eps(row, eps_view):
    """Return (trailing_eps, verified) for a row.

    Source priority:
        1. Alpha Vantage (verified) or yfinance trailing EPS, from eps_view
        2. Derived price / TTM P/E, never verified
    """
    if eps_view is not None and eps_view.trailing_eps is not None:
        return eps_view.trailing_eps, eps_view.is_verified
    if row.ttm_pe and row.current:
        return round(row.current / row.ttm_pe, 2), False
    return None, False


def is_gate_exempt(row, macro_tickers):
    """Funds, macro gauges, crypto and section rows are not assessed."""
    return (
        row.ticker in macro_tickers
        or row.is_fund
        or row.rtype not in RowType.EQUITY_LIKE
    )


def assess_fundamentals(row, eps, macro_tickers):
    """Apply the data-quality checks, then the red-flag rules, to one row.

    DATA CHECK rules (value looks implausible, verify before trusting):
        Fwd P/E below 5x
        Fwd P/E below 0.6x TTM P/E (implies more than 65% one-year EPS growth)
        Net margin above 90%
        Net margin sign disagrees with TTM EPS sign
        Negative TTM EPS with a positive Fwd P/E below 10x

    Red-flag rules (any one keeps the row out of IN BUY ZONES):
        D/E above 1.0, net margin below 0, TTM P/E above 100x,
        Fwd P/E above 80x, negative TTM EPS

    A field marked suspect by a DATA CHECK rule is not used by the red-flag
    rules, because a red flag built on a bad value would be just as misleading.
    """
    if is_gate_exempt(row, macro_tickers):
        return FundamentalsCheck()

    de, margin, ttm_pe, fwd_pe = row.de, row.margin, row.ttm_pe, row.fwd_pe
    checks, suspect = [], set()

    if fwd_pe is not None and fwd_pe < 5:
        checks.append(f"FWD P/E {fwd_pe:.0f}x BELOW 5x")
        suspect.add("fwd_pe")
    elif fwd_pe is not None and ttm_pe and fwd_pe < 0.6 * ttm_pe:
        checks.append(f"FWD P/E {fwd_pe:.0f}x UNDER 0.6x TTM {ttm_pe:.0f}x")
        suspect.add("fwd_pe")
    if eps is not None and eps < 0 and fwd_pe is not None and fwd_pe < 10 \
            and "fwd_pe" not in suspect:
        checks.append(f"FWD P/E {fwd_pe:.0f}x WITH NEGATIVE EPS")
        suspect.add("fwd_pe")
    if margin is not None and margin > 0.90:
        checks.append(f"NET MARGIN {margin:.0%} ABOVE 90%")
        suspect.add("margin")
    elif margin and eps and (margin < 0) != (eps < 0):
        checks.append("NET MARGIN SIGN vs EPS SIGN")
        suspect.add("margin")

    red, amber = [], []
    if de is not None:
        if de > 2.0:
            red.append(f"EXTREME LEVERAGE D/E={de:.2f}")
        elif de > 1.0:
            red.append(f"HIGH LEVERAGE D/E={de:.2f}")
    if margin is not None and "margin" not in suspect:
        if margin < 0:
            red.append("LOSS-MAKING")
        elif margin < 0.05:
            amber.append(f"THIN MARGIN {margin:.0%}")
    if ttm_pe is not None and ttm_pe > 100:
        red.append(f"HIGH TTM P/E {ttm_pe:.0f}x")
    if fwd_pe is not None and fwd_pe > 80 and "fwd_pe" not in suspect:
        red.append(f"FWD P/E STRETCHED {fwd_pe:.0f}x")
    if eps is not None and eps < 0:
        red.append("NEGATIVE EPS")

    return FundamentalsCheck(tuple(red), tuple(amber), tuple(checks),
                             frozenset(suspect))


# =============================================================================
# Sessions and the D5 flag
# =============================================================================

# Exchange label -> (timezone, regular session close). A close of None marks a
# 24-hour market whose daily bar is only complete once the UTC day has ended.
EXCHANGES = {
    "LSE": ("Europe/London", time(16, 30)),
    "TSX": ("America/Toronto", time(16, 0)),
    "US": ("America/New_York", time(16, 0)),
    "NYMEX": ("America/New_York", time(17, 0)),
    "CRYPTO": ("UTC", None),
}

#: Display order for the per-exchange session line in sheet headers.
EXCHANGE_ORDER = ("LSE", "US", "NYMEX", "TSX", "CRYPTO")


def exchange_for(symbol):
    """Map a provider symbol to an exchange label in EXCHANGES."""
    s = (symbol or "").upper()
    if s.endswith(".L"):
        return "LSE"
    if s.endswith(".TO") or s.endswith(".V"):
        return "TSX"
    if s.endswith("-USD"):
        return "CRYPTO"
    if s.endswith("=F"):
        return "NYMEX"
    return "US"


def bar_is_complete(bar_date, exchange, now_utc):
    """True when the daily bar dated ``bar_date`` is a completed session.

    A bar from an earlier local date is complete. A bar dated today is complete
    only once the exchange's regular session has closed. A 24-hour market's bar
    for today is never complete.
    """
    tz_name, close = EXCHANGES[exchange]
    local = now_utc.astimezone(ZoneInfo(tz_name))
    if bar_date < local.date():
        return True
    if bar_date > local.date():
        return False
    return close is not None and local.time() >= close


def d5_flag(view):
    """Return (label, is_above, crossed) for a D5View, or None without data.

    The flag compares the latest completed close with the D5 EMA. When the
    previous completed close sat on the other side of the previous D5, the
    label reports the cross instead.
    """
    if view is None:
        return None
    above = view.close >= view.d5
    was_above = view.prev_close >= view.prev_d5
    if above != was_above:
        return ("CROSSED ABOVE D5" if above else "CROSSED BELOW D5"), above, True
    return ("Close > D5" if above else "Close < D5"), above, False


# =============================================================================
# Text helpers
# =============================================================================

def ma_trend_text(current, w20, w50, m20, m50, d200):
    """Compact multi-line summary of price position vs each moving average."""
    if current is None:
        return "N/A"
    lines = []
    if d200 is not None:
        lines.append(f"D200: {'P>SMA' if current > d200 else 'P<SMA'}")
    if w20 is not None and w50 is not None:
        p_w20 = "P>" if current > w20 else "P<"
        align = "20>50" if w20 > w50 else "20<50"
        lines.append(f"W: {p_w20}20 {align}")
    elif w20 is not None:
        lines.append(f"W: {'P>20' if current > w20 else 'P<20'} (no W50)")
    if m20 is not None and m50 is not None:
        p_m20 = "P>" if current > m20 else "P<"
        align = "20>50" if m20 > m50 else "20<50"
        lines.append(f"M: {p_m20}20 {align}")
    elif m20 is not None:
        lines.append(f"M: {'P>20' if current > m20 else 'P<20'} (no M50)")
    return "\n".join(lines) if lines else "N/A"


def first_sentence(txt):
    """Extract the first meaningful clause from a thesis note."""
    if not txt:
        return ""
    for sep in [". ", " — ", " | "]:
        idx = txt.find(sep)
        if 0 < idx < 180:
            return txt[:idx + (1 if sep == ". " else 0)].strip()
    return txt[:150].strip()


# A currency amount such as "$125.75" or "£42.20", but not a scaled figure
# such as "$34B", "$1T+" or "$500M", which describe a company, not a price.
_PRICE = re.compile(
    r"[$£€]\s?\d[\d,]*(?:\.\d+)?(?![\d.,]|\s?(?:[KMBT]|bn|tn)\b)"
)
# A pence quote such as "1121.5p" or "64p".
_PENCE_QUOTE = re.compile(r"\b\d[\d,]*(?:\.\d+)?p\b")
# An inline level or moving-average value such as "AL1=94.85" or "D200=1130p".
_LEVEL_VALUE = re.compile(r"\b(?:AL[1-3]|D200|W20|W50|M20|M50)\s?=")
# Level-status wording that the generator now computes on every run.
_STATUS = re.compile(
    r"\bAL[1-3]\b|78\.6%|\bDEEPEST VALUE\b|\bBACK UP TRUCK\b|\bSTRONG BUY\b|"
    r"\bACCUMULATE (?:NOW|ZONE)\b|\bNEAR ATH\b|\bAT ATH\b|\bWAIT FOR PULLBACK\b|"
    r"\b(?:ABOVE|BELOW) (?:AL[1-3]|78\.6%)|\bHIT\b"
)


def note_issues(text):
    """Return the price and status fragments found in a free-text note.

    Thesis notes must hold neither: prices go stale and statuses contradict the
    computed zone. Used to report rows whose notes need cleaning.
    """
    if not text:
        return []
    found = []
    for pattern in (_PRICE, _PENCE_QUOTE, _LEVEL_VALUE, _STATUS):
        found.extend(m.group(0).strip() for m in pattern.finditer(text))
    return found
