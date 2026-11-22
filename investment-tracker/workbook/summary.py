"""Daily Summary sheet: the first tab.

Shows every priced asset grouped by what can be done with it:

    IN BUY ZONES            in a buy zone and passed the fundamentals gate
    FALLING KNIFE           in a buy zone but at least one red flag
    DATA CHECK              in a buy zone with implausible data, or a unit check
    STRUCTURE BROKEN        price below the anchor low; Fib no longer applies
    APPROACHING / WATCHING  above AL1
    NO FIB / ETF            no usable levels

Derived from the same rows and the same shared calculations as Alert Levels,
then sorted by actionability so the first tab shows what needs attention.
"""

from openpyxl.utils import get_column_letter

from modules.calculations import (
    BUY_ZONE_RANKS,
    RANK_ABOVE,
    RANK_APPROACHING,
    RANK_BROKEN,
    RANK_UNIT_CHECK,
    classify_row,
    first_sentence,
    upside_to_ath,
)
from workbook.styles import (
    AL1_BG,
    AL2_BG,
    AL3_BG,
    AMB,
    D4_BG,
    DARK,
    GRN,
    RED_C,
    ZONE_STYLES,
    aln,
    bdr,
    fill,
    fnt,
    money,
    write_d5_cell,
)

SHEET_TITLE = "Daily Summary"

COLS = [
    "Asset", "Ticker", "CCY",
    "Current\nPrice",
    "Zone / Action",
    "AL1\n38.2%", "AL2\n50.0%", "AL3\n61.8%", "Deep Value\n78.6%",
    "Gap to\nAL1",
    "D5 EMA\nFlag",
    "Upside\nto ATH",
    "Analyst\nConsensus",
    "Analyst\nPrice Target",
    "Key Thesis",
]
WIDTHS = [26, 8, 5, 11, 20, 12, 12, 12, 12, 10, 12, 10, 14, 13, 52]
LAST_COL = get_column_letter(len(COLS))   # "O"

# Column numbers.
COL_GAP, COL_D5, COL_UPSIDE = 10, 11, 12
COL_CONSENSUS, COL_TARGET, COL_THESIS = 13, 14, 15

# Summary groups, in display order. Each is (key, header text, header colour).
GROUP_BUY = "buy"
GROUP_KNIFE = "knife"
GROUP_CHECK = "check"
GROUP_BROKEN = "broken"
GROUP_APPROACH = "approach"
GROUP_WATCH = "watch"
GROUP_NO_FIB = "no_fib"

SECTION_DEFS = [
    (GROUP_BUY,
     "IN BUY ZONES  -  Price at a Fib alert level and no red flags - action required today",
     "B71C1C"),
    (GROUP_KNIFE,
     "FALLING KNIFE - WATCH ONLY  -  In a buy zone but a red flag is set "
     "(D/E>1.0, margin<0, TTM P/E>100, Fwd P/E>80 or negative EPS)",
     "6D4C41"),
    (GROUP_CHECK,
     "DATA CHECK - VERIFY BEFORE ACTING  -  In a buy zone but fundamentals look "
     "implausible, or price and levels are ~100x apart",
     "BF360C"),
    (GROUP_BROKEN,
     "STRUCTURE BROKEN  -  Price below the 2022 anchor low; Fib levels no longer apply",
     "3E2723"),
    (GROUP_APPROACH,
     "APPROACHING AL1  -  Within 15% of the 38.2% accumulate level - set price alerts",
     "E65100"),
    (GROUP_WATCH,
     "WATCHING  -  Above AL1 - no entry yet; monitor for pullback to alert levels",
     "37474F"),
    (GROUP_NO_FIB,
     "NO FIB / ETF  -  Manual levels or ETF; refer to Alert Levels tab",
     "455A64"),
]
GROUP_ORDER = {key: idx for idx, (key, _label, _bg) in enumerate(SECTION_DEFS)}
SECTION_BY_KEY = {key: (label, bg) for key, label, bg in SECTION_DEFS}


