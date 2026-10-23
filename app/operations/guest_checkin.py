"""Guest-submitted arrival details; submission is not evidence of actual attendance."""
import hashlib
import json
import re
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from app.models import Booking, BookingStatus, GuestCheckin, GuestCheckinEvent, Property
from app.operations.tasks import TaskError, form_token, verify_token


def booking_fingerprint(booking):
    return hashlib.sha256(json.dumps([booking.property_id, booking.check_in.isoformat(),
        booking.check_out.isoformat(), booking.status.value]).encode()).hexdigest()


def rules_hash(booking):
    return hashlib.sha256((booking.property.house_rules or '').encode()).hexdigest()


def guest_actor(booking):
    return 'guest:' + hashlib.sha256(booking.guest_portal_token.encode()).hexdigest()[:32]


def purpose(booking):
    return 'checkin:' + booking_fingerprint(booking) + ':' + rules_hash(booking)


def find_guest_booking(db, portal_token):
    if not portal_token or len(portal_token) > 256:
        return None
    return db.query(Booking).filter_by(guest_portal_token=portal_token,
        status=BookingStatus.confirmed, is_block=False).first()


def checkin_token(db, booking):
    row = db.query(GuestCheckin).filter_by(booking_id=booking.id).first()
    return form_token(guest_actor(booking), SimpleNamespace(id=booking.id, version=row.version if row else 0),
                      purpose=purpose(booking))


def is_current(booking, row):
    return bool(row and row.booking_fingerprint == booking_fingerprint(booking) and row.rules_hash == rules_hash(booking))


def submit_checkin(db, *, portal_token, token, contact_name, contact_phone, party_size, arrival_time, requests, acknowledged):
    booking = find_guest_booking(db, portal_token)
    if not booking:
        raise LookupError('Stay link not available')
    property_id, booking_id = booking.property_id, booking.id
    try:
        # Use the same lock order as reservation edits and source sync so the
        # acknowledged dates cannot change between validation and persistence.
        from app.operations.stock import lock_stock
        lock_stock(db)
        db.execute(update(Property).where(Property.id == property_id).values(name=Property.name))
        db.expire_all()
        booking = find_guest_booking(db, portal_token)
        if not booking or booking.id != booking_id:
            raise LookupError('Stay link not available')
        expected = verify_token(token, guest_actor(booking), booking.id, purpose=purpose(booking))
        row = db.query(GuestCheckin).filter_by(booking_id=booking.id).first()
        if expected != (row.version if row else 0):
            raise TaskError('Arrival details changed; reload this page before submitting')
        local_today = datetime.now(ZoneInfo(booking.property.time_zone)).date()
        if local_today > booking.check_out.date():
            raise TaskError('Arrival details can no longer be updated for this stay')
        contact_name, contact_phone, arrival_time, requests = contact_name.strip(), contact_phone.strip(), arrival_time.strip(), requests.strip()
        if not 1 <= len(contact_name) <= 160 or len(requests) > 2000:
            raise TaskError('Use a contact name up to 160 characters and requests up to 2000 characters')
        if len(contact_phone) > 40 or (contact_phone and not re.fullmatch(r'[0-9+(). -]+', contact_phone)):
            raise TaskError('Enter a valid contact phone number')
        maximum = booking.property.max_guests if booking.property.max_guests is not None else 100
        if not 1 <= party_size <= maximum:
            raise TaskError('Party size exceeds the permitted number of guests')
        if arrival_time and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', arrival_time):
            raise TaskError('Use an arrival time in HH:MM format')
        if booking.property.house_rules and not acknowledged:
            raise TaskError('Please confirm that you have read the house rules')
        if row is None:
            row = GuestCheckin(booking_id=booking.id)
            db.add(row)
        row.contact_name, row.contact_phone, row.party_size = contact_name, contact_phone, party_size
        row.arrival_time, row.requests = arrival_time, requests
        row.rules_hash, row.rules_snapshot = rules_hash(booking), booking.property.house_rules or ''
        row.booking_fingerprint = booking_fingerprint(booking)
        row.version, row.submitted_at = expected+1, datetime.utcnow()
        payload = {key:getattr(row,key) for key in ('contact_name','contact_phone','party_size','arrival_time',
            'requests','rules_hash','rules_snapshot','booking_fingerprint','version')}
        db.add(GuestCheckinEvent(booking_id=booking.id, token_digest=hashlib.sha256(token.encode()).hexdigest(),
            payload=json.dumps(payload, sort_keys=True)))
        db.commit()
        return row.id
    except IntegrityError:
        db.rollback(); raise TaskError('These details were already submitted; reload this page') from None
    except Exception:
        db.rollback(); raise
