from datetime import datetime,date,timedelta,timezone
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import BookingWindow,Property
from app.operations.booking_windows import window_token,save_window,window_reason
from app.operations.booking_window_router import router
from app.operations.tasks import TaskError
from app.public_api import get_quote,get_calendar
from test_operations import db,prop

@pytest.fixture
def property(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','window-synthetic-'+32*'x');monkeypatch.setenv('ADMIN_USER','window-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    return prop(db,published=True,max_guests=3,base_nightly_rate=100)

def save(db,property,**changes):
    args=dict(actor='window-admin',property_id=property.id,token=window_token(db,'window-admin',property.id),minimum_notice_days=0,maximum_advance_days=90,same_day_cutoff='14:00',note='Synthetic booking window rule')
    args.update(changes);return save_window(db,**args)

def test_local_midnight_inclusive_bounds_and_cutoff(db,property):
    save(db,property)
    now=datetime(2026,10,23,3,59,tzinfo=timezone.utc) # Brisbane 13:59
    assert window_reason(db,property.id,date(2026,10,23),now=now) is None
    assert window_reason(db,property.id,date(2026,10,23),now=now+timedelta(minutes=1))=='same_day_cutoff'
    assert window_reason(db,property.id,date(2026,10,22),now=now)=='minimum_notice'
    assert window_reason(db,property.id,date(2027,1,21),now=now) is None
    assert window_reason(db,property.id,date(2027,1,22),now=now)=='maximum_advance'
    save(db,property,minimum_notice_days=2)
    assert window_reason(db,property.id,date(2026,10,24),now=now)=='minimum_notice'
    assert window_reason(db,property.id,date(2026,10,25),now=now) is None

@pytest.mark.parametrize('changes',[{'minimum_notice_days':-1},{'maximum_advance_days':1096},{'minimum_notice_days':3,'maximum_advance_days':2},{'same_day_cutoff':'24:00'},{'note':'short'}])
def test_invalid_window_no_partial_save(db,property,changes):
    with pytest.raises(TaskError):save(db,property,**changes)
    assert db.query(BookingWindow).count()==0

def test_stale_form_and_invalid_timezone_fail_closed(db,property):
    stale=window_token(db,'window-admin',property.id);save(db,property)
    with pytest.raises(TaskError):save(db,property,token=stale)
    property.time_zone='Invalid/Zone';db.commit()
    assert window_reason(db,property.id,date.today())=='booking_timezone_needs_review'
    with pytest.raises(TaskError):save(db,property)

def test_quote_and_calendar_consume_window_without_hiding_physical_stock(db,property):
    save(db,property,minimum_notice_days=3)
    now=datetime.now(timezone.utc)+timedelta(hours=10);start=now.date()+timedelta(days=1);end=start+timedelta(days=1)
    quote=get_quote(property_id=property.id,check_in=str(start),check_out=str(end),guests=1,db=db)['result']
    assert not quote['bookable'] and quote['reason']=='minimum_notice'
    day=get_calendar(property_id=property.id,start=str(start),end=str(end),db=db)['result']['days'][0]
    assert day['closed_arrival'] and day['booking_window_reason']=='minimum_notice' and day['available_units']==1

def test_window_page_admin_only_and_origin_protected(db,property):
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get('/operations/booking-windows').status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('window-admin'))
        assert client.get('/operations/booking-windows').status_code==200
        assert client.post(f'/operations/booking-windows/{property.id}',data={'token':window_token(db,'window-admin',property.id),'minimum_notice_days':'0','note':'Synthetic cross origin'},headers={'Origin':'https://evil.invalid'}).status_code==403
