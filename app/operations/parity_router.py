"""Administrator workflows added by the Hostaway parity programme."""
import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from sqlalchemy import or_, func
from urllib.parse import urlencode
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from app.db import get_db
from app.models import (Booking, BookingStatus, Cleaner, Guest, PlatformName, IcalFeed, InventoryEvent,
                        Property, PropertyPhoto, ReservationEvent, StaffUser, CleaningTask, BookingAccount)
from app.operations.calendar import calendar_data
from app.operations.inventory import inventory_rows, inventory_token, save_inventory
from app.operations.reservations import reservation_token, save_reservation, stay_dates
from app.operations import stay_operations
from app.operations.router import require_admin, templates
from app.operations.tasks import TaskError
from app.operations.pricing_limits import limits_token, save_limits
from app.operations.billing import entries as account_entries, totals as account_totals
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


def property_readiness(prop, photo_count, cleaner_names):
    """Presence/validity checks only, never certify source accuracy or activate services."""
    def present(value):
        return bool(value and str(value).strip())

    def number(value, minimum, maximum=None):
        try:
            value = Decimal(str(value))
            return value.is_finite() and value >= minimum and (maximum is None or value <= maximum)
        except (InvalidOperation, ValueError, TypeError):
            return False

    try:
        ZoneInfo(prop.time_zone)
        zone_valid = True
    except (ValueError, TypeError, KeyError):
        zone_valid = False
    checks = [
        ('Address / unit reference', present(prop.address)),
        ('Description', present(prop.description)),
        ('Bedroom count', number(prop.bedrooms, 0)),
        ('Bathroom count', number(prop.bathrooms, 1)),
        ('Guest capacity', number(prop.max_guests, 1)),
        ('Cover photo', present(prop.photo_url)),
        ('Active photo gallery', photo_count > 0),
        ('Property timezone', zone_valid),
        ('Commission from owner agreement', number(prop.commission_pct, 0, 100)),
        ('Base nightly rate for direct quotes', number(prop.base_nightly_rate, Decimal('0.01'))),
        ('Arrival instructions', present(prop.check_in_instructions)),
        ('House rules', present(prop.house_rules)),
        ('Wi-Fi information', present(prop.wifi_info)),
        ('Active default cleaner', prop.default_cleaner in cleaner_names),
    ]
    return {'missing': [label for label, valid in checks if not valid],
            'recorded': sum(valid for _, valid in checks), 'total': len(checks),
            'photo_count': photo_count, 'zone_valid': zone_valid,
            'commission_ready': checks[8][1],
            'guest_info_ready': all(present(value) for value in
                (prop.check_in_instructions, prop.house_rules, prop.wifi_info))}


@router.get('/operations/setup')
def setup(request: Request, db=Depends(get_db), admin=Depends(require_admin)):
    properties = db.query(Property).order_by(Property.name).all()
    feeds = db.query(IcalFeed).filter_by(active=True).all()
    staff = db.query(StaffUser).filter_by(active=True).count()
    active_cleaners = db.query(Cleaner).filter_by(active=True).all()
    cleaner_names = {cleaner.name for cleaner in active_cleaners}
    photo_counts = dict(db.query(PropertyPhoto.property_id, func.count(PropertyPhoto.id))
        .filter(PropertyPhoto.active.is_(True)).group_by(PropertyPhoto.property_id).all())
    rows = []
    for prop in properties:
        sources = [f for f in feeds if f.property_id == prop.id]
        rows.append({'property': prop, 'feed_count': len(sources),
            'synced': sum(bool(f.last_synced_at and not f.last_sync_error) for f in sources),
            'errors': sum(bool(f.last_sync_error) for f in sources),
            **property_readiness(prop, photo_counts.get(prop.id, 0), cleaner_names)})
    return templates.TemplateResponse(request=request, name='setup.html', context={
        'rows': rows, 'staff_count': staff, 'cleaner_count': len(active_cleaners)})


