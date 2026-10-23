"""Date overrides and direct-sale restrictions. No channel rate writeback."""
import hashlib
import json
from datetime import date, timedelta
from types import SimpleNamespace
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from app.models import InventoryDay, InventoryEvent, Property
from app.operations.reservations import money, stay_dates
from app.operations.tasks import TaskError, form_token, verify_token

FIELDS = ('night', 'nightly_rate', 'minimum_stay', 'maximum_stay', 'closed_arrival', 'closed_departure', 'stop_sell')


def inventory_rows(db, property_id, start, end):
    stay_dates(start, end)
    return db.query(InventoryDay).filter(InventoryDay.property_id == property_id,
        InventoryDay.night >= start, InventoryDay.night < end).order_by(InventoryDay.night).all()


def state(rows):
    return [{key: (str(getattr(row, key)) if key == 'nightly_rate' and row.nightly_rate is not None else getattr(row, key))
             for key in FIELDS} for row in rows]


def version(rows):
    return int(hashlib.sha256(json.dumps(state(rows), sort_keys=True).encode()).hexdigest()[:15], 16)


def inventory_token(db, actor, property_id, start, end):
    return form_token(actor, SimpleNamespace(id=property_id, version=version(inventory_rows(db, property_id, start, end))),
                      purpose=f'inventory:{start}:{end}')


def save_inventory(db, *, actor, property_id, start, end, token, nightly_rate='', minimum_stay=1,
                   maximum_stay=None, closed_arrival=False, closed_departure=False, stop_sell=False,
                   note='', reset=False):
    expected = verify_token(token, actor, property_id, purpose=f'inventory:{start}:{end}')
    lower, upper = stay_dates(start, end)
    note = note.strip()
    if not 10 <= len(note) <= 1000:
        raise TaskError('Record a change note of 10–1000 characters')
    rate = money(nightly_rate)
    if rate is not None and rate <= 0:
        raise TaskError('Nightly rates must be greater than zero')
    if not 1 <= minimum_stay <= 366 or (maximum_stay is not None and not minimum_stay <= maximum_stay <= 366):
        raise TaskError('Stay limits must be 1–366 nights, with maximum at least minimum')
    try:
        locked = db.execute(update(Property).where(Property.id == property_id).values(name=Property.name))
        if locked.rowcount != 1:
            raise TaskError('Property not found')
        db.expire_all()
        rows = inventory_rows(db, property_id, start, end)
        if version(rows) != expected:
            raise TaskError('Rates or restrictions changed; reload before saving')
        before = state(rows)
        by_night = {r.night: r for r in rows}
        if reset:
            for row in rows:
                db.delete(row)
        else:
            day = lower.date()
            while day < upper.date():
                key = day.isoformat()
                row = by_night.get(key)
                if row is None:
                    row = InventoryDay(property_id=property_id, night=key)
                    db.add(row)
                row.nightly_rate, row.minimum_stay, row.maximum_stay = rate, minimum_stay, maximum_stay
                row.closed_arrival, row.closed_departure, row.stop_sell = closed_arrival, closed_departure, stop_sell
                day += timedelta(days=1)
        db.flush()
        after = state(inventory_rows(db, property_id, start, end))
        db.add(InventoryEvent(property_id=property_id, actor=actor, note=note,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            changes=json.dumps({'start': start, 'end_excluded': end, 'before': before, 'after': after}, sort_keys=True)))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise TaskError('Request already saved; reload before submitting again') from None
    except Exception:
        db.rollback()
        raise


def restriction_reason(db, property_id, start: date, end: date, *, check_window=True):
    if check_window:
        from app.operations.booking_windows import window_reason
        reason=window_reason(db,property_id,start)
        if reason:return reason
    rows = db.query(InventoryDay).filter(InventoryDay.property_id == property_id,
        InventoryDay.night >= start.isoformat(), InventoryDay.night <= end.isoformat()).all()
    nights = (end-start).days
    for row in rows:
        if row.night == start.isoformat() and row.closed_arrival:
            return 'arrival_closed'
        if row.night == end.isoformat() and row.closed_departure:
            return 'departure_closed'
        if row.night < end.isoformat():
            if row.stop_sell:
                return 'stop_sell'
            if nights < row.minimum_stay:
                return 'minimum_stay'
            if row.maximum_stay is not None and nights > row.maximum_stay:
                return 'maximum_stay'
    return None
