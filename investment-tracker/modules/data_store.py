"""JSON persistence for investment-tracker sidecar data.

Loads and saves the files under ``workbook/data/`` and nothing else: no provider calls,
no Fib maths, no Excel. Required files fail loudly with the offending path named,
because a missing or malformed portfolio file should stop the run rather than
silently produce a misleading workbook.
"""

from __future__ import annotations

import json

from modules.models import MacroSwingRow, PortfolioRow


def load_portfolio_rows(path):
    """Load editable portfolio rows from workbook/data/portfolio_rows.json."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("expected a list of row objects")
        return [PortfolioRow.from_dict(item) for item in data if isinstance(item, dict)]
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
    """Load historical macro swing examples for the methodology sheet."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("expected a list of row objects")
        return [MacroSwingRow.from_dict(item) for item in data if isinstance(item, dict)]
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


def load_tuple_map(path, label):
    """Load a required JSON object whose values should behave like tuples.

    Used for the fallback maps (``ma_data.json``, ``analyst_data.json``) whose
    values are stored as arrays and consumed as fixed-shape tuples.
    """
    data = load_json_object(path, label)
    return {
        str(k): tuple(v) if isinstance(v, list) else v
        for k, v in data.items()
    }
