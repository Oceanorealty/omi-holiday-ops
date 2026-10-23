"""Bounded, opt-in lifecycle messages with persisted dispatch claims."""
from __future__ import annotations
import os
import secrets
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.comms.mailer import send_email, smtp_configured
from app.comms.templates import render
from app.models import Booking, BookingStatus, MessageDelivery, MessageLog, MessageStatus, MessageTemplate, TriggerEvent


def _due_events(booking: Booking, today: date) -> list[TriggerEvent]:
    if not booking.notifications_enabled or booking.source_missing_since:
        return []
    ci, co = booking.check_in.date(), booking.check_out.date()
    created = booking.created_at.date() if booking.created_at else today
    events = []
    if created == today and today <= ci:
        events.append(TriggerEvent.booking_confirmed)
    if ci - timedelta(days=3) <= today < ci:
        events.append(TriggerEvent.pre_arrival)
    if today == ci:
        events.append(TriggerEvent.check_in_day)
    if co + timedelta(days=1) <= today <= co + timedelta(days=3):
        events.append(TriggerEvent.post_checkout)
    if booking.marketing_consent and today == co + timedelta(days=180):
        events.append(TriggerEvent.guest_re_engagement)
    return events


def _anchor(booking, event):
    if event == TriggerEvent.booking_confirmed:
        return booking.created_at.date().isoformat()
    return (booking.check_out if event in (TriggerEvent.post_checkout, TriggerEvent.guest_re_engagement) else booking.check_in).isoformat()


def _claim(db, booking, event):
    key = dict(booking_id=booking.id, event=event.value, scheduled_for=_anchor(booking, event))
    # Savepoints allow concurrent creators to converge on the unique delivery key.
    delivery = db.query(MessageDelivery).filter_by(**key).first()
    if not delivery:
        try:
            with db.begin_nested():
                delivery = MessageDelivery(**key, state="pending")
                db.add(delivery)
                db.flush()
        except IntegrityError:
            delivery = db.query(MessageDelivery).filter_by(**key).one()
    claimed = db.execute(update(MessageDelivery).where(MessageDelivery.id == delivery.id, MessageDelivery.state == "pending").values(state="sending")).rowcount
    db.commit()  # Persist before SMTP: a crash leaves a reviewable unknown outcome.
    return delivery if claimed else None


def process_due_messages(db: Session, today: date | None = None, *, dry_run: bool = False) -> dict:
    summary = {"sent": 0, "failed": 0, "skipped_no_email": 0, "skipped_no_smtp": 0,
               "held": 0, "planned": []}
    templates = {t.trigger_event: t for t in db.query(MessageTemplate).filter_by(active=True).all()}
    bookings = db.query(Booking).filter(Booking.status == BookingStatus.confirmed, Booking.is_block.is_(False)).all()
    for booking in bookings:
        local_today = today or datetime.now(ZoneInfo(booking.property.time_zone or "UTC")).date()
        for event in _due_events(booking, local_today):
            template = templates.get(event)
            if not template:
                continue
            log = db.query(MessageLog).filter_by(booking_id=booking.id, trigger_event=event).first()
            if log and log.status == MessageStatus.sent:
                continue
            summary["planned"].append({"booking_id": booking.id, "event": event.value})
            if dry_run or os.environ.get("OPS_MESSAGE_DISPATCH_ENABLED") != "1":
                summary["held"] += 1
                continue
            if not booking.guest or not booking.guest.email:
                summary["skipped_no_email"] += 1
                continue
            if not smtp_configured():
                summary["skipped_no_smtp"] += 1
                continue
            if not booking.guest_portal_token:
                booking.guest_portal_token = secrets.token_urlsafe(24)
            subject, body = render(template, booking)
            delivery = _claim(db, booking, event)
            if not delivery:
                summary["held"] += 1
                continue
            # Recheck the persisted booking after the claim transaction.
            db.refresh(booking)
            if booking.status != BookingStatus.confirmed or event not in _due_events(booking, local_today) or _anchor(booking, event) != delivery.scheduled_for:
                delivery.state = "cancelled"
                db.commit()
                continue
            subject, body = render(template, booking)
            try:
                send_email(booking.guest.email, subject, body)
            except Exception:
                delivery.state = "unknown"
                summary["failed"] += 1
                # SMTP errors can occur after acceptance: require operator reconciliation.
            else:
                delivery.state, delivery.completed_at = "sent", datetime.utcnow()
                if not log:
                    log = MessageLog(booking_id=booking.id, trigger_event=event)
                    db.add(log)
                log.status, log.sent_at, log.error = MessageStatus.sent, datetime.utcnow(), None
                summary["sent"] += 1
            db.commit()
    return summary
