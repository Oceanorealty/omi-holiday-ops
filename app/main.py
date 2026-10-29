from datetime import datetime
from decimal import Decimal, InvalidOperation

import os
import secrets as secrets_module
import uuid
from calendar import monthrange

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request, UploadFile, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import migrate
from app.auth import SESSION_COOKIE, SESSION_MAX_AGE, SessionAuthMiddleware, make_session_cookie
from app.comms.owner_reports import send_owner_reports, _generate_statement, finalize_statement
from app.comms.templates import seed_default_templates
from app.comms.triggers import process_due_messages
from app.db import Base, SessionLocal, engine
from app.models import (
    Booking,
    BookingStatus,
    Cleaner,
    CleaningStatus,
    CleaningTask,
    Expense,
    Guest,
    IcalFeed,
    MessageLog,
    MessageTemplate,
    OwnerStatement,
    PlatformName,
    PriceRule,
    PriceRuleType,
    Property,
    StaffRole,
    StaffUser,
    Transaction,
)
from app.gst import gst_registered
from app.pricing import price_breakdown
from app.operations.router import router as operations_router, require_admin
from app.public_api import router as public_router
from app.security import hash_password, verify_password
from app.reconcile.importer import ImportError_, parse_csv
from app.reconcile.matching import suggest_bookings
from app.reconcile.summary import company_monthly_summary, monthly_summary
from app.sync.cleaning import sync_cleaning_tasks
from app.sync.ical_export import build_ics
from app.sync.ical_sync import recompute_conflicts, sync_all, sync_feed

from app.deployment import ensure_release_snapshot
_release_snapshot = ensure_release_snapshot(engine, snapshot_schema='ops_backup_20261023_gapfill')
print(f"OPS_RELEASE_SNAPSHOT state={_release_snapshot['state']} tables={_release_snapshot['tables']}", flush=True)
migrate.run(engine)
Base.metadata.create_all(bind=engine)

with SessionLocal() as _db:
    seed_default_templates(_db)

app = FastAPI(title="Omi Holiday — Operations")
from app.operations.task_router import router as task_router
from app.operations.parity_router import router as parity_router
from app.operations.channel_router import router as channel_router
from app.operations.task_plan_router import router as task_plan_router
from app.operations.guest_checkin_router import router as guest_checkin_router
from app.operations.inbox_router import router as inbox_router
from app.operations.agreement_router import router as agreement_router
from app.operations.billing_router import router as billing_router
from app.operations.guest_access_router import router as guest_access_router
from app.operations.calendar_note_router import router as calendar_note_router
from app.operations.task_time_router import router as task_time_router
from app.operations.calendar_period_router import router as calendar_period_router
from app.operations.task_attachment_router import router as task_attachment_router
from app.operations.reservation_request_router import router as reservation_request_router
from app.operations.stock_router import router as stock_router
from app.operations.custom_field_router import router as custom_field_router
app.include_router(custom_field_router)
from app.operations.registration_router import router as registration_router
app.include_router(registration_router)
from app.operations.booking_window_router import router as booking_window_router
app.include_router(booking_window_router)
app.include_router(stock_router)
app.include_router(reservation_request_router)
app.include_router(guest_checkin_router)
app.include_router(inbox_router)
app.include_router(agreement_router)
app.include_router(billing_router)
app.include_router(guest_access_router)
app.include_router(calendar_note_router)
app.include_router(task_time_router)
app.include_router(calendar_period_router)
app.include_router(task_attachment_router)
app.include_router(task_plan_router)
app.include_router(channel_router)
app.include_router(parity_router)
app.include_router(task_router)
app.include_router(operations_router)
from app.public_booking import router as public_booking_router
app.include_router(public_booking_router)
from app.operations.payment_router import router as payment_router
app.include_router(payment_router)
from app.operations.guest_directory_router import router as guest_directory_router
app.include_router(guest_directory_router)
from app.operations.property_photo_router import router as property_photo_router
app.include_router(property_photo_router)
from app.operations.my_work_router import router as my_work_router
app.include_router(my_work_router)
from app.operations.staff_router import router as staff_router
app.include_router(staff_router)
app.include_router(public_router)
from app.operations.internal_trial_router import router as internal_trial_router
app.include_router(internal_trial_router)
app.add_middleware(SessionAuthMiddleware)