@router.get('/operations/reservations')
def reservation_list(request: Request, property_id: str = '', status: str = 'confirmed',
                     q: str = '', channel: str = '', start: str = '', end: str = '', page: int = 1,
                     draft_check_in: str = '', draft_check_out: str = '',
                     db=Depends(get_db), admin=Depends(require_admin)):
    property_id = optional_property(property_id)
    if status not in ('confirmed', 'cancelled', 'all'):
        raise HTTPException(422, 'Unknown status')
    q = q.strip()
    if len(q) > 120 or not 1 <= page <= 500:
        raise HTTPException(422, 'Use a search up to 120 characters and a page between 1 and 500')
    if channel and channel not in {p.value for p in PlatformName}:
        raise HTTPException(422, 'Unknown booking source')
    draft_booking = None
    if draft_check_in or draft_check_out:
        if property_id is None:
            raise HTTPException(422, 'Select a property before prefilling reservation dates')
        try:
            arrival, departure = stay_dates(draft_check_in, draft_check_out)
        except TaskError as exc:
            raise HTTPException(422, str(exc)) from None
        draft_booking = SimpleNamespace(check_in=arrival, check_out=departure, amount=None, guest=None)
    try:
        lower = date.fromisoformat(start) if start else None
        upper = date.fromisoformat(end) if end else None
        if (lower and lower.isoformat() != start) or (upper and upper.isoformat() != end) or (lower and upper and lower >= upper):
            raise ValueError()
    except ValueError:
        raise HTTPException(422, 'Choose valid dates with the end after the first night') from None
    query = db.query(Booking).filter(Booking.is_block.is_(False))
    if property_id is not None:
        selected_property(db, property_id)
        query = query.filter_by(property_id=property_id)
    if status != 'all':
        query = query.filter(Booking.status == BookingStatus(status))
    if channel:
        query = query.filter(Booking.platform == PlatformName(channel))
    if lower:
        query = query.filter(Booking.check_out > datetime.combine(lower, datetime.min.time()))
    if upper:
        query = query.filter(Booking.check_in < datetime.combine(upper, datetime.min.time()))
    if lower or upper:
        query = query.filter(Booking.check_out > Booking.check_in)
    if q:
        pattern = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        matches = [Booking.uid.ilike(pattern, escape='\\'),
                   Booking.guest.has(or_(Guest.name.ilike(pattern, escape='\\'), Guest.email.ilike(pattern, escape='\\')))]
        if q.isascii() and q.isdigit() and len(q) <= 18:
            matches.append(Booking.id == int(q))
        query = query.filter(or_(*matches))
    rows = query.order_by(Booking.check_in.desc(), Booking.id.desc()).offset((page - 1) * 200).limit(201).all()
    filters = dict(property_id=property_id or '', status=status, q=q, channel=channel, start=start, end=end)
    return templates.TemplateResponse(request=request, name='reservations.html', context={
        'rows': rows[:200], 'truncated': len(rows) > 200, 'status': status, 'property_id': property_id,
        'draft_booking': draft_booking,
        **filters, 'page': page, 'channels': [p.value for p in PlatformName],
        'previous_url': '/operations/reservations?' + urlencode({**filters, 'page': page - 1}) if page > 1 else None,
        'next_url': '/operations/reservations?' + urlencode({**filters, 'page': page + 1}) if len(rows) > 200 and page < 500 else None,
        'properties': db.query(Property).order_by(Property.name).all(), 'token': reservation_token(admin)})


@router.get('/operations/reservations/{entity_id}')
def reservation_detail(entity_id: int, request: Request, db=Depends(get_db), admin=Depends(require_admin)):
    row = db.get(Booking, entity_id)
    if not row or row.is_block:
        raise HTTPException(404, 'Reservation not found')
    editable = row.platform.value == 'direct' and row.source_feed_id is None
    first, last = row.check_in.date(), row.check_out.date()
    nights = (last - first).days
    calendar_end = min(max(last, first + timedelta(days=1)), first + timedelta(days=62))
    account = db.get(BookingAccount, entity_id)
    account_summary = None
    account_needs_review = False
    if account:
        try:
            account_summary = account_totals(account_entries(db, entity_id))
        except TaskError:
            account_needs_review = True
    return templates.TemplateResponse(request=request, name='reservation_detail.html', context={
        **stay_operations.context(db, row, admin),
        'booking': row, 'editable': editable, 'token': reservation_token(admin, row),
        'stay_nights': nights if nights > 0 else None,
        'stay_calendar_url': '/operations/calendar?' + urlencode(dict(property_id=row.property_id,
            start=first.isoformat(), end=calendar_end.isoformat())),
        'calendar_clipped': nights > 62,
        'cleaning_task': db.query(CleaningTask).filter_by(booking_id=entity_id).first(),
        'account': account, 'account_summary': account_summary, 'account_needs_review': account_needs_review,
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


@router.post('/operations/reservations/{entity_id}/attendance')
def stay_record(entity_id: int, request: Request, token: str = Form(...), action: str = Form(...),
                note: str = Form(...), arrived_at: str = Form(''), departed_at: str = Form(''),
                expected_departure: str = Form(''), target_status: str = Form(''),
                db=Depends(get_db), admin=Depends(require_admin)):
    same_origin(request)
    try:
        stay_operations.record(db, actor=admin, booking_id=entity_id, submitted_token=token,
            action=action, note=note, arrived_at=arrived_at, departed_at=departed_at,
            expected_departure=expected_departure, target_status=target_status)
    except TaskError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse('/operations/reservations/' + str(entity_id) + '#attendance', status_code=303)