def summary_group(zone_rank, check):
    """Return the summary group for a zone rank and its fundamentals check.

    Buy-zone rows pass through the quality gate: a red flag makes the row a
    falling knife; otherwise a pending data check holds it back. Exempt rows
    (funds, macro gauges, crypto) carry an empty check and pass straight in.
    A broken anchor low is its own rank, so it is excluded from IN BUY ZONES
    regardless of fundamentals.
    """
    if zone_rank in BUY_ZONE_RANKS:
        if check.is_red:
            return GROUP_KNIFE
        if check.needs_data_check:
            return GROUP_CHECK
        return GROUP_BUY
    if zone_rank == RANK_UNIT_CHECK:
        return GROUP_CHECK
    if zone_rank == RANK_BROKEN:
        return GROUP_BROKEN
    if zone_rank == RANK_APPROACHING:
        return GROUP_APPROACH
    if zone_rank == RANK_ABOVE:
        return GROUP_WATCH
    return GROUP_NO_FIB


def build_summary_rows(rows, ctx):
    """Derive sorted summary records from portfolio rows.

    Section rows and rows without a current price are omitted because the
    summary is an actionable price/zone view.

    Sort order: group, then zone depth (deepest first: below 78.6%, AL3, AL2,
    AL1), then gap to AL1 (most negative first).
    """
    summary = []
    for record in rows:
        if record.ticker in ctx.excluded_tickers:
            continue
        if record.is_section or record.current is None:
            continue

        zone = classify_row(record)
        check = ctx.fundamentals_for(record)
        summary.append({
            "row": record,
            "zone_rank": zone.rank, "al1": zone.al1, "al2": zone.al2,
            "al3": zone.al3, "d4": zone.d4, "gap": zone.gap_to_al1,
            "group": summary_group(zone.rank, check),
            "check": check,
            "upside": upside_to_ath(record.current, record.macro_hi),
            "analyst": ctx.analyst_for(record),
            "note": first_sentence(record.notes),
        })

    summary.sort(key=lambda x: (
        GROUP_ORDER[x["group"]],
        x["zone_rank"],
        x["gap"] if x["gap"] is not None else 99,
    ))
    return summary


def _write_header(ws, ctx):
    ws.freeze_panes = "A4"
    ws.sheet_view.zoomScale = 95

    ws.merge_cells(f"A1:{LAST_COL}1")
    ws["A1"] = "SIPP / ISA — Daily Action Summary  |  Fibonacci Alert Levels"
    ws["A1"].font = fnt(bold=True, color="FFFFFF", size=13)
    ws["A1"].fill = fill(DARK)
    ws["A1"].alignment = aln()
    ws.row_dimensions[1].height = 22

    ws.merge_cells(f"A2:{LAST_COL}2")
    ws["A2"] = (
        f"{ctx.run_date}  |  {ctx.d5_basis_text()}  |  "
        "IN BUY ZONES: at AL1/AL2/AL3 with no red flags - action required  |  "
        "FALLING KNIFE: in a zone but a red flag is set - watch only  |  "
        "DATA CHECK: verify before acting  |  "
        "STRUCTURE BROKEN: below anchor low  |  "
        "APPROACHING: within 15% of AL1 - set alerts  |  "
        "WATCHING: above AL1 - no action  |  "
        "Gap to AL1: negative = already in zone  |  "
        "D5 EMA Flag: completed close vs 5-day EMA; green above, red below, "
        "bold = crossed  |  Italic Current = intraday price  |  "
        "Upside = % from current to effective cycle ATH"
    )
    ws["A2"].font = fnt(italic=True, color="555555", size=7.5)
    ws["A2"].fill = fill("F0F0F0")
    ws["A2"].alignment = aln()
    ws.row_dimensions[2].height = 13

    for ci, (heading, width) in enumerate(zip(COLS, WIDTHS), 1):
        c = ws.cell(row=3, column=ci, value=heading)
        c.font = fnt(bold=True, color="FFFFFF", size=7.5)
        c.fill = fill(DARK)
        c.alignment = aln()
        c.border = bdr()
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.row_dimensions[3].height = 30


def _write_section_row(ws, row, text, bg):
    ws.merge_cells(f"A{row}:{LAST_COL}{row}")
    c = ws.cell(row=row, column=1, value=text)
    c.font = fnt(bold=True, color="FFFFFF", size=8)
    c.fill = fill(bg)
    c.alignment = aln("left")
    c.border = bdr()
    ws.row_dimensions[row].height = 14