@app.middleware("http")
async def prevent_private_page_caching(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "private, no-store"
        existing = response.headers.get("Vary", "")
        response.headers["Vary"] = ", ".join(dict.fromkeys([v.strip() for v in existing.split(",") if v.strip()] + ["Cookie", "Authorization"]))
    return response

# Outermost middleware — lets omiholiday.com fetch /api/public/* directly
# from the visitor's own browser (a different origin), read-only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://omiholiday.com", "https://www.omiholiday.com"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def _scheduled_sync():
    db = SessionLocal()
    try:
        sync_all(db)
        sync_cleaning_tasks(db)
        from app.operations.task_plans import materialize_schedules
        materialize_schedules(db)
        process_due_messages(db)
        send_owner_reports(db)
    finally:
        db.close()


scheduler = BackgroundScheduler()
scheduler.add_job(_scheduled_sync, "interval", minutes=30, id="ical_sync")


@app.on_event("startup")
def start_scheduler():
    if os.environ.get("OPS_SCHEDULER_ENABLED", "1") == "1" and not scheduler.running:
        scheduler.start()


@app.on_event("shutdown")
def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/healthz")
def healthz():
    # Render calls this frequently. Keep liveness cheap so it does not keep
    # a scale-to-zero database awake around the clock.
    return {"status": "ok", "release": "ops-gapfill-20261023"}


@app.get("/operations/readiness")
def database_readiness(admin=Depends(require_admin)):
    from app.operations.health import database_health
    return database_health(engine)


@app.get("/login")
def login_page(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(
        "login.html", {"request": request, "next": next, "error": error}
    )


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    admin_user = os.environ.get("ADMIN_USER", "")
    admin_password = os.environ.get("ADMIN_PASSWORD", "")

    logged_in_as = None
    named_role = None
    if secrets_module.compare_digest(username, admin_user) and secrets_module.compare_digest(
        password, admin_password
    ):
        logged_in_as = username
    else:
        db = SessionLocal()
        try:
            staff = (
                db.query(StaffUser)
                .filter(StaffUser.username == username, StaffUser.active.is_(True))
                .first()
            )
            if staff and verify_password(password, staff.password_hash):
                logged_in_as = staff.username
                named_role = staff.role
        finally:
            db.close()

    if logged_in_as:
        target=next if next.startswith('/') and not next.startswith('//') and chr(92) not in next and not any(ord(c)<32 for c in next) else '/'
        if named_role is not None and named_role!=StaffRole.admin and not (target=='/my-work' or target.startswith('/my-work/')):target='/my-work'
        response = RedirectResponse(url=target, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            make_session_cookie(logged_in_as),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=request.url.scheme=="https",
            samesite="lax",
        )
        return response

    return RedirectResponse(url=f"/login?error=1&next={next}", status_code=303)


@app.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/")
def dashboard(request: Request):
    db = SessionLocal()
    try:
        bookings = (
            db.query(Booking)
            .filter(Booking.status == BookingStatus.confirmed, Booking.is_block.is_(False))
            .order_by(Booking.check_in)
            .all()
        )
        # Conflicts still count blocks (a real booking overlapping a manual
        # block is exactly the double-booking this table should warn about)
        # even though blocks themselves aren't listed as a row here.
        all_confirmed = (
            db.query(Booking).filter(Booking.status == BookingStatus.confirmed).all()
        )
        conflicts = [b for b in all_confirmed if b.has_conflict]
        properties = db.query(Property).order_by(Property.name).all()

        logs_by_booking: dict[int, list] = {}
        if bookings:
            logs = (
                db.query(MessageLog)
                .filter(MessageLog.booking_id.in_([b.id for b in bookings]))
                .all()
            )
            for log in logs:
                logs_by_booking.setdefault(log.booking_id, []).append(log)

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "bookings": bookings,
                "conflicts": conflicts,
                "properties": properties,
                "logs_by_booking": logs_by_booking,
                "now": datetime.utcnow(),
            },
        )
    finally:
        db.close()


