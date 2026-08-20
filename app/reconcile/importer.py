"""
Parses a generic transactions CSV: columns `date`, `amount`, `note`
(case-insensitive, any order — extra columns are ignored). This is
deliberately not a per-platform parser (Airbnb/Booking.com/bank exports all
use different column names) — export from whichever source, then rename/
reorder columns to match this format before uploading.
"""

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

from dateutil import parser as date_parser


class ImportError_(Exception):
    pass


def parse_csv(content: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise ImportError_("Empty file")

    columns = {name.strip().lower(): name for name in reader.fieldnames}
    for required in ("date", "amount"):
        if required not in columns:
            raise ImportError_(
                f"Missing required column '{required}'. Found columns: {list(columns.values())}"
            )

    rows = []
    for i, row in enumerate(reader, start=2):  # header is row 1
        raw_date = row[columns["date"]].strip()
        raw_amount = row[columns["amount"]].strip()
        note = row[columns["note"]].strip() if "note" in columns else ""

        if not raw_date or not raw_amount:
            continue  # skip blank rows

        try:
            occurred_at = date_parser.parse(raw_date)
        except (ValueError, OverflowError):
            raise ImportError_(f"Row {i}: couldn't parse date '{raw_date}'")

        try:
            amount = Decimal(raw_amount.replace(",", "").replace("$", ""))
        except InvalidOperation:
            raise ImportError_(f"Row {i}: couldn't parse amount '{raw_amount}'")

        rows.append({"occurred_at": occurred_at, "amount": amount, "note": note})

    return rows
