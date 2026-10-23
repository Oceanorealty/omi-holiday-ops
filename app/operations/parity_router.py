"""Administrator workflows added by the Hostaway parity programme."""
import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from app.db import get_db
from app.models import (Booking, BookingStatus, Cleaner, IcalFeed, InventoryEvent,
                        Property, ReservationEvent, StaffUser)
from app.operations.calendar import calendar_data
from app.operations.inventory import inventory_rows, inventory_token, save_inventory
from app.operations.reservations import reservation_token, save_reservation
from app.operations.router import require_admin, templates
from app.operations.tasks import TaskError
from app.operations.pricing_limits import limits_token, save_limits
from app.models import PricingLimit

router = APIRouter()


def same_origin(request):
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'Submit from this site')


def selected_property(db, property_id):
    prop = db.get(Property, property_id)
    if not prop:
        raise HTTPException(404, 'Property not found')
    return prop


def optional_property(value):
    try:
        result = int(value) if value else None
        if result is not None and result < 1:
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise HTTPException(422, 'Invalid property') from None


@router.get('/operations/setup')
def setup(request: Request, db=Depends(get_db), admin=Depends(require_admin)):
    properties = db.query(Property).order_by(Property.name).all()
    feeds = db.query(IcalFeed).filter_by(active=True).all()
    staff = db.query(StaffUser).filter_by(active=True).count()
    cleaners = db.query(Cleaner).filter_by(active=True).count()
    rows = []
    for prop in properties:
        sources = [f for f in feeds if f.property_id == prop.id]
        rows.append({'property': prop, 'feed_count': len(sources),
            'synced': sum(bool(f.last_synced_at and not f.last_sync_error) for f in sources),
            'errors': sum(bool(f.last_sync_error) for f in sources),
            'commission_ready': prop.commission_pct is not None,
            'guest_info_ready': bool(prop.check_in_instructions and prop.house_rules)})
    return templates.TemplateResponse(request=request, name='setup.html', context={
        'rows': rows, 'staff_count': staff, 'cleaner_count': cleaners})


@router.get('/operations/reservations')
def reservation_list(request: Request, property_id: str = '', status: str = 'confirmed',
                     db=Depends(get_db), admin=Depends(require_admin)):
    property_id = optional_property(property_id)
    if status not in ('confirmed', 'cancelled', 'all'):
        raise HTTPException(422, 'Unknown status')
    query = db.query(Booking).filter(Booking.is_block.is_(False))
    if property_id is not None:
        selected_property(db, property_id)
        query = query.filter_by(property_id=property_id)
    if status != 'all':
        query = query.filter(Booking.status == BookingStatus(status))
    rows = query.order_by(Booking.check_in.desc(), Booking.id.desc()).limit(201).all()
    return templates.TemplateResponse(request=request, name='reservations.html', context={
        'rows': rows[:200], 'truncated': len(rows) > 200, 'status': status, 'property_id': property_id,
        'properties': db.query(Property).order_by(Property.name).all(), 'token': reservation_token(admin)})


@router.get('/operations/reservations/{entity_id}')
def reservation_detail(entity_id: int, request: Request, db=Depends(get_db), admin=Depends(require_admin)):
    row = db.get(Booking, entity_id)
    if not row or row.is_block:
        raise HTTPException(404, 'Reservation not found')
    editable = row.platform.value == 'direct' and row.source_feed_id is None
    return templates.TemplateResponse(request=request, name='reservation_detail.html', context={
        'booking': row, 'editable': editable, 'token': reservation_token(admin, row),
        'events': db.query(ReservationEvent).filter_by(booking_id=entity_id).order_by(ReservationEvent.id.desc()).limit(100).all()})


@router.post('/operations/reservations/{entity_id}')
def reservation_save(entity_id: int, request: Request, token: str = Form(...), property_id: int = Form(...),
                     check_in: str = Form(''), check_out: str = Form(''), amount: str = Form(''),
                     guest_name: str = Form(''), guest_email: str = Form(''), note: str = Form(...),
                     action: str = Form('save'), db=Depends(get_db), admin=Depends(require_admin)):
    same_origin(request)
    if entity_id < 0:
        raise HTTPException(422, 'Invalid reservation')
    try:
        saved = save_reservation(db, actor=admin, entity_id=entity_id, token=token, property_id=property_id,
            check_in=check_in, check_out=check_out, amount=amount, guest_name=guest_name,
            guest_email=guest_email, note=note, action=action)
    except TaskError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse(f'/operations/reservations/{saved}', status_code=303)


@router.get('/operations/inventory')
def inventory_page(request: Request, property_id: str = '', start: date = None, end: date = None,
                   db=Depends(get_db), admin=Depends(require_admin)):
    property_id = optional_property(property_id)
    properties = db.query(Property).order_by(Property.name).all()
    prop = selected_property(db, property_id) if property_id is not None else (properties[0] if properties else None)
    start = start or datetime.now(ZoneInfo('Australia/Brisbane')).date()
    end = end or start + timedelta(days=14)
    if not 1 <= (end-start).days <= 62:
        raise HTTPException(422, 'Choose 1–62 nights')
    rows = inventory_rows(db, prop.id, start.isoformat(), end.isoformat()) if prop else []
    data = {'properties': properties, 'property': prop, 'start': start, 'end': end, 'rows': rows,
        'limits':db.get(PricingLimit,prop.id) if prop else None,
        'limits_token':limits_token(db,admin,prop.id) if prop else '',
        'token': inventory_token(db, admin, prop.id, start.isoformat(), end.isoformat()) if prop else '',
        'events': db.query(InventoryEvent).filter_by(property_id=prop.id).order_by(InventoryEvent.id.desc()).limit(20).all() if prop else []}
    return templates.TemplateResponse(request=request, name='inventory.html', context=data)


