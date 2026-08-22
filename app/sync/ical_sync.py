"""
Booking sync engine: fetches each property's iCal feeds, upserts bookings by
UID (idempotent — safe to re-run), and flags overlapping bookings on the same
property across different platforms as conflicts.
"""

from datetime import datetime, time

import requests
from icalendar import Calendar
from sqlalchemy.orm import Session

from app.models import Booking, BookingStatus, IcalFeed


def _to_datetime(value) -> datetime:
    """iCal DTSTART/DTEND can be a date or a datetime; normalize to datetime."""
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.min)


def fetch_and_parse(url: str) -> list[dict]:
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)

    events = []
    for component in cal.walk("VEVENT"):
        uid = str(component.get("uid"))
        dtstart = component.get("dtstart")
        dtend = component.get("dtend")
        if not uid or dtstart is None or dtend is None:
            continue
        events.append(
            {
                "uid": uid,
                "summary": str(component.get("summary") or ""),
                "check_in": _to_datetime(dtstart.dt),
                "check_out": _to_datetime(dtend.dt),
            }
        )
    return events


def sync_feed(db: Session, feed: IcalFeed) -> dict:
    """Sync a single iCal feed. Returns a summary dict for reporting."""
    result = {"feed_id": feed.id, "added": 0, "updated": 0, "error": None}
    try:
        events = fetch_and_parse(feed.url)
    except Exception as exc:  # noqa: BLE001 — surface any fetch/parse failure to the UI
        feed.last_sync_error = str(exc)
        feed.last_synced_at = datetime.utcnow()
        db.commit()
        result["error"] = str(exc)
        return result

    seen_uids = set()
    for event in events:
        seen_uids.add(event["uid"])
        existing = (
            db.query(Booking)
            .filter_by(property_id=feed.property_id, platform=feed.platform, uid=event["uid"])
            .first()
        )
        if existing:
            existing.summary = event["summary"]
            existing.check_in = event["check_in"]
            existing.check_out = event["check_out"]
            existing.status = BookingStatus.confirmed
            result["updated"] += 1
        else:
            db.add(
                Booking(
                    property_id=feed.property_id,
                    platform=feed.platform,
                    uid=event["uid"],
                    summary=event["summary"],
                    check_in=event["check_in"],
                    check_out=event["check_out"],
                    status=BookingStatus.confirmed,
                )
            )
            result["added"] += 1

    # Anything previously synced from this feed but no longer present upstream
    # was cancelled or removed — mark it rather than deleting, so history is kept.
    # Excludes manual blocks: they share a platform value with real feeds
    # (see main.py's create_block) but were never part of any feed's synced
    # UIDs, so they'd otherwise get wrongly cancelled on the first sync.
    stale = (
        db.query(Booking)
        .filter_by(property_id=feed.property_id, platform=feed.platform, is_block=False)
        .filter(~Booking.uid.in_(seen_uids) if seen_uids else True)
        .all()
    )
    for booking in stale:
        booking.status = BookingStatus.cancelled

    feed.last_synced_at = datetime.utcnow()
    feed.last_sync_error = None
    db.commit()
    return result


def sync_all(db: Session) -> list[dict]:
    feeds = db.query(IcalFeed).filter_by(active=True).all()
    results = [sync_feed(db, feed) for feed in feeds]
    recompute_conflicts(db)
    return results


def recompute_conflicts(db: Session) -> None:
    """Flag bookings that overlap another confirmed booking on the same property."""
    bookings = (
        db.query(Booking)
        .filter(Booking.status == BookingStatus.confirmed)
        .order_by(Booking.property_id, Booking.check_in)
        .all()
    )

    by_property: dict[int, list[Booking]] = {}
    for b in bookings:
        by_property.setdefault(b.property_id, []).append(b)

    for prop_bookings in by_property.values():
        for b in prop_bookings:
            b.has_conflict = False
        for i, a in enumerate(prop_bookings):
            for b in prop_bookings[i + 1 :]:
                if a.check_in < b.check_out and b.check_in < a.check_out:
                    a.has_conflict = True
                    b.has_conflict = True
    db.commit()
