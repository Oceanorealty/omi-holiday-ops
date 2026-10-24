"""Sandbox-only cancellation and late callback races, with no Stripe network."""
import copy,hashlib,hmac,json,time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from threading import Event,Lock

import pytest
import stripe
from app.models import Booking,PublicPayment,PublicCheckoutHold,OpsCommand
from app.operations.stock import available_units
from app.public_booking import _access_token
from test_public_booking import env,checkout,webhook


@pytest.fixture
def cancellable(env,monkeypatch):
    client,sessions,body,intents,refunds=env
    original=stripe.PaymentIntent.create
    def create(**kw):
        pi=original(**kw);pi.update(status='requires_payment_method',amount_received=0,amount_capturable=0)
        return pi
    calls=[]
    def cancel(intent_id,**kw):
        calls.append((intent_id,kw['idempotency_key']))
        intents[intent_id]['status']='canceled'
        return copy.deepcopy(intents[intent_id])
    monkeypatch.setattr(stripe.PaymentIntent,'create',create)
    monkeypatch.setattr(stripe.PaymentIntent,'cancel',cancel)
    result=checkout(env).json()['result']
    auth={'Authorization':'Bearer '+result['access_token']}
    return env,result,auth,calls


def cancelled_event(client,intent,**changes):
    pi=copy.deepcopy(intent);pi.update(changes)
    event={'type':'payment_intent.canceled','livemode':False,'data':{'object':pi}}
    payload=json.dumps(event);stamp=int(time.time())
    signature=hmac.new(b'whsec_local_test',f'{stamp}.{payload}'.encode(),hashlib.sha256).hexdigest()
    return client.post('/api/public/stripe-webhook',content=payload,
        headers={'stripe-signature':f't={stamp},v1={signature}','Content-Type':'application/json'})


def test_cancel_releases_stock_only_after_verified_provider_result(cancellable):
    (client,sessions,body,intents,refunds),result,auth,calls=cancellable
    url='/api/public/checkout/'+body['checkout_id']
    with sessions() as db:
        before=db.get(PublicCheckoutHold,body['checkout_id']).expires_at
        ci,co=datetime.fromisoformat(body['check_in']),datetime.fromisoformat(body['check_out'])
        assert available_units(db,body['property_id'],ci,co)==0
    response=client.post(url+'/cancel',headers=auth)
    assert response.status_code==200 and response.json()['result']['state']=='cancelled'
    assert not response.json()['result']['hold_active']
    assert response.headers['cache-control']=='no-store'
    assert len(calls)==1 and not refunds
    assert 'client_secret' not in response.text and 'test@example.com' not in response.text
    assert client.post(url+'/cancel',headers=auth).json()['result']['state']=='cancelled' and len(calls)==1
    assert client.post(url+'/resume',headers=auth).json()['result']['state']=='cancelled'
    with sessions() as db:
        assert available_units(db,body['property_id'],ci,co)==1
        assert db.query(Booking).count()==0
        assert db.get(PublicCheckoutHold,body['checkout_id']).expires_at==before
        audit=db.query(OpsCommand).filter_by(purpose='sandbox-checkout-cancel').one()
        assert json.loads(audit.payload)['provider_state']=='canceled'


def test_cancel_requires_original_authorization_and_test_configuration(cancellable,monkeypatch):
    (client,sessions,body,_,_),_,auth,calls=cancellable
    url='/api/public/checkout/'+body['checkout_id']+'/cancel'
    assert client.post(url).status_code==404
    assert client.post(url,headers={'Authorization':'Bearer wrong'}).status_code==404
    assert not calls
    monkeypatch.setenv('STRIPE_SECRET_KEY','sk_live_synthetic_not_real')
    assert client.post(url,headers=auth).status_code==503 and not calls
    with sessions() as db:assert db.query(OpsCommand).count()==0


@pytest.mark.parametrize('state',['processing','succeeded','requires_capture'])
def test_submitted_or_authorized_payment_is_not_cancelled(cancellable,state):
    (client,sessions,body,intents,refunds),result,auth,calls=cancellable
    pi=intents[result['payment_intent_id']];pi['status']=state
    if state=='succeeded':pi['amount_received']=pi['amount']
    if state=='requires_capture':pi['amount_capturable']=pi['amount']
    response=client.post('/api/public/checkout/'+body['checkout_id']+'/cancel',headers=auth)
    assert response.status_code==200 and response.json()['result']['cancellation_available'] is False
    assert response.json()['result']['provider_state']==state
    assert not calls and not refunds
    with sessions() as db:
        assert db.get(PublicPayment,body['checkout_id']).state=='awaiting_payment'
        assert db.query(OpsCommand).count()==0


