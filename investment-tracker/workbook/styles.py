"""Shared openpyxl style helpers and workbook colour constants.

These are presentation config, not market data. Sheet renderers import from here
so colour choices stay consistent across Alert Levels, Daily Summary, Fib
Methodology, and How to Run.
"""

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side


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
