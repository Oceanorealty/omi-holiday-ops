"""Per-field portal release rules. No rule preserves the existing portal display."""
import hashlib
import json
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from sqlalchemy.exc import IntegrityError
from app.models import (Booking,BookingAccount,GuestAccessRule,GuestAccountClearance,GuestCheckin,Property)
from app.operations.agreements import booking_fingerprint,latest_agreement,document_state
from app.operations.billing import entries,totals
from app.operations.guest_checkin import is_current
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError,form_token,verify_token

FIELDS={'door_code':'Door code','wifi_info':'WiFi','check_in_instructions':'Check-in instructions'}


def rule_token(actor,property_id,field,row):
    return form_token(actor,SimpleNamespace(id=property_id,version=row.version if row else 0),purpose='guest-access:'+field)


def save_rule(db,*,actor,property_id,field,token,opening_hours,require_arrival,require_agreement,require_account_clear,hide_after_checkout,note):
    if field not in FIELDS:raise TaskError('Unknown portal field')
    try:
        hours=int(opening_hours) if opening_hours.strip() else None
        if hours is not None and (not 0<=hours<=8760 or str(hours)!=opening_hours.strip()):raise ValueError()
    except (ValueError,AttributeError):raise TaskError('Use whole hours from 0 to 8760, or leave blank') from None
    expected=verify_token(token,actor,property_id,purpose='guest-access:'+field)
    try:
        lock_stock(db);db.expire_all()
        if not db.get(Property,property_id):raise TaskError('Property not found')
        row=db.query(GuestAccessRule).filter_by(property_id=property_id,field=field).first()
        if expected!=(row.version if row else 0):raise TaskError('Portal rule changed; reload before saving')
        if not row:
            row=GuestAccessRule(property_id=property_id,field=field,version=0);db.add(row)
        row.opening_hours=hours;row.require_arrival=bool(require_arrival);row.require_agreement=bool(require_agreement)
        row.require_account_clear=bool(require_account_clear);row.hide_after_checkout=bool(hide_after_checkout)
        row.version=expected+1;row.updated_at=datetime.utcnow()
        record_command(db,token,'guest-access:'+field,actor,note,{'property_id':property_id,'field':field,'revision':row.version,
            'opening_hours':hours,'require_arrival':row.require_arrival,'require_agreement':row.require_agreement,
            'require_account_clear':row.require_account_clear,'hide_after_checkout':row.hide_after_checkout})
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('This rule submission was already saved') from None
    except Exception:
        db.rollback();raise


def ledger_state(db,booking):
    account=db.get(BookingAccount,booking.id)
    if not account:return None,None,None
    rows=entries(db,booking.id);summary=totals(rows)
    encoded=json.dumps([account.currency,[[r.id,r.kind,str(r.amount),r.payment_id,r.voids_id] for r in rows]],sort_keys=True)
    return account,summary,hashlib.sha256(encoded.encode()).hexdigest()


def clearance_token(db,actor,booking):
    account,summary,ledger_hash=ledger_state(db,booking)
    if not account:return ''
    return form_token(actor,SimpleNamespace(id=booking.id,version=0),
        purpose='guest-account-review:'+booking_fingerprint(booking)+':'+ledger_hash)


def account_is_clear(db,booking):
    account,summary,ledger_hash=ledger_state(db,booking)
    review=db.get(GuestAccountClearance,booking.id)
    return bool(account and summary['balance']<=0 and any(r.kind=='charge' for r in summary['active'])
        and review and review.booking_fingerprint==booking_fingerprint(booking) and review.ledger_hash==ledger_hash)


def review_account(db,*,actor,booking_id,token,note):
    try:
        lock_stock(db);db.expire_all();booking=db.get(Booking,booking_id)
        if not booking or booking.is_block:raise TaskError('Guest reservation not found')
        account,summary,ledger_hash=ledger_state(db,booking)
        if not account:raise TaskError('Create and reconcile the booking account first')
        verify_token(token,actor,booking_id,purpose='guest-account-review:'+booking_fingerprint(booking)+':'+ledger_hash)
        if summary['balance']>0 or not any(r.kind=='charge' for r in summary['active']):
            raise TaskError('Review actual charges and clear the account balance before confirming')
        row=db.get(GuestAccountClearance,booking_id)
        if not row:row=GuestAccountClearance(booking_id=booking_id);db.add(row)
        row.booking_fingerprint=booking_fingerprint(booking);row.ledger_hash=ledger_hash;row.actor=actor;row.reviewed_at=datetime.utcnow()
        record_command(db,token,'guest-account-review',actor,note,{'booking_id':booking_id,'ledger_hash':ledger_hash,'booking_fingerprint':row.booking_fingerprint})
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('This account review was already recorded') from None
    except Exception:
        db.rollback();raise


def portal_fields(db,booking,now=None):
    if not booking:return {'released_fields':{},'guest_access_status':{}}
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('Use an aware current time')
    rules={r.field:r for r in db.query(GuestAccessRule).filter_by(property_id=booking.property_id).all()}
    status={};released={}
    for field in FIELDS:
        value=booking.door_code if field=='door_code' else getattr(booking.property,field)
        row=rules.get(field);reasons=[]
        if row:
            try:
                zone=ZoneInfo(booking.property.time_zone)
                start=booking.check_in.replace(tzinfo=zone).astimezone(timezone.utc)
                end=booking.check_out.replace(tzinfo=zone).astimezone(timezone.utc)
                if end<=start:reasons.append('Stay dates need team review')
                if row.opening_hours is not None and now<start-timedelta(hours=row.opening_hours):
                    reasons.append('Available closer to check-in')
                if row.hide_after_checkout and now>=end:reasons.append('Stay has ended')
            except (ValueError,ZoneInfoNotFoundError,TypeError):reasons.append('Stay timezone needs team review')
            if row.require_arrival and not is_current(booking,db.query(GuestCheckin).filter_by(booking_id=booking.id).first()):
                reasons.append('Submit current arrival details')
            if row.require_agreement:
                agreement=latest_agreement(db,booking.id)
                if not agreement or document_state(db,agreement,booking)!='acknowledged':reasons.append('Current stay agreement needs acknowledgement')
            if row.require_account_clear and not account_is_clear(db,booking):reasons.append('Booking account needs team review or payment')
        status[field]={'allowed':not reasons,'reasons':reasons,'configured':row is not None}
        released[field]=value if not reasons else None
    return {'released_fields':released,'guest_access_status':status}