def _write_footer(ws, row, ctx):
    ws.merge_cells(f"A{row}:{LAST_COL}{row}")
    ws[f"A{row}"] = (
        f"Prices: {ctx.data_source}  |  "
        "Fib: 2022 bear market low to effective cycle ATH  |  "
        "AL1=38.2%  AL2=50%  AL3=61.8%  Deep Value=78.6%  |  "
        "Gap to AL1: negative = price already in zone  |  "
        "Funds, crypto and macro gauges are exempt from the fundamentals gate  |  "
        "Not financial advice."
    )
    ws[f"A{row}"].font = fnt(italic=True, color="888888", size=7)
    ws[f"A{row}"].fill = fill("F0F0F0")
    ws[f"A{row}"].alignment = aln()
    ws.row_dimensions[row].height = 12


def _zone_label(entry, ctx):
    """Zone text: action verb only for rows that can be acted on.

    Sentiment gauges cannot be bought, and falling-knife or data-check rows
    should not be, so they show the zone without the action verb.
    """
    _bg, _fc, short, plain = ZONE_STYLES[entry["zone_rank"]]
    if entry["row"].ticker in ctx.sentiment_tickers:
        return plain
    if entry["group"] in (GROUP_KNIFE, GROUP_CHECK):
        reasons = list(entry["check"].red) or list(entry["check"].data_check)
        return plain + (f"\n{reasons[0]}" if reasons else "")
    return short


