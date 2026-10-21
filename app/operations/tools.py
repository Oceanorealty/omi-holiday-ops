"""Allowlisted, property-scoped facts for human or MCP callers. No model/API calls."""
from datetime import date, datetime, timedelta
from app.models import Booking, BookingStatus, CleaningStatus, CleaningTask, Property
from app.reconcile.summary import monthly_summary


class OpsTools:
    def __init__(self, db, property_ids):
        self.db, self.scope = db, frozenset(property_ids)
        if not self.scope or any(type(i) is not int or i < 1 for i in self.scope):
            raise ValueError("explicit_property_scope_required")

    def property_facts(self, property_id):
        if type(property_id) is not int or property_id not in self.scope:
            raise PermissionError("property_not_authorized")
        prop = self.db.get(Property, property_id)
        if not prop:
            raise LookupError("property_not_found")
        return {"property_id": prop.id, "name": prop.name, "description": prop.description,
                "max_guests": prop.max_guests, "house_rules": prop.house_rules,
                "time_zone": prop.time_zone, "evidence": f"property:{prop.id}",
                "limitations": ["No door codes, Wi-Fi secrets, private address, guest identity or portal tokens exposed"]}

    def arrivals(self, start, end):
        start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
        if end_d < start_d or (end_d - start_d).days > 31:
            raise ValueError("date_range_max_31_days")
        rows = self.db.query(Booking).filter(Booking.property_id.in_(self.scope),
            Booking.status == BookingStatus.confirmed, Booking.is_block.is_(False),
            Booking.check_in >= datetime.combine(start_d, datetime.min.time()),
            Booking.check_in < datetime.combine(end_d + timedelta(days=1), datetime.min.time())).order_by(Booking.check_in, Booking.id).limit(201).all()
        return {"items": [{"booking_id": b.id, "property_id": b.property_id,
            "check_in": b.check_in.isoformat(), "check_out": b.check_out.isoformat(),
            "source_needs_review": bool(b.source_missing_since), "evidence": f"booking:{b.id}"} for b in rows[:200]],
            "truncated": len(rows) > 200}

    def overdue_tasks(self, as_of):
        boundary = datetime.fromisoformat(as_of)
        if boundary.tzinfo:
            raise ValueError("use_property_local_naive_time")
        rows = self.db.query(CleaningTask).filter(CleaningTask.property_id.in_(self.scope),
            CleaningTask.status != CleaningStatus.done, CleaningTask.cancelled_at.is_(None), CleaningTask.due_date < boundary).order_by(CleaningTask.due_date).limit(201).all()
        return {"items": [{"task_id": t.id, "property_id": t.property_id,
            "due_date": t.due_date.isoformat(), "status": t.status.value,
            "schedule_review_required": bool(t.schedule_review_required), "evidence": f"cleaning_task:{t.id}"} for t in rows[:200]],
            "truncated": len(rows) > 200}

    def reconciliation(self):
        rows = monthly_summary(self.db, property_ids=self.scope)
        return {"basis": "checkout_month; received=confirmed bank receipts; AUD baseline only",
                "items": [{**r, "expected": str(r["expected"]) if r["expected"] is not None else None,
                           "known_expected": str(r["known_expected"]), "received": str(r["received"]),
                           "reported": str(r["reported"]), "variance": str(r["variance"]) if r["variance"] is not None else None,
                           "evidence": f"property:{r['property_id']}:month:{r['month']}"} for r in rows]}
