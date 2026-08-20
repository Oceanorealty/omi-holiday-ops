from datetime import datetime
from decimal import Decimal, InvalidOperation

import os
import secrets as secrets_module

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import migrate
from app.auth import SESSION_COOKIE, SESSION_MAX_AGE, SessionAuthMiddleware, make_session_cookie
from app.comms.templates import seed_default_templates
from app.comms.triggers import process_due_messages
from app.db import Base, SessionLocal, engine
from app.models import (
    Booking,
    BookingStatus,
    CleaningStatus,
    CleaningTask,
    Guest,
    IcalFeed,
    MessageLog,
    MessageTemplate,
    PlatformName,
    Property,
    Transaction,
)
from app.reconcile.importer import ImportError_, parse_csv
from app.reconcile.matching import suggest_bookings
from app.reconcile.summary import monthly_summary
from app.sync.cleaning import sync_cleaning_tasks
from app.sync.ical_sync import recompute_conflicts, sync_all, sync_feed

migrate.run(engine)
Base.metadata.create_all(bind=engine)

with SessionLocal() as _db:
    seed_default_templates(_db)

app = FastAPI(title="Omi Holiday — Operations")
app.add_middleware(SessionAuthMiddleware)
# Outermost middleware — lets omiholiday.com fetch /api/public/* directly
# from the visitor's own browser (a different origin), read-only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://omiholiday.com", "https://www.omiholiday.com"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def _scheduled_sync():
    db = SessionLocal()
    try:
        sync_all(db)
        sync_cleaning_tasks(db)
        process_due_messages(db)
    finally:
        db.close()


scheduler = BackgroundScheduler()
scheduler.add_job(_scheduled_sync, "interval", minutes=30, id="ical_sync")


@app.on_event("startup")
def start_scheduler():
    if not scheduler.running:
        scheduler.start()


@app.on_event("shutdown")
def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/login")
def login_page(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(
        "login.html", {"request": request, "next": next, "error": error}
    )


@app.post("/login")
def login_submit(username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    admin_user = os.environ.get("ADMIN_USER", "")
    admin_password = os.environ.get("ADMIN_PASSWORD", "")

    user_ok = secrets_module.compare_digest(username, admin_user)
    pass_ok = secrets_module.compare_digest(password, admin_password)

    if user_ok and pass_ok:
        response = RedirectResponse(url=next or "/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            make_session_cookie(),
            max_age=SESSION_MAX_AGE,
            httponly=True,
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
            .filter(Booking.status == BookingStatus.confirmed)
            .order_by(Booking.check_in)
            .all()
        )
        conflicts = [b for b in bookings if b.has_conflict]
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
        process_due_messages(db)
    finally:
        db.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/bookings/{booking_id}/amount")
def update_booking_amount(booking_id: int, amount: str = Form("")):
    db = SessionLocal()
    try:
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


@app.post("/bookings/{booking_id}/guest")
def update_guest(booking_id: int, name: str = Form(""), email: str = Form("")):
    db = SessionLocal()
    try:
        booking = db.get(Booking, booking_id)
        if booking:
            if not booking.guest:
                booking.guest = Guest()
                db.add(booking.guest)
            booking.guest.name = name or None
            booking.guest.email = email or None
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
    active: str = Form(""),
):
    db = SessionLocal()
    try:
        template = db.get(MessageTemplate, template_id)
        if template:
            template.subject = subject
            template.body = body
            template.active = bool(active)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/templates", status_code=303)


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
def properties_page(request: Request):
    db = SessionLocal()
    try:
        properties = db.query(Property).order_by(Property.name).all()
        return templates.TemplateResponse(
            "properties.html",
            {"request": request, "properties": properties, "platforms": list(PlatformName)},
        )
    finally:
        db.close()


@app.post("/properties")
def create_property(name: str = Form(...), address: str = Form(""), default_cleaner: str = Form("")):
    db = SessionLocal()
    try:
        prop = Property(name=name, address=address or None, default_cleaner=default_cleaner or None)
        db.add(prop)
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


@app.get("/cleaning")
def cleaning_page(request: Request):
    db = SessionLocal()
    try:
        tasks = (
            db.query(CleaningTask)
            .filter(CleaningTask.status != CleaningStatus.done)
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
        return templates.TemplateResponse(
            "cleaning.html",
            {"request": request, "tasks": tasks, "done_tasks": done_tasks, "now": datetime.utcnow()},
        )
    finally:
        db.close()


@app.post("/cleaning/{task_id}/assign")
def assign_cleaning_task(task_id: int, assignee: str = Form("")):
    db = SessionLocal()
    try:
        task = db.get(CleaningTask, task_id)
        if task:
            task.assignee = assignee or None
            if task.status != CleaningStatus.done:
                task.status = CleaningStatus.assigned if assignee else CleaningStatus.pending
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/cleaning", status_code=303)


@app.post("/cleaning/{task_id}/done")
def complete_cleaning_task(task_id: int):
    db = SessionLocal()
    try:
        task = db.get(CleaningTask, task_id)
        if task:
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
        if task:
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
