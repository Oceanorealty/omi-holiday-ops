from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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
)
from app.sync.cleaning import sync_cleaning_tasks
from app.sync.ical_sync import recompute_conflicts, sync_all, sync_feed

Base.metadata.create_all(bind=engine)

with SessionLocal() as _db:
    seed_default_templates(_db)

app = FastAPI(title="Omi Holiday — Operations")
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