@app.post("/sync")
def trigger_sync():
    db = SessionLocal()
    try:
        sync_all(db)
        sync_cleaning_tasks(db)
        from app.operations.task_plans import materialize_schedules
        materialize_schedules(db)
        process_due_messages(db)
        send_owner_reports(db)
    finally:
        db.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/bookings/{booking_id}/amount")
def update_booking_amount(booking_id: int, amount: str = Form("")):
    db = SessionLocal()
    try:
        from app.operations.stock import lock_stock
        lock_stock(db)
        booking = db.get(Booking, booking_id)
        if booking:
            try:
                booking.amount = Decimal(amount) if amount.strip() else None
            except InvalidOperation:
                pass  # leave unchanged on bad input rather than 500
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/bookings/{booking_id}/door-code")
def update_door_code(booking_id: int, door_code: str = Form("")):
    db = SessionLocal()
    try:
        booking = db.get(Booking, booking_id)
        if booking:
            booking.door_code = door_code or None
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/bookings/{booking_id}/guest")
def update_guest(booking_id: int, name: str = Form(""), email: str = Form(""), language: str = Form("en")):
    db = SessionLocal()
    try:
        from app.operations.stock import lock_stock
        lock_stock(db)
        booking = db.get(Booking, booking_id)
        if booking:
            if not booking.guest:
                booking.guest = Guest()
                db.add(booking.guest)
            booking.guest.name = name or None
            booking.guest.email = email or None
            booking.guest.language = language or "en"
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/", status_code=303)


@app.get("/templates")
def templates_page(request: Request):
    db = SessionLocal()
    try:
        message_templates = (
            db.query(MessageTemplate).order_by(MessageTemplate.trigger_event).all()
        )
        return templates.TemplateResponse(
            "templates.html", {"request": request, "message_templates": message_templates}
        )
    finally:
        db.close()


@app.post("/templates/{template_id}")
def update_template(
    template_id: int,
    subject: str = Form(...),
    body: str = Form(...),
    subject_zh: str = Form(""),
    body_zh: str = Form(""),
    active: str = Form(""),
):
    db = SessionLocal()
    try:
        template = db.get(MessageTemplate, template_id)
        if template:
            template.subject = subject
            template.body = body
            template.subject_zh = subject_zh or None
            template.body_zh = body_zh or None
            template.active = bool(active)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/templates", status_code=303)


@app.get("/pricing")
def pricing_page(request: Request):
    db = SessionLocal()
    try:
        properties = (
            db.query(Property)
            .filter(Property.pricing_mode == "dynamic")
            .order_by(Property.name)
            .all()
        )
        rules = db.query(PriceRule).order_by(PriceRule.name).all()
        return templates.TemplateResponse(
            "pricing.html",
            {
                "request": request,
                "properties": properties,
                "all_properties": db.query(Property).order_by(Property.name).all(),
                "rules": rules,
                "rule_types": list(PriceRuleType),
                "breakdown": None,
            },
        )
    finally:
        db.close()


@app.post("/pricing/calculate")
def pricing_calculate(request: Request, property_id: int = Form(...), check_in: str = Form(...), check_out: str = Form(...)):
    db = SessionLocal()
    try:
        prop = db.get(Property, property_id)
        try:
            ci = datetime.strptime(check_in, "%Y-%m-%d").date()
            co = datetime.strptime(check_out, "%Y-%m-%d").date()
        except ValueError:
            ci = co = None

        breakdown = None
        total = None
        if prop and ci and co and co > ci:
            breakdown = price_breakdown(db, prop, ci, co)
            if all(r["price"] is not None for r in breakdown):
                total = sum((r["price"] for r in breakdown), Decimal("0"))

        properties = db.query(Property).filter(Property.pricing_mode == "dynamic").order_by(Property.name).all()
        rules = db.query(PriceRule).order_by(PriceRule.name).all()
        return templates.TemplateResponse(
            "pricing.html",
            {
                "request": request,
                "properties": properties,
                "all_properties": db.query(Property).order_by(Property.name).all(),
                "rules": rules,
                "rule_types": list(PriceRuleType),
                "breakdown": breakdown,
                "breakdown_total": total,
                "selected_property": prop,
                "selected_check_in": check_in,
                "selected_check_out": check_out,
            },
        )
    finally:
        db.close()


