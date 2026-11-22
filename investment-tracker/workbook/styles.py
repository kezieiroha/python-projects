"""Shared openpyxl style helpers and workbook colour constants.

These are presentation config, not market data. Sheet renderers import from here
so colour choices stay consistent across Alert Levels, Daily Summary, Fib
Methodology, and How to Run.
"""

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from modules.calculations import (
    RANK_AL1,
    RANK_AL2,
    RANK_AL3,
    RANK_APPROACHING,
    RANK_ABOVE,
    RANK_BROKEN,
    RANK_DEEP,
    RANK_NO_FIB,
    RANK_UNIT_CHECK,
    d5_flag,
    is_known_unit,
)


DARK = "1A1A2E"
SECT = "16213E"
AL1_BG = "E8F5E9"
AL2_BG = "FFF8E1"
AL3_BG = "FFEBEE"
D4_BG = "FFCCCC"
FIB_BG = "EDE7F6"
FUND_BG = "E0F2F1"
ISA_BG = "E3F2FD"
SIPP_BG = "F3E5F5"
MANUAL = "FFF3E0"
ALT = "F5F5F5"
WHITE = "FFFFFF"
GRN = "2E7D32"
AMB = "E65100"
RED_C = "C62828"
BLUE = "0D47A1"
PURPLE = "4A148C"
MA_BG = "E0F7FA"
MA_HEAD = "00695C"
CHECK_BG = "FFE0B2"     # DATA CHECK / superseded-ATH highlight
CHECK_FC = "BF360C"

# Zone rank -> (background, font colour, short label, plain label).
# The short label carries the action verb for a buy-zone row that passed the
# quality gate. The plain label states the zone only, for rows that cannot be
# acted on: sentiment gauges, falling knives, and rows awaiting a data check.
ZONE_STYLES = {
    RANK_DEEP: ("880000", "FFFFFF", "BELOW 78.6%  DEEPEST VALUE", "BELOW 78.6%"),
    RANK_AL3: ("FFCDD2", "880000", "AL3 HIT  BACK UP TRUCK", "AL3 HIT"),
    RANK_AL2: ("FFE0B2", "BF360C", "AL2 HIT  STRONG BUY", "AL2 HIT"),
    RANK_AL1: ("E8F5E9", "1B5E20", "AL1 HIT  ACCUMULATE", "AL1 HIT"),
    RANK_APPROACHING: ("FFF9C4", "F57F17", "APPROACHING AL1", "APPROACHING AL1"),
    RANK_ABOVE: ("F5F5F5", "757575", "Above AL1 - Watching", "Above AL1"),
    RANK_BROKEN: ("D7CCC8", "3E2723", "BELOW ANCHOR LOW  STRUCTURE BROKEN",
                  "BELOW ANCHOR LOW"),
    RANK_UNIT_CHECK: ("E1BEE7", "4A148C", "UNIT CHECK", "UNIT CHECK"),
    RANK_NO_FIB: ("ECEFF1", "607D8B", "No Fib / ETF", "No Fib / ETF"),
}


def fill(hex_color):
    return PatternFill("solid", fgColor=hex_color)


def fnt(bold=False, color="000000", size=8, italic=False):
    return Font(name="Arial", bold=bold, color=color, size=size, italic=italic)


def aln(h="center", v="center", wrap=True):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)


def bdr():
    side = Side(style="thin", color="CCCCCC")
    return Border(left=side, right=side, top=side, bottom=side)


def ma_num_fmt(val):
    """Number format for a moving-average cell, scaled to magnitude."""
    if val >= 10000:
        return '#,##0'
    if val >= 1:
        return '#,##0.00'
    return '0.0000'


def ma_cell_color(current, ema_val):
    """Return (background, font colour) for price vs moving-average comparison."""
    if current is None or ema_val is None:
        return MA_BG, "888888"
    if current > ema_val:
        return "C8E6C9", GRN
    return "FFCDD2", RED_C


def d5_cell_style(is_above):
    """Return (background, font colour) for the D5 EMA Flag: green above, red below."""
    return ("C8E6C9", GRN) if is_above else ("FFCDD2", RED_C)


def write_d5_cell(ws, row, col, view, size=8):
    """Write a D5 EMA Flag cell. Used by both Alert Levels and Daily Summary.

    Green when the latest completed close is above D5, red when below, bold
    when it crossed D5 since the previous completed session. N/A when the
    ticker has no daily history (offline runs, placeholders).
    """
    flag = d5_flag(view)
    if flag is None:
        c = ws.cell(row=row, column=col, value="N/A")
        c.font = fnt(size=size, color="888888")
        c.fill = fill(MA_BG)
    else:
        label, is_above, crossed = flag
        bg, fc = d5_cell_style(is_above)
        c = ws.cell(row=row, column=col, value=label)
        c.font = fnt(size=size, color=fc, bold=crossed)
        c.fill = fill(bg)
    c.alignment = aln()
    c.border = bdr()
    return c


def eps_text(eps, unit, verified):
    """Format a trailing EPS in the unit of the value shown.

    ``unit`` is the EPS's own unit (see ``SheetContext.eps_unit_for``): the
    established reporting unit for provider EPS, or the row's unit for EPS
    derived from price and P/E. An unknown unit shows the bare number rather
    than guessing a symbol. Unverified values carry a "~" prefix.
    """
    sign = "-" if eps < 0 else ""
    prefix = "" if verified else "~"
    amount = money(abs(eps), unit, 2) if is_known_unit(unit) else f"{abs(eps):,.2f}"
    return f"{sign}{prefix}{amount}"


def money(value, ccy, decimals=0):
    """Format a price in the row's own unit: $12, GBP 12, 1,234p, or CODE 12.

    Analyst targets used to be printed with "$" for every row, including LSE
    lines quoted in pence.
    """
    if value is None:
        return "N/A"
    code = (ccy or "").strip()
    number = f"{value:,.{decimals}f}"
    if code in ("GBp", "GBX") or code.upper() == "GBX":
        return f"{number}p"
    if code.upper() == "USD" or not code:
        return f"${number}"
    if code.upper() == "GBP":
        return f"GBP {number}"
    return f"{code.upper()} {number}"
