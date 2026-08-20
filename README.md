# Omi Holiday — Operations

The Omi Holiday operations automation system. Live at
https://ops.omiholiday.com (deployed on Render, free tier + Neon Postgres).

- **Phase 1** — unified booking calendar, syncing every platform's iCal feed
  so double-bookings and availability are visible in one place instead of
  tracked by hand across Airbnb / Booking.com / Ctrip.
- **Phase 2** — automated guest email at each booking lifecycle stage
  (confirmed, pre-arrival, check-in day, post-checkout).

See `/Users/alanpan/.claude/plans/temporal-questing-stream.md` for the full
phased roadmap (cleaning dispatch and financial reconciliation are Phase 3–4).

## Run it locally

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

Then open http://localhost:8000. Without `DATABASE_URL` set it falls back to
a local SQLite file; without `SMTP_*` set, guest emails are logged as
`skipped_no_smtp` instead of actually sending — so the whole pipeline runs
end to end without needing real credentials for local dev.

## Environment variables (set in Render → Environment)

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string (Neon). Required in production — Render's free tier has no persistent disk, so without this, data is wiped on every restart. |
| `SMTP_HOST` | Mail server host, e.g. `mail.omiholiday.com` |
| `SMTP_PORT` | Usually `587` (STARTTLS) |
| `SMTP_USER` | Full mailbox address, e.g. `bookings@omiholiday.com` |
| `SMTP_PASSWORD` | Mailbox password |
| `SMTP_FROM` | Optional; defaults to `SMTP_USER` if unset |

## How to use

1. Go to **Properties**, add a property.
2. For each property, paste in its iCal feed URL from each platform
   (Airbnb: listing → Availability → Export Calendar; Booking.com: Rates &
   Availability → Sync Calendars; Ctrip: similar export option in the
   extranet). Feeds sync immediately when added, and every 30 minutes after
   that.
3. Go to **Bookings** to see everything in one calendar. Overlapping
   bookings on the same property (a double-booking across platforms) are
   flagged automatically. Fill in each booking's guest email inline —
   iCal feeds usually don't include it, so it has to be added by hand
   before automated messages can send.
4. Go to **Message Templates** to edit the four guest emails (placeholders:
   `{guest_name}`, `{property_name}`, `{check_in}`, `{check_out}`).
5. Click **Sync now** any time to force an immediate refresh — this also
   sends any due guest messages, same as the 30-minute background job.

## Data model

`app/models.py` — properties, iCal feeds, bookings, guests, message
templates/logs, plus `CleaningTask` and `Transaction` tables stubbed in now
so Phase 3/4 don't need a schema migration later.