def test_unknown_cancel_result_keeps_identity_and_stock_until_retry(cancellable,monkeypatch):
    (client,sessions,body,intents,_),result,auth,calls=cancellable
    real_cancel=stripe.PaymentIntent.cancel
    def lost(intent_id,**kw):
        real_cancel(intent_id,**kw)
        raise stripe.APIConnectionError('Synthetic response lost after provider cancellation')
    monkeypatch.setattr(stripe.PaymentIntent,'cancel',lost)
    url='/api/public/checkout/'+body['checkout_id']
    assert client.post(url+'/cancel',headers=auth).status_code==503
    status=client.get(url,headers=auth).json()['result']
    assert status['state']=='awaiting_payment' and status['hold_active']
    with sessions() as db:
        assert db.get(PublicPayment,body['checkout_id']).failure_code=='cancellation_unconfirmed'
        assert db.query(OpsCommand).count()==0
    # Retry retrieves the already-cancelled original; it does not make a second cancellation call.
    response=client.post(url+'/cancel',headers=auth)
    assert response.json()['result']['state']=='cancelled' and len(calls)==1
    with sessions() as db:assert db.query(OpsCommand).count()==1


def test_concurrent_cancel_requests_audit_once(cancellable):
    (client,sessions,body,_,_),_,auth,_=cancellable
    url='/api/public/checkout/'+body['checkout_id']+'/cancel'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:client.post(url,headers=auth),range(2)))
    assert all(r.status_code==200 and r.json()['result']['state']=='cancelled' for r in results)
    with sessions() as db:
        assert db.query(OpsCommand).count()==1 and db.query(Booking).count()==0


def test_stale_resume_cannot_resurrect_a_cancelled_checkout(cancellable,monkeypatch):
    (client,sessions,body,intents,_),_,auth,_=cancellable
    original=stripe.PaymentIntent.retrieve;entered=Event();release=Event();guard=Lock();counter=[0]
    def delayed(intent_id,**kw):
        snapshot=copy.deepcopy(original(intent_id,**kw))
        with guard:counter[0]+=1;first=counter[0]==1
        if first:
            entered.set();assert release.wait(10)
        return snapshot
    monkeypatch.setattr(stripe.PaymentIntent,'retrieve',delayed)
    url='/api/public/checkout/'+body['checkout_id']
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(client.post,url+'/resume',headers=auth)
        try:
            assert entered.wait(5)
            assert client.post(url+'/cancel',headers=auth).json()['result']['state']=='cancelled'
        finally:release.set()
        response=pending.result(timeout=10)
    assert response.status_code==200 and response.json()['result']['state']=='cancelled'
    assert 'client_secret' not in response.text
    with sessions() as db:assert db.query(Booking).count()==0


def test_success_callback_wins_over_a_failed_racing_cancellation(cancellable,monkeypatch):
    (client,sessions,body,intents,_),result,auth,_=cancellable
    def already_succeeded(intent_id,**kw):
        pi=intents[intent_id];pi.update(status='succeeded',amount_received=pi['amount'])
        assert webhook(client,pi).status_code==200
        raise stripe.InvalidRequestError('Already succeeded','status')
    monkeypatch.setattr(stripe.PaymentIntent,'cancel',already_succeeded)
    response=client.post('/api/public/checkout/'+body['checkout_id']+'/cancel',headers=auth)
    assert response.status_code==200 and response.json()['result']['state']=='confirmed'
    with sessions() as db:assert db.query(Booking).count()==1 and db.query(OpsCommand).filter_by(purpose='sandbox-checkout-cancel').count()==0