@app.post("/properties/{property_id}/base-rate")
def update_base_rate(property_id: int, base_nightly_rate: str = Form("")):
    db = SessionLocal()
    try:
        prop = db.get(Property, property_id)
        if prop:
            try:
                prop.base_nightly_rate = Decimal(base_nightly_rate) if base_nightly_rate.strip() else None
            except InvalidOperation:
                pass
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/pricing", status_code=303)


@app.post("/price-rules")
def create_price_rule(
    name: str = Form(...),
    rule_type: str = Form(...),
    multiplier: str = Form(...),
    property_id: str = Form(""),
    days_of_week: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    days_before: str = Form(""),
):
    db = SessionLocal()
    try:
        try:
            mult = Decimal(multiplier)
        except InvalidOperation:
            return RedirectResponse(url="/pricing", status_code=303)
        rule = PriceRule(
            name=name,
            rule_type=PriceRuleType(rule_type),
            multiplier=mult,
            property_id=int(property_id) if property_id else None,
            days_of_week=days_of_week or None,
            days_before=int(days_before) if days_before.strip().isdigit() else None,
        )
        if start_date:
            rule.start_date = datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            rule.end_date = datetime.strptime(end_date, "%Y-%m-%d")
        db.add(rule)
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/pricing", status_code=303)


@app.post("/price-rules/{rule_id}/toggle")
def toggle_price_rule(rule_id: int):
    db = SessionLocal()
    try:
        rule = db.get(PriceRule, rule_id)
        if rule:
            rule.active = not rule.active
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/pricing", status_code=303)


@app.post("/price-rules/{rule_id}/delete")
def delete_price_rule(rule_id: int):
    db = SessionLocal()
    try:
        rule = db.get(PriceRule, rule_id)
        if rule:
            db.delete(rule)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/pricing", status_code=303)


@app.get("/api/public/properties")
def public_properties():
    db = SessionLocal()
    try:
        properties = (
            db.query(Property)
            .filter(Property.published.is_(True))
            .order_by(Property.name)
            .all()
        )
        return [
            {
                "name": p.name,
                "suburb": p.suburb,
                "photo_url": p.photo_url,
                "description": p.description,
                "bedrooms": p.bedrooms,
                "bathrooms": p.bathrooms,
                "max_guests": p.max_guests,
                "listing_url": p.listing_url,
            }
            for p in properties
        ]
    finally:
        db.close()


@app.get("/properties")
def properties_page(request: Request, q: str = "", kind: str = "all", publication: str = "all", property_id: int | None = None, page: int = 1):
    db = SessionLocal()
    try:
        from app.operations.property_index import index_data
        from app.operations.tasks import TaskError
        try:data = index_data(db,q=q,kind=kind,publication=publication,property_id=property_id,page=page)
        except TaskError as exc:raise HTTPException(422,str(exc)) from None
        properties = data['properties']
        # Lazily backfill the outbound export token, same pattern as
        # owner_portal_token — existing properties get one on first visit
        # here rather than needing a migration to fill it in.
        changed = False
        for prop in properties:
            if not prop.ical_export_token:
                prop.ical_export_token = secrets_module.token_urlsafe(24)
                changed = True
        if changed:
            db.commit()
        all_properties = db.query(Property).order_by(Property.name).all()
        return templates.TemplateResponse(
            "properties.html",
            {
                "request": request,
                **data,
                "all_properties": all_properties,
                "platforms": list(PlatformName),
            },
        )
    finally:
        db.close()


@app.post("/properties")
def create_property(
    name: str = Form(...),
    address: str = Form(""),
    default_cleaner: str = Form(""),
    parent_property_id: str = Form(""),
):
    db = SessionLocal()
    try:
        prop = Property(
            name=name,
            address=address or None,
            default_cleaner=default_cleaner or None,
            parent_property_id=int(parent_property_id) if parent_property_id else None,
        )
        db.add(prop)
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/properties", status_code=303)


