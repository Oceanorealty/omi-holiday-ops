"""Local direct reservations. No OTA, payment or outbound messaging calls."""
import hashlib
import json
import secrets
import uuid
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.models import (Booking, BookingStatus, CleaningStatus, CleaningTask,
                        Guest, PlatformName, Property, ReservationEvent)
from app.operations.tasks import TaskError, form_token, verify_token


def snapshot(row):
    if row is None:
        return {}
    return {"property_id": row.property_id, "check_in": row.check_in.isoformat(),
            "check_out": row.check_out.isoformat(), "status": row.status.value,
            "amount": str(row.amount) if row.amount is not None else None,
            "guest_id": row.guest_id, "summary": row.summary,
            "source_feed_id": row.source_feed_id, "platform": row.platform.value,
            "is_block": row.is_block, "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "guest_fingerprint": hashlib.sha256(json.dumps([row.guest.name, row.guest.email]).encode()).hexdigest() if row.guest else None}


def revision(row):
    state = json.dumps(snapshot(row), sort_keys=True)
    return int(hashlib.sha256(state.encode()).hexdigest()[:15], 16) if row else 0


def reservation_token(actor, row=None):
    ref = SimpleNamespace(id=row.id, version=revision(row)) if row else None
    return form_token(actor, ref, purpose='reservation')


def money(value):
    if value == '' or value is None:
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal('99999999.99'):
            raise ValueError()
        if result != result.quantize(Decimal('.01')):
            raise ValueError()
        return result.quantize(Decimal('.01'))
    except (ValueError, InvalidOperation):
        raise TaskError('Use a non-negative amount with at most two decimal places') from None


def stay_dates(check_in, check_out):
    try:
        start, end = date.fromisoformat(check_in), date.fromisoformat(check_out)
        if start.isoformat() != check_in or end.isoformat() != check_out or not 1 <= (end-start).days <= 366:
            raise ValueError()
    except (ValueError, TypeError):
        raise TaskError('Choose valid YYYY-MM-DD dates for a stay of 1–366 nights') from None
    return datetime.combine(start, time.min), datetime.combine(end, time.min)


def save_reservation(db, *, actor, entity_id, token, property_id, check_in='', check_out='',
                     amount='', guest_name='', guest_email='', note='', action='save', commit=True):
    expected = verify_token(token, actor, entity_id, purpose='reservation')
    note, guest_name, guest_email = note.strip(), guest_name.strip(), guest_email.strip()
    if not 10 <= len(note) <= 1000:
        raise TaskError('Record a change note of 10–1000 characters')
    if action not in ('save', 'cancel', 'restore') or (not entity_id and action != 'save'):
        raise TaskError('Unknown reservation action')
    if len(guest_name) > 160 or len(guest_email) > 254 or (guest_email and
            ('@' not in guest_email or any(c.isspace() for c in guest_email) or guest_email.count('@') != 1)):
        raise TaskError('Enter a valid guest name and email')
    try:
        # Serialize local booking writers on the property, including on SQLite.
        from app.operations.stock import available_units, lock_stock, refresh_conflicts
        lock_stock(db)
        # Source sync is still authoritative; external channel inventory is not reserved here.
        locked = db.execute(update(Property).where(Property.id == property_id).values(name=Property.name))
        if locked.rowcount != 1:
            raise TaskError('Property not found')
        db.expire_all()
        row = db.get(Booking, entity_id) if entity_id else None
        if entity_id and (row is None or row.property_id != property_id or revision(row) != expected):
            raise TaskError('Reservation changed; reload before saving')
        if row and (row.platform != PlatformName.direct or row.source_feed_id is not None or row.is_block):
            raise TaskError('Only local direct reservations can be changed here; manage channel bookings at their source')
        before = snapshot(row)
        if action == 'cancel':
            if row.status != BookingStatus.confirmed:
                raise TaskError('Reservation is already cancelled')
            row.status = BookingStatus.cancelled
        else:
            if row and row.status == BookingStatus.cancelled and action != 'restore':
                raise TaskError('Use Restore to reopen a cancelled reservation')
            if action == 'restore':
                if row.status != BookingStatus.cancelled:
                    raise TaskError('Only cancelled reservations can be restored')
                start, end, total = row.check_in, row.check_out, row.amount
            else:
                start, end = stay_dates(check_in, check_out)
                total = money(amount)
            from app.operations.inventory import restriction_reason
            reason = restriction_reason(db, property_id, start.date(), end.date(), check_window=(row is None or action=='restore' or row.check_in!=start or row.check_out!=end))
            if reason:
                raise TaskError('Stay restrictions prevent this booking: ' + reason)
            if available_units(db,property_id,start,end,exclude_id=entity_id)<1:
                raise TaskError('Dates are unavailable or inventory needs review; no reservation saved')
            if row is None:
                row = Booking(property_id=property_id, platform=PlatformName.direct,
                    guest_portal_token=secrets.token_urlsafe(32),
                    uid='direct-'+uuid.uuid4().hex, notifications_enabled=False, marketing_consent=False)
                db.add(row)
            row.check_in, row.check_out, row.amount = start, end, total
            row.status = BookingStatus.confirmed
            if action == 'save':
                existing = row.guest
                if not existing or (existing.name or '') != guest_name or (existing.email or '') != guest_email:
                    # Do not modify a guest shared by other reservations.
                    row.guest = Guest(name=guest_name or None, email=guest_email or None,
                                      language=existing.language if existing else 'en')
        row.updated_at = datetime.utcnow()
        db.flush()
        task = db.query(CleaningTask).filter_by(booking_id=row.id).one_or_none()
        if row.status == BookingStatus.cancelled:
            if task and task.status != CleaningStatus.done:
                task.cancelled_at = task.cancelled_at or datetime.utcnow()
                task.schedule_review_required = False
        elif task is None:
            prop = db.get(Property, property_id)
            db.add(CleaningTask(booking_id=row.id, property_id=property_id, due_date=row.check_out,
                assignee=prop.default_cleaner,
                status=CleaningStatus.assigned if prop.default_cleaner else CleaningStatus.pending))
        else:
            task.cancelled_at = None
            if task.due_date != row.check_out:
                if task.schedule_locked or task.status == CleaningStatus.done:
                    task.schedule_review_required = task.schedule_reviewed_checkout != row.check_out
                else:
                    task.due_date, task.schedule_review_required = row.check_out, False
        # Keep the original Bookings page's stored conflict flags consistent.
        db.flush()
        refresh_conflicts(db)
        db.add(ReservationEvent(booking_id=row.id, token_digest=hashlib.sha256(token.encode()).hexdigest(),
            actor=actor, action='create' if not entity_id else action, note=note,
            before_state=json.dumps(before, sort_keys=True), after_state=json.dumps(snapshot(row), sort_keys=True)))
        if commit:
            db.commit()
        else:
            db.flush()
        return row.id
    except IntegrityError:
        db.rollback()
        raise TaskError('This request was already saved; reload before submitting again') from None
    except Exception:
        db.rollback()
        raise