def test_signed_cancellation_and_replays_release_hold_once(cancellable):
    (client,sessions,body,intents,_),result,auth,calls=cancellable
    pi=copy.deepcopy(intents[result['payment_intent_id']]);pi['status']='canceled'
    assert cancelled_event(client,pi).json()['state']=='cancelled'
    assert cancelled_event(client,pi).json()['state']=='cancelled'
    assert client.post('/api/public/checkout/'+body['checkout_id']+'/cancel',headers=auth).json()['result']['state']=='cancelled'
    assert not calls
    with sessions() as db:assert db.query(OpsCommand).count()==1 and db.query(Booking).count()==0


@pytest.mark.parametrize('change',[{'amount_received':1},{'amount_capturable':1},{'livemode':True},
    {'currency':'usd'},{'status':'processing'},{'id':'pi_other'},{'amount':1}])
def test_mismatched_cancel_event_cannot_release_inventory(cancellable,change):
    (client,sessions,body,intents,_),result,_,_=cancellable
    pi=copy.deepcopy(intents[result['payment_intent_id']]);pi['status']='canceled'
    assert cancelled_event(client,pi,**change).status_code==409
    with sessions() as db:assert db.get(PublicPayment,body['checkout_id']).state=='awaiting_payment'


def test_late_success_after_verified_cancel_is_refunded_not_booked(cancellable):
    (client,sessions,body,intents,refunds),result,auth,_=cancellable
    assert client.post('/api/public/checkout/'+body['checkout_id']+'/cancel',headers=auth).status_code==200
    pi=copy.deepcopy(intents[result['payment_intent_id']]);pi.update(status='succeeded',amount_received=pi['amount'])
    assert webhook(client,pi).json()['state']=='refunded'
    assert webhook(client,pi).json()['state']=='refunded' and len(refunds)==1
    earlier=copy.deepcopy(pi);earlier.update(status='canceled',amount_received=0)
    assert cancelled_event(client,earlier).json()['state']=='refunded'
    assert len(refunds)==1
    with sessions() as db:assert db.query(Booking).count()==0


def test_cancellation_event_never_cancels_a_confirmed_booking(cancellable):
    (client,sessions,body,intents,_),result,auth,calls=cancellable
    pi=copy.deepcopy(intents[result['payment_intent_id']]);pi.update(status='succeeded',amount_received=pi['amount'])
    assert webhook(client,pi).json()['state']=='confirmed'
    pi.update(status='canceled',amount_received=0)
    assert cancelled_event(client,pi).status_code==409
    assert client.post('/api/public/checkout/'+body['checkout_id']+'/cancel',headers=auth).json()['result']['state']=='confirmed'
    assert not calls
    with sessions() as db:assert db.query(Booking).count()==1


def test_expired_ambiguous_create_is_recovered_and_cancelled_with_same_key(env,monkeypatch):
    client,sessions,body,intents,refunds=env
    original=stripe.PaymentIntent.create;creates=[];cancels=[]
    def ambiguous(**kw):
        creates.append({k:v for k,v in kw.items() if k!='api_key'})
        if len(creates)==1:
            pi=original(**kw);pi.update(status='requires_payment_method',amount_received=0,amount_capturable=0)
            raise stripe.APIConnectionError('Synthetic creation response lost')
        return next(iter(intents.values()))
    def cancel(intent_id,**kw):
        cancels.append(kw['idempotency_key']);intents[intent_id]['status']='canceled';return intents[intent_id]
    monkeypatch.setattr(stripe.PaymentIntent,'create',ambiguous)
    monkeypatch.setattr(stripe.PaymentIntent,'cancel',cancel)
    assert checkout(env).status_code==503
    with sessions() as db:
        assert db.get(PublicPayment,body['checkout_id']).payment_intent_id is None
        db.get(PublicCheckoutHold,body['checkout_id']).expires_at=datetime.utcnow()-timedelta(seconds=1);db.commit()
    response=client.post('/api/public/checkout/'+body['checkout_id']+'/cancel',
        headers={'Authorization':'Bearer '+_access_token(body['checkout_id'])})
    assert response.status_code==200 and response.json()['result']['state']=='cancelled'
    assert creates[0]==creates[1] and len(intents)==1 and len(cancels)==1 and not refunds
    with sessions() as db:assert db.query(Booking).count()==0 and db.query(OpsCommand).count()==1