def render(ws, rows, ctx):
    """Write the Daily Summary sheet from prepared portfolio rows."""
    _write_header(ws, ctx)
    summary = build_summary_rows(rows, ctx)

    row = 4
    last_group = None

    for entry in summary:
        record = entry["row"]
        rank = entry["zone_rank"]
        group = entry["group"]

        if group != last_group:
            label, bg = SECTION_BY_KEY[group]
            _write_section_row(ws, row, label, bg)
            row += 1
            last_group = group

        zone_bg, zone_fc, _short, _plain = ZONE_STYLES[rank]
        highlighted = group == GROUP_BUY
        row_bg = zone_bg if highlighted else ("FFFFFF" if row % 2 == 0 else "F9F9F9")

        def cell(col, val, bold=False, color="000000", num_fmt=None, align="center",
                 italic=False):
            c = ws.cell(row=row, column=col, value=val)
            c.font = fnt(bold=bold, color=color, size=8, italic=italic)
            c.fill = fill(row_bg)
            c.alignment = aln(align)
            c.border = bdr()
            if num_fmt:
                c.number_format = num_fmt
            return c

        cell(1, record.asset, bold=highlighted,
             color=(zone_fc if highlighted else "000000"), align="left")
        cell(2, record.ticker, bold=True,
             color=(zone_fc if highlighted else "0000FF"))
        cell(3, record.ccy)
        cell(4, record.current, bold=True, color="0000FF", num_fmt='#,##0.00',
             italic=ctx.is_intraday(record))

        # E: zone label, colour-coded
        c = ws.cell(row=row, column=5, value=_zone_label(entry, ctx))
        c.font = fnt(bold=rank in BUY_ZONE_RANKS, color=zone_fc, size=7.5)
        c.fill = fill(zone_bg)
        c.alignment = aln()
        c.border = bdr()

        # F/G/H/I: alert levels
        for col, val, al_bg, al_fc in (
            (6, entry["al1"], AL1_BG, GRN),
            (7, entry["al2"], AL2_BG, AMB),
            (8, entry["al3"], AL3_BG, RED_C),
            (9, entry["d4"], D4_BG, "880000"),
        ):
            if val:
                c = ws.cell(row=row, column=col, value=val)
                c.font = fnt(size=8, bold=highlighted, color=al_fc)
                c.number_format = '#,##0.00'
            else:
                c = ws.cell(row=row, column=col, value="-")
                c.font = fnt(size=8, color="888888")
            c.fill = fill(al_bg)
            c.alignment = aln()
            c.border = bdr()

        # J: gap to AL1
        gap = entry["gap"]
        if gap is not None:
            if gap < 0:
                gap_bg, gap_fc = AL1_BG, GRN      # in zone
            elif gap < 0.08:
                gap_bg, gap_fc = AL2_BG, AMB      # very close
            else:
                gap_bg, gap_fc = "F5F5F5", "757575"
            c = ws.cell(row=row, column=COL_GAP, value=f"{gap:+.1%}")
            c.font = fnt(size=8, bold=(gap < 0), color=gap_fc)
            c.fill = fill(gap_bg)
        else:
            c = ws.cell(row=row, column=COL_GAP, value="-")
            c.font = fnt(size=8, color="888888")
            c.fill = fill("F5F5F5")
        c.alignment = aln()
        c.border = bdr()

        # K: D5 EMA flag
        write_d5_cell(ws, row, COL_D5, ctx.d5_for(record))

        # L: upside to effective ATH
        upside = entry["upside"]
        if upside is not None:
            if upside > 0.30:
                up_bg, up_fc = AL1_BG, GRN
            elif upside > 0.10:
                up_bg, up_fc = "FFF8E1", AMB
            else:
                up_bg, up_fc = "F5F5F5", "757575"
            c = ws.cell(row=row, column=COL_UPSIDE, value=f"{upside:+.1%}")
            c.font = fnt(size=8, color=up_fc)
            c.fill = fill(up_bg)
        else:
            c = ws.cell(row=row, column=COL_UPSIDE, value="-")
            c.font = fnt(size=8, color="888888")
            c.fill = fill("F5F5F5")
        c.alignment = aln()
        c.border = bdr()

        # M/N: analyst consensus and price target
        analyst = entry["analyst"]
        is_etf = record.is_etf
        if analyst and not is_etf:
            consensus = analyst.consensus
            if consensus == "Strong Buy":
                cons_bg, cons_fc = AL1_BG, GRN
            elif consensus == "Buy":
                cons_bg, cons_fc = "E8F5E9", "1B5E20"
            elif consensus == "Hold":
                cons_bg, cons_fc = AL2_BG, AMB
            else:
                cons_bg, cons_fc = "FFCDD2", RED_C
            c = ws.cell(row=row, column=COL_CONSENSUS,
                        value=f"{consensus} ({analyst.n_analysts})")
            c.font = fnt(size=8, bold=(consensus in ("Strong Buy", "Buy")), color=cons_fc)
            c.fill = fill(cons_bg)
        else:
            c = ws.cell(row=row, column=COL_CONSENSUS, value=record.na_label)
            c.font = fnt(size=8, color="888888")
            c.fill = fill("F5F5F5")
        c.alignment = aln()
        c.border = bdr()

        if analyst and not is_etf and record.current:
            pt_bold = False
            pt_upside = analyst.upside_from(record.current)
            if pt_upside is not None:
                pt_label = f"{money(analyst.target, record.ccy)} ({pt_upside:+.0%})"
                if pt_upside > 0.30:
                    pt_bg, pt_fc, pt_bold = AL1_BG, GRN, True
                elif pt_upside > 0.10:
                    pt_bg, pt_fc = "E8F5E9", "1B5E20"
                elif pt_upside >= 0:
                    pt_bg, pt_fc = AL2_BG, AMB
                else:
                    pt_bg, pt_fc = "FFCDD2", RED_C
            else:
                pt_label, pt_bg, pt_fc = "N/A", "F5F5F5", "888888"
            c = ws.cell(row=row, column=COL_TARGET, value=pt_label)
            c.font = fnt(size=8, bold=pt_bold, color=pt_fc)
            c.fill = fill(pt_bg)
        else:
            c = ws.cell(row=row, column=COL_TARGET, value="N/A")
            c.font = fnt(size=8, color="888888")
            c.fill = fill("F5F5F5")
        c.alignment = aln()
        c.border = bdr()

        # O: key thesis (first clause of the note)
        c = ws.cell(row=row, column=COL_THESIS, value=entry["note"])
        c.font = fnt(size=7.5, italic=not highlighted)
        c.fill = fill(row_bg)
        c.alignment = aln("left")
        c.border = bdr()

        ws.row_dimensions[row].height = 28
        row += 1

    _write_footer(ws, row, ctx)
    return ws
