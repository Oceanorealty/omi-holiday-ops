"""Checkout-month expectation versus confirmed bank receipts, with completeness."""
from collections import defaultdict
from decimal import Decimal

from sqlalchemy.orm import Session

from app.gst import gst_component
from app.models import Booking, BookingStatus, OwnerStatement, Transaction


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


def company_monthly_summary(db: Session) -> list[dict]:
    """All properties combined, by month — rolled up from owner statements
    (see OwnerStatement in app/models.py). commission_amount summed here is
    Omi Holiday's own revenue line; gross/expenses/net_payout are owners'
    money passing through, not Omi's."""
    statements = db.query(OwnerStatement).all()

    totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "gross_revenue": Decimal("0"),
            "total_expenses": Decimal("0"),
            "commission_amount": Decimal("0"),
            "net_payout": Decimal("0"),
        }
    )
    for s in statements:
        t = totals[s.period]
        t["gross_revenue"] += s.gross_revenue
        t["total_expenses"] += s.total_expenses
        t["commission_amount"] += s.commission_amount
        t["net_payout"] += s.net_payout

    rows = []
    for period in sorted(totals, reverse=True):
        t = totals[period]
        rows.append(
            {
                "period": period,
                **t,
                # The GST included within commission_amount, if it's a
                # GST-inclusive price — only meaningful when GST_REGISTERED
                # is set (see app/gst.py); computed either way since it's
                # cheap, left to the caller whether to show it.
                "gst_on_commission": gst_component(t["commission_amount"]),
            }
        )
    return rows