@app.post("/properties/{property_id}/settings")
def update_property_settings(
    property_id: int,
    owner_name: str = Form(""),
    owner_email: str = Form(""),
    commission_pct: str = Form(""),
    pricing_mode: str = Form("manual"),
    cleaner_pay_type: str = Form(""),
    cleaner_pay_rate: str = Form(""),
):
    db = SessionLocal()
    try:
        prop = db.get(Property, property_id)
        if prop:
            prop.owner_name = owner_name or None
            prop.owner_email = owner_email or None
            try:
                prop.commission_pct = Decimal(commission_pct) if commission_pct.strip() else None
            except InvalidOperation:
                pass
            prop.pricing_mode = pricing_mode or "manual"
            prop.cleaner_pay_type = cleaner_pay_type or None
            try:
                prop.cleaner_pay_rate = Decimal(cleaner_pay_rate) if cleaner_pay_rate.strip() else None
            except InvalidOperation:
                pass
            if prop.owner_email and not prop.owner_portal_token:
                prop.owner_portal_token = secrets_module.token_urlsafe(24)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/properties", status_code=303)


@app.post("/properties/{property_id}/listing")
def update_property_listing(
    property_id: int,
    suburb: str = Form(""),
    photo_url: str = Form(""),
    description: str = Form(""),
    bedrooms: str = Form(""),
    bathrooms: str = Form(""),
    max_guests: str = Form(""),
    listing_url: str = Form(""),
    published: str = Form(""),
):
    db = SessionLocal()
    try:
        prop = db.get(Property, property_id)
        if prop:
            prop.suburb = suburb or None
            prop.photo_url = photo_url or None
            prop.description = description or None
            prop.bedrooms = int(bedrooms) if bedrooms.strip().isdigit() else None
            prop.bathrooms = int(bathrooms) if bathrooms.strip().isdigit() else None
            prop.max_guests = int(max_guests) if max_guests.strip().isdigit() else None
            prop.listing_url = listing_url or None
            prop.published = bool(published)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/properties", status_code=303)


@app.post("/properties/{property_id}/guest-info")
def update_guest_info(
    property_id: int,
    wifi_info: str = Form(""),
    check_in_instructions: str = Form(""),
    house_rules: str = Form(""),
    airbnb_review_url: str = Form(""),
    google_review_url: str = Form(""),
):
    db = SessionLocal()
    try:
        prop = db.get(Property, property_id)
        if prop:
            prop.wifi_info = wifi_info or None
            prop.check_in_instructions = check_in_instructions or None
            prop.house_rules = house_rules or None
            prop.airbnb_review_url = airbnb_review_url or None
            prop.google_review_url = google_review_url or None
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/properties", status_code=303)


@app.post("/properties/{property_id}/feeds")
def add_feed(property_id: int, platform: str = Form(...), url: str = Form(...)):
    db = SessionLocal()
    try:
        feed = IcalFeed(property_id=property_id, platform=PlatformName(platform), url=url)
        db.add(feed)
        db.commit()
        db.refresh(feed)
        # Sync immediately so the new feed's data — and any conflicts or cleaning
        # tasks it creates — shows up without waiting for the schedule.
        sync_feed(db, feed)
        recompute_conflicts(db)
        sync_cleaning_tasks(db)
    finally:
        db.close()
    return RedirectResponse(url="/properties", status_code=303)


@app.post("/feeds/{feed_id}/delete")
def delete_feed(feed_id: int):
    db = SessionLocal()
    try:
        feed = db.get(IcalFeed, feed_id)
        if feed:
            db.delete(feed)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/properties", status_code=303)


@app.post("/properties/{property_id}/blocks")
def create_block(property_id: int, start_date: str = Form(...), end_date: str = Form(...), note: str = Form("")):
    db = SessionLocal()
    try:
        from app.operations.stock import lock_stock,refresh_conflicts
        from app.operations.reservations import stay_dates
        lock_stock(db)
        if not db.get(Property,property_id):raise HTTPException(404,'Property not found')
        try:start,end=stay_dates(start_date,end_date)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        block = Booking(
            property_id=property_id,
            platform=PlatformName.other,
            uid=f"block-{uuid.uuid4()}",
            summary=note or "Blocked",
            check_in=start,
            check_out=end,
            status=BookingStatus.confirmed,
            is_block=True,
        )
        db.add(block)
        refresh_conflicts(db)
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/properties", status_code=303)


