"""Read-only local calendar. Counts reserved nights, never claims actual occupancy."""
from datetime import date, datetime, timedelta
from sqlalchemy import or_
from app.models import Booking, BookingStatus, InventoryDay, Property, CalendarNote
from app.operations.stock import stock_context,units_from_context,context_conflicts,inventory_interval


def calendar_data(db, start: date, end: date, property_id=None):
    count = (end - start).days
    if not 1 <= count <= 62:
        raise ValueError('Choose an end date after the start, within 62 nights.')
    query = db.query(Property)
    if property_id is not None:
        query = query.filter(Property.id == property_id)
    properties = query.order_by(Property.name, Property.id).limit(101).all()
    if len(properties) > 100:
        raise ValueError('Select one property; this view supports up to 100 properties.')
    if property_id is not None and not properties:
        raise LookupError('Property not found')
    ids = [p.id for p in properties]
    stops = {(r.property_id, r.night) for r in db.query(InventoryDay).filter(
        InventoryDay.property_id.in_(ids), InventoryDay.night >= start.isoformat(),
        InventoryDay.night < end.isoformat(), InventoryDay.stop_sell.is_(True)).all()}
    notes = {(r.property_id, r.night): r.body for r in db.query(CalendarNote).filter(
        CalendarNote.property_id.in_(ids), CalendarNote.night >= start.isoformat(),
        CalendarNote.night < end.isoformat()).all()}
    lower, upper = datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time())
    bookings = db.query(Booking).filter(Booking.property_id.in_(ids), Booking.status == BookingStatus.confirmed,
        Booking.check_in < upper, or_(Booking.check_out >= lower, Booking.check_out <= Booking.check_in)
        ).order_by(Booking.id).limit(5001).all()
    if len(bookings) > 5000:
        raise ValueError('Too many records; select one property or a shorter period.')
    days = [start + timedelta(days=i) for i in range(count)]
    grouped = {p.id: [] for p in properties}
    for booking in bookings:
        grouped[booking.property_id].append(booking)
    rows = []
    for prop in properties:
        records = grouped[prop.id]
        stock=stock_context(db,prop.id,lower,upper)
        invalid = [b.id for b in stock['records'] if b.check_out <= b.check_in]
        cells = []
        for day in days:
            # Checkout does not consume the departing night. Same-day stays occupy
            # no overnight inventory; their exact-time conflicts remain flagged below.
            overnight = [b for b in records if b.check_in.date() <= day < b.check_out.date()]
            in_day = [b for b in records if b.check_in < datetime.combine(day + timedelta(days=1), datetime.min.time())
                      and b.check_out > datetime.combine(day, datetime.min.time()) and b.check_out > b.check_in]
            day_start=datetime.combine(day,datetime.min.time())
            day_end=datetime.combine(day+timedelta(days=1),datetime.min.time())
            conflict_ids=context_conflicts(stock,day_start,day_end)
            stays = [b for b in overnight if not b.is_block]
            blocks = [b for b in overnight if b.is_block]
            cells.append({'note_end':(day+timedelta(days=1)).isoformat(),'internal_note':notes.get((prop.id,day.isoformat()),''),'date': day.isoformat(), 'stop_sell': (prop.id, day.isoformat()) in stops,
                'available_units':0 if (prop.id,day.isoformat()) in stops else units_from_context(stock,day_start,day_end),
                'checkout_holds':sum(h.check_in<day_end and h.check_out>day_start for h in stock.get('holds',[])),
                'booking_ids': [b.id for b in stays],
                'block_ids': [b.id for b in blocks], 'conflict_ids': sorted(conflict_ids),
                'source_review_ids': [b.id for b in in_day if b.source_missing_since],
                'arrivals': [b.id for b in in_day if not b.is_block and b.check_in.date() == day],
                'departures': [b.id for b in records if not b.is_block and b.check_out.date() == day and b.check_out > b.check_in]})
        reserved = sum(bool(c['booking_ids']) for c in cells)
        blocked = sum(bool(c['block_ids']) for c in cells)
        sellable = sum(not c['block_ids'] and not c['stop_sell'] for c in cells)
        conflicts = sum(bool(c['conflict_ids']) for c in cells)
        stopped_reservations = sum(bool(c['booking_ids']) and c['stop_sell'] for c in cells)
        ambiguous = bool(invalid or conflicts or stopped_reservations or stock['shared'])
        rows.append({'property_id': prop.id, 'name': prop.name, 'time_zone': prop.time_zone,
            'parent_property_id': prop.parent_property_id, 'days': cells, 'calendar_nights': count,
            'inventory_capacity':stock['capacity'],'shared_inventory':stock['shared'],
            'reserved_nights': reserved, 'blocked_nights': blocked, 'sellable_nights': sellable,
            'stop_sell_nights': sum(c['stop_sell'] for c in cells), 'stopped_reserved_nights': stopped_reservations,
            'conflict_days': conflicts, 'invalid_booking_ids': invalid,
            'reserved_night_pct': round(reserved / sellable * 100, 1) if sellable and not ambiguous else None,
            'evidence': [f'booking:{b.id}' for b in records]})
    return {'start': start.isoformat(), 'end': end.isoformat(), 'dates': [d.isoformat() for d in days], 'rows': rows,
        'limitations': 'Local confirmed reservations and active checkout holds; end date excluded. Dates use stored property-local booking times. '
        'Checkout holds reduce available units temporarily but are not reservations or occupancy. Blocks and stop-sell restrictions reduce sellable nights. Conflicts, invalid dates or reservations on stopped nights suppress percentages. Missing-source reservations remain reserved until reviewed. '
        'Shared inventory is applied only through explicit resource bindings; parent-child names alone do not link inventory. '
        'Reserved-night percentages are suppressed for resource-mapped listings. No cross-property totals or actual guest attendance inferred.'}
