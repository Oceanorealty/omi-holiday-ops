"""No Stripe network, mail, scheduler or production database is used."""
import copy
import hashlib
import hmac
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import stripe
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.models import Booking, Property, PublicPayment
from app.public_api import router as public_router
from app.public_booking import router as booking_router


@pytest.fixture(params=["sqlite"] + (["postgresql"] if os.environ.get("OPS_TEST_POSTGRES_URL") else []))
def env(tmp_path, monkeypatch, request):
    monkeypatch.setenv("SECRET_KEY", "test-secret-for-quote-and-status-" + "x" * 32)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake_no_network")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_local_test")
    monkeypatch.setenv("PUBLIC_BOOKING_ENABLED", "test")
    monkeypatch.setenv("OPS_PAYMENT_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("OPS_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("OPS_MESSAGE_DISPATCH_ENABLED", "0")
    monkeypatch.setenv("OPS_OWNER_EMAIL_ENABLED", "0")
    admin_engine = None
    if request.param == "postgresql":
        url = make_url(os.environ["OPS_TEST_POSTGRES_URL"].replace("postgres://", "postgresql://", 1))
        if url.database != "ops_release_test":
            pytest.fail("PostgreSQL tests require the dedicated B5 test database")
        schema = "b5_test_" + uuid4().hex
        admin_engine = create_engine(url)
        with admin_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
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
              "livemode": False, "amount": kwargs["amount"], "amount_received": kwargs["amount"],
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
    if admin_engine:
        with admin_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


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
    monkeypatch.setenv("OPS_PAYMENT_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("OPS_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("OPS_MESSAGE_DISPATCH_ENABLED", "0")
    monkeypatch.setenv("OPS_OWNER_EMAIL_ENABLED", "0")
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


@pytest.mark.parametrize('name,value', [('OPS_PAYMENT_ENVIRONMENT','production'),('OPS_SCHEDULER_ENABLED','1'),('OPS_MESSAGE_DISPATCH_ENABLED','1'),('OPS_OWNER_EMAIL_ENABLED','1')])
def test_sandbox_environment_isolation(env,monkeypatch,name,value):
    monkeypatch.setenv(name,value)
    assert checkout(env).status_code==503
    assert not env[3]


@pytest.mark.parametrize('stage',['before_checkout','after_checkout'])
def test_current_restrictions_checked_at_checkout_and_fulfillment(env,stage):
    from app.models import BookingWindow
    client,sessions,body,intents,refunds=env
    if stage=='after_checkout':assert checkout(env).status_code==200
    with sessions() as db:
        db.add(BookingWindow(property_id=body['property_id'],minimum_notice_days=100))
        db.commit()
    if stage=='before_checkout':
        assert checkout(env).status_code==409 and not intents
    else:
        assert webhook(client,next(iter(intents.values()))).json()['state']=='refunded'
        assert len(refunds)==1
    with sessions() as db:assert db.query(Booking).count()==0


def test_shared_capacity_is_used_instead_of_single_booking_overlap(env):
    from app.models import StockResource,StockBinding
    client,sessions,body,intents,refunds=env
    with sessions() as db:
        p=db.get(Property,body['property_id'])
        resource=StockResource(name='Synthetic two room pool',capacity=2,time_zone=p.time_zone)
        db.add(resource);db.flush()
        db.add(StockBinding(property_id=p.id,resource_id=resource.id,quantity=1));db.commit()
    for _ in range(3):assert checkout(env,checkout_id=str(uuid4())).status_code==200
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses=list(pool.map(lambda pi:webhook(client,pi),list(intents.values())))
    assert all(r.status_code==200 for r in responses)
    with sessions() as db:
        assert db.query(Booking).count()==2
        assert sorted(r.state for r in db.query(PublicPayment))==['confirmed','confirmed','refunded']
    assert len(refunds)==1


def test_linked_listing_booking_prevents_payment_creation(env):
    from app.models import StockResource,StockBinding,BookingStatus,PlatformName
    client,sessions,body,intents,_=env
    with sessions() as db:
        p=db.get(Property,body['property_id'])
        q=Property(name='Synthetic linked unit',time_zone=p.time_zone)
        resource=StockResource(name='Synthetic shared apartment',capacity=1,time_zone=p.time_zone)
        db.add_all([q,resource]);db.flush()
        db.add_all([StockBinding(property_id=id,resource_id=resource.id,quantity=1) for id in [p.id,q.id]])
        db.add(Booking(property_id=q.id,platform=PlatformName.airbnb,uid='synthetic-ota',status=BookingStatus.confirmed,
            check_in=datetime.fromisoformat(body['check_in']),check_out=datetime.fromisoformat(body['check_out'])))
        db.commit()
    assert checkout(env).status_code==409 and not intents


def test_webhook_body_limit_and_malformed_payload(env):
    assert env[0].post('/api/public/stripe-webhook',content=b'x'*262145).status_code==413


def test_delayed_refund_result_cannot_regress_completed_state(env,monkeypatch):
    client,sessions,body,intents,_=env
    assert checkout(env).status_code==200
    with sessions() as db:
        db.get(Property,body['property_id']).published=False;db.commit()
    def delayed_pending(**kwargs):
        # Simulates another callback finishing while the first provider call is in flight.
        from app.operations.stock import lock_stock
        with sessions() as db:
            lock_stock(db)
            db.get(PublicPayment,body['checkout_id']).state='refunded';db.commit()
        return {'id':'re_synthetic_pending','status':'pending'}
    monkeypatch.setattr(stripe.Refund,'create',delayed_pending)
    assert webhook(client,next(iter(intents.values()))).json()['state']=='refunded'
    with sessions() as db:assert db.get(PublicPayment,body['checkout_id']).state=='refunded'


def test_payment_review_is_admin_only_and_hides_customer_and_credentials(env,monkeypatch):
    from app.operations.payment_router import router as review_router
    from app.operations.router import require_admin
    client,sessions,body,_,_=env
    assert checkout(env).status_code==200
    app=FastAPI();app.include_router(review_router)
    def database():
        with sessions() as db:yield db
    app.dependency_overrides[get_db]=database
    with TestClient(app) as review:
        assert review.get('/operations/payments').status_code==403
        app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
        response=review.get('/operations/payments')
        assert response.status_code==200 and 'awaiting payment' in response.text
        assert response.headers['cache-control']=='no-store'
        for value in ['test@example.com','sk_test_fake_no_network','whsec_local_test','fake_client_secret']:
            assert value not in response.text
        assert review.get('/operations/payments?state=invalid').status_code==422


def test_sandbox_checkout_page_requires_isolation_and_test_publishable_key(env,monkeypatch):
    client,_,body,_,_=env
    url='/api/public/test-checkout?property_id='+str(body['property_id'])
    assert client.get(url).status_code==503
    monkeypatch.setenv('STRIPE_PUBLISHABLE_KEY','pk_live_not_real')
    assert client.get(url).status_code==503
    monkeypatch.setenv('STRIPE_PUBLISHABLE_KEY','pk_test_synthetic_public')
    response=client.get(url)
    assert response.status_code==200 and 'Sandbox only' in response.text
    assert 'pk_test_synthetic_public' in response.text
    assert 'sk_test_fake_no_network' not in response.text and 'whsec_local_test' not in response.text
    assert response.headers['cache-control']=='no-store'
    assert response.headers['referrer-policy']=='no-referrer'
    assert response.headers['x-robots-tag']=='noindex, nofollow'
    monkeypatch.setenv('OPS_PAYMENT_ENVIRONMENT','production')
    assert client.get(url).status_code==503


def test_verified_payment_creates_one_atomic_sandbox_account(env):
    from app.models import BookingAccount,BookingLedgerEntry,PublicPaymentAccount
    from app.operations.billing import totals
    client,sessions,body,intents,_=env
    assert checkout(env).status_code==200
    pi=next(iter(intents.values()))
    assert webhook(client,pi).status_code==200 and webhook(client,pi).status_code==200
    with sessions() as db:
        payment=db.get(PublicPayment,body['checkout_id']);link=db.get(PublicPaymentAccount,payment.id)
        assert link and link.booking_id==payment.booking_id
        account=db.get(BookingAccount,link.booking_id)
        rows=db.query(BookingLedgerEntry).filter_by(booking_id=link.booking_id).all()
        assert len(rows)==2 and totals(rows)['balance']==0 and account.currency=='AUD'
        received=next(r for r in rows if r.kind=='payment')
        assert json.loads(received.receipt_snapshot)['environment']=='sandbox'
        assert 'no real money' in received.evidence_note.lower()


def test_ledger_failure_rolls_back_booking_and_entries(env,monkeypatch):
    from app.models import BookingAccount,BookingLedgerEntry,PublicPaymentAccount
    import app.operations.payment_ledger as ledger
    client,sessions,_,intents,_=env
    checkout(env);pi=next(iter(intents.values()));original=ledger.link_new_payment
    def fail(db,payment,booking):
        original(db,payment,booking)
        raise RuntimeError('Synthetic failure after account insertion')
    with monkeypatch.context() as m:
        m.setattr(ledger,'link_new_payment',fail)
        with pytest.raises(RuntimeError):webhook(client,pi)
    with sessions() as db:
        for model in [Booking,BookingAccount,BookingLedgerEntry,PublicPaymentAccount]:assert db.query(model).count()==0
    assert webhook(client,pi).status_code==200


def test_authorized_resume_reuses_existing_intent_and_hides_guest_details(env):
    client,sessions,body,intents,_=env
    result=checkout(env).json()['result'];auth={'Authorization':'Bearer '+result['access_token']}
    url='/api/public/checkout/'+body['checkout_id']
    assert client.get(url).status_code==404 and client.post(url+'/resume').status_code==404
    result2=client.post(url+'/resume',headers=auth)
    assert result2.status_code==200 and result2.json()['result']['payment_intent_id']==result['payment_intent_id']
    assert len(intents)==1
    for response in [client.get(url,headers=auth),result2]:
        assert 'test@example.com' not in response.text and 'Test Guest' not in response.text
        assert response.headers['cache-control']=='no-store'
    assert webhook(client,next(iter(intents.values()))).status_code==200
    terminal=client.post(url+'/resume',headers=auth).json()['result']
    assert terminal['state']=='confirmed' and 'client_secret' not in terminal


def test_resume_recovers_ambiguous_create_without_new_snapshot(env,monkeypatch):
    from app.public_booking import _access_token
    client,sessions,body,intents,_=env
    create=stripe.PaymentIntent.create
    with monkeypatch.context() as m:
        m.setattr(stripe.PaymentIntent,'create',lambda **kw:(_ for _ in ()).throw(stripe.APIConnectionError('Synthetic loss')))
        assert checkout(env).status_code==503
    auth={'Authorization':'Bearer '+_access_token(body['checkout_id'])}
    response=client.post('/api/public/checkout/'+body['checkout_id']+'/resume',headers=auth)
    assert response.status_code==200 and len(intents)==1
    with sessions() as db:assert db.query(PublicPayment).count()==1 and db.query(Booking).count()==0


def test_payment_review_triage_filters_stale_and_unlinked_without_mutation(env):
    from app.operations.payment_router import router as review_router
    from app.operations.router import require_admin
    client,sessions,body,_,_=env
    now=datetime.utcnow()
    with sessions() as db:
        for name,state,hours in [('recent','awaiting_payment',1),('stale','awaiting_payment',24),
                ('creating','creating',24),('refund','refund_failed',1),('done','refunded',24),
                ('unlinked','confirmed',1)]:
            db.add(PublicPayment(id=name,fingerprint='synthetic',property_id=body['property_id'],
                check_in=now,check_out=now+timedelta(days=1),guests=1,
                guest_name='Private synthetic guest',guest_email='private@example.test',
                amount=100,currency='AUD',state=state,created_at=now-timedelta(hours=hours)))
        db.commit()
    app=FastAPI();app.include_router(review_router)
    def database():
        with sessions() as db:yield db
    app.dependency_overrides[get_db]=database
    app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with TestClient(app) as review:
        response=review.get('/operations/payments?review=true')
        assert response.status_code==200
        assert '4 attempts need review' in response.text
        for name in ['stale','creating','refund','unlinked']:
            assert '<td>'+name+'<br>' in response.text
        for name in ['recent','done']:
            assert '<td>'+name+'<br>' not in response.text
        assert 'Do not charge again' in response.text
        assert 'private@example.test' not in response.text
        assert 'Private synthetic guest' not in response.text
        filtered=review.get('/operations/payments?review=true&state=confirmed')
        assert '<td>unlinked<br>' in filtered.text and '<td>refund<br>' not in filtered.text
        assert '1 attempts · Page 1 of 1' in filtered.text
        assert review.get('/operations/payments?review=invalid').status_code==422
    with sessions() as db:
        assert db.query(PublicPayment).count()==6
        assert db.get(PublicPayment,'stale').state=='awaiting_payment'
        assert db.get(PublicPayment,'refund').state=='refund_failed'
