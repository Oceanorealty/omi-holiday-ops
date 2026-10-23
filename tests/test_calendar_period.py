from datetime import datetime
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.operations.calendar_period_router import router,period_month
from test_operations import db,prop,booking


def test_leap_month_grid_and_checkout_nights(db):
    p=prop(db)
    b=booking(db,p,check_in=datetime(2028,2,28),check_out=datetime(2028,3,1))
    feb=period_month(db,p.id,2028,2)
    cells=[cell for week in feb['weeks'] for cell in week if cell]
    assert len(cells)==29 and cells[-1]['date']=='2028-02-29'
    assert cells[-1]['booking_ids']==[b.id] and cells[-1]['available_units']==0
    march=period_month(db,p.id,2028,3)
    assert march['row']['days'][0]['booking_ids']==[] and march['row']['days'][0]['departures']==[b.id]


def test_year_view_scopes_listing_and_month_links_without_secrets(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','period-synthetic-'+32*'x')
    monkeypatch.setenv('ADMIN_USER','period-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    p=prop(db,name='Selected synthetic listing');q=prop(db,name='Unselected synthetic listing')
    booking(db,p,check_in=datetime(2028,2,28),check_out=datetime(2028,3,1),door_code='SECRET-PERIOD',guest_portal_token='SECRET-LINK')
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        path=f'/operations/calendar-period?property_id={p.id}&view=year&year=2028&month=2'
        assert client.get(path).status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('period-admin'))
        response=client.get(path);assert response.status_code==200
        assert response.text.count('id="night-')==366
        assert 'SECRET-' not in response.text
        assert f'property_id={p.id}&amp;view=month&amp;year=2028&amp;month=2#night-2028-02-29' in response.text
        # Property names remain available in the selector; only selected data is rendered.
        monthly=client.get(f'/operations/calendar-period?property_id={p.id}&view=month&year=2028&month=3')
        assert monthly.status_code==200 and 'Departures:' in monthly.text
        assert client.get('/operations/calendar-period?property_id=9999').status_code==404
        for query in ['year=1','year=9999','month=0','month=13','view=invalid','year=bad']:
            assert client.get('/operations/calendar-period?'+query).status_code==422


def test_period_empty_property_list_and_date_boundaries(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','period-synthetic-'+32*'x')
    from app.operations.router import require_admin
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with TestClient(app) as client:
        assert 'Create a property' in client.get('/operations/calendar-period').text
        p=prop(db)
        for year,month in [(2,1),(9998,12)]:
            response=client.get(f'/operations/calendar-period?year={year}&month={month}&property_id={p.id}')
            assert response.status_code==200
