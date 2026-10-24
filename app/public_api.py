"""Public, read-only API for the omiholiday.com marketing site (B3).

Every route here is listed in PUBLIC_PREFIXES (app/auth.py), so it's
reachable without the staff session cookie — fetched directly from a site
visitor's browser. Two things that follow from that:

- Responses are built field-by-field from an explicit allow-list (see
  _property_summary) rather than dumping the ORM row, so a sensitive column
  added to Property later doesn't silently leak here.
- "Not published" and "doesn't exist" must look identical (404), so the
  API never confirms the existence of an unpublished listing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import _sign
from app.db import get_db
from app.models import Booking, BookingStatus, InventoryDay, Property
from app.pricing import price_breakdown, suggested_price

router = APIRouter(prefix="/api/public")

QUOTE_TTL_SECONDS = 30 * 60
MAX_RANGE_DAYS = 366  # guards /calendar and /quote against unbounded date ranges


def _envelope(result):
    return {"status": "success", "result": result}


def _money(value: Optional[Decimal]) -> Optional[str]:
    """AUD amount as a fixed 2dp string, e.g. "250.00" — never a float/cents."""
    if value is None:
        return None
    return str(value.quantize(Decimal("0.01")))


def _parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field!r}, expected YYYY-MM-DD") from None


def _property_summary(prop: Property) -> dict:
    return {
        "id": prop.id,
        "name": prop.name,
        "suburb": prop.suburb,
        "photo_url": prop.photo_url,
        "bedrooms": prop.bedrooms,
        "bathrooms": prop.bathrooms,
        "max_guests": prop.max_guests,
        "listing_url": prop.listing_url,
        "base_nightly_rate": _money(prop.base_nightly_rate),
        "currency": "AUD",
        "is_unit": prop.parent_property_id is not None,
        "parent_id": prop.parent_property_id,
        "reservation_request_url": f"/api/public/reservation-request?property_id={prop.id}",
    }


def _published_property_or_404(db: Session, property_id: int) -> Property:
    prop = db.get(Property, property_id)
    if not prop or not prop.published:
        raise HTTPException(status_code=404, detail="Listing not found")
    return prop


def _has_overlapping_booking(db: Session, property_id: int, start_dt: datetime, end_dt: datetime) -> bool:
    from app.operations.stock import available_units
    try:return available_units(db,property_id,start_dt,end_dt)<1
    except ValueError:return True


@router.get("/listings")
def list_listings(
    guests: Optional[int] = Query(None, ge=1),
    suburb: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(Property).filter(Property.published.is_(True))
    if guests is not None:
        q = q.filter(Property.max_guests.isnot(None), Property.max_guests >= guests)
    if suburb:
        q = q.filter(Property.suburb.ilike(f"%{suburb}%"))
    listings = [_property_summary(p) for p in q.order_by(Property.id).all()]
    return _envelope(listings)


@router.get("/listings/{property_id}")
def get_listing(property_id: int, db: Session = Depends(get_db)):
    prop = _published_property_or_404(db, property_id)
    data = _property_summary(prop)
    data["description"] = prop.description
    from app.operations.property_photos import public_gallery
    data["photos"] = public_gallery(db,prop.id)
    # A sub-unit that isn't itself published stays hidden even when its
    # parent is public — "not published" must mean invisible everywhere.
    data["units"] = [_property_summary(u) for u in sorted(prop.units, key=lambda u: u.id) if u.published]
    return _envelope(data)


@router.get("/listings/{property_id}/calendar")
def get_calendar(
    property_id: int,
    start: str = Query(...),
    end: str = Query(...),
    db: Session = Depends(get_db),
):
    prop = _published_property_or_404(db, property_id)
    start_d = _parse_date(start, "start")
    end_d = _parse_date(end, "end")
    if end_d <= start_d:
        raise HTTPException(status_code=400, detail="'end' must be after 'start'")
    if (end_d - start_d).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"Range too large (max {MAX_RANGE_DAYS} days)")

    start_dt = datetime.combine(start_d, datetime.min.time())
    end_dt = datetime.combine(end_d, datetime.min.time())
    bookings = (
        db.query(Booking)
        .filter(
            Booking.property_id == property_id,
            Booking.status == BookingStatus.confirmed,
            Booking.check_in < end_dt,
            Booking.check_out > start_dt,
        )
        .all()
    )

    overrides = {row.night: row for row in db.query(InventoryDay).filter(InventoryDay.property_id == property_id,
        InventoryDay.night >= start_d.isoformat(), InventoryDay.night < end_d.isoformat()).all()}
    from app.operations.stock import stock_context,units_from_context
    try:stock=stock_context(db,property_id,start_dt,end_dt)
    except ValueError:raise HTTPException(409,'Inventory needs review before dates can be displayed') from None
    from app.operations.booking_windows import window_context,window_reason
    booking_window=window_context(db,property_id)
    days = []
    current = start_d
    while current < end_d:
        day_start = datetime.combine(current, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        overlapping = [b for b in bookings if b.check_in < day_end and b.check_out > day_start]
        override = overrides.get(current.isoformat())
        blocked = any(b.is_block for b in overlapping) or bool(override and override.stop_sell)
        reserved = any(not b.is_block for b in overlapping)
        remaining=units_from_context(stock,day_start,day_end)

        if blocked:
            day_status = "blocked"
        elif remaining<1 and reserved:
            day_status = "reserved"
        elif remaining<1:
            day_status = "unavailable"
        else:
            day_status = "available"

        price = None
        if day_status == "available":
            price = _money(suggested_price(db, prop, current))

        window_restriction=window_reason(db,property_id,current,context=booking_window) if booking_window is not None else None
        days.append({"date": current.isoformat(), "status": day_status, "price": price,
            "available_units":0 if blocked else remaining,
            "minimum_stay": override.minimum_stay if override else 1,
            "maximum_stay": override.maximum_stay if override else None,
            "closed_arrival": bool(override and override.closed_arrival) or bool(window_restriction),
            "booking_window_reason":window_restriction,
            "closed_departure": bool(override and override.closed_departure)})
        current += timedelta(days=1)

    return _envelope({"property_id": property_id, "days": days})


def _make_quote_token(
    property_id: int,
    check_in: date,
    check_out: date,
    guests: int,
    total: Optional[Decimal],
    expires_at: datetime,
) -> str:
    # Pipe-delimited payload, HMAC'd the same way as the staff session
    # cookie (app.auth._sign/_secret — SECRET_KEY, falling back to
    # ADMIN_PASSWORD). B5 verifies a checkout request against this the same
    # way app.auth.verify_session_cookie checks the cookie: split on the
    # last ".", recompute the HMAC with hmac.compare_digest, then check
    # expires_at. No endpoint here parses this token back — that's B5.
    payload = "|".join(
        [
            'quote-v2',
            str(property_id),
            check_in.isoformat(),
            check_out.isoformat(),
            str(guests),
            _money(total) or "NA",
            expires_at.isoformat(),
            secrets.token_hex(16),
        ]
    )
    return _sign(payload)


@router.get("/quote")
def get_quote(
    property_id: int = Query(...),
    check_in: str = Query(...),
    check_out: str = Query(...),
    guests: int = Query(..., ge=1),
    db: Session = Depends(get_db),
):
    prop = _published_property_or_404(db, property_id)
    ci = _parse_date(check_in, "check_in")
    co = _parse_date(check_out, "check_out")
    if co <= ci:
        raise HTTPException(status_code=400, detail="'check_out' must be after 'check_in'")
    if (co - ci).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"Stay too long (max {MAX_RANGE_DAYS} nights)")

    rows = price_breakdown(db, prop, ci, co)
    nightly_total: Optional[Decimal] = None
    if rows and all(row["price"] is not None for row in rows):
        nightly_total = sum((row["price"] for row in rows), Decimal("0"))

    bookable = True
    reason = None
    if prop.max_guests is not None and guests > prop.max_guests:
        bookable, reason = False, "guests_exceed_max"
    elif nightly_total is None:
        bookable, reason = False, "pricing_unavailable"
    else:
        ci_dt = datetime.combine(ci, datetime.min.time())
        co_dt = datetime.combine(co, datetime.min.time())
        if _has_overlapping_booking(db, property_id, ci_dt, co_dt):
            bookable, reason = False, "dates_unavailable"

    from app.operations.inventory import restriction_reason
    restriction = restriction_reason(db, property_id, ci, co)
    if bookable and restriction:
        bookable, reason = False, restriction

    components = []
    subtotal = total = None
    if nightly_total is not None:
        components = [{"name": "accommodation", "amount": _money(nightly_total)}]
        subtotal = total = _money(nightly_total)

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=QUOTE_TTL_SECONDS)).replace(microsecond=0)
    quote_token = _make_quote_token(property_id, ci, co, guests, nightly_total, expires_at)

    return _envelope(
        {
            "property_id": property_id,
            "check_in": ci.isoformat(),
            "check_out": co.isoformat(),
            "guests": guests,
            "components": components,
            "subtotal": subtotal,
            "total": total,
            "currency": "AUD",
            "bookable": bookable,
            "reason": reason,
            "quote_token": quote_token,
            "quote_expires_at": expires_at.isoformat(),
        }
    )
