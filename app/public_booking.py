"""Sandbox direct checkout. Disabled unless explicitly enabled on a test deployment.

Payment snapshots live in the database, not in Stripe metadata. Database locks
serialize successful website payments; OTA imports can still reveal conflicts
later because iCal is asynchronous. This is not an OTA inventory reservation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import _secret, _sign
from app.db import get_db
from app.models import Booking, BookingStatus, Guest, PlatformName, Property, PublicPayment, PublicCheckoutHold
from app.public_api import _has_overlapping_booking, _money
from app.pricing import price_breakdown
from app.operations.inventory import restriction_reason
from app.operations.stock import lock_stock, refresh_conflicts
from app.operations.tasks import TaskError
from app.operations.checkout_holds import HOLD_MINUTES, hold_status

router = APIRouter(prefix="/api/public", tags=["public-booking"])


def _configured():
    # Test-only rollout: a production/live Stripe key cannot enable this module.
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if (os.environ.get("PUBLIC_BOOKING_ENABLED") != "test"
            or os.environ.get("OPS_PAYMENT_ENVIRONMENT") != "sandbox"
            or os.environ.get("OPS_SCHEDULER_ENABLED") != "0"
            or os.environ.get("OPS_MESSAGE_DISPATCH_ENABLED", "0") != "0"
            or os.environ.get("OPS_OWNER_EMAIL_ENABLED", "0") != "0"
            or not key.startswith(("sk_test_", "rk_test_")) or len(_secret()) < 32):
        raise HTTPException(503, "test_payments_not_configured")
    return key


class GuestInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254)
    phone: str = Field(default="", max_length=50)
    language: Literal["en", "zh"] = "en"

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if value.count("@") != 1 or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("Invalid email")
        local, domain = value.split("@")
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("Invalid email")
        return local + "@" + domain.lower()


class CheckoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checkout_id: UUID
    property_id: int = Field(gt=0, strict=True)
    check_in: date
    check_out: date
    guests: int = Field(ge=1, le=100, strict=True)
    guest: GuestInput
    quote_token: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def dates_valid(self):
        if not 0 < (self.check_out - self.check_in).days <= 366:
            raise ValueError("Invalid stay dates")
        return self


def _access_token(checkout_id):
    return _sign("public-booking-v1|" + checkout_id)


def _lock(db):
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
    lock_stock(db)
    db.expire_all()


def _verify_quote(body, total):
    try:
        payload, mac = body.quote_token.rsplit(".", 1)
        if not hmac.compare_digest(_sign(payload), body.quote_token):
            raise ValueError()
        prefix, pid, ci, co, guests, amount, expiry, nonce = payload.split("|")
        if prefix != "quote-v2" or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
            raise ValueError()
        expires_at = datetime.fromisoformat(expiry)
        if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
            raise ValueError()
        if (pid, ci, co, guests, amount) != (
            str(body.property_id), body.check_in.isoformat(), body.check_out.isoformat(),
            str(body.guests), _money(total),
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(409, "quote_expired_or_changed") from None


def _fingerprint(body):
    # Token expiry changes between re-quotes; immutable booking/customer fields
    # must not change when retrying a checkout ID.
    data = body.model_dump(mode="json", exclude={"quote_token"})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _checkout_result(db, payment, intent):
    if intent.get("livemode") is not False:
        raise HTTPException(503, "expected_test_payment")
    return {"status": "success", "result": {
        "checkout_id": payment.id,
        "payment_intent_id": intent["id"],
        "client_secret": intent["client_secret"],
        "access_token": _access_token(payment.id),
        "amount": _money(payment.amount), "currency": payment.currency,
        "state": payment.state, "provider_state": intent.get("status"), **hold_status(db,payment),
    }}


def _cancellation_review(payment):
    return payment.failure_code=='cancellation_unconfirmed' and payment.state in ('creating','awaiting_payment')


def _status_result(db,payment,**extra):
    return {'status':'success','result':{'checkout_id':payment.id,'state':payment.state,
        'booking_ref':payment.booking_id,'amount':_money(payment.amount),'currency':payment.currency,
        **hold_status(db,payment),'cancellation_pending_review':_cancellation_review(payment),**extra}}


def _provider_intent(db,payment,key,*,recover_for_cancel=False):
    if payment.payment_intent_id:
        intent=stripe.PaymentIntent.retrieve(payment.payment_intent_id,api_key=key)
    else:
        # After 24h Stripe may prune the original idempotency key. Never create
        # a replacement for an old ambiguous attempt, even when cancelling.
        if payment.created_at<datetime.utcnow()-timedelta(hours=23):
            raise HTTPException(409,'checkout_requires_reconciliation')
        hold=db.get(PublicCheckoutHold,payment.id)
        if not recover_for_cancel and (not hold or hold.expires_at<=datetime.utcnow()):
            raise HTTPException(409,'checkout_hold_expired_requires_review')
        intent=stripe.PaymentIntent.create(amount=int(payment.amount*100),currency=payment.currency.lower(),
            payment_method_types=['card'],metadata={'omi_checkout_id':payment.id},
            idempotency_key='omi-checkout-v1-'+payment.id,api_key=key)
    if (not isinstance(intent.get('id'),str) or not intent['id'].startswith('pi_')
            or intent.get('livemode') is not False or intent.get('amount')!=int(payment.amount*100)
            or intent.get('currency')!=payment.currency.lower()
            or intent.get('metadata',{}).get('omi_checkout_id')!=payment.id
            or (payment.payment_intent_id and payment.payment_intent_id!=intent.get('id'))):
        raise HTTPException(503,'payment_snapshot_mismatch')
    return intent


def _record_cancelled(db,payment,intent,source):
    if (not isinstance(intent.get('id'),str) or not intent['id'].startswith('pi_')
            or intent.get('livemode') is not False or intent.get('status')!='canceled'
            or intent.get('amount_received')!=0 or intent.get('amount_capturable')!=0
            or intent.get('amount')!=int(payment.amount*100) or intent.get('currency')!=payment.currency.lower()
            or intent.get('metadata',{}).get('omi_checkout_id')!=payment.id
            or (payment.payment_intent_id and payment.payment_intent_id!=intent.get('id'))):
        raise HTTPException(409,'payment_cancellation_not_verified')
    if payment.state=='cancelled':return
    # A delayed replay of the original cancellation must not regress a later
    # compensating refund for a contradictory success event.
    if payment.state in ('refund_pending','refund_failed','refunded') and payment.failure_code=='success_after_checkout_cancelled':return
    if payment.state not in ('creating','awaiting_payment') or payment.booking_id is not None:
        raise HTTPException(409,'payment_cancellation_state_conflict')
    payment.payment_intent_id=intent['id']
    payment.state='cancelled';payment.failure_code='checkout_cancelled_before_payment'
    from app.operations.task_plans import record_command
    record_command(db,'sandbox-cancel-v1:'+payment.id,'sandbox-checkout-cancel','sandbox-checkout',
        'Verified Stripe sandbox cancellation with no received or capturable funds.',
        {'checkout_id':payment.id,'intent_id':intent['id'],'provider_state':'canceled',
         'amount_received':0,'amount_capturable':0,'source':source})


def _ensure_intent(db,payment,key):
    checkout_id=payment.id
    intent=_provider_intent(db,payment,key)
    db.rollback()
    _lock(db)
    payment=db.get(PublicPayment,checkout_id,populate_existing=True)
    if payment.payment_intent_id and payment.payment_intent_id!=intent['id']:
        raise HTTPException(409,'payment_intent_mismatch')
    # A parallel cancellation/callback wins over an older provider response.
    if payment.state in ('confirmed','cancelled','refunded','refund_pending','refund_failed'):
        db.commit();return _status_result(db,payment)
    payment.payment_intent_id=intent['id']
    if intent.get('status')=='canceled':
        _record_cancelled(db,payment,intent,'checkout-recovery')
        db.commit();return _status_result(db,payment)
    if payment.state=='creating':payment.state='awaiting_payment'
    if _cancellation_review(payment):
        db.commit();return _status_result(db,payment,provider_state=intent.get('status'),
            cancellation_available=intent.get('status') in ('requires_payment_method','requires_confirmation','requires_action')
                and intent.get('amount_received')==0 and intent.get('amount_capturable')==0)
    db.commit()
    return _checkout_result(db,payment,intent)


@router.post("/booking")
def create_checkout(body: CheckoutInput, response: Response, db: Session = Depends(get_db)):
    key = _configured()
    response.headers["Cache-Control"] = "no-store"
    checkout_id = str(body.checkout_id)
    fingerprint = _fingerprint(body)
    try:
        _lock(db)
        payment = db.get(PublicPayment, checkout_id)
        if payment and payment.fingerprint != fingerprint:
            raise HTTPException(409, "checkout_id_reused")
        if payment and payment.state not in ("creating", "awaiting_payment"):
            raise HTTPException(409, "checkout_already_processed")
        if payment is None:
            prop = db.get(Property, body.property_id)
            if not prop or not prop.published:
                raise HTTPException(404, "listing_not_found")
            try:
                today = datetime.now(ZoneInfo(prop.time_zone)).date()
            except (ValueError, ZoneInfoNotFoundError, TypeError):
                raise HTTPException(409, 'booking_timezone_needs_review') from None
            if not 0 <= (body.check_in-today).days <= 1095:
                raise HTTPException(422, 'invalid_arrival_date')
            restriction = restriction_reason(db, prop.id, body.check_in, body.check_out)
            if restriction:
                raise HTTPException(409, restriction)
            if prop.max_guests is not None and body.guests > prop.max_guests:
                raise HTTPException(422, "exceeds_max_guests")
            ci = datetime.combine(body.check_in, datetime.min.time())
            co = datetime.combine(body.check_out, datetime.min.time())
            if _has_overlapping_booking(db, prop.id, ci, co):
                raise HTTPException(409, "dates_unavailable")
            rows = price_breakdown(db, prop, body.check_in, body.check_out)
            if not rows or any(row["price"] is None for row in rows):
                raise HTTPException(422, "pricing_unavailable")
            total = sum((row["price"] for row in rows), Decimal("0.00")).quantize(Decimal("0.01"))
            if not total.is_finite() or not Decimal("0.50") <= total <= Decimal("999999.99"):
                raise HTTPException(422, "invalid_payment_amount")
            _verify_quote(body, total)
            payment = PublicPayment(
                id=checkout_id, fingerprint=fingerprint, property_id=prop.id,
                check_in=ci, check_out=co, guests=body.guests,
                guest_name=body.guest.name, guest_email=body.guest.email,
                guest_phone=body.guest.phone, language=body.guest.language,
                amount=total, currency="AUD", state="creating",
            )
            db.add(payment);db.flush()
            db.add(PublicCheckoutHold(payment_id=checkout_id,expires_at=datetime.utcnow()+timedelta(minutes=HOLD_MINUTES)))
        db.commit()
        return _ensure_intent(db, payment, key)
    except stripe.StripeError:
        db.rollback()
        raise HTTPException(503, "payment_provider_unavailable") from None
    except Exception:
        db.rollback()
        raise


def _refund(db, payment, key):
    checkout_id = payment.id
    intent_id = payment.payment_intent_id
    refund_id = payment.refund_id
    # Persist pending before making the external request. If a process crashes,
    # Stripe's webhook retry resumes this branch with the same idempotency key.
    payment.state = "refund_pending"
    db.commit()
    try:
        if refund_id:
            refund = stripe.Refund.retrieve(refund_id, api_key=key)
        else:
            refund = stripe.Refund.create(
                payment_intent=intent_id, idempotency_key="omi-conflict-refund-v1-" + checkout_id,
                api_key=key,
            )
    except stripe.StripeError:
        raise HTTPException(503, "refund_retry_required") from None
    db.rollback()
    _lock(db)
    payment = db.get(PublicPayment, checkout_id, populate_existing=True)
    if payment.state == 'refunded':
        db.commit()
        return {'received': True, 'state': 'refunded'}
    payment.refund_id = refund["id"]
    if refund.get("status") == "succeeded":
        payment.state = "refunded"
        db.commit()
        return {"received": True, "state": "refunded"}
    payment.state = "refund_pending" if refund.get("status") == "pending" else "refund_failed"
    db.commit()
    raise HTTPException(503, "refund_not_complete")


@router.post("/stripe-webhook")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    key = _configured()
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "webhook_not_configured")
    chunks=[]; size=0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>262144: raise HTTPException(413, "webhook_too_large")
        chunks.append(chunk)
    payload=b''.join(chunks)
    try:
        event = stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), secret)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(400, "invalid_signature") from None
    if event.get("livemode") is not False:
        raise HTTPException(400, "expected_test_event")
    return await run_in_threadpool(_apply_event, event, db, key)


def _apply_event(event, db, key):
    if not isinstance(event.get("type"), str):
        raise HTTPException(400, "invalid_payment_event")
    if event["type"] not in ("payment_intent.succeeded","payment_intent.canceled"):
        return {"received": True}
    pi = event.get("data", {}).get("object")
    if not isinstance(pi, dict) or not isinstance(pi.get('id'), str) or not isinstance(pi.get('metadata', {}), dict):
        raise HTTPException(400, 'invalid_payment_event')
    checkout_id = pi.get("metadata", {}).get("omi_checkout_id")
    if not checkout_id:
        return {"received": True}  # a payment belonging to another integration
    try:
        _lock(db)
        payment = db.get(PublicPayment, checkout_id)
        if payment is None:
            raise HTTPException(409, "checkout_not_found")
        if event['type']=='payment_intent.canceled':
            _record_cancelled(db,payment,pi,'signed-webhook')
            db.commit();return {'received':True,'state':payment.state}
        if (pi.get("livemode") is not False or pi.get("status") != "succeeded"
                or pi.get("amount_received") != int(payment.amount * 100)
                or pi.get("currency") != payment.currency.lower()
                or (payment.payment_intent_id and payment.payment_intent_id != pi["id"])):
            raise HTTPException(409, "payment_snapshot_mismatch")
        payment.payment_intent_id = pi["id"]
        if payment.state in ("confirmed", "refunded"):
            db.commit()
            return {"received": True, "state": payment.state}
        if payment.state=='cancelled':
            payment.failure_code='success_after_checkout_cancelled'
            return _refund(db,payment,key)
        if payment.state in ("refund_pending", "refund_failed"):
            return _refund(db, payment, key)
        prop = db.get(Property, payment.property_id)
        restriction = restriction_reason(db, payment.property_id, payment.check_in.date(), payment.check_out.date()) if prop else None
        if (not prop or not prop.published or restriction or (prop.max_guests is not None and payment.guests>prop.max_guests) or _has_overlapping_booking(
                db, payment.property_id, payment.check_in, payment.check_out, exclude_checkout_id=payment.id)):
            payment.failure_code = "listing_or_dates_unavailable"
            return _refund(db, payment, key)
        guest = Guest(name=payment.guest_name, email=payment.guest_email,
                      phone=payment.guest_phone, language=payment.language)
        db.add(guest)
        db.flush()
        booking = Booking(
            property_id=payment.property_id, guest_id=guest.id,
            platform=PlatformName.direct, uid=pi["id"], summary="Website sandbox booking",
            check_in=payment.check_in, check_out=payment.check_out,
            status=BookingStatus.confirmed, amount=payment.amount,
        )
        db.add(booking)
        db.flush()
        payment.booking_id = booking.id
        payment.state = "confirmed"
        from app.operations.payment_ledger import link_new_payment
        link_new_payment(db,payment,booking)
        refresh_conflicts(db)
        # Existing scheduler handles cleaning/messages after commit; no mail or
        # unrelated bookings are processed inside the Stripe callback.
        db.commit()
        return {"received": True, "state": "confirmed"}
    except Exception:
        db.rollback()
        raise


@router.get("/booking/{payment_intent_id}")
def checkout_status(payment_intent_id: str, response: Response,
                    authorization: str = Header(default=""), db: Session = Depends(get_db)):
    _configured()
    response.headers["Cache-Control"] = "no-store"
    payment = db.query(PublicPayment).filter_by(payment_intent_id=payment_intent_id).first()
    if not payment or not hmac.compare_digest(authorization, "Bearer " + _access_token(payment.id)):
        raise HTTPException(404, "checkout_not_found")
    # Never disclose guest info, access codes or a guest-portal magic link here.
    return {"status": "success", "result": {
        "state": payment.state, "booking_ref": payment.booking_id,
        "amount": _money(payment.amount), "currency": payment.currency, **hold_status(db,payment),
        "cancellation_pending_review":_cancellation_review(payment),
    }}


@router.get('/test-checkout')
def sandbox_checkout_page(request: Request, property_id: int, db: Session = Depends(get_db)):
    _configured()
    publishable_key = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
    if not publishable_key.startswith('pk_test_'):
        raise HTTPException(503, 'test_publishable_key_not_configured')
    prop = db.get(Property, property_id)
    if not prop or not prop.published:
        raise HTTPException(404, 'listing_not_found')
    from app.operations.router import templates
    return templates.TemplateResponse(request=request, name='sandbox_checkout.html',
        context={'property':prop, 'publishable_key':publishable_key, 'checkout_id':(checkout_id:=str(uuid4())), 'resume_token':_access_token(checkout_id)},
        headers={'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer', 'X-Robots-Tag':'noindex, nofollow'})


@router.get('/checkout/{checkout_id}')
def recover_status(checkout_id: UUID, response: Response, authorization: str = Header(default=''), db: Session = Depends(get_db)):
    _configured()
    payment = _authorized_checkout(db, str(checkout_id), authorization)
    response.headers['Cache-Control'] = 'no-store'
    return {'status':'success','result':{'checkout_id':payment.id, 'state':payment.state,
        'booking_ref':payment.booking_id, 'amount':_money(payment.amount), 'currency':payment.currency, **hold_status(db,payment),'cancellation_pending_review':_cancellation_review(payment)}}


def _authorized_checkout(db, checkout_id, authorization):
    payment = db.get(PublicPayment, checkout_id)
    if not payment or not hmac.compare_digest(authorization, 'Bearer '+_access_token(checkout_id)):
        raise HTTPException(404,'checkout_not_found')
    return payment


@router.post('/checkout/{checkout_id}/resume')
def resume_checkout(checkout_id: UUID, response: Response, authorization: str = Header(default=''), db: Session = Depends(get_db)):
    key = _configured()
    response.headers['Cache-Control'] = 'no-store'
    try:
        _lock(db)
        payment = _authorized_checkout(db, str(checkout_id), authorization)
        if payment.state not in ('creating','awaiting_payment'):
            db.commit()
            return {'status':'success','result':{'checkout_id':payment.id,'state':payment.state,
                'booking_ref':payment.booking_id,'amount':_money(payment.amount),'currency':payment.currency, **hold_status(db,payment),'cancellation_pending_review':_cancellation_review(payment)}}
        db.commit()
        return _ensure_intent(db,payment,key)
    except stripe.StripeError:
        db.rollback();raise HTTPException(503,'payment_provider_unavailable') from None
    except Exception:
        db.rollback();raise


@router.post('/checkout/{checkout_id}/cancel')
def cancel_checkout(checkout_id:UUID,response:Response,authorization:str=Header(default=''),db:Session=Depends(get_db)):
    key=_configured();response.headers['Cache-Control']='no-store'
    identity=str(checkout_id)
    try:
        _lock(db)
        payment=_authorized_checkout(db,identity,authorization)
        if payment.state not in ('creating','awaiting_payment'):
            db.commit();return _status_result(db,payment)
        db.commit()  # Never hold the stock write lock during a provider call.
        intent=_provider_intent(db,payment,key,recover_for_cancel=True)
        if intent.get('status')!='canceled':
            if (intent.get('status') not in ('requires_payment_method','requires_confirmation','requires_action')
                    or intent.get('amount_received')!=0 or intent.get('amount_capturable')!=0):
                db.rollback();_lock(db)
                payment=_authorized_checkout(db,identity,authorization)
                db.commit();return _status_result(db,payment,provider_state=intent.get('status'),cancellation_available=False)
            intent=stripe.PaymentIntent.cancel(intent['id'],cancellation_reason='requested_by_customer',
                idempotency_key='omi-checkout-cancel-v1-'+identity,api_key=key)
        db.rollback();_lock(db)
        payment=_authorized_checkout(db,identity,authorization)
        if payment.state not in ('creating','awaiting_payment','cancelled'):
            db.commit();return _status_result(db,payment)
        _record_cancelled(db,payment,intent,'authenticated-checkout')
        db.commit();return _status_result(db,payment)
    except stripe.StripeError:
        db.rollback();_lock(db)
        payment=_authorized_checkout(db,identity,authorization)
        if payment.state not in ('creating','awaiting_payment'):
            db.commit();return _status_result(db,payment)
        payment.failure_code='cancellation_unconfirmed'
        db.commit()
        raise HTTPException(503,'cancellation_unconfirmed_check_original_payment') from None
    except Exception:
        db.rollback();raise
