"""Explicitly invoked, quarantined import core; no production route or promotion.

Own metadata deliberately stays separate from app.db.Base. Importing this module
does not connect, create tables, start workers or register a public endpoint.
Listing observations are not assumed to represent distinct physical apartments.
"""
from datetime import date
import hashlib
import json
import re

from sqlalchemy import Column, ForeignKey, MetaData, String, Table, Text, select


metadata = MetaData()
batches = Table(
    "internal_trial_batches", metadata,
    Column("id", String(64), primary_key=True),
    Column("state", String(24), nullable=False),
    Column("manifest", Text, nullable=False),
    Column("rollback_note", Text, nullable=True),
)
records = Table(
    "internal_trial_records", metadata,
    Column("batch_id", String(64), ForeignKey(batches.c.id), primary_key=True),
    Column("identity", String(300), primary_key=True),
    Column("payload", Text, nullable=False),
)
PROVIDERS = {"airbnb", "booking_com", "expedia", "ctrip", "direct", "other"}
FIELDS = {
    "listing": {"title", "suburb", "unit_reference"},
    "reservation": {"listing_id", "check_in", "check_out", "status", "guest_count"},
    "cleaning": {"listing_id", "reservation_id", "due_date", "status", "cleaner_reference"},
}
COMMON = {"kind", "provider", "source_account", "source_id", "provenance"}
MAX_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 2000


class TrialImportError(ValueError):
    pass


def _text(value, limit=240):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise TrialImportError("Missing or oversized text field")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise TrialImportError("Control characters are not accepted")
    return value.strip()


def _id(value):
    value = _text(value, 100)
    if not re.fullmatch(r"[A-Za-z0-9@._+-]+", value):
        raise TrialImportError("Invalid external identity")
    return value


def _date(value):
    value = _text(value, 10)
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise TrialImportError("Dates must use YYYY-MM-DD") from None
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def preview(content):
    """Validate an allow-listed, non-guest-PII export without any writes."""
    if not isinstance(content, bytes) or len(content) > MAX_BYTES:
        raise TrialImportError("Use at most 2 MiB of UTF-8 JSON")
    try:
        data = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise TrialImportError("Invalid UTF-8 JSON") from None
    if not isinstance(data, list) or not 1 <= len(data) <= MAX_RECORDS:
        raise TrialImportError("Import 1 to 2000 source observations per batch")
    unique = {}
    for raw in data:
        if not isinstance(raw, dict):
            raise TrialImportError("Each source observation must be an object")
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in FIELDS:
            raise TrialImportError("Unknown observation kind")
        if not COMMON <= raw.keys() or raw.keys() - COMMON - FIELDS[kind]:
            raise TrialImportError("Unknown or missing fields; omit credentials and guest personal data")
        row = {k: _text(v, 1000 if k == "provenance" else 240) for k, v in raw.items()
               if k != "guest_count"}
        for key in ("source_id", "source_account", "listing_id", "reservation_id", "cleaner_reference"):
            if key in row:
                row[key] = _id(row[key])
        if row["provider"] not in PROVIDERS:
            raise TrialImportError("Unknown source platform")
        if kind == "listing" and "title" not in row:
            raise TrialImportError("Listing title is required")
        if kind == "reservation":
            if not {"listing_id", "check_in", "check_out", "status"} <= row.keys():
                raise TrialImportError("Reservation is missing source listing, dates or status")
            row["check_in"], row["check_out"] = _date(row["check_in"]), _date(row["check_out"])
            if row["check_out"] <= row["check_in"] or row["status"] not in {"confirmed", "cancelled"}:
                raise TrialImportError("Invalid reservation dates or status")
            if "guest_count" in raw:
                count = raw["guest_count"]
                if type(count) is not int or not 0 <= count <= 100:
                    raise TrialImportError("Invalid guest count")
                row["guest_count"] = count
        if kind == "cleaning":
            if not {"listing_id", "due_date", "status"} <= row.keys():
                raise TrialImportError("Cleaning observation is missing listing, date or status")
            row["due_date"] = _date(row["due_date"])
            if row["status"] not in {"pending", "assigned", "done", "cancelled"}:
                raise TrialImportError("Unknown source cleaning status")
        identity = ":".join(row[k] for k in ("provider", "source_account", "kind", "source_id"))
        if identity in unique and unique[identity] != row:
            raise TrialImportError("Conflicting duplicate source observations")
        unique[identity] = row
    for row in unique.values():
        prefix = row["provider"] + ":" + row["source_account"] + ":"
        if row["kind"] != "listing" and prefix + "listing:" + row["listing_id"] not in unique:
            raise TrialImportError("Include each observation's source listing; do not guess a physical unit")
        if row["kind"] == "cleaning" and "reservation_id" in row:
            reservation = unique.get(prefix + "reservation:" + row["reservation_id"])
            if not reservation or reservation["listing_id"] != row["listing_id"]:
                raise TrialImportError("Cleaning reservation must reference the same source listing")
    canonical = _json([unique[k] for k in sorted(unique)])
    return {"id": hashlib.sha256(canonical.encode()).hexdigest(), "manifest": canonical,
            "counts": {kind: sum(r["kind"] == kind for r in unique.values()) for kind in FIELDS},
            "physical_units_reconciled": False, "state": "quarantined"}


def install_schema(engine):
    """Explicit integration step after backup; never called on module import."""
    metadata.create_all(engine)


def apply(engine, content, *, expected_digest):
    plan = preview(content)
    if plan["id"] != expected_digest:
        raise TrialImportError("Preview changed; review the new export before importing")
    with engine.begin() as conn:
        existing = conn.execute(select(batches.c.state).where(batches.c.id == plan["id"])).scalar_one_or_none()
        if existing:
            if existing == "archived":
                raise TrialImportError("Batch was rolled back; it cannot be silently reactivated")
            return {**plan, "already_present": True}
        conn.execute(batches.insert().values(id=plan["id"], state="quarantined", manifest=plan["manifest"]))
        for row in json.loads(plan["manifest"]):
            identity = ":".join(row[k] for k in ("provider", "source_account", "kind", "source_id"))
            conn.execute(records.insert().values(batch_id=plan["id"], identity=identity, payload=_json(row)))
    return {**plan, "already_present": False}


def observations(engine, batch_id):
    with engine.connect() as conn:
        state = conn.execute(select(batches.c.state).where(batches.c.id == batch_id)).scalar_one_or_none()
        if state != "quarantined":
            return []
        return [json.loads(p) for p in conn.execute(select(records.c.payload)
                .where(records.c.batch_id == batch_id).order_by(records.c.identity)).scalars()]


def rollback(engine, batch_id, *, note):
    """Reversible visibility rollback; retains the original manifest and records."""
    note = _text(note, 500)
    with engine.begin() as conn:
        result = conn.execute(batches.update().where(batches.c.id == batch_id)
                              .values(state="archived", rollback_note=note))
        if not result.rowcount:
            raise TrialImportError("Unknown import batch")

