"""Monthly expected-vs-received rollup per property, for the reconciliation page."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy.orm import Session

from app.gst import gst_component
from app.models import Booking, BookingStatus, OwnerStatement, Transaction


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
