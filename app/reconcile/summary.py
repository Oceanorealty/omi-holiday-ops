"""Monthly expected-vs-received rollup per property, for the reconciliation page."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Booking, BookingStatus, Transaction


def monthly_summary(db: Session) -> list[dict]:
    bookings = (
        db.query(Booking)
        .filter(Booking.status == BookingStatus.confirmed, Booking.amount.isnot(None))
        .all()
    )
    transactions = db.query(Transaction).filter(Transaction.matched.is_(True)).all()

    expected: dict[tuple, Decimal] = defaultdict(Decimal)
    received: dict[tuple, Decimal] = defaultdict(Decimal)
    property_names: dict[int, str] = {}

    for b in bookings:
        key = (b.property_id, b.check_out.strftime("%Y-%m"))
        expected[key] += Decimal(b.amount)
        property_names[b.property_id] = b.property.name

    for t in transactions:
        if not t.booking:
            continue
        key = (t.booking.property_id, t.booking.check_out.strftime("%Y-%m"))
        received[key] += Decimal(t.amount)
        property_names[t.booking.property_id] = t.booking.property.name

    rows = []
    for key in sorted(set(expected) | set(received), key=lambda k: (k[1], k[0]), reverse=True):
        property_id, month = key
        exp = expected.get(key, Decimal("0"))
        rec = received.get(key, Decimal("0"))
        rows.append(
            {
                "property_name": property_names.get(property_id, "—"),
                "month": month,
                "expected": exp,
                "received": rec,
                "variance": rec - exp,
            }
        )
    return rows