def test_old_unknown_intent_is_not_recreated_even_for_cancellation(env,monkeypatch):
    client,sessions,body,intents,_=env;calls=[]
    def unavailable(**kw):calls.append(1);raise stripe.APIConnectionError('Synthetic unknown creation')
    monkeypatch.setattr(stripe.PaymentIntent,'create',unavailable)
    assert checkout(env).status_code==503
    with sessions() as db:
        db.get(PublicPayment,body['checkout_id']).created_at=datetime.utcnow()-timedelta(hours=24);db.commit()
    response=client.post('/api/public/checkout/'+body['checkout_id']+'/cancel',
        headers={'Authorization':'Bearer '+_access_token(body['checkout_id'])})
    assert response.status_code==409 and len(calls)==1 and not intents
    with sessions() as db:assert db.query(OpsCommand).count()==0


@pytest.mark.parametrize('change',[{'id':'pi_other'},{'amount_received':1},{'amount_capturable':1},
    {'livemode':True},{'currency':'usd'},{'metadata':{'omi_checkout_id':'other'}}])
def test_unverified_cancel_response_never_releases_hold(cancellable,monkeypatch,change):
    (client,sessions,body,intents,_),result,auth,_=cancellable
    bad=copy.deepcopy(intents[result['payment_intent_id']]);bad['status']='canceled';bad.update(change)
    monkeypatch.setattr(stripe.PaymentIntent,'cancel',lambda *a,**kw:bad)
    response=client.post('/api/public/checkout/'+body['checkout_id']+'/cancel',headers=auth)
    assert response.status_code==409
    with sessions() as db:
        assert db.get(PublicPayment,body['checkout_id']).state=='awaiting_payment'
        assert db.query(OpsCommand).count()==0


def test_unconfirmed_request_suppresses_payment_secret_until_cancellation_resolves(cancellable,monkeypatch):
    (client,sessions,body,intents,_),_,auth,_=cancellable
    original=stripe.PaymentIntent.cancel
    monkeypatch.setattr(stripe.PaymentIntent,'cancel',lambda *a,**kw:(_ for _ in ()).throw(stripe.APIConnectionError('Synthetic cancellation unavailable')))
    url='/api/public/checkout/'+body['checkout_id']
    assert client.post(url+'/cancel',headers=auth).status_code==503
    status=client.get(url,headers=auth).json()['result']
    assert status['cancellation_pending_review'] and status['hold_active']
    resumed=client.post(url+'/resume',headers=auth)
    assert resumed.status_code==200 and resumed.json()['result']['cancellation_pending_review']
    assert 'client_secret' not in resumed.text and len(intents)==1
    monkeypatch.setattr(stripe.PaymentIntent,'cancel',original)
    assert client.post(url+'/cancel',headers=auth).json()['result']['state']=='cancelled'


def test_provider_cancelled_status_recovers_lost_response(cancellable,monkeypatch):
    (client,sessions,body,intents,_),_,auth,_=cancellable
    original=stripe.PaymentIntent.cancel
    def lost(*a,**kw):original(*a,**kw);raise stripe.APIConnectionError('Synthetic lost confirmation')
    monkeypatch.setattr(stripe.PaymentIntent,'cancel',lost)
    url='/api/public/checkout/'+body['checkout_id']
    assert client.post(url+'/cancel',headers=auth).status_code==503
    result=client.post(url+'/resume',headers=auth)
    assert result.json()['result']['state']=='cancelled' and 'client_secret' not in result.text
    with sessions() as db:assert db.query(OpsCommand).count()==1


def test_unconfirmed_cancel_enters_admin_queue_before_hold_expiry(cancellable,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.db import get_db
    from app.operations.router import require_admin
    from app.operations.payment_router import router
    (client,sessions,body,_,_),_,auth,_=cancellable
    monkeypatch.setattr(stripe.PaymentIntent,'cancel',lambda *a,**kw:(_ for _ in ()).throw(stripe.APIConnectionError('Synthetic cancellation timeout')))
    assert client.post('/api/public/checkout/'+body['checkout_id']+'/cancel',headers=auth).status_code==503
    app=FastAPI();app.include_router(router)
    def database():
        with sessions() as db:yield db
    app.dependency_overrides[get_db]=database;app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with TestClient(app) as review:
        response=review.get('/operations/payments?review=true')
        assert response.status_code==200 and '1 attempts need review' in response.text
        assert 'Cancellation is not confirmed' in response.text
        assert 'test@example.com' not in response.text
