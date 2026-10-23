"""Source-scoped iCal observations. Missing events are not cancellation evidence."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar
from sqlalchemy.orm import Session

from app.models import Booking, BookingStatus, IcalFeed


def _to_datetime(value, zone="UTC"):
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo(zone)).replace(tzinfo=None) if value.tzinfo else value
    return datetime.combine(value, time.min)


def fetch_and_parse(url: str) -> list[dict]:
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)
    events = []
    seen = set()
    for component in cal.walk("VEVENT"):
        uid = str(component.get("uid") or "").strip()
        start, end = component.get("dtstart"), component.get("dtend")
        if not uid or start is None or end is None or uid in seen:
            raise ValueError("invalid_or_duplicate_calendar_event")
        seen.add(uid)
        events.append({"uid": uid, "summary": str(component.get("summary") or ""),
                       "check_in": start.dt, "check_out": end.dt,
                       "cancelled": str(component.get("status") or "").upper() == "CANCELLED"})
    return events


def sync_feed(db: Session, feed: IcalFeed) -> dict:
    result = {"feed_id": feed.id, "added": 0, "updated": 0, "missing": 0, "error": None}
    now = datetime.utcnow()
    try:
        events = fetch_and_parse(feed.url)
        zone = feed.property.time_zone or "UTC"
        # Validate the entire response before applying any observation.
        normalized = []
        for e in events:
            start, end = _to_datetime(e["check_in"], zone), _to_datetime(e["check_out"], zone)
            if end <= start:
                raise ValueError("invalid_calendar_range")
            normalized.append({**e, "check_in": start, "check_out": end})
        observations = []
        for e in normalized:
            existing = db.query(Booking).filter_by(property_id=feed.property_id, platform=feed.platform, uid=e["uid"]).first()
            if existing and (existing.is_block or existing.source_feed_id not in (None, feed.id)):
                raise ValueError("calendar_source_collision")
            if existing and existing.source_feed_id is None:
                count = db.query(IcalFeed).filter_by(property_id=feed.property_id, platform=feed.platform).count()
                if count != 1:
                    raise ValueError("legacy_source_mapping_required")
            observations.append((e, existing))
        seen = set()
        for e, existing in observations:
            seen.add(e["uid"])
            if not existing:
                existing = Booking(property_id=feed.property_id, platform=feed.platform, uid=e["uid"],
                                   source_feed_id=feed.id, notifications_enabled=False)
                db.add(existing)
                result["added"] += 1
            else:
                result["updated"] += 1
            existing.source_feed_id = feed.id
            existing.summary = e["summary"]
            existing.check_in, existing.check_out = e["check_in"], e["check_out"]
            existing.status = BookingStatus.cancelled if e.get("cancelled") else BookingStatus.confirmed
            existing.source_last_seen_at = now
            existing.source_missing_since = None
        stale = db.query(Booking).filter_by(source_feed_id=feed.id, is_block=False).all()
        for booking in stale:
            if booking.uid not in seen:
                booking.source_missing_since = booking.source_missing_since or now
                result["missing"] += 1
        feed.last_synced_at, feed.last_sync_error = now, None
        db.commit()
    except Exception:
        db.rollback()
        # Never persist provider errors: they may embed credential-bearing URLs.
        feed.last_sync_error = "calendar_sync_failed_review_required"
        db.commit()
        result.update(added=0, updated=0, missing=0, error=feed.last_sync_error)
    return result


def sync_all(db: Session) -> list[dict]:
    results = [sync_feed(db, f) for f in db.query(IcalFeed).filter_by(active=True).all()]
    recompute_conflicts(db)
    return results


def recompute_conflicts(db: Session) -> None:
    # Reset cancelled bookings too, so old conflict flags cannot survive cancellation.
    db.query(Booking).update({Booking.has_conflict: False})
    bookings = db.query(Booking).filter(Booking.status == BookingStatus.confirmed).order_by(Booking.property_id, Booking.check_in).all()
    grouped = {}
    for b in bookings:
        grouped.setdefault(b.property_id, []).append(b)
    for group in grouped.values():
        for i, a in enumerate(group):
            for other in group[i + 1:]:
                if a.check_in < other.check_out and other.check_in < a.check_out:
                    a.has_conflict = other.has_conflict = True
    db.commit()
