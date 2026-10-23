"""Quote -> guest request -> reviewed local booking, without pretending to take payment."""
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from app.auth import _secret, _sign
from app.models import Property, ReservationRequest
from app.operations.reservations import money, reservation_token, save_reservation, stay_dates
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError, verify_token
from app.public_api import get_quote


def parse_quote(token):
    if not _secret() or not isinstance(token,str) or len(token)>4096:
        raise TaskError('A configured, valid signed quote is required')
    try:
        payload,signature=token.rsplit('.',1)
        if not hmac.compare_digest(_sign(payload),token):raise ValueError()
        prefix,property_id,check_in,check_out,guests,amount,expiry,nonce=payload.split('|')
        if prefix!='quote-v2' or not re.fullmatch(r'[a-f0-9]{32}',nonce):raise ValueError()
        expires=datetime.fromisoformat(expiry)
        if expires.tzinfo is None or expires<=datetime.now(timezone.utc):raise ValueError()
        property_id,guests=int(property_id),int(guests)
        if property_id<1 or guests<1:raise ValueError()
        stay_dates(check_in,check_out)
        total=money(amount)
        if total is None:raise ValueError()
        return dict(property_id=property_id,check_in=check_in,check_out=check_out,guests=guests,total=total)
    except (ValueError,TypeError,OverflowError):
        raise TaskError('Quote is invalid or expired; request a new quote') from None


def validate_current_quote(db,values):
    from fastapi import HTTPException
    prop=db.get(Property,values['property_id'])
    if not prop or not prop.published or stay_dates(values['check_in'],values['check_out'])[0].date()<datetime.now(ZoneInfo(prop.time_zone)).date():
        raise TaskError('This property or stay is no longer available')
    try:
        current=get_quote(property_id=values['property_id'],check_in=values['check_in'],check_out=values['check_out'],guests=values['guests'],db=db)['result']
    except HTTPException:
        raise TaskError('This property or stay is no longer available') from None
    if not current['bookable']:
        raise TaskError('Stay is no longer bookable: '+str(current['reason']))
    if money(current['total'])!=values['total']:
        raise TaskError('The price changed; request a new quote before proceeding')


def submit_request(db,*,quote_token,guest_name,guest_email):
    values=parse_quote(quote_token)
    guest_name,guest_email=guest_name.strip(),guest_email.strip()
    if not 1<=len(guest_name)<=160 or not 3<=len(guest_email)<=254 or guest_email.count('@')!=1 or any(c.isspace() for c in guest_email):
        raise TaskError('Enter a valid guest name and email')
    digest=hashlib.sha256(quote_token.encode()).hexdigest()
    payload_digest=hashlib.sha256(json.dumps([guest_name,guest_email.casefold()]).encode()).hexdigest()
    try:
        locked=db.execute(update(Property).where(Property.id==values['property_id']).values(name=Property.name))
        if locked.rowcount!=1:raise TaskError('Property not available')
        db.expire_all()
        existing=db.query(ReservationRequest).filter_by(quote_digest=digest).first()
        if existing:
            if existing.payload_digest!=payload_digest:raise TaskError('This quote was already used with different guest details')
            db.rollback()
            return existing.reference
        validate_current_quote(db,values)
        row=ReservationRequest(reference='request-'+secrets.token_hex(16),quote_digest=digest,payload_digest=payload_digest,
            property_id=values['property_id'],check_in=values['check_in'],check_out=values['check_out'],guests=values['guests'],
            quoted_amount=values['total'],guest_name=guest_name,guest_email=guest_email)
        db.add(row);db.flush()
        record_command(db,quote_token,'reservation-request','guest', 'Guest submitted a reservation request; no inventory reserved',{'request_id':row.id})
        db.commit()
        return row.reference
    except IntegrityError:
        db.rollback();raise TaskError('Request already submitted; retry with the same quote and guest details') from None
    except Exception:
        db.rollback();raise


def review_request(db,*,actor,entity_id,token,action,note):
    expected=verify_token(token,actor,entity_id,purpose='reservation-request-review')
    if action not in ('approve','reject'):raise TaskError('Unknown review action')
    try:
        locked=db.execute(update(ReservationRequest).where(ReservationRequest.id==entity_id,
            ReservationRequest.version==expected,ReservationRequest.state=='pending')
            .values(version=expected+1).execution_options(synchronize_session=False))
        if locked.rowcount!=1:raise TaskError('Request changed or was already reviewed; reload before saving')
        db.expire_all();row=db.get(ReservationRequest,entity_id)
        if action=='approve':
            from app.operations.stock import lock_stock
            lock_stock(db)
            db.execute(update(Property).where(Property.id==row.property_id).values(name=Property.name))
            db.expire_all();row=db.get(ReservationRequest,entity_id)
            validate_current_quote(db,dict(property_id=row.property_id,check_in=row.check_in,check_out=row.check_out,
                guests=row.guests,total=row.quoted_amount))
            row.booking_id=save_reservation(db,actor=actor,entity_id=0,token=reservation_token(actor),property_id=row.property_id,
                check_in=row.check_in,check_out=row.check_out,amount=str(row.quoted_amount),guest_name=row.guest_name,
                guest_email=row.guest_email,note=note,commit=False)
            row.state='approved'
        else:
            row.state='rejected'
        record_command(db,token,'request-review',actor,note,{'request_id':row.id,'action':action,'booking_id':row.booking_id})
        db.commit()
        return row.booking_id
    except IntegrityError:
        db.rollback();raise TaskError('Request was already processed; reload before saving') from None
    except Exception:
        db.rollback();raise
