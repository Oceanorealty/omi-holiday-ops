from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import Base, SessionLocal, engine
from app.models import Booking, BookingStatus, IcalFeed, PlatformName, Property
from app.sync.ical_sync import recompute_conflicts, sync_all, sync_feed

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Omi Holiday — Operations")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def _scheduled_sync():
    db = SessionLocal()
    try:
        sync_all(db)
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
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "bookings": bookings,
                "conflicts": conflicts,
                "properties": properties,
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
    finally:
        db.close()
    return RedirectResponse(url="/", status_code=303)


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
        # Sync immediately so the new feed's data — and any conflicts it creates
        # against existing bookings — shows up without waiting for the schedule.
        sync_feed(db, feed)
        recompute_conflicts(db)
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
