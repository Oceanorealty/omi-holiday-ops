"""Internal attendance journal; never edits reservation inventory or dispatches work."""
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from app.models import Booking, BookingStatus, ReservationEvent
from app.operations.reservations import revision
from app.operations.stock import lock_stock
from app.operations.tasks import TaskError, form_token, verify_token


def state(db, booking_id):
    event = db.query(ReservationEvent).filter_by(booking_id=booking_id, action='stay_record').order_by(ReservationEvent.id.desc()).first()
    if not event:
        return dict(status='not_recorded', arrived_at='', departed_at='', expected_departure='', event_id=0)
    try:
        data = json.loads(event.after_state)
        if not isinstance(data, dict) or set(data) != {'status', 'arrived_at', 'departed_at', 'expected_departure'} or not all(isinstance(v, str) for v in data.values()) or data['status'] not in ('not_recorded', 'in_house', 'departed'):
            raise ValueError()
        return {**data, 'event_id': event.id}
    except (ValueError, KeyError, TypeError):
        raise TaskError('Attendance history needs review; no status inferred') from None


def version(db, booking):
    current = state(db, booking.id)
    fingerprint = f'{revision(booking)}:{current["event_id"]}'
    version = int(hashlib.sha256(fingerprint.encode()).hexdigest()[:15], 16)
    return version


def token(db, actor, booking):
    return form_token(actor, SimpleNamespace(id=booking.id, version=version(db, booking)), purpose='stay-record')


def local_time(value, zone):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%dT%H:%M')
        if parsed.strftime('%Y-%m-%dT%H:%M') != value:
            raise ValueError()
        first, second = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError()
        utc = first.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) != parsed:
            raise ValueError()
        return utc
    except (ValueError, TypeError):
        raise TaskError('Enter an unambiguous local date and time') from None


def record(db, *, actor, booking_id, submitted_token, action, note, arrived_at='', departed_at='', expected_departure='', target_status='', now=None):
    if not 10 <= len(note.strip()) <= 960:
        raise TaskError('Record a reason of 10–960 characters')
    try:
        lock_stock(db)
        db.expire_all()
        booking = db.get(Booking, booking_id)
        if not booking or booking.is_block:
            raise TaskError('Guest reservation not found')
        expected_version = verify_token(submitted_token, actor, booking_id, purpose='stay-record')
        if expected_version != version(db, booking):
            raise TaskError('Reservation or attendance changed; reload before saving')
        before = state(db, booking_id)
        after = {k: v for k, v in before.items() if k != 'event_id'}
        zone = ZoneInfo(booking.property.time_zone)
        now = now or datetime.now(timezone.utc)
        if action == 'arrive' and before['status'] == 'not_recorded':
            after.update(status='in_house', arrived_at=arrived_at)
        elif action == 'depart' and before['status'] == 'in_house':
            after.update(status='departed', departed_at=departed_at)
        elif action == 'plan_departure' and before['status'] != 'departed':
            after['expected_departure'] = expected_departure
        elif action == 'correct' and target_status in ('not_recorded', 'in_house', 'departed'):
            after.update(status=target_status, arrived_at=arrived_at, departed_at=departed_at, expected_departure=expected_departure)
        else:
            raise TaskError('This action is not valid for the recorded attendance state')
        if action != 'correct' and booking.status != BookingStatus.confirmed:
            raise TaskError('Cancelled reservation: review it before recording attendance')
        if after['status'] == 'not_recorded' and (after['arrived_at'] or after['departed_at']):
            raise TaskError('Unrecorded attendance cannot contain actual times')
        if after['status'] == 'in_house' and (not after['arrived_at'] or after['departed_at']):
            raise TaskError('In-house attendance requires an arrival and no departure')
        if after['status'] == 'departed' and (not after['arrived_at'] or not after['departed_at']):
            raise TaskError('Departure requires both actual times')
        arrived = local_time(after['arrived_at'], zone) if after['arrived_at'] else None
        departed = local_time(after['departed_at'], zone) if after['departed_at'] else None
        expected = local_time(after['expected_departure'], zone) if after['expected_departure'] else None
        if (arrived and arrived > now) or (departed and departed > now):
            raise TaskError('Actual attendance cannot be in the future')
        if arrived and departed and departed < arrived:
            raise TaskError('Actual departure cannot precede arrival')
        if expected and arrived and expected < arrived:
            raise TaskError('Expected departure cannot precede actual arrival')
        if after == {k: v for k, v in before.items() if k != 'event_id'}:
            raise TaskError('No attendance change to save')
        db.add(ReservationEvent(booking_id=booking_id, token_digest=hashlib.sha256(submitted_token.encode()).hexdigest(),
            actor=actor, action='stay_record', note=f'{action}: {note.strip()}',
            before_state=json.dumps(before, sort_keys=True), after_state=json.dumps(after, sort_keys=True)))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise TaskError('Request already recorded; reload before submitting again') from None
    except Exception:
        db.rollback()
        raise


def context(db, booking, actor):
    current = state(db, booking.id)
    following = db.query(Booking).filter(Booking.property_id == booking.property_id,
        Booking.id != booking.id, Booking.is_block == False, Booking.status == BookingStatus.confirmed,
        Booking.check_in >= booking.check_out.replace(hour=0, minute=0, second=0, microsecond=0)).order_by(Booking.check_in, Booking.id).first()
    warnings = []
    if following and following.check_in.date() == booking.check_out.date():
        warnings.append(f'Same-day handover to reservation #{following.id}; arrival time and cleaning readiness need confirmation.')
    if current['expected_departure']:
        expected = datetime.fromisoformat(current['expected_departure'])
        if following and expected.date() >= following.check_in.date():
            warnings.append('Expected departure overlaps the next arrival day; coordinate manually. Exact next arrival time is not recorded here.')
    if booking.status == BookingStatus.cancelled and current['status'] == 'in_house':
        warnings.append('Cancelled reservation still has an in-house record; review and correct with a reason.')
    return dict(stay=current, stay_token=token(db, actor, booking), stay_warnings=warnings)
