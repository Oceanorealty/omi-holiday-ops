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
from uuid import UUID
from zoneinfo import ZoneInfo

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import _secret, _sign
from app.db import get_db
from app.models import Booking, BookingStatus, Guest, PlatformName, Property, PublicPayment
from app.public_api import _has_overlapping_booking, _money, _quote_rows

router = APIRouter(prefix="/api/public", tags=["public-booking"])


def _configured():
    # Test-only rollout: a production/live Stripe key cannot enable this module.
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if (os.environ.get("PUBLIC_BOOKING_ENABLED") != "test"
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
        today = datetime.now(ZoneInfo("Australia/Brisbane")).date()
        if self.check_in < today or not 0 < (self.check_out - self.check_in).days <= 366:
            raise ValueError("Invalid stay dates")
        if (self.check_in - today).days > 730:
            raise ValueError("Check-in too far in the future")
        return self


def _access_token(checkout_id):
    return _sign("public-booking-v1|" + checkout_id)


def _lock(db):
    # SQLite needs its writer lock BEFORE any reads. PostgreSQL's table lock
    # also excludes concurrent Booking inserts from existing staff/import code
    # while this transaction checks inventory and commits its own booking.
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    elif dialect == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("LOCK TABLE bookings IN SHARE ROW EXCLUSIVE MODE"))
        db.execute(text("LOCK TABLE public_payments IN SHARE ROW EXCLUSIVE MODE"))
    else:
        raise HTTPException(503, "unsupported_payment_database")


def _verify_quote(body, total):
    try:
        payload, mac = body.quote_token.rsplit(".", 1)
        if not hmac.compare_digest(_sign(payload), body.quote_token):
            raise ValueError()
        pid, ci, co, guests, amount, expiry = payload.split("|")
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


def _checkout_result(payment, intent):
    if intent.get("livemode") is not False:
        raise HTTPException(503, "expected_test_payment")
    return {"status": "success", "result": {
        "checkout_id": payment.id,
        "payment_intent_id": intent["id"],
        "client_secret": intent["client_secret"],
        "access_token": _access_token(payment.id),
        "amount": _money(payment.amount), "currency": payment.currency,
        "state": payment.state,
    }}


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
            if prop.max_guests is not None and body.guests > prop.max_guests:
                raise HTTPException(422, "exceeds_max_guests")
            ci = datetime.combine(body.check_in, datetime.min.time())
            co = datetime.combine(body.check_out, datetime.min.time())
            if _has_overlapping_booking(db, prop.id, ci, co):
                raise HTTPException(409, "dates_unavailable")
            rows = _quote_rows(db, prop, body.check_in, body.check_out)
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
            db.add(payment)
        db.commit()
        # Retrying a network failure uses identical persisted Stripe parameters.
        if payment.payment_intent_id:
            intent = stripe.PaymentIntent.retrieve(payment.payment_intent_id, api_key=key)
        else:
            # Stripe can prune idempotency keys after 24h. Never accidentally
            # create a second intent after an ambiguous old network failure.
            if payment.created_at < datetime.utcnow() - timedelta(hours=23):
                raise HTTPException(409, "checkout_requires_reconciliation")
            intent = stripe.PaymentIntent.create(
                amount=int(payment.amount * 100), currency=payment.currency.lower(),
                payment_method_types=["card"], metadata={"omi_checkout_id": payment.id},
                idempotency_key="omi-checkout-v1-" + payment.id, api_key=key,
            )
        if intent.get("livemode") is not False:
            raise HTTPException(503, "expected_test_payment")
        db.rollback()  # release the read transaction before reacquiring locks
        _lock(db)
        payment = db.get(PublicPayment, checkout_id, populate_existing=True)
        if payment.payment_intent_id and payment.payment_intent_id != intent["id"]:
            raise HTTPException(409, "payment_intent_mismatch")
        payment.payment_intent_id = intent["id"]
        if payment.state == "creating":
            payment.state = "awaiting_payment"
        db.commit()
        return _checkout_result(payment, intent)
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
    payload = await request.body()
    if len(payload) > 262144:
        raise HTTPException(413, "webhook_too_large")
    try:
        event = stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), secret)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(400, "invalid_signature") from None
    if event.get("livemode") is not False:
        raise HTTPException(400, "expected_test_event")
    return await run_in_threadpool(_apply_event, event, db, key)


def _apply_event(event, db, key):
    if event["type"] != "payment_intent.succeeded":
        return {"received": True}
    pi = event["data"]["object"]
    checkout_id = pi.get("metadata", {}).get("omi_checkout_id")
    if not checkout_id:
        return {"received": True}  # a payment belonging to another integration
    try:
        _lock(db)
        payment = db.get(PublicPayment, checkout_id)
        if payment is None:
            raise HTTPException(409, "checkout_not_found")
        if (pi.get("livemode") is not False or pi.get("status") != "succeeded"
                or pi.get("amount_received") != int(payment.amount * 100)
                or pi.get("currency") != payment.currency.lower()
                or (payment.payment_intent_id and payment.payment_intent_id != pi["id"])):
            raise HTTPException(409, "payment_snapshot_mismatch")
        payment.payment_intent_id = pi["id"]
        if payment.state in ("confirmed", "refunded"):
            db.commit()
            return {"received": True, "state": payment.state}
        if payment.state in ("refund_pending", "refund_failed"):
            return _refund(db, payment, key)
        prop = db.get(Property, payment.property_id)
        if (not prop or not prop.published or _has_overlapping_booking(
                db, payment.property_id, payment.check_in, payment.check_out)):
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
        "amount": _money(payment.amount), "currency": payment.currency,
    }}