@app.post("/blocks/{booking_id}/delete")
def delete_block(booking_id: int):
    db = SessionLocal()
    try:
        from app.operations.stock import lock_stock,refresh_conflicts
        lock_stock(db)
        booking = db.get(Booking, booking_id)
        if booking and booking.is_block:
            booking.status=BookingStatus.cancelled
            refresh_conflicts(db)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/properties", status_code=303)


@app.get("/ical/{property_id}/{token}.ics")
def property_ical_export(property_id: int, token: str):
    db = SessionLocal()
    try:
        prop = db.get(Property, property_id)
        if not prop or not prop.ical_export_token or prop.ical_export_token != token:
            return Response(status_code=404)
        from app.sync.ical_export import build_resource_ics
        try:content=build_resource_ics(db,property_id)
        except ValueError:return Response(status_code=503,headers={'Retry-After':'60'})
        return Response(content=content, media_type="text/calendar")
    finally:
        db.close()


@app.get("/cleaning")
def cleaning_page(request: Request):
    db = SessionLocal()
    try:
        tasks = (
            db.query(CleaningTask)
            .filter(CleaningTask.status != CleaningStatus.done, CleaningTask.cancelled_at.is_(None))
            .order_by(CleaningTask.due_date)
            .all()
        )
        done_tasks = (
            db.query(CleaningTask)
            .filter(CleaningTask.status == CleaningStatus.done)
            .order_by(CleaningTask.due_date.desc())
            .limit(20)
            .all()
        )
        cleaners = db.query(Cleaner).filter(Cleaner.active.is_(True)).order_by(Cleaner.name).all()

        # Linen needs: bed count per property, summed by due date, for the
        # upcoming week — lets staff plan pickup/delivery in one glance
        # instead of counting checkouts by hand every morning.
        linen_by_date: dict = {}
        for t in tasks:
            beds = (t.property.bedrooms if t.property and t.property.bedrooms else 1)
            day = t.due_date.date()
            linen_by_date[day] = linen_by_date.get(day, 0) + beds
        linen_rows = sorted(linen_by_date.items())

        return templates.TemplateResponse(
            "cleaning.html",
            {
                "request": request,
                "tasks": tasks,
                "done_tasks": done_tasks,
                "now": datetime.utcnow(),
                "cleaners": cleaners,
                "linen_rows": linen_rows,
            },
        )
    finally:
        db.close()


@app.post("/cleaning/{task_id}/assign")
def assign_cleaning_task(task_id: int, assignee: str = Form(""), cleaner_id: str = Form("")):
    db = SessionLocal()
    try:
        task = db.get(CleaningTask, task_id)
        if task and task.cancelled_at is None:
            task.assignee = assignee or None
            task.cleaner_id = int(cleaner_id) if cleaner_id else None
            if task.status != CleaningStatus.done:
                task.status = CleaningStatus.assigned if (assignee or cleaner_id) else CleaningStatus.pending
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/cleaning", status_code=303)


@app.post("/cleaning/{task_id}/quality-check")
def toggle_quality_check(task_id: int, quality_notes: str = Form("")):
    db = SessionLocal()
    try:
        task = db.get(CleaningTask, task_id)
        if task and task.cancelled_at is None:
            task.quality_checked = not task.quality_checked
            task.quality_notes = quality_notes or None
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/cleaning", status_code=303)


@app.post("/cleaners")
def create_cleaner(
    name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    pay_type: str = Form(""),
    pay_rate: str = Form(""),
):
    db = SessionLocal()
    try:
        cleaner = Cleaner(
            name=name,
            phone=phone or None,
            email=email or None,
            pay_type=pay_type or None,
            portal_token=secrets_module.token_urlsafe(24),
        )
        try:
            cleaner.pay_rate = Decimal(pay_rate) if pay_rate.strip() else None
        except InvalidOperation:
            pass
        db.add(cleaner)
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/cleaning", status_code=303)


@app.get("/cleaner/{token}")
def cleaner_portal(request: Request, token: str):
    db = SessionLocal()
    try:
        cleaner = db.query(Cleaner).filter(Cleaner.portal_token == token).first()
        if not cleaner:
            return templates.TemplateResponse(
                "cleaner_portal.html", {"request": request, "cleaner": None, "tasks": []}
            )
        tasks = (
            db.query(CleaningTask)
            .filter(CleaningTask.cleaner_id == cleaner.id, CleaningTask.status != CleaningStatus.done, CleaningTask.cancelled_at.is_(None))
            .order_by(CleaningTask.due_date)
            .all()
        )
        return templates.TemplateResponse(
            "cleaner_portal.html",
            {"request": request, "cleaner": cleaner, "tasks": tasks, "token": token},
        )
    finally:
        db.close()


