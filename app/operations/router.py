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
    from app.operations.internal_trial_workflows import listing_cards
    trial_cards = listing_cards(db.get_bind())
    return {
        "internal_trial_source_count": len(trial_cards),
        "internal_trial_property_cards": trial_cards[:12],
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


@router.get('/operations/tools')
def tools_page(request: Request, q: str = '', admin=Depends(require_admin)):
    return templates.TemplateResponse(request=request, name='operations_tools.html', context={'q': q.strip()[:100]})


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


@router.get('/operations/property-cleanup')
def property_cleanup_review(request: Request, ids: str, db=Depends(get_db), admin=Depends(require_admin)):
    from app.operations.property_cleanup import preview, parse_ids, CleanupError
    from app.operations.tasks import form_token
    try:
        selected = parse_ids(ids)
        report = preview(db.connection(), selected)
        token = form_token(admin, purpose='property-cleanup:'+report['digest'])
    except CleanupError as exc:
        raise HTTPException(409, str(exc)) from None
    return templates.TemplateResponse(request=request, name='property_cleanup.html', context={
        'report':report,'target_ids':','.join(map(str,selected)),
        'statement_ids':','.join(str(r['id']) for r in report['statements']),'token':token},
        headers={'Cache-Control':'no-store'})


@router.post('/operations/property-cleanup')
def property_cleanup_apply(request: Request, target_ids: str=Form(...), statement_ids: str=Form(''),
                           digest: str=Form(...), token: str=Form(...), confirmation: str=Form(...),
                           db=Depends(get_db), admin=Depends(require_admin)):
    import hashlib
    import json
    from app.operations.property_cleanup import remove, parse_ids, CleanupError
    from app.operations.tasks import verify_token, TaskError
    from app.models import OpsCommand
    origin=request.headers.get('origin')
    if not origin or origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403,'Submit from this site')
    try:
        if confirmation != 'DELETE THESE TEST RECORDS':
            raise CleanupError('Type the exact confirmation shown on the review page')
        selected=parse_ids(target_ids)
        statements=parse_ids(statement_ids) if statement_ids.strip() else []
        verify_token(token,admin,0,purpose='property-cleanup:'+digest)
        report=remove(db.connection(),selected,digest,statements)
        db.add(OpsCommand(token_digest=hashlib.sha256(token.encode()).hexdigest(),
            purpose='property-fixture-cleanup',actor=admin,
            note='Explicit administrator removal of reviewed internal fixture records; no backup requested.',
            payload=json.dumps({'properties':report['properties'],'statement_ids':statements,'review_digest':digest},sort_keys=True)))
        db.commit()
    except (CleanupError,TaskError) as exc:
        db.rollback()
        raise HTTPException(409,str(exc)) from None
    except Exception:
        db.rollback()
        raise
    return RedirectResponse('/properties',status_code=303)


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


def calendar_context(db, start, end, property_id, include_context=False):
    from zoneinfo import ZoneInfo
    from datetime import datetime
    start = start or datetime.now(ZoneInfo("Australia/Brisbane")).date()
    end = end or start + timedelta(days=14)
    try:
        data = calendar_data(db, start, end, property_id)
        data["context_enabled"] = include_context
        if include_context:
            from app.operations.calendar_context import add_context
            try:
                add_context(db, data)
            except ValueError as exc:
                data["context_enabled"] = False
                data["context_error"] = str(exc)
        return data
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except LookupError:
        raise HTTPException(404, 'Property not found') from None


@router.get('/operations/calendar')
def calendar_page(request: Request, start: date = None, end: date = None, property_id: int = None,
                  view: str = 'grid', include_context: bool = False,
                  db=Depends(get_db), admin=Depends(require_admin)):
    if view not in ('grid', 'list'):
        raise HTTPException(422, 'Choose grid or list view')
    data = calendar_context(db, start, end, property_id, include_context)
    data['selected_property_id'] = property_id
    data['view'] = view
    first, last = date.fromisoformat(data['start']), date.fromisoformat(data['end'])
    span = last - first
    try:
        data['previous_start'], data['previous_end'] = (first - span).isoformat(), first.isoformat()
    except OverflowError:
        data['previous_start'] = None
    try:
        data['next_start'], data['next_end'] = last.isoformat(), (last + span).isoformat()
    except OverflowError:
        data['next_start'] = None
    return templates.TemplateResponse(request=request, name='operations_calendar.html', context=data, headers={'Cache-Control':'no-store'})


@router.get('/api/ops/calendar')
def calendar_api(start: date = None, end: date = None, property_id: int = None, include_context: bool = False,
                 db=Depends(get_db), admin=Depends(require_admin)):
    from starlette.responses import JSONResponse
    return JSONResponse(calendar_context(db, start, end, property_id, include_context), headers={'Cache-Control':'no-store'})
