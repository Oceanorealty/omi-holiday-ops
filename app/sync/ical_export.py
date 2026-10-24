"""
Outbound iCal export: publishes a property's confirmed bookings and manual
blocks as one combined .ics feed. Pasting this URL into another platform's
"import calendar" field closes the loop on the inbound-only sync in
ical_sync.py — a booking or block recorded here blocks availability
everywhere, not just on the platform it came from.

No guest details go into the feed (just "Reserved"/"Blocked" + dates) since
this is fetched by other platforms, not just staff.
"""

from icalendar import Calendar, Event

from app.models import Booking, BookingStatus


def build_ics(bookings: list[Booking]) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Omi Holiday Ops//omiholiday.com//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Omi Holiday availability")

    for booking in bookings:
        if booking.status != BookingStatus.confirmed:
            continue
        event = Event()
        event.add("uid", f"omi-booking-{booking.id}@omiholiday.com")
        event.add("dtstart", booking.check_in.date())
        event.add("dtend", booking.check_out.date())
        event.add("summary", "Blocked" if booking.is_block else "Reserved")
        cal.add_component(event)

    return cal.to_ical()


def build_resource_ics(db,property_id,*,start=None):
    """Rolling two-year, capacity-aware unavailable-night feed; never contains guest data."""
    import hashlib
    from datetime import datetime,timedelta
    from zoneinfo import ZoneInfo
    from app.models import InventoryDay,Property
    from app.operations.stock import stock_context,units_from_context
    prop=db.get(Property,property_id)
    if not prop:raise ValueError('Property not found')
    start=start or datetime.now(ZoneInfo(prop.time_zone)).date()
    end=start+timedelta(days=730)
    context=stock_context(db,property_id,datetime.combine(start,datetime.min.time()),datetime.combine(end,datetime.min.time()),include_checkout_holds=False)
    stopped={r.night for r in db.query(InventoryDay).filter(InventoryDay.property_id==property_id,
        InventoryDay.night>=start.isoformat(),InventoryDay.night<end.isoformat(),InventoryDay.stop_sell.is_(True)).all()}
    cal=Calendar.from_ical(build_ics([]))
    run_start=None
    def append_interval(first,last):
        event=Event()
        digest=hashlib.sha256(f'{property_id}:{first}:{last}'.encode()).hexdigest()[:32]
        event.add('uid',f'omi-stock-{digest}@omiholiday.com')
        event.add('dtstart',first);event.add('dtend',last);event.add('summary','Unavailable')
        cal.add_component(event)
    for offset in range(731):
        day=start+timedelta(days=offset)
        unavailable=day<end and (day.isoformat() in stopped or units_from_context(context,
            datetime.combine(day,datetime.min.time()),datetime.combine(day+timedelta(days=1),datetime.min.time()))<1)
        if unavailable and run_start is None:run_start=day
        elif not unavailable and run_start is not None:
            append_interval(run_start,day);run_start=None
    return cal.to_ical()