@app.post("/cleaner/{token}/tasks/{task_id}/done")
def cleaner_portal_complete(token: str, task_id: int):
    db = SessionLocal()
    try:
        cleaner = db.query(Cleaner).filter(Cleaner.portal_token == token).first()
        task = db.get(CleaningTask, task_id)
        if cleaner and task and task.cleaner_id == cleaner.id and task.cancelled_at is None:
            task.status = CleaningStatus.done
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url=f"/cleaner/{token}", status_code=303)


@app.post("/cleaning/{task_id}/done")
def complete_cleaning_task(task_id: int):
    db = SessionLocal()
    try:
        task = db.get(CleaningTask, task_id)
        if task and task.cancelled_at is None:
            task.status = CleaningStatus.done
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/cleaning", status_code=303)


@app.post("/cleaning/{task_id}/reopen")
def reopen_cleaning_task(task_id: int):
    db = SessionLocal()
    try:
        task = db.get(CleaningTask, task_id)
        if task and task.cancelled_at is None:
            task.status = CleaningStatus.assigned if task.assignee else CleaningStatus.pending
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/cleaning", status_code=303)


@app.get("/transactions")
def transactions_page(request: Request, error: str = ""):
    db = SessionLocal()
    try:
        unmatched = (
            db.query(Transaction)
            .filter(Transaction.matched.is_(False))
            .order_by(Transaction.occurred_at.desc())
            .all()
        )
        matched = (
            db.query(Transaction)
            .filter(Transaction.matched.is_(True))
            .order_by(Transaction.occurred_at.desc())
            .limit(30)
            .all()
        )
        suggestions = {t.id: suggest_bookings(db, t) for t in unmatched}
        properties = db.query(Property).order_by(Property.name).all()
        summary = monthly_summary(db)
        expenses = db.query(Expense).order_by(Expense.occurred_at.desc()).limit(30).all()
        statements = db.query(OwnerStatement).order_by(OwnerStatement.period.desc()).limit(30).all()
        company_summary = company_monthly_summary(db)

        return templates.TemplateResponse(
            "transactions.html",
            {
                "request": request,
                "unmatched": unmatched,
                "matched": matched,
                "suggestions": suggestions,
                "properties": properties,
                "summary": summary,
                "error": error,
                "expenses": expenses,
                "statements": statements,
                "company_summary": company_summary,
                "gst_registered": gst_registered(),
            },
        )
    finally:
        db.close()


@app.post("/transactions/upload")
def upload_transactions(
    file: UploadFile,
    source: str = Form(...),
    property_id: str = Form(""),
):
    db = SessionLocal()
    try:
        content = file.file.read().decode("utf-8-sig")
        try:
            rows = parse_csv(content)
        except ImportError_ as exc:
            return RedirectResponse(url=f"/transactions?error={exc}", status_code=303)

        prop_id = int(property_id) if property_id else None
        for row in rows:
            db.add(
                Transaction(
                    property_id=prop_id,
                    source=source,
                    amount=row["amount"],
                    occurred_at=row["occurred_at"],
                    raw_note=row["note"],
                )
            )
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/transactions", status_code=303)


@app.post("/transactions/{transaction_id}/match")
def match_transaction(transaction_id: int, booking_id: int = Form(...)):
    db = SessionLocal()
    try:
        transaction = db.get(Transaction, transaction_id)
        booking = db.get(Booking, booking_id)
        if transaction and booking:
            transaction.booking_id = booking.id
            transaction.property_id = booking.property_id
            transaction.matched = True
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/transactions", status_code=303)


@app.post("/transactions/{transaction_id}/unmatch")
def unmatch_transaction(transaction_id: int):
    db = SessionLocal()
    try:
        transaction = db.get(Transaction, transaction_id)
        if transaction:
            transaction.booking_id = None
            transaction.matched = False
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/transactions", status_code=303)


