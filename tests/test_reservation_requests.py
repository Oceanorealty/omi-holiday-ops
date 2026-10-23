from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
import html

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from app.auth import SESSION_COOKIE, make_session_cookie, _sign
from app.db import get_db
from app.models import Booking, CleaningTask, Guest, OpsCommand, ReservationEvent, ReservationRequest
from app.operations.reservation_requests import parse_quote, review_request, submit_request
from app.operations.reservation_request_router import router
from app.operations.tasks import TaskError, form_token
from app.public_api import _make_quote_token, get_quote
from test_operations import db, prop, booking


@pytest.fixture
def setup(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-request-key-'+'x'*32)
    monkeypatch.setenv('ADMIN_USER','request-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    p=prop(db,published=True,max_guests=3,base_nightly_rate=Decimal('200'))
    start=(datetime.now(timezone.utc)+timedelta(days=7)).date()
    end=start+timedelta(days=2)
    quote=get_quote(property_id=p.id,check_in=start.isoformat(),check_out=end.isoformat(),guests=2,db=db)['result']
    return p,start,end,quote


def submit(db,quote,**changes):
    args=dict(quote_token=quote['quote_token'],guest_name='Synthetic guest',guest_email='guest@example.invalid')
    args.update(changes)
    return submit_request(db,**args)


def approve(db,row,**changes):
    args=dict(actor='request-admin',entity_id=row.id,token=form_token('request-admin',row,purpose='reservation-request-review'),
        action='approve',note='Reviewed availability and accommodation quote')
    args.update(changes)
    return review_request(db,**args)


def test_quote_tokens_are_unique_and_request_does_not_reserve_stock(db,setup):
    p,start,end,quote=setup
    another=get_quote(property_id=p.id,check_in=start.isoformat(),check_out=end.isoformat(),guests=2,db=db)['result']
    assert another['quote_token']!=quote['quote_token']
    reference=submit(db,quote)
    assert submit(db,quote)==reference
    assert db.query(ReservationRequest).count()==1 and db.query(Booking).count()==db.query(CleaningTask).count()==0
    assert db.query(OpsCommand).count()==1
    assert parse_quote(quote['quote_token'])['total']==400
    with pytest.raises(TaskError):submit(db,quote,guest_email='another@example.invalid')


def test_review_creates_exactly_one_booking_and_cannot_replay(db,setup):
    _,_,_,quote=setup;submit(db,quote)
    row=db.query(ReservationRequest).one()
    stale=dict(actor='request-admin',entity_id=row.id,token=form_token('request-admin',row,purpose='reservation-request-review'),action='approve',note='Repeat previously reviewed reservation request')
    booking_id=approve(db,row)
    b=db.get(Booking,booking_id)
    assert row.state=='approved' and row.booking_id==b.id and b.amount==400
    assert b.guest.email=='guest@example.invalid' and not b.notifications_enabled
    assert db.query(CleaningTask).count()==1 and db.query(ReservationEvent).count()==1
    with pytest.raises(TaskError):review_request(db,**stale)
    assert db.query(Booking).count()==1


@pytest.mark.parametrize('change',['price','inventory','capacity','published','past'])
def test_approval_rechecks_conditions_and_preserves_pending_request(db,setup,change):
    p,start,end,quote=setup;submit(db,quote);row=db.query(ReservationRequest).one()
    if change=='price':p.base_nightly_rate=999
    elif change=='inventory':booking(db,p,check_in=datetime.combine(start,datetime.min.time()),check_out=datetime.combine(end,datetime.min.time()))
    elif change=='capacity':p.max_guests=1
    elif change=='published':p.published=False
    else:row.check_in='2020-01-01';row.check_out='2020-01-03'
    db.commit();before=db.query(Booking).count()
    with pytest.raises(TaskError):approve(db,row)
    assert row.state=='pending' and row.booking_id is None and row.version==1
    assert db.query(Booking).count()==before and db.query(ReservationEvent).count()==0


def test_rejection_and_invalid_note_do_not_create_booking(db,setup):
    _,_,_,quote=setup;submit(db,quote);row=db.query(ReservationRequest).one()
    with pytest.raises(TaskError):approve(db,row,note='short')
    assert row.state=='pending' and row.version==1 and db.query(Guest).count()==0
    approve(db,row,action='reject')
    assert row.state=='rejected' and db.query(Booking).count()==0


def test_quote_expiry_tampering_and_invalid_guest_input(db,setup):
    p,start,end,quote=setup
    expired=_make_quote_token(p.id,start,end,2,Decimal('400'),datetime.now(timezone.utc)-timedelta(seconds=1))
    for token in (expired,quote['quote_token']+'x',_sign('not-a-quote')):
        with pytest.raises(TaskError):submit(db,quote,quote_token=token)
    for fields in ({'guest_name':''},{'guest_email':'bad'},{'guest_email':'with space@example.invalid'}):
        with pytest.raises(TaskError):submit(db,quote,**fields)
    assert db.query(ReservationRequest).count()==0


def test_simultaneous_approval_of_overlapping_requests_does_not_oversell(db,setup):
    p,start,end,quote=setup
    submit(db,quote)
    second=get_quote(property_id=p.id,check_in=start.isoformat(),check_out=end.isoformat(),guests=2,db=db)['result']
    submit(db,second,guest_name='Second synthetic guest')
    ids=[r.id for r in db.query(ReservationRequest).order_by(ReservationRequest.id)]
    factory=sessionmaker(bind=db.bind)
    def run(entity_id):
        with factory() as session:
            row=session.get(ReservationRequest,entity_id)
            try:approve(session,row);return 'saved'
            except TaskError:return 'held'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run,ids))==['held','saved']
    assert db.query(Booking).count()==1


def test_public_request_form_and_review_permissions(db,setup):
    p,start,end,quote=setup
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get(f'/api/public/reservation-request?property_id={p.id}').status_code==200
        url=f'/api/public/reservation-request?property_id={p.id}&check_in={start}&check_out={end}&guests=2'
        page=client.get(url);assert page.status_code==200
        token=html.unescape(re.search(r'name="quote_token" type="hidden" value="([^"]+)"',page.text).group(1))
        assert client.get('/operations/reservation-requests').status_code==403
        values={'quote_token':token,'guest_name':'Synthetic guest','guest_email':'guest@example.invalid'}
        assert client.post('/api/public/reservation-request',data=values,headers={'origin':'https://elsewhere.invalid'}).status_code==403
        result=client.post('/api/public/reservation-request',data=values)
        assert result.status_code==200 and 'does not reserve inventory' in result.text
        assert 'guest@example.invalid' not in result.text
        client.cookies.set(SESSION_COOKIE,make_session_cookie('request-admin'))
        assert client.get('/operations/reservation-requests?property_id=').status_code==200
        row=db.query(ReservationRequest).one()
        token=form_token('request-admin',row,purpose='reservation-request-review')
        result=client.post(f'/operations/reservation-requests/{row.id}',data={'token':token,'action':'reject','note':'Rejected synthetic request during review'})
        assert result.status_code==200 and row.state=='rejected'
