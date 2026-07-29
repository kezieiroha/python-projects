"""Pure investment-tracker calculations.

These helpers intentionally have no file, network, CLI, or openpyxl dependencies.
They are shared by ticker ingestion, Alert Levels rendering, and Daily Summary
rendering so the workbook cannot drift between separate implementations.
"""

from dataclasses import dataclass


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


def fib_levels(current, macro_lo, macro_hi, manual=None, is_manual=False):
    """Return AL1/AL2/AL3/D4 levels and gap-to-AL1 for a row."""
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

    gap = (current - al1) / al1 if current is not None and al1 else None
    return al1, al2, al3, d4, gap


def classify_zone(current, macro_lo=None, macro_hi=None, manual=None, is_manual=False):
    """Classify current price relative to alert levels.

    Rank meanings:
    0 below D4, 1 below AL3, 2 below AL2, 3 below AL1,
    4 approaching AL1, 5 above AL1, 9 no usable Fib levels.
    """
    al1, al2, al3, d4, gap = fib_levels(
        current, macro_lo, macro_hi, manual=manual, is_manual=is_manual
    )
    if al1 is None or current is None:
        return ZoneResult(9, "No Fib / ETF", "—", "555555", None, al1, al2, al3, d4)
    if d4 and current < d4:
        return ZoneResult(
            0,
            f"BELOW 78.6% ({d4}) — DEEPEST VALUE.",
            "BELOW 78.6%\nDEEPEST VALUE",
            "880000",
            gap,
            al1,
            al2,
            al3,
            d4,
        )
    if al3 and current < al3:
        return ZoneResult(
            1,
            f"AL3 HIT ({al3}) — BACK UP TRUCK.",
            "AL3 HIT\nBACK UP TRUCK",
            "C62828",
            gap,
            al1,
            al2,
            al3,
            d4,
        )
    if al2 and current < al2:
        return ZoneResult(
            2,
            f"AL2 HIT ({al2}) — STRONG BUY.",
            "AL2 HIT\nSTRONG BUY NOW",
            "E65100",
            gap,
            al1,
            al2,
            al3,
            d4,
        )
    if current < al1:
        return ZoneResult(
            3,
            f"AL1 HIT ({al1}) — ACCUMULATE.",
            "AL1 HIT\nACCUMULATE NOW",
            "2E7D32",
            gap,
            al1,
            al2,
            al3,
            d4,
        )
    if gap is not None and gap <= 0.15:
        return ZoneResult(
            4,
            f"APPROACHING AL1 ({al1}) — SET ALERTS.",
            "Above AL1\nNo action yet",
            "555555",
            gap,
            al1,
            al2,
            al3,
            d4,
        )
    return ZoneResult(
        5,
        f"ABOVE AL1 ({al1}) — WATCHING.",
        "Above AL1\nNo action yet",
        "555555",
        gap,
        al1,
        al2,
        al3,
        d4,
    )