@app.post("/transactions/{transaction_id}/confirm-received")
def confirm_transaction_received(transaction_id: int):
    db = SessionLocal()
    try:
        transaction = db.get(Transaction, transaction_id)
        if transaction:
            transaction.confirmed_received = not transaction.confirmed_received
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/transactions", status_code=303)


@app.post("/properties/{property_id}/expenses")
def create_expense(
    property_id: int,
    description: str = Form(...),
    amount: str = Form(...),
    category: str = Form(""),
    occurred_at: str = Form(""),
):
    db = SessionLocal()
    try:
        try:
            expense_amount = Decimal(amount)
        except InvalidOperation:
            return RedirectResponse(url="/transactions", status_code=303)
        try:
            when = datetime.strptime(occurred_at, "%Y-%m-%d") if occurred_at else datetime.utcnow()
        except ValueError:
            when = datetime.utcnow()
        db.add(
            Expense(
                property_id=property_id,
                description=description,
                amount=expense_amount,
                category=category or None,
                occurred_at=when,
            )
        )
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/transactions", status_code=303)


@app.post("/expenses/{expense_id}/delete")
def delete_expense(expense_id: int):
    db = SessionLocal()
    try:
        from app.models import WorkOrderCost
        if db.query(WorkOrderCost).filter_by(expense_id=expense_id).first():
            raise HTTPException(409, 'Task-generated expenses retain their history; record a reviewed accounting adjustment instead')
        expense = db.get(Expense, expense_id)
        if expense:
            db.delete(expense)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/transactions", status_code=303)


@app.post("/properties/{property_id}/statements/generate")
def generate_owner_statement(property_id: int, period: str = Form(...)):
    """period is 'YYYY-MM'. Recomputes gross/expenses/commission from actual
    data every time it's called (until finalized), so re-generating just
    refreshes the numbers rather than creating duplicates."""
    db = SessionLocal()
    try:
        prop = db.get(Property, property_id)
        if not prop:
            return RedirectResponse(url="/transactions", status_code=303)

        try:
            _generate_statement(db, prop, period)
        except ValueError:
            raise HTTPException(400, "Invalid statement period") from None
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/transactions", status_code=303)


@app.post("/statements/{statement_id}/adjust")
def adjust_owner_statement(statement_id: int, adjustment_amount: str = Form("0"), adjustment_note: str = Form("")):
    db = SessionLocal()
    try:
        statement = db.get(OwnerStatement, statement_id)
        if statement and not statement.finalized:
            try:
                statement.adjustment_amount = Decimal(adjustment_amount) if adjustment_amount.strip() else Decimal("0")
            except InvalidOperation:
                pass
            statement.adjustment_note = adjustment_note or None
            statement.net_payout = (
                statement.gross_revenue
                - statement.total_expenses
                - statement.commission_amount
                + statement.adjustment_amount
            )
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/transactions", status_code=303)


@app.post("/statements/{statement_id}/finalize")
def finalize_owner_statement(statement_id: int):
    db = SessionLocal()
    try:
        statement = db.get(OwnerStatement, statement_id)
        if statement:
            try:
                finalize_statement(db, statement)
            except ValueError:
                raise HTTPException(409, "Statement needs missing amounts or commission settings") from None
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/transactions", status_code=303)


@app.get("/owner/{token}")
def owner_portal(request: Request, token: str):
    db = SessionLocal()
    try:
        prop = db.query(Property).filter(Property.owner_portal_token == token).first()
        if not prop:
            return templates.TemplateResponse(
                "owner_portal.html", {"request": request, "property": None}
            )
        statements = (
            db.query(OwnerStatement)
            .filter(OwnerStatement.property_id == prop.id, OwnerStatement.finalized.is_(True))
            .order_by(OwnerStatement.period.desc())
            .limit(12)
            .all()
        )
        upcoming = (
            db.query(Booking)
            .filter(
                Booking.property_id == prop.id,
                Booking.status == BookingStatus.confirmed,
                Booking.check_out >= datetime.utcnow(),
            )
            .order_by(Booking.check_in)
            .limit(10)
            .all()
        )
        return templates.TemplateResponse(
            "owner_portal.html",
            {"request": request, "property": prop, "statements": statements, "upcoming": upcoming},
        )
    finally:
        db.close()
