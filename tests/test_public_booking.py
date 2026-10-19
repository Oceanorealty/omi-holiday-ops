"""No Stripe network, mail, scheduler or production database is used."""
import copy
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import stripe
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.models import Booking, Property, PublicPayment
from app.public_api import router as public_router
from app.public_booking import router as booking_router


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-for-quote-and-status-" + "x" * 32)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake_no_network")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_local_test")
    monkeypatch.setenv("PUBLIC_BOOKING_ENABLED", "test")
    engine = create_engine("sqlite:///" + str(tmp_path / "test.db"),
                           connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        prop = Property(name="Test listing", published=True, max_guests=4,
                        base_nightly_rate=100, pricing_mode="manual")
        db.add(prop)
        db.commit()
        pid = prop.id
    app = FastAPI()
    app.include_router(public_router)
    app.include_router(booking_router)

    def database():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    intents = {}
    refunds = []

    def create(**kwargs):
        checkout_id = kwargs["metadata"]["omi_checkout_id"]
        pi = {"id": "pi_" + checkout_id, "client_secret": "fake_client_secret",
              "livemode": False, "amount_received": kwargs["amount"],
              "currency": kwargs["currency"], "status": "succeeded", "metadata": kwargs["metadata"]}
        intents[pi["id"]] = pi
        return pi

    def refund(**kwargs):
        refunds.append(kwargs)
        return {"id": "re_fake", "status": "succeeded"}

    monkeypatch.setattr(stripe.PaymentIntent, "create", create)
    monkeypatch.setattr(stripe.PaymentIntent, "retrieve", lambda id, **kwargs: intents[id])
    monkeypatch.setattr(stripe.Refund, "create", refund)
    ci = (datetime.now() + timedelta(days=30)).date().isoformat()
    co = (datetime.now() + timedelta(days=32)).date().isoformat()
    with TestClient(app) as client:
        quote = client.get("/api/public/quote", params={"property_id": pid, "check_in": ci,
                           "check_out": co, "guests": 2}).json()["result"]
        body = {"checkout_id": str(uuid4()), "property_id": pid, "check_in": ci,
                "check_out": co, "guests": 2, "guest": {"name": "Test Guest", "email": "test@example.com"},
                "quote_token": quote["quote_token"]}
        yield client, sessions, body, intents, refunds
    engine.dispose()


def webhook(client, intent, **changes):
    pi = copy.deepcopy(intent)
    pi.update(changes)
    event = {"id": "evt_" + str(uuid4()), "object": "event", "livemode": False,
             "type": "payment_intent.succeeded", "data": {"object": pi}}
    payload = json.dumps(event)
    timestamp = int(time.time())
    sig = hmac.new(b"whsec_local_test", f"{timestamp}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post("/api/public/stripe-webhook", content=payload,
                       headers={"stripe-signature": f"t={timestamp},v1={sig}", "Content-Type": "application/json"})


def checkout(env, **changes):
    client, _, body, _, _ = env
    body = copy.deepcopy(body)
    body.update(changes)
    return client.post("/api/public/booking", json=body)


def test_checkout_replay_status_and_duplicate_webhooks(env):
    client, sessions, body, intents, _ = env
    first = checkout(env)
    assert first.status_code == 200, first.text
    result = first.json()["result"]
    assert checkout(env).json()["result"]["payment_intent_id"] == result["payment_intent_id"]
    assert first.headers["cache-control"] == "no-store"
    pi = intents[result["payment_intent_id"]]
    assert webhook(client, pi).status_code == 200
    assert webhook(client, pi).status_code == 200
    with sessions() as db:
        assert db.query(Booking).count() == 1
        assert db.get(PublicPayment, body["checkout_id"]).state == "confirmed"
    url = "/api/public/booking/" + pi["id"]
    assert client.get(url).status_code == 404
    assert client.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 404
    status = client.get(url, headers={"Authorization": "Bearer " + result["access_token"]})
    assert status.json()["result"]["state"] == "confirmed"
    assert "guest_portal_url" not in status.text
    assert status.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("change", [{"guests": 0}, {"guests": -1}, {"guests": 5},
    {"check_in": "2000-01-01"}, {"check_out": "2099-01-01"},
    {"guest": {"name": "A", "email": "bad"}}, {"quote_token": "invalid"}])
def test_invalid_inputs_do_not_create_payments(env, change):
    assert checkout(env, **change).status_code in (409, 422)
    with env[1]() as db:
        assert db.query(PublicPayment).count() == 0
    assert not env[3]


def test_disabled_and_live_key_rejected(env, monkeypatch):
    monkeypatch.delenv("PUBLIC_BOOKING_ENABLED")
    assert checkout(env).status_code == 503
    monkeypatch.setenv("PUBLIC_BOOKING_ENABLED", "test")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_not_real")
    assert checkout(env).status_code == 503


def test_changed_quote_and_reused_checkout(env):
    client, sessions, body, _, _ = env
    with sessions() as db:
        db.get(Property, body["property_id"]).base_nightly_rate = 101
        db.commit()
    assert checkout(env).status_code == 409
    with sessions() as db:
        db.get(Property, body["property_id"]).base_nightly_rate = 100
        db.commit()
    assert checkout(env).status_code == 200
    assert checkout(env, guest={"name": "Other", "email": "other@example.com"}).status_code == 409


@pytest.mark.parametrize("changes", [{"amount_received": 1}, {"currency": "usd"},
                                  {"livemode": True}, {"id": "pi_other"}])
def test_payment_mismatch_never_creates_booking(env, changes):
    client, sessions, _, intents, _ = env
    checkout(env)
    pi = next(iter(intents.values()))
    assert webhook(client, pi, **changes).status_code == 409
    with sessions() as db:
        assert db.query(Booking).count() == 0


def test_bad_signature_rejected(env):
    assert env[0].post("/api/public/stripe-webhook", content="{}").status_code == 400


def test_two_concurrent_paid_checkouts_only_one_booking(env):
    client, sessions, _, intents, refunds = env
    assert checkout(env).status_code == 200
    assert checkout(env, checkout_id=str(uuid4())).status_code == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda pi: webhook(client, pi), list(intents.values())))
    assert all(r.status_code == 200 for r in responses)
    with sessions() as db:
        assert db.query(Booking).count() == 1
        assert sorted(p.state for p in db.query(PublicPayment)) == ["confirmed", "refunded"]
    assert len(refunds) == 1


