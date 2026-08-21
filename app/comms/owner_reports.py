"""Monthly owner statement emails — one per property per month, sent once
the previous calendar month has fully closed. Idempotent via
OwnerStatement.sent_at, so re-running this on every scheduled sync is safe.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.comms.mailer import send_email, smtp_configured
from app.models import Booking, BookingStatus, Expense, OwnerStatement, Property


def _previous_period(today: datetime) -> str:
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - timedelta(days=1)
    return last_month_end.strftime("%Y-%m")


def _generate_statement(db: Session, prop: Property, period: str) -> OwnerStatement:
    year, month = (int(x) for x in period.split("-"))
    start = datetime(year, month, 1)
    end = datetime(year, month, monthrange(year, month)[1], 23, 59, 59)

    gross = (
        db.query(Booking)
        .filter(
            Booking.property_id == prop.id,
            Booking.status == BookingStatus.confirmed,
            Booking.check_out >= start,
            Booking.check_out <= end,
        )
        .all()
    )
    gross_revenue = sum((b.amount or Decimal("0")) for b in gross)

    expenses = (
        db.query(Expense)
        .filter(Expense.property_id == prop.id, Expense.occurred_at >= start, Expense.occurred_at <= end)
        .all()
    )
    total_expenses = sum((e.amount for e in expenses), Decimal("0"))

    commission_pct = prop.commission_pct or Decimal("0")
    commission_amount = (gross_revenue * commission_pct / Decimal("100")).quantize(Decimal("0.01"))

    statement = (
        db.query(OwnerStatement)
        .filter(OwnerStatement.property_id == prop.id, OwnerStatement.period == period)
        .first()
    )
    if not statement:
        statement = OwnerStatement(property_id=prop.id, period=period, adjustment_amount=Decimal("0"))
        db.add(statement)

    if not statement.finalized:
        statement.gross_revenue = gross_revenue
        statement.total_expenses = total_expenses
        statement.commission_amount = commission_amount
        statement.net_payout = gross_revenue - total_expenses - commission_amount + statement.adjustment_amount
        statement.generated_at = datetime.utcnow()

    return statement


def send_owner_reports(db: Session, today: datetime | None = None) -> dict:
    today = today or datetime.utcnow()
    period = _previous_period(today)
    summary = {"generated": 0, "sent": 0, "skipped_no_smtp": 0}

    owners = db.query(Property).filter(Property.owner_email.isnot(None)).all()
    for prop in owners:
        statement = _generate_statement(db, prop, period)
        summary["generated"] += 1
        db.commit()

        if statement.sent_at:
            continue  # already emailed this period

        if not smtp_configured():
            summary["skipped_no_smtp"] += 1
            continue

        body = (
            f"Hi {prop.owner_name or 'there'},\n\n"
            f"Here is your statement for {prop.name} — {period}:\n\n"
            f"Gross revenue: ${statement.gross_revenue}\n"
            f"Expenses: -${statement.total_expenses}\n"
            f"Management commission: -${statement.commission_amount}\n"
            f"Net payout: ${statement.net_payout}\n\n"
            f"Full details anytime at: https://ops.omiholiday.com/owner/{prop.owner_portal_token}"
        )
        send_email(prop.owner_email, f"Omi Holiday — {prop.name} statement ({period})", body)
        statement.sent_at = datetime.utcnow()
        summary["sent"] += 1
        db.commit()

    return summary
