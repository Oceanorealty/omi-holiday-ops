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