def test_refund_failure_persists_pending_then_retries(env, monkeypatch):
    client, sessions, body, intents, _ = env
    checkout(env)
    pi = next(iter(intents.values()))
    with sessions() as db:
        db.get(Property, body["property_id"]).published = False
        db.commit()
    def fail(**kwargs):
        raise stripe.APIConnectionError("simulated failure")
    monkeypatch.setattr(stripe.Refund, "create", fail)
    assert webhook(client, pi).status_code == 503
    with sessions() as db:
        assert db.get(PublicPayment, body["checkout_id"]).state == "refund_pending"
        assert db.query(Booking).count() == 0
    monkeypatch.setattr(stripe.Refund, "create", lambda **kwargs: {"id": "re_retry", "status": "succeeded"})
    assert webhook(client, pi).json()["state"] == "refunded"
    assert webhook(client, pi).json()["state"] == "refunded"


def test_database_failure_before_commit_is_retryable(env, monkeypatch):
    client, sessions, _, intents, _ = env
    checkout(env)
    original = sessions.class_.commit
    def fail_once(db):
        monkeypatch.setattr(sessions.class_, "commit", original)
        raise RuntimeError("database temporarily unavailable")
    monkeypatch.setattr(sessions.class_, "commit", fail_once)
    with pytest.raises(RuntimeError):
        webhook(client, next(iter(intents.values())))
    with sessions() as db:
        assert db.query(Booking).count() == 0
    assert webhook(client, next(iter(intents.values()))).status_code == 200


def test_manual_quote_ignores_dynamic_rules(env):
    from app.models import PriceRule, PriceRuleType
    client, sessions, body, _, _ = env
    with sessions() as db:
        db.add(PriceRule(property_id=body["property_id"], name="test", active=True,
                         rule_type=PriceRuleType.last_minute, days_before=100, multiplier=2))
        db.commit()
    assert checkout(env).json()["result"]["amount"] == "200.00"
