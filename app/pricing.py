"""Rule-based dynamic pricing — a free, self-contained alternative to a
paid market-data engine like PriceLabs/Beyond. Suggests a nightly rate by
multiplying Property.base_nightly_rate by every active PriceRule that
matches the date (day-of-week, a date range, or "last minute"). Rules
stack multiplicatively, so a Friday inside a festival date range gets
both multipliers.

This only ever *suggests* a number for staff to act on — nothing here
pushes a price to Airbnb/Booking.com automatically, since we don't have
write access to those platforms' calendars.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import PriceRule, PriceRuleType, Property


def _rule_matches(rule: PriceRule, target: date, today: date) -> bool:
    if not rule.active:
        return False
    if rule.rule_type == PriceRuleType.day_of_week:
        if not rule.days_of_week:
            return False
        days = {int(d) for d in rule.days_of_week.split(",") if d.strip().isdigit()}
        return target.weekday() in days
    if rule.rule_type == PriceRuleType.date_range:
        if not rule.start_date or not rule.end_date:
            return False
        return rule.start_date.date() <= target <= rule.end_date.date()
    if rule.rule_type == PriceRuleType.last_minute:
        if rule.days_before is None:
            return False
        return 0 <= (target - today).days <= rule.days_before
    return False


def suggested_price(db: Session, prop: Property, target: date, today: date | None = None) -> Decimal | None:
    """Returns the suggested nightly rate for `prop` on `target` night, or
    None if the property has no base rate set."""
    if prop.base_nightly_rate is None:
        return None
    today = today or datetime.utcnow().date()

    rules = (
        db.query(PriceRule)
        .filter(
            PriceRule.active.is_(True),
            (PriceRule.property_id == prop.id) | (PriceRule.property_id.is_(None)),
        )
        .all()
    )

    price = prop.base_nightly_rate
    for rule in rules:
        if _rule_matches(rule, target, today):
            price = (price * rule.multiplier).quantize(Decimal("0.01"))
    return price


def price_breakdown(db: Session, prop: Property, check_in: date, check_out: date) -> list[dict]:
    """One row per night from check_in (inclusive) to check_out (exclusive)."""
    rows = []
    current = check_in
    while current < check_out:
        rows.append({"date": current, "price": suggested_price(db, prop, current)})
        current = date.fromordinal(current.toordinal() + 1)
    return rows
