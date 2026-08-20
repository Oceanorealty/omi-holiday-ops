"""
Suggests candidate bookings for an unmatched transaction — matching money is
too risky to do fully automatically without real sample payout data to
calibrate against, so this narrows the list for a human to pick from rather
than auto-confirming a match.

Ranking: bookings whose checkout is within 21 days of the transaction date
(payouts typically land within a few weeks of checkout), closest date first;
an amount within 2% of the booking's recorded amount is boosted to the top.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Booking, BookingStatus, Transaction

WINDOW_DAYS = 21


def suggest_bookings(db: Session, transaction: Transaction, limit: int = 5) -> list[Booking]:
    window_start = transaction.occurred_at - timedelta(days=WINDOW_DAYS)
    window_end = transaction.occurred_at + timedelta(days=WINDOW_DAYS)

    query = db.query(Booking).filter(
        Booking.status == BookingStatus.confirmed,
        Booking.check_out >= window_start,
        Booking.check_out <= window_end,
    )
    if transaction.property_id:
        query = query.filter(Booking.property_id == transaction.property_id)

    candidates = query.all()

    def score(b: Booking) -> tuple:
        date_distance = abs((b.check_out - transaction.occurred_at).days)
        amount_match = 0
        if b.amount is not None:
            diff = abs(Decimal(b.amount) - transaction.amount)
            if diff <= transaction.amount * Decimal("0.02"):
                amount_match = -1  # sorts first
        return (amount_match, date_distance)

    candidates.sort(key=score)
    return candidates[:limit]
