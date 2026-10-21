from datetime import date, datetime
import re
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE, make_session_cookie
from app.db import get_db
from app.models import BookingStatus
from app.operations.calendar import calendar_data
from app.operations.router import router
from test_operations import db, prop, booking


def test_night_counts_exclude_checkout_and_cancelled(db):
    p=prop(db)
    b=booking(db,p)
    booking(db,p,status=BookingStatus.cancelled)
    result=calendar_data(db,date(2026,10,21),date(2026,10,24))['rows'][0]
    assert result['reserved_nights']==2 and result['reserved_night_pct']==66.7
    assert result['days'][2]['booking_ids']==[]
    assert result['days'][2]['departures']==[b.id]


def test_blocks_conflicts_and_missing_sources_remain_visible(db):
    p=prop(db)
    b=booking(db,p,source_missing_since=datetime(2026,10,21))
    block=booking(db,p,is_block=True,check_in=datetime(2026,10,22),check_out=datetime(2026,10,23))
    r=calendar_data(db,date(2026,10,21),date(2026,10,24))['rows'][0]
    assert r['sellable_nights']==2 and r['blocked_nights']==1
    assert r['conflict_days']==1 and r['reserved_night_pct'] is None
    assert r['days'][1]['conflict_ids']==[b.id,block.id]
    assert r['days'][0]['source_review_ids']==[b.id]


def test_adjacent_stays_do_not_conflict_and_properties_are_scoped(db):
    p=prop(db);q=prop(db,name='Other')
    booking(db,p,check_out=datetime(2026,10,22,10))
    booking(db,p,check_in=datetime(2026,10,22,14),check_out=datetime(2026,10,23,10))
    booking(db,q)
    r=calendar_data(db,date(2026,10,21),date(2026,10,24),p.id)
    assert len(r['rows'])==1 and r['rows'][0]['conflict_days']==0
    assert r['rows'][0]['reserved_nights']==2
    assert len(r['rows'][0]['evidence'])==2


def test_invalid_dates_and_fully_blocked_ratio_not_invented(db):
    p=prop(db)
    booking(db,p,check_out=datetime(2026,10,20))
    r=calendar_data(db,date(2026,10,21),date(2026,10,22))['rows'][0]
    assert r['invalid_booking_ids'] and r['reserved_night_pct'] is None
    q=prop(db);booking(db,q,is_block=True)
    r=calendar_data(db,date(2026,10,21),date(2026,10,23),q.id)['rows'][0]
    assert r['sellable_nights']==0 and r['reserved_night_pct'] is None


@pytest.mark.parametrize('length',[0,-1,63])
def test_period_bounds(db,length):
    from datetime import timedelta
    with pytest.raises(ValueError):calendar_data(db,date(2026,10,21),date(2026,10,21)+timedelta(days=length))


def test_calendar_routes_admin_only_and_no_secret_output(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','calendar-synthetic-key-'+'x'*32)
    monkeypatch.setenv('ADMIN_USER','calendar-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-password')
    p=prop(db,name='<script>unsafe</script>');booking(db,p,door_code='SECRET_DOOR',guest_portal_token='SECRET_PORTAL')
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        url='/operations/calendar?start=2026-10-21&end=2026-10-24'
        assert client.get(url).status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('calendar-admin'))
        response=client.get(url)
        assert response.status_code==200 and '&lt;script&gt;' in response.text
        assert '<script>unsafe</script>' not in response.text
        api=client.get('/api/ops/calendar?start=2026-10-21&end=2026-10-24')
        assert api.status_code==200
        assert 'SECRET_DOOR' not in api.text and 'SECRET_PORTAL' not in response.text
        assert client.get(url+'&property_id=9999').status_code==404
        assert client.get('/api/ops/calendar?start=2026-10-24&end=2026-10-21').status_code==422
        overview=client.get('/operations').text
        title=re.search(r'<title>(.*?)</title>',overview,re.S).group(1)
        assert '<form' not in title and overview.count('Resolve a review')==1