@router.post('/operations/pricing-limits')
def pricing_limits_save(request: Request,property_id: int=Form(...),token: str=Form(...),minimum_rate: str=Form(''),
                        maximum_rate: str=Form(''),note: str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:
        save_limits(db,actor=admin,property_id=property_id,token=token,minimum_rate=minimum_rate,maximum_rate=maximum_rate,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/inventory?property_id={property_id}',status_code=303)


@router.post('/operations/inventory')
def inventory_save(request: Request, token: str = Form(...), property_id: int = Form(...),
                   start: str = Form(...), end: str = Form(...), nightly_rate: str = Form(''),
                   minimum_stay: int = Form(1), maximum_stay: str = Form(''),
                   closed_arrival: str = Form(''), closed_departure: str = Form(''), stop_sell: str = Form(''),
                   action: str = Form('save'), note: str = Form(...), db=Depends(get_db), admin=Depends(require_admin)):
    same_origin(request)
    if action not in ('save', 'reset') or any(v not in ('', '1') for v in (closed_arrival, closed_departure, stop_sell)):
        raise HTTPException(422, 'Invalid inventory action')
    try:
        maximum = int(maximum_stay) if maximum_stay else None
        save_inventory(db, actor=admin, property_id=property_id, start=start, end=end, token=token,
            nightly_rate=nightly_rate, minimum_stay=minimum_stay, maximum_stay=maximum,
            closed_arrival=closed_arrival == '1', closed_departure=closed_departure == '1',
            stop_sell=stop_sell == '1', note=note, reset=action == 'reset')
    except ValueError as exc:
        raise HTTPException(409, str(exc) if isinstance(exc, TaskError) else 'Invalid maximum stay') from None
    return RedirectResponse(f'/operations/inventory?property_id={property_id}&start={start}&end={end}', status_code=303)


def report_data(db, start, end, property_id=None):
    data = calendar_data(db, start, end, property_id)
    for row in data['rows']:
        bookings = db.query(Booking).filter(Booking.property_id == row['property_id'],
            Booking.is_block.is_(False), Booking.status == BookingStatus.confirmed,
            Booking.check_in < datetime.combine(end, datetime.min.time()),
            Booking.check_out > datetime.combine(start, datetime.min.time())).all()
        known, missing, uncertain = Decimal(0), 0, 0
        channels = {}
        for booking in bookings:
            if booking.check_out.date() <= booking.check_in.date():
                continue
            overlap = (min(end, booking.check_out.date()) - max(start, booking.check_in.date())).days
            nights = (booking.check_out.date() - booking.check_in.date()).days
            if overlap <= 0:
                continue
            channels[booking.platform.value] = channels.get(booking.platform.value, 0) + overlap
            if booking.amount is None:
                missing += 1
            else:
                known += booking.amount * Decimal(overlap) / Decimal(nights)
            uncertain += bool(booking.source_missing_since)
        row.update(known_booking_value=str(known.quantize(Decimal('.01'))), missing_amounts=missing,
                   source_reviews=uncertain, channel_nights=channels,
                   complete=not (missing or uncertain or row['conflict_days'] or row['invalid_booking_ids'] or row['stopped_reserved_nights'] or row['shared_inventory']))
    return data


def report_period(start, end):
    start = start or datetime.now(ZoneInfo('Australia/Brisbane')).date().replace(day=1)
    if end is None:
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


@router.get('/operations/reports')
def reports(request: Request, start: date = None, end: date = None, property_id: str = '',
            db=Depends(get_db), admin=Depends(require_admin)):
    property_id = optional_property(property_id)
    start, end = report_period(start, end)
    try:
        data = report_data(db, start, end, property_id)
    except (ValueError, LookupError) as exc:
        raise HTTPException(422, str(exc)) from None
    data.update(properties=db.query(Property).order_by(Property.name).all(), selected_property_id=property_id)
    return templates.TemplateResponse(request=request, name='reports.html', context=data)


@router.get('/operations/reports.csv')
def report_csv(start: date, end: date, property_id: int = None, db=Depends(get_db), admin=Depends(require_admin)):
    try:
        data = report_data(db, start, end, property_id)
    except (ValueError, LookupError) as exc:
        raise HTTPException(422, str(exc)) from None
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['property_id', 'property', 'start', 'end_excluded', 'reserved_nights', 'blocked_nights',
                     'sellable_nights', 'reserved_night_pct', 'known_prorated_booking_value_AUD_assumed',
                     'missing_amounts', 'source_reviews', 'conflict_days', 'stop_sell_nights', 'data_complete'])
    for row in data['rows']:
        name = row['name']
        if name.lstrip().startswith(('=', '+', '-', '@')) or name.startswith(('\t', '\r', '\n')):
            name = "'" + name
        writer.writerow([row['property_id'], name, data['start'], data['end'], row['reserved_nights'],
            row['blocked_nights'], row['sellable_nights'], row['reserved_night_pct'], row['known_booking_value'],
            row['missing_amounts'], row['source_reviews'], row['conflict_days'], row['stop_sell_nights'], row['complete']])
    return Response('\ufeff' + output.getvalue(), media_type='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="ops-property-report.csv"'})
