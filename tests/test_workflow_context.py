"""Synthetic cross-page workflow checks; never use online business records."""
import html
import re
from datetime import datetime
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from app.db import get_db
from app.models import (Booking,CleaningTask,CleaningStatus,MessageLog,OpsCommand,
    ReservationEvent,PlatformName)
from app.operations.router import router as calendar_router, require_admin, templates
from app.operations.parity_router import router
from test_operations import db,prop,booking


@pytest.fixture
def client(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-workflow-context-only')
    app=FastAPI();app.include_router(calendar_router);app.include_router(router)
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with TestClient(app,follow_redirects=False) as c:yield c


def create_form(body):
    return re.search(r'<form method="post" action="/operations/reservations/0".*?</form>',body,re.S).group()


@pytest.mark.parametrize('view',['grid','list'])
def test_calendar_carries_exact_property_and_dates_without_creating_records(db,client,view):
    prop(db,name='Synthetic first property');p=prop(db,name='Synthetic selected property')
    page=client.get(f'/operations/calendar?property_id={p.id}&start=2026-10-21&end=2026-10-23&view={view}')
    assert page.status_code==200
    link=html.unescape(re.search(r'href="([^"]*draft_check_in=[^"]+)"',page.text).group(1))
    assert f'property_id={p.id}&draft_check_in=2026-10-21&draft_check_out=2026-10-23#create-reservation' in link
    response=client.get(link);assert response.status_code==200
    form=create_form(response.text)
    assert f'<option value="{p.id}" selected>' in form
    assert 'name="check_in" value="2026-10-21"' in form
    assert 'name="check_out" value="2026-10-23"' in form
    assert 'no reservation has been created' in response.text
    assert all(db.query(model).count()==0 for model in [Booking,CleaningTask,ReservationEvent,OpsCommand,MessageLog])


def test_prefilled_form_keeps_original_save_and_conflict_checks(db,client):
    prop(db,name='Synthetic first');p=prop(db,name='Synthetic second')
    url=f'/operations/reservations?property_id={p.id}&draft_check_in=2026-10-21&draft_check_out=2026-10-23'
    def values():
        form=create_form(client.get(url).text)
        return dict(token=html.unescape(re.search(r'name="token" value="([^"]+)"',form).group(1)),
            property_id=str(p.id),check_in='2026-10-21',check_out='2026-10-23',amount='',
            guest_name='Synthetic workflow guest',guest_email='',note='Synthetic local workflow verification')
    response=client.post('/operations/reservations/0',data=values(),headers={'Origin':'http://testserver'})
    assert response.status_code==303
    b=db.query(Booking).one();assert b.property_id==p.id and b.guest.email is None
    task=db.query(CleaningTask).one();assert task.booking_id==b.id and task.assignee is None
    detail=client.get(response.headers['location']).text
    assert f'href="/cleaning#cleaning-task-{task.id}"' in detail
    response=client.post('/operations/reservations/0',data=values(),headers={'Origin':'http://testserver'})
    assert response.status_code==409 and db.query(Booking).count()==1
    assert db.query(CleaningTask).count()==1 and db.query(MessageLog).count()==0


@pytest.mark.parametrize('query',[
    'draft_check_in=2026-10-21&draft_check_out=2026-10-23',
    'property_id=1&draft_check_in=2026-10-21',
    'property_id=1&draft_check_in=2026-10-23&draft_check_out=2026-10-21',
    'property_id=1&draft_check_in=2026-10-21&draft_check_out=2026-10-21',
    'property_id=1&draft_check_in=2026-1-1&draft_check_out=2026-10-23',
    'property_id=1&draft_check_in=2026-01-01&draft_check_out=2028-01-01'])
def test_invalid_draft_context_is_rejected_without_writes(db,client,query):
    prop(db)
    assert client.get('/operations/reservations?'+query).status_code==422
    assert db.query(Booking).count()==db.query(CleaningTask).count()==0


def test_property_filter_is_respected_even_without_draft_dates(db,client):
    prop(db,name='Synthetic first');p=prop(db,name='Synthetic selected')
    form=create_form(client.get(f'/operations/reservations?property_id={p.id}').text)
    assert f'<option value="{p.id}" selected>' in form
    assert 'name="check_in" value=""' in form
    assert client.get('/operations/reservations?property_id=999&draft_check_in=2026-10-21&draft_check_out=2026-10-23').status_code==404


def test_cleaning_rows_link_back_and_only_visible_active_tasks_get_focus_links(db,client):
    p=prop(db);b=booking(db,p,platform=PlatformName.direct)
    other=booking(db,p,platform=PlatformName.direct)
    active=CleaningTask(booking_id=b.id,property_id=p.id,due_date=b.check_out,status=CleaningStatus.pending)
    done=CleaningTask(booking_id=other.id,property_id=p.id,due_date=other.check_out,status=CleaningStatus.done)
    db.add_all([active,done]);db.commit()
    body=templates.env.get_template('cleaning.html').render(request=Request({'type':'http','path':'/cleaning','headers':[]}),
        tasks=[active],done_tasks=[done],cleaners=[],linen_rows=[],now=datetime(2026,10,21))
    for t in [active,done]:
        assert f'id="cleaning-task-{t.id}"' in body
        assert f'href="/operations/reservations/{t.booking_id}"' in body
    assert f'href="/cleaning#cleaning-task-{active.id}"' in client.get(f'/operations/reservations/{b.id}').text
    assert 'href="/cleaning#' not in client.get(f'/operations/reservations/{other.id}').text
    active.cancelled_at=datetime(2026,10,21);db.commit()
    assert 'href="/cleaning#' not in client.get(f'/operations/reservations/{b.id}').text
