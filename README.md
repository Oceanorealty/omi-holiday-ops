# Omi Holiday — Operations

Phase 1 of the Omi Holiday operations automation system: a unified booking
calendar that syncs from every platform's iCal feed, so double-bookings and
availability are visible in one place instead of tracked by hand across
Airbnb / Booking.com / Ctrip.

See `/Users/alanpan/.claude/plans/temporal-questing-stream.md` for the full
phased roadmap (guest communication, cleaning dispatch, financial
reconciliation are Phase 2–4, not built yet).

## Run it

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

Then open http://localhost:8000

## How to use

1. Go to **Properties**, add a property.
2. For each property, paste in its iCal feed URL from each platform
   (Airbnb: listing → Availability → Export Calendar; Booking.com: Rates &
   Availability → Sync Calendars; Ctrip: similar export option in the
   extranet). Feeds sync immediately when added, and every 30 minutes after
   that.
3. Go to **Bookings** to see everything in one calendar. Overlapping
   bookings on the same property (a double-booking across platforms) are
   flagged automatically.
4. Click **Sync now** any time to force an immediate refresh.

## Data model

`app/models.py` — properties, iCal feeds, bookings, guests, plus
`CleaningTask` and `Transaction` tables stubbed in now so Phase 3/4 don't
need a schema migration later.
