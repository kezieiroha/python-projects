"""JSON persistence for investment-tracker sidecar data.

Loads and saves the files under ``workbook/data/`` and nothing else: no provider calls,
no Fib maths, no Excel. Required files fail loudly with the offending path named,
because a missing or malformed portfolio file should stop the run rather than
silently produce a misleading workbook.
"""

from __future__ import annotations

import json
import os

from modules.calculations import validate_manual_levels
from modules.models import MacroSwingRow, PortfolioRow, StaticEntry, is_iso_date


def _check_low_date(value, label):
    """Raise when an optional ``macro_lo_date`` is present but not YYYY-MM-DD."""
    if value is not None and not is_iso_date(value):
        raise ValueError(f"{label}: macro_lo_date {value!r} is not YYYY-MM-DD")


def load_portfolio_rows(path):
    """Load editable portfolio rows from workbook/data/portfolio_rows.json.

    Each row's ``direction``, manual-level order and optional ``macro_lo_date``
    are validated on load. A contradiction would silently invert the zone
    logic, and a malformed date would silently disable the cycle-high check,
    so either stops the run with the row named.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("expected a list of row objects")
        rows = [PortfolioRow.from_dict(item) for item in data if isinstance(item, dict)]
        for row in rows:
            label = f"row {row.ticker or row.asset!r}"
            problem = validate_manual_levels(row)
            if problem:
                raise ValueError(f"{label}: {problem}")
            _check_low_date(row.macro_lo_date, label)
        return rows
    except Exception as ex:
        raise RuntimeError(f"Could not load portfolio rows from {path}: {ex}") from ex


def save_portfolio_rows(path, rows):
    """Persist edits to workbook/data/portfolio_rows.json."""
    saved = []
    for row in rows:
        ticker = (row.ticker or "").strip().upper()
        saved.append(row.replace(ticker=ticker).to_dict() if ticker else row.to_dict())
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(saved, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def load_macro_swing_data(path):
    """Load historical macro swing examples for the methodology sheet.

    Every anchor must declare ``ccy``: without it the levels cannot be read
    against a current price, so a missing unit fails loudly, naming the row.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("expected a list of row objects")
        swings = [MacroSwingRow.from_dict(item) for item in data if isinstance(item, dict)]
        for swing in swings:
            label = f"anchor {swing.ticker or '(no ticker)'}"
            if not (swing.ccy and str(swing.ccy).strip()):
                raise ValueError(f"{label} has no ccy")
            _check_low_date(swing.macro_lo_date, label)
        return swings
    except Exception as ex:
        raise RuntimeError(f"Could not load macro swing data from {path}: {ex}") from ex


def load_json_object(path, label):
    """Load a required JSON object, failing loudly with the offending file named."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        return data
    except Exception as ex:
        raise RuntimeError(f"Could not load {label} from {path}: {ex}") from ex


def load_unit_map(path, label, warn=print):
    """Load a fallback map (``ma_data.json``, ``analyst_data.json``) with units.

    Each value is either ``{"ccy": "GBp", "values": [...]}`` or a legacy bare
    array. Returns ticker -> StaticEntry. An entry with no unit (legacy array,
    or an object without ``ccy``) still loads, because these files are optional
    fallbacks, but its price amounts will be withheld; one warning is printed
    per file and ticker so the unit can be added by hand.
    """
    name = os.path.basename(str(path))
    entries = {}
    for ticker, value in load_json_object(path, label).items():
        if isinstance(value, dict):
            ccy, values = value.get("ccy"), value.get("values")
        else:
            ccy, values = None, value
        ccy = ccy.strip() if isinstance(ccy, str) and ccy.strip() else None
        entries[str(ticker)] = StaticEntry(
            ccy, tuple(values) if isinstance(values, list) else ())
        if ccy is None:
            warn(f"[fallback] {name} {ticker}: no unit recorded; static price "
                 f"amounts withheld. Add \"ccy\" to the entry once its source "
                 f"unit is verified.")
    return entries
