"""Checkout-month expectation versus confirmed bank receipts, with completeness."""
from decimal import Decimal
from app.models import Booking, BookingStatus, Transaction


def monthly_summary(db, property_ids=None):
    groups = {}
    booking_query = db.query(Booking).filter(Booking.status == BookingStatus.confirmed, Booking.is_block.is_(False))
    transaction_query = db.query(Transaction).filter(Transaction.matched.is_(True))
    if property_ids is not None:
        booking_query = booking_query.filter(Booking.property_id.in_(property_ids))
        transaction_query = transaction_query.join(Booking).filter(Booking.property_id.in_(property_ids))
    for b in booking_query.all():
        key = (b.property_id, b.check_out.strftime("%Y-%m"))
        row = groups.setdefault(key, {"property_name": b.property.name, "property_id": b.property_id,
                                    "month": key[1], "known_expected": Decimal(0), "received": Decimal(0),
                                    "reported": Decimal(0), "missing_amount_count": 0})
        if b.amount is None:
            row["missing_amount_count"] += 1
        else:
            row["known_expected"] += b.amount
    for t in transaction_query.all():
        if not t.booking or t.booking.is_block:
            continue
        b = t.booking
        key = (b.property_id, b.check_out.strftime("%Y-%m"))
        row = groups.setdefault(key, {"property_name": b.property.name, "property_id": b.property_id,
                                    "month": key[1], "known_expected": Decimal(0), "received": Decimal(0),
                                    "reported": Decimal(0), "missing_amount_count": 0})
        row["reported"] += t.amount
        if t.confirmed_received:
            row["received"] += t.amount
    rows = []
    for key in sorted(groups, key=lambda k: (k[1], k[0]), reverse=True):
        row = groups[key]
        row["expected"] = None if row["missing_amount_count"] else row["known_expected"]
        row["variance"] = None if row["expected"] is None else row["received"] - row["expected"]
        rows.append(row)
    return rows
