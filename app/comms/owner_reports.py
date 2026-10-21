"""Reviewable owner statements. Unknown income blocks finalization and delivery."""
import os
from datetime import datetime, timedelta
from decimal import Decimal
from app.comms.mailer import send_email, smtp_configured
from app.models import Booking, BookingStatus, Expense, OwnerStatement, Property, StatementDelivery
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError


def _previous_period(today):
    return (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def _generate_statement(db, prop, period):
    start = datetime.strptime(period, "%Y-%m")
    if start.strftime("%Y-%m") != period:
        raise ValueError("invalid_period")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    statement = db.query(OwnerStatement).filter_by(property_id=prop.id, period=period).first()
    if statement and statement.finalized:
        return statement
    bookings = db.query(Booking).filter(Booking.property_id == prop.id,
        Booking.status == BookingStatus.confirmed, Booking.is_block.is_(False),
        Booking.check_out >= start, Booking.check_out < end).all()
    expenses = db.query(Expense).filter(Expense.property_id == prop.id, Expense.occurred_at >= start, Expense.occurred_at < end).all()
    if not statement:
        statement = OwnerStatement(property_id=prop.id, period=period, adjustment_amount=Decimal(0))
        db.add(statement)
    statement.missing_amount_count = sum(b.amount is None for b in bookings)
    statement.gross_revenue = sum((b.amount for b in bookings if b.amount is not None), Decimal(0))
    statement.total_expenses = sum((e.amount for e in expenses), Decimal(0))
    statement.commission_amount = (statement.gross_revenue * (prop.commission_pct or Decimal(0)) / 100).quantize(Decimal("0.01"))
    statement.net_payout = statement.gross_revenue - statement.total_expenses - statement.commission_amount + statement.adjustment_amount
    statement.generated_at = datetime.utcnow()
    return statement


def finalize_statement(db, statement):
    if statement.finalized:
        return statement  # Finalized snapshots are immutable; never toggle them open.
    prop = db.get(Property, statement.property_id)
    statement = _generate_statement(db, prop, statement.period)
    if statement.missing_amount_count or prop.commission_pct is None:
        raise ValueError("statement_incomplete")
    statement.finalized = True
    return statement


def send_owner_reports(db, today=None):
    period = _previous_period(today or datetime.utcnow())
    summary = {"generated": 0, "sent": 0, "skipped_no_smtp": 0, "held_for_review": 0}
    for prop in db.query(Property).filter(Property.owner_email.isnot(None)).all():
        statement = _generate_statement(db, prop, period)
        db.commit()
        summary["generated"] += 1
        # Explicit publish/finalize is necessary; drafts never silently email.
        if not statement.finalized or statement.missing_amount_count or os.environ.get("OPS_OWNER_EMAIL_ENABLED") != "1":
            summary["held_for_review"] += 1
            continue
        if statement.sent_at:
            continue
        if not smtp_configured():
            summary["skipped_no_smtp"] += 1
            continue
        delivery = db.query(StatementDelivery).filter_by(statement_id=statement.id).first()
        if not delivery:
            try:
                with db.begin_nested():
                    delivery = StatementDelivery(statement_id=statement.id, state="pending")
                    db.add(delivery)
                    db.flush()
            except IntegrityError:
                delivery = db.query(StatementDelivery).filter_by(statement_id=statement.id).one()
        claimed = db.execute(update(StatementDelivery).where(StatementDelivery.id == delivery.id,
            StatementDelivery.state == "pending").values(state="sending")).rowcount
        db.commit()
        if not claimed:
            summary["held_for_review"] += 1
            continue
        body = (f"Statement for {prop.name} — {period}\n"
                f"Checkout-month revenue: AUD {statement.gross_revenue}\n"
                f"Expenses: AUD {statement.total_expenses}\n"
                f"Commission: AUD {statement.commission_amount}\n"
                f"Calculated payout (not a bank receipt): AUD {statement.net_payout}\n")
        try:
            send_email(prop.owner_email, f"Omi Holiday statement ({period})", body)
        except Exception:
            delivery.state = "unknown"
            summary["held_for_review"] += 1
        else:
            delivery.state = "sent"
            statement.sent_at = datetime.utcnow()
            summary["sent"] += 1
        db.commit()
    return summary
