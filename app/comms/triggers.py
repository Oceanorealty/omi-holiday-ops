"""
Decides which (booking, trigger_event) pairs are due today, and sends them.

Idempotency: a MessageLog row is only a permanent "done" marker once its status
is `sent`. Anything else (skipped_no_email, skipped_no_smtp, failed) is retried
on every run — e.g. a guest email added after the initial sync should still get
its booking_confirmed email next time `process_due_messages` runs.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.comms.mailer import send_email, smtp_configured
from app.comms.templates import render
from app.models import Booking, BookingStatus, MessageLog, MessageStatus, MessageTemplate, TriggerEvent


def _due_events(booking: Booking, today: date) -> list[TriggerEvent]:
    events = [TriggerEvent.booking_confirmed]
    check_in = booking.check_in.date()
    check_out = booking.check_out.date()
    if today >= check_in - timedelta(days=3):
        events.append(TriggerEvent.pre_arrival)
    if today >= check_in:
        events.append(TriggerEvent.check_in_day)
    if today >= check_out + timedelta(days=1):
        events.append(TriggerEvent.post_checkout)
    return events


def _get_or_create_log(db: Session, booking_id: int, event: TriggerEvent) -> MessageLog | None:
    """Returns the log row to write this run's result into, or None if it's
    already permanently sent and nothing more to do."""
    log = (
        db.query(MessageLog)
        .filter_by(booking_id=booking_id, trigger_event=event)
        .one_or_none()
    )
    if log is None:
        log = MessageLog(booking_id=booking_id, trigger_event=event, status=MessageStatus.failed)
        db.add(log)
        db.flush()
        return log
    if log.status == MessageStatus.sent:
        return None
    return log


def process_due_messages(db: Session, today: date | None = None) -> dict:
    today = today or date.today()
    templates = {t.trigger_event: t for t in db.query(MessageTemplate).filter_by(active=True).all()}
    bookings = db.query(Booking).filter(Booking.status == BookingStatus.confirmed).all()

    summary = {"sent": 0, "failed": 0, "skipped_no_email": 0, "skipped_no_smtp": 0}

    for booking in bookings:
        for event in _due_events(booking, today):
            template = templates.get(event)
            if not template:
                continue
            log = _get_or_create_log(db, booking.id, event)
            if log is None:
                continue  # already sent for this booking+event

            log.error = None
            guest_email = booking.guest.email if booking.guest else None
            if not guest_email:
                log.status = MessageStatus.skipped_no_email
                summary["skipped_no_email"] += 1
            elif not smtp_configured():
                log.status = MessageStatus.skipped_no_smtp
                summary["skipped_no_smtp"] += 1
            else:
                subject, body = render(template, booking)
                try:
                    send_email(guest_email, subject, body)
                    log.status = MessageStatus.sent
                    summary["sent"] += 1
                except Exception as exc:  # noqa: BLE001 — record any send failure
                    log.status = MessageStatus.failed
                    log.error = str(exc)
                    summary["failed"] += 1
            db.commit()

    return summary
