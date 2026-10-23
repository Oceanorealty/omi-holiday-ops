"""Property-local calendar-day booking windows for direct bookings only."""
import re
from datetime import datetime,timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from sqlalchemy.exc import IntegrityError
from app.models import BookingWindow,Property
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError,form_token,verify_token


def window_token(db,actor,property_id):
    row=db.get(BookingWindow,property_id)
    return form_token(actor,SimpleNamespace(id=property_id,version=row.version if row else 0),purpose='booking-window')


def save_window(db,*,actor,property_id,token,minimum_notice_days,maximum_advance_days,same_day_cutoff,note):
    if not 0<=minimum_notice_days<=1095 or (maximum_advance_days is not None and not minimum_notice_days<=maximum_advance_days<=1095):raise TaskError('Use 0–1095 calendar days; maximum must be at least minimum')
    cutoff=same_day_cutoff.strip()
    if cutoff and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',cutoff):raise TaskError('Use a cutoff time in HH:MM format')
    try:
        lock_stock(db);db.expire_all();prop=db.get(Property,property_id)
        if not prop:raise TaskError('Property not found')
        try:ZoneInfo(prop.time_zone)
        except (ZoneInfoNotFoundError,ValueError,TypeError):raise TaskError('Review the property timezone before setting booking windows') from None
        expected=verify_token(token,actor,property_id,purpose='booking-window');row=db.get(BookingWindow,property_id)
        if expected!=(row.version if row else 0):raise TaskError('Booking window changed; reload first')
        before=snapshot(row)
        if not row:row=BookingWindow(property_id=property_id);db.add(row)
        row.version=expected+1;row.minimum_notice_days=minimum_notice_days;row.maximum_advance_days=maximum_advance_days;row.same_day_cutoff=cutoff
        record_command(db,token,'booking-window',actor,note,{'property_id':property_id,'before':before,'after':snapshot(row)})
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('Booking window already saved; reload first') from None
    except Exception:db.rollback();raise


def snapshot(row):
    return {k:getattr(row,k) for k in ('version','minimum_notice_days','maximum_advance_days','same_day_cutoff')} if row else None


def window_context(db,property_id,now=None):
    row=db.get(BookingWindow,property_id)
    if not row:return None
    prop=db.get(Property,property_id)
    try:
        if not prop:raise ValueError()
        current=now or datetime.now(timezone.utc)
        if current.tzinfo is None:raise ValueError()
        return row,current.astimezone(ZoneInfo(prop.time_zone))
    except (ZoneInfoNotFoundError,ValueError,TypeError):return 'invalid'


def window_reason(db,property_id,arrival,*,now=None,context=None):
    ctx=window_context(db,property_id,now) if context is None else context
    if ctx is None:return None
    if ctx=='invalid':return 'booking_timezone_needs_review'
    row,current=ctx;days=(arrival-current.date()).days
    if days<row.minimum_notice_days:return 'minimum_notice'
    if row.maximum_advance_days is not None and days>row.maximum_advance_days:return 'maximum_advance'
    if days==0 and row.same_day_cutoff and current.strftime('%H:%M')>=row.same_day_cutoff:return 'same_day_cutoff'
    return None
