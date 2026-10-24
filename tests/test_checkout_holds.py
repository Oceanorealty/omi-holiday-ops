"""Fixed sandbox leases: no Stripe network, real guest data or production database."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from uuid import uuid4

import pytest
import stripe
from app.models import (Booking,PublicPayment,PublicCheckoutHold,StockResource,StockBinding,Property)
from app.operations.stock import available_units,configure_stock,config_token
from app.operations.tasks import TaskError
from app.operations.calendar import calendar_data
from app.operations.reservations import save_reservation
from app.sync.ical_export import build_resource_ics
from test_hostaway_parity import reservation_args
from test_public_booking import env,checkout,webhook


def expire(sessions):
    with sessions() as db:
        for hold in db.query(PublicCheckoutHold):hold.expires_at=datetime.utcnow()-timedelta(seconds=1)
        db.commit()


def test_concurrent_unpaid_checkouts_only_one_lease_and_provider_intent(env):
    client,sessions,_,intents,_=env
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:checkout(env,checkout_id=str(uuid4())),range(2)))
    assert sorted(r.status_code for r in results)==[200,409]
    assert len(intents)==1
    with sessions() as db:
        assert db.query(PublicCheckoutHold).count()==db.query(PublicPayment).count()==1
        assert db.query(Booking).count()==0


def test_retry_and_resume_never_extend_lease_or_disclose_guest(env):
    client,sessions,body,intents,_=env
    first=checkout(env).json()['result']
    assert first['hold_active'] and 0<first['hold_seconds_remaining']<=900
    second=checkout(env).json()['result']
    auth={'Authorization':'Bearer '+first['access_token']}
    url='/api/public/checkout/'+body['checkout_id']
    resumed=client.post(url+'/resume',headers=auth).json()['result']
    status=client.get(url,headers=auth)
    assert first['hold_expires_at']==second['hold_expires_at']==resumed['hold_expires_at']
    assert second['hold_seconds_remaining']<=first['hold_seconds_remaining']
    assert len(intents)==1 and client.get(url).status_code==404
    assert 'test@example.com' not in status.text and 'Test Guest' not in status.text
    assert status.headers['cache-control']=='no-store'
    expire(sessions)
    result=client.get(url,headers=auth).json()['result']
    assert result['state']=='awaiting_payment' and not result['hold_active'] and result['hold_seconds_remaining']==0


def test_hold_blocks_public_quote_and_calendar_until_expiry(env):
    client,sessions,body,_,_=env
    assert checkout(env).status_code==200
    args={k:body[k] for k in ['property_id','check_in','check_out','guests']}
    assert not client.get('/api/public/quote',params=args).json()['result']['bookable']
    url=f"/api/public/listings/{body['property_id']}/calendar"
    params={'start':body['check_in'],'end':body['check_out']}
    data=client.get(url,params=params).json()['result']
    assert all(d['available_units']==0 for d in data['days'])
    assert 'hold_expires_at' not in str(data) and body['checkout_id'] not in str(data)
    expire(sessions)
    assert client.get('/api/public/quote',params=args).json()['result']['bookable']
    assert all(d['available_units']==1 for d in client.get(url,params=params).json()['result']['days'])


def test_hold_is_not_reservation_occupancy_or_exported_ical(env):
    _,sessions,body,_,_=env
    assert checkout(env).status_code==200
    with sessions() as db:
        first=datetime.fromisoformat(body['check_in']).date();last=datetime.fromisoformat(body['check_out']).date()
        row=calendar_data(db,first,last,body['property_id'])['rows'][0]
        assert row['reserved_nights']==0 and row['reserved_night_pct']==0
        assert all(d['checkout_holds']==1 and d['available_units']==0 and not d['booking_ids'] for d in row['days'])
        assert b'BEGIN:VEVENT' not in build_resource_ics(db,body['property_id'],start=first)


def test_local_reservation_respects_hold_then_can_book_after_expiry(env):
    _,sessions,body,_,_=env
    checkout(env)
    with sessions() as db:
        p=db.get(Property,body['property_id'])
        args=reservation_args(db,'synthetic-admin',p,check_in=body['check_in'],check_out=body['check_out'])
        with pytest.raises(TaskError):save_reservation(db,**args)
        assert db.query(Booking).count()==0
    expire(sessions)
    with sessions() as db:
        args=reservation_args(db,'synthetic-admin',db.get(Property,body['property_id']),check_in=body['check_in'],check_out=body['check_out'])
        save_reservation(db,**args)
        assert db.query(Booking).count()==1


def test_expired_ambiguous_create_requires_review_without_new_provider_call(env,monkeypatch):
    from app.public_booking import _access_token
    client,sessions,body,intents,_=env
    calls=[]
    def failed(**kw):
        calls.append(kw['idempotency_key']);raise stripe.APIConnectionError('synthetic lost response')
    monkeypatch.setattr(stripe.PaymentIntent,'create',failed)
    assert checkout(env).status_code==503
    expire(sessions)
    auth={'Authorization':'Bearer '+_access_token(body['checkout_id'])}
    response=client.post('/api/public/checkout/'+body['checkout_id']+'/resume',headers=auth)
    assert response.status_code==409 and 'hold_expired_requires_review' in response.text
    assert checkout(env).status_code==409 and len(calls)==1 and not intents


def test_expired_payment_cannot_take_inventory_from_newer_active_hold(env):
    client,sessions,_,intents,refunds=env
    first=checkout(env).json()['result'];old=copy.deepcopy(intents[first['payment_intent_id']])
    expire(sessions)
    second=checkout(env,checkout_id=str(uuid4())).json()['result']
    assert webhook(client,old).json()['state']=='refunded'
    assert webhook(client,intents[second['payment_intent_id']]).json()['state']=='confirmed'
    assert len(refunds)==1
    with sessions() as db:assert db.query(Booking).count()==1


def test_shared_quantity_and_terminal_payment_do_not_double_consume(env):
    client,sessions,body,intents,_=env
    with sessions() as db:
        p=db.get(Property,body['property_id'])
        r=StockResource(name='Synthetic three unit pool',capacity=3,time_zone=p.time_zone)
        other=Property(name='Synthetic one unit listing',time_zone=p.time_zone)
        db.add_all([r,other]);db.flush();other_id=other.id
        db.add_all([StockBinding(property_id=p.id,resource_id=r.id,quantity=2),StockBinding(property_id=other.id,resource_id=r.id,quantity=1)]);db.commit()
    result=checkout(env).json()['result']
    with sessions() as db:
        ci=datetime.fromisoformat(body['check_in']);co=datetime.fromisoformat(body['check_out'])
        assert available_units(db,body['property_id'],ci,co)==0
        assert available_units(db,other_id,ci,co)==1
    assert checkout(env,checkout_id=str(uuid4())).status_code==409
    assert webhook(client,intents[result['payment_intent_id']]).json()['state']=='confirmed'
    with sessions() as db:
        assert available_units(db,body['property_id'],ci,co)==0
        assert available_units(db,other_id,ci,co)==1
        assert db.query(Booking).count()==1
        assert db.get(PublicPayment,body['checkout_id']).state=='confirmed'


def test_stock_binding_changes_are_held_while_checkout_active(env):
    _,sessions,body,_,_=env
    checkout(env)
    with sessions() as db:
        p=db.get(Property,body['property_id'])
        r=StockResource(name='Synthetic mapping',capacity=1,time_zone=p.time_zone);db.add(r);db.commit()
        with pytest.raises(TaskError,match='active checkout holds'):
            configure_stock(db,actor='synthetic-admin',token=config_token(db,'synthetic-admin'),action='bind',
                property_id=p.id,resource_id=r.id,note='Change reviewed synthetic resource mapping')
        assert db.query(StockBinding).count()==0


def test_checkout_boundary_nights_remain_available(env):
    client,_,body,_,_=env
    checkout(env)
    first=datetime.fromisoformat(body['check_out']).date()
    args={'property_id':body['property_id'],'check_in':first.isoformat(),
        'check_out':(first+timedelta(days=2)).isoformat(),'guests':2}
    result=client.get('/api/public/quote',params=args).json()['result']
    assert result['bookable']
    assert checkout(env,checkout_id=str(uuid4()),check_in=args['check_in'],check_out=args['check_out'],quote_token=result['quote_token']).status_code==200


def test_late_success_can_confirm_if_inventory_is_still_free(env):
    client,sessions,_,intents,_=env
    checkout(env);expire(sessions)
    assert webhook(client,next(iter(intents.values()))).json()['state']=='confirmed'
    with sessions() as db:assert db.query(Booking).count()==1


def test_expired_holds_enter_admin_review_without_guest_details(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.db import get_db
    from app.operations.router import require_admin
    from app.operations.payment_router import router
    _,sessions,_,_,_=env
    checkout(env);expire(sessions)
    app=FastAPI();app.include_router(router)
    def database():
        with sessions() as db:yield db
    app.dependency_overrides[get_db]=database
    with TestClient(app) as client:
        assert client.get('/operations/payments?review=true').status_code==403
        app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
        response=client.get('/operations/payments?review=true')
        assert response.status_code==200 and '1 attempts need review' in response.text
        assert 'Inventory hold expired; this does not prove payment failure' in response.text
        assert 'test@example.com' not in response.text
