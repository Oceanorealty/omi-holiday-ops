"""Signed, expiring, snapshot-bound admin decisions. No external sends or retries."""
import base64
import binascii
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from app.auth import _secret
from app.models import (Booking, BookingStatus, CleaningStatus, CleaningTask, MessageDelivery,
    MessageLog, MessageStatus, OperationsReview, OwnerStatement, StatementDelivery, TriggerEvent)

KINDS = {"source": Booking, "cleaning": CleaningTask, "message": MessageDelivery, "statement": StatementDelivery}
ACTIONS = {"source": ("keep_active", "confirm_cancelled"),
           "cleaning": ("keep_schedule", "use_checkout"),
           "message": ("record_sent", "close_without_resend"),
           "statement": ("record_sent", "close_without_resend")}


class ReviewError(ValueError):
    pass


def snapshot(db, kind, entity_id):
    if kind not in KINDS:
        raise ReviewError("Unknown review type")
    row = db.get(KINDS[kind], entity_id)
    if not row:
        raise ReviewError("Record not found")
    if kind == "source":
        if not row.source_missing_since or row.status != BookingStatus.confirmed:
            raise ReviewError("Source review is no longer open")
        data = dict(status=row.status.value, missing=str(row.source_missing_since),
                    last_seen=str(row.source_last_seen_at), start=str(row.check_in), end=str(row.check_out), feed=row.source_feed_id)
    elif kind == "cleaning":
        booking = db.get(Booking, row.booking_id)
        if not row.schedule_review_required or not booking or booking.status != BookingStatus.confirmed:
            raise ReviewError("Schedule review is no longer open")
        data = dict(due=str(row.due_date), locked=row.schedule_locked, status=row.status.value,
                    checkout=str(booking.check_out), booking_id=booking.id)
    else:
        # A sending record may be in-flight. Only stale claims can be reviewed.
        if row.state != "unknown" and not (row.state == "sending" and row.created_at <= datetime.utcnow() - timedelta(hours=1)):
            raise ReviewError("Delivery is still active or already resolved")
        data = dict(state=row.state, created=str(row.created_at))
        if kind == "message":
            data.update(booking_id=row.booking_id, event=row.event, scheduled_for=row.scheduled_for)
        else:
            data.update(statement_id=row.statement_id)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return row, digest


def issue_token(db, actor, kind, entity_id, action, *, now=None):
    if not _secret() or action not in ACTIONS.get(kind, ()):
        raise ReviewError("Review configuration invalid")
    _, version = snapshot(db, kind, entity_id)
    payload = dict(actor=actor, kind=kind, id=entity_id, action=action, version=version, expires=int(now or time.time()) + 900)
    encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    mac = hmac.new(_secret().encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + mac


def _verify(token, actor, kind, entity_id, action):
    if not _secret() or len(token) > 4096:
        raise ReviewError("Invalid review token")
    try:
        encoded, mac = token.rsplit(".", 1)
        expected = hmac.new(_secret().encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        if (payload["actor"], payload["kind"], payload["id"], payload["action"]) != (actor, kind, entity_id, action):
            raise ValueError()
        if payload["expires"] <= time.time() or action not in ACTIONS.get(kind, ()):
            raise ValueError()
        return payload["version"]
    except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error):
        raise ReviewError("Invalid or expired review token") from None


def apply_review(db, *, actor, kind, entity_id, action, token, note):
    if not 10 <= len(note.strip()) <= 1000:
        raise ReviewError("Describe the evidence checked in 10–1000 characters")
    version = _verify(token, actor, kind, entity_id, action)
    try:
        if kind == 'source':
            from app.operations.stock import lock_stock
            lock_stock(db)
        # This insert serializes SQLite writers and supplies a single-use key.
        audit = OperationsReview(token_digest=hashlib.sha256(token.encode()).hexdigest(), actor=actor,
            entity_kind=kind, entity_id=entity_id, action=action, note=note.strip(),
            before_hash=version, after_hash="pending")
        db.add(audit); db.flush()
        row = db.query(KINDS[kind]).filter_by(id=entity_id).with_for_update().one_or_none()
        if row:
            db.refresh(row)
        if row and kind == "cleaning":
            linked = db.query(Booking).filter_by(id=row.booking_id).with_for_update().one()
            db.refresh(linked)
        row, current = snapshot(db, kind, entity_id)
        if current != version:
            raise ReviewError("Record changed; reload and review the latest state")
        if kind == "source":
            if action == "confirm_cancelled":
                row.status = BookingStatus.cancelled
                row.notifications_enabled = False
                if row.cleaning_task and row.cleaning_task.status != CleaningStatus.done:
                    row.cleaning_task.cancelled_at = datetime.utcnow()
                    row.cleaning_task.schedule_review_required = False
                from app.operations.stock import refresh_conflicts
                refresh_conflicts(db)
            # This is an operator decision, not a fabricated new provider observation.
            row.source_missing_since = None
            after = dict(status=row.status.value, missing=None)
        elif kind == "cleaning":
            booking = db.query(Booking).filter_by(id=row.booking_id).with_for_update().one()
            if action == "use_checkout":
                if row.status == CleaningStatus.done:
                    raise ReviewError("Completed work keeps its historical schedule")
                row.due_date, row.schedule_locked = booking.check_out, False
            else:
                row.schedule_locked = True
            row.schedule_reviewed_checkout = booking.check_out
            row.schedule_review_required = False
            after = dict(due=str(row.due_date), locked=row.schedule_locked, checkout=str(booking.check_out))
        else:
            row.state = "sent" if action == "record_sent" else "closed"
            if action == "record_sent":
                if kind == "message":
                    log = db.query(MessageLog).filter_by(booking_id=row.booking_id, trigger_event=TriggerEvent(row.event)).first()
                    if not log:
                        log = MessageLog(booking_id=row.booking_id, trigger_event=TriggerEvent(row.event))
                        db.add(log)
                    log.status, log.sent_at, log.error = MessageStatus.sent, datetime.utcnow(), None
                    row.completed_at = datetime.utcnow()
                else:
                    statement = db.get(OwnerStatement, row.statement_id)
                    if not statement:
                        raise ReviewError("Statement not found")
                    statement.sent_at = datetime.utcnow()
            after = dict(state=row.state)
        audit.after_hash = hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest()
        db.commit()
        return audit.id
    except (ReviewError, IntegrityError):
        db.rollback()
        raise ReviewError("Review was not saved; it may have changed or already been processed") from None
