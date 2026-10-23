"""Authenticated administrator workbench; never exposes public integration controls."""
import os
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from app.auth import SESSION_COOKIE, _secret, verify_session_cookie
from app.db import get_db
from app.models import Booking, BookingStatus, CleaningTask, IntegrationRun, MessageDelivery, Property, ShadowReservation, StaffRole, StaffUser

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
DIRECT_PROVIDERS = ('booking_com', 'airbnb', 'expedia', 'vrbo', 'ctrip', 'other')


def require_admin(request: Request, db=Depends(get_db)):
    # Explicitly fail closed even if the legacy app is in no-auth local-dev mode.
    if not _secret():
        raise HTTPException(403, "Administrator login required")
    username = verify_session_cookie(request.cookies.get(SESSION_COOKIE, ""))
    if not username:
        raise HTTPException(403, "Administrator login required")
    shared = os.environ.get("ADMIN_USER")
    if shared and os.environ.get("ADMIN_PASSWORD") and username == shared:
        return username
    user = db.query(StaffUser).filter_by(username=username, active=True).first()
    if not user or user.role != StaffRole.admin:
        raise HTTPException(403, "Administrator access required")
    return username


def overview(db):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.models import WorkOrder
    today = datetime.now(ZoneInfo("Australia/Brisbane")).date().isoformat()
    return {
        "overdue_work_orders": db.query(WorkOrder).filter(WorkOrder.status.in_(["open", "in_progress"]), WorkOrder.due_date < today).count(),
        "missing_sources": db.query(Booking).filter(Booking.source_missing_since.isnot(None), Booking.status == BookingStatus.confirmed).count(),
        "missing_amounts": db.query(Booking).filter(Booking.amount.is_(None), Booking.is_block.is_(False), Booking.status == BookingStatus.confirmed).count(),
        "schedule_reviews": db.query(CleaningTask).filter_by(schedule_review_required=True).count(),
        "message_reviews": db.query(MessageDelivery).filter(MessageDelivery.state.in_(["sending", "unknown"])).count(),
        "unmapped": db.query(ShadowReservation).filter(ShadowReservation.provider.in_(DIRECT_PROVIDERS), ShadowReservation.state=="unmapped").count(),
        "shadow_count": db.query(ShadowReservation).filter(ShadowReservation.provider.in_(DIRECT_PROVIDERS)).count(),
        "runs": db.query(IntegrationRun).filter(IntegrationRun.provider.in_(DIRECT_PROVIDERS)).order_by(IntegrationRun.id.desc()).limit(10).all(),
        "dispatch_enabled": os.environ.get("OPS_MESSAGE_DISPATCH_ENABLED") == "1",
        "scheduler_enabled": os.environ.get("OPS_SCHEDULER_ENABLED", "1") == "1",
    }


@router.get("/operations")
def workbench(request: Request, db=Depends(get_db), admin=Depends(require_admin)):
    context = overview(db)
    context["review_items"], context["reviews_truncated"] = review_items(db, admin)
    from app.models import OperationsReview
    context["review_history"] = db.query(OperationsReview).order_by(OperationsReview.id.desc()).limit(20).all()
    return templates.TemplateResponse(request=request, name="operations.html", context=context)


@router.get("/api/ops/overview")
def workbench_data(db=Depends(get_db), admin=Depends(require_admin)):
    data = overview(db)
    data["runs"] = [{"id": r.id, "provider": r.provider, "state": r.state,
                    "record_count": r.record_count, "error_code": r.error_code} for r in data["runs"]]
    return data


def review_items(db, actor):
    from datetime import datetime, timedelta
    from app.models import StatementDelivery
    from app.operations.reviews import ACTIONS, ReviewError, issue_token
    groups = {
        "source": db.query(Booking).filter(Booking.source_missing_since.isnot(None), Booking.status == BookingStatus.confirmed).order_by(Booking.id).limit(51).all(),
        "cleaning": db.query(CleaningTask).filter_by(schedule_review_required=True).order_by(CleaningTask.id).limit(51).all(),
        "message": db.query(MessageDelivery).filter((MessageDelivery.state == "unknown") | ((MessageDelivery.state == "sending") & (MessageDelivery.created_at < datetime.utcnow() - timedelta(hours=1)))).order_by(MessageDelivery.id).limit(51).all(),
        "statement": db.query(StatementDelivery).filter((StatementDelivery.state == "unknown") | ((StatementDelivery.state == "sending") & (StatementDelivery.created_at < datetime.utcnow() - timedelta(hours=1)))).order_by(StatementDelivery.id).limit(51).all(),
    }
    result = []
    for kind, records in groups.items():
        for row in records[:50]:
            try:
                actions = [(a, issue_token(db, actor, kind, row.id, a)) for a in ACTIONS[kind]]
            except ReviewError:
                continue
            result.append({"kind": kind, "id": row.id, "record": row, "actions": actions})
    return result, any(len(records) > 50 for records in groups.values())


from fastapi import Form
from fastapi.responses import RedirectResponse
from app.operations.reviews import ReviewError, apply_review


@router.post("/operations/reviews/{kind}/{entity_id}/{action}")
def save_review(kind: str, entity_id: int, action: str, request: Request,
                token: str = Form(...), note: str = Form(...),
                db=Depends(get_db), admin=Depends(require_admin)):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Review must be submitted from this site")
    try:
        apply_review(db, actor=admin, kind=kind, entity_id=entity_id, action=action, token=token, note=note)
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse("/operations", status_code=303)


from datetime import date, timedelta
from app.operations.calendar import calendar_data


def calendar_context(db, start, end, property_id):
    from zoneinfo import ZoneInfo
    from datetime import datetime
    start = start or datetime.now(ZoneInfo("Australia/Brisbane")).date()
    end = end or start + timedelta(days=14)
    try:
        return calendar_data(db, start, end, property_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except LookupError:
        raise HTTPException(404, 'Property not found') from None


@router.get('/operations/calendar')
def calendar_page(request: Request, start: date = None, end: date = None, property_id: int = None,
                  db=Depends(get_db), admin=Depends(require_admin)):
    data = calendar_context(db, start, end, property_id)
    data['selected_property_id'] = property_id
    return templates.TemplateResponse(request=request, name='operations_calendar.html', context=data)


@router.get('/api/ops/calendar')
def calendar_api(start: date = None, end: date = None, property_id: int = None,
                 db=Depends(get_db), admin=Depends(require_admin)):
    return calendar_context(db, start, end, property_id)
