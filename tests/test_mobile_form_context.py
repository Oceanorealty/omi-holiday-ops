"""Original HTTP form flows, synthetic data only; no external operations."""
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit, urlencode
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db import get_db
from app.models import Booking, BookingStatus, BookingAccount, BookingLedgerEntry, Cleaner, CleaningTask, ReservationEvent, PlatformName
from app.operations.parity_router import router, reservation_return
from app.operations.billing_router import router as billing_router
from app.operations.router import require_admin
from app.operations.reservations import reservation_token
from app.operations.stay_operations import token as attendance_token
from app.operations.billing import create_account, create_token, account_token, add_entry
from test_operations import db, prop, booking


class Page(HTMLParser):
    def __init__(self,text):
        super().__init__();self.inputs=[];self.links=[];self.forms=[];self.feed(text)
    def handle_starttag(self,tag,attrs):
        item=dict(attrs)
        if tag=='input':self.inputs.append(item)
        if tag=='a':self.links.append(item.get('href',''))
        if tag=='form':self.forms.append(item)
    def value(self,name):
        return next(i.get('value','') for i in self.inputs if i.get('name')==name)


@pytest.fixture
def sample(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-mobile-forms-'+'x'*32)
    row=booking(db,platform=PlatformName.direct)
    app=FastAPI();app.include_router(router);app.include_router(billing_router)
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with TestClient(app) as client:
        yield row,client


@pytest.mark.parametrize('target',[
    'https://unrelated.invalid/operations/reservations','//unrelated.invalid/operations/reservations',
    '/operations/reservations/7','/operations/reservations?next=https://unrelated.invalid',
    '/operations/reservations?property_id=1&property_id=2','/operations/reservations?page=999999',
    '/operations/reservations?start=2026-11-03&end=2026-11-01','/operations/reservations?q=%0D%0Ainjected',
])
def test_return_context_rejects_external_or_invalid_targets(target):
    assert reservation_return(target)=='/operations/reservations'


def test_list_detail_edit_and_return_retain_allowed_filters(db,sample):
    row,client=sample
    target='/operations/reservations?'+urlencode(dict(property_id=row.property_id,status='all',q=str(row.id),start='2026-10-20',end='2026-11-01'))
    page=Page(client.get(target).text)
    detail=next(url for url in page.links if url.startswith(f'/operations/reservations/{row.id}?'))
    assert parse_qs(urlsplit(detail).query)['return_to']==[target]
    form=Page(client.get(detail).text)
    assert form.value('return_to')==target and target in form.links
    result=client.post(f'/operations/reservations/{row.id}',data=dict(token=reservation_token('synthetic-admin',row),
        property_id=row.property_id,check_in='2026-10-24',check_out='2026-10-26',amount='200',
        note='Synthetic reviewed date change',return_to=target),follow_redirects=False)
    assert result.status_code==303 and parse_qs(urlsplit(result.headers['location']).query)['return_to']==[target]
    assert target in Page(client.get(result.headers['location']).text).links


def test_conflicting_reservation_html_preserves_dates_not_free_text(db,sample):
    row,client=sample
    data=dict(token=reservation_token('synthetic-admin'),property_id=row.property_id,check_in='2026-10-21',
        check_out='2026-10-23',amount='175.00',guest_name='SENSITIVE_INPUT_NAME',guest_email='private-input@example.invalid',
        note='SENSITIVE_INPUT_NOTE',return_to=f'/operations/reservations?property_id={row.property_id}')
    response=client.post('/operations/reservations/0',data=data,headers={'Accept':'text/html'})
    assert response.status_code==409 and 'Review before saving again' in response.text
    page=Page(response.text)
    assert page.value('check_in')=='2026-10-21' and page.value('check_out')=='2026-10-23' and page.value('amount')=='175.00'
    assert any(f.get('action')=='/operations/reservations/0' for f in page.forms)
    assert 'SENSITIVE_INPUT' not in response.text and 'private-input@example.invalid' not in response.text
    assert db.query(Booking).count()==1 and db.query(ReservationEvent).count()==0


def test_attendance_html_error_retains_time_and_context_without_state_change(db,sample):
    row,client=sample
    target=f'/operations/reservations?property_id={row.property_id}&status=all'
    data=dict(token=attendance_token(db,'synthetic-admin',row),action='arrive',arrived_at='2090-01-01T14:00',
        note='SENSITIVE_INPUT_NOTE',return_to=target)
    response=client.post(f'/operations/reservations/{row.id}/attendance',data=data,headers={'Accept':'text/html'})
    page=Page(response.text)
    assert response.status_code==409 and 'Actual attendance cannot be in the future' in response.text
    assert page.value('arrived_at')=='2090-01-01T14:00' and target in page.links
    assert 'SENSITIVE_INPUT_NOTE' not in response.text and db.query(ReservationEvent).count()==0
    assert any(f.get('action')==f'/operations/reservations/{row.id}' for f in page.forms)
    api=client.post(f'/operations/reservations/{row.id}/attendance',data=data)
    assert api.status_code==409 and api.json()['detail']=='Actual attendance cannot be in the future'


def test_refund_error_preserves_safe_fields_never_replays_or_relaxes_limit(db,sample):
    row,client=sample
    create_account(db,actor='synthetic-admin',booking_id=row.id,token=create_token('synthetic-admin',row),
        currency='AUD',opening_amount='200',note='Synthetic account evidence')
    acc=db.get(BookingAccount,row.id)
    payment=add_entry(db,actor='synthetic-admin',booking_id=row.id,token=account_token('synthetic-admin',acc),
        kind='payment',amount='80',description='Synthetic payment',occurred_on='2026-10-01',reference='synthetic-payment',note='Synthetic receipt evidence')
    data=dict(token=account_token('synthetic-admin',acc),kind='refund',amount='100',description='SENSITIVE_INPUT_DESCRIPTION',
        occurred_on='2026-10-02',reference='SENSITIVE_INPUT_REFERENCE',payment_id=str(payment),note='SENSITIVE_INPUT_NOTE')
    response=client.post(f'/operations/accounts/{row.id}/entries',data=data,headers={'Accept':'text/html'})
    assert response.status_code==409 and 'cannot exceed' in response.text
    page=Page(response.text)
    assert page.value('amount')=='100' and page.value('occurred_on')=='2026-10-02'
    assert 'value="refund" selected' in response.text and 'SENSITIVE_INPUT' not in response.text
    assert db.query(BookingLedgerEntry).count()==2
    data.update(amount='20',description='Synthetic reviewed refund',reference='synthetic-refund20',note='Synthetic reviewed receipt evidence')
    # Account pages contain several purposes; use the entry token explicitly, never replay a captured POST.
    data['token']=account_token('synthetic-admin',acc)
    assert client.post(f'/operations/accounts/{row.id}/entries',data=data,follow_redirects=False).status_code==303
    assert client.post(f'/operations/accounts/{row.id}/entries',data=data,headers={'Accept':'text/html'}).status_code==409
    assert db.query(BookingLedgerEntry).count()==3


def test_origin_and_permission_guards_still_fail_closed(db,sample):
    row,client=sample
    response=client.post(f'/operations/reservations/{row.id}/attendance',data=dict(token='bad',action='arrive',note='Synthetic origin test'),
        headers={'Accept':'text/html','Origin':'https://unrelated.invalid'})
    assert response.status_code==403 and 'Review before saving again' not in response.text
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as outsider:
        assert outsider.get(f'/operations/reservations/{row.id}?return_to=/operations/reservations',headers={'Accept':'text/html'}).status_code==403


def test_stale_cleaner_html_error_keeps_queue_and_original_assignment(db,monkeypatch):
    from test_cleaner_assignment_availability import setup_client
    client,task_id,cleaner_id=setup_client(db,monkeypatch)
    db.get(Cleaner,cleaner_id).active=False;db.commit()
    with client:
        response=client.post(f'/cleaning/{task_id}/assign',data=dict(cleaner_id=cleaner_id,queue_property_id='1',
            queue_view='unassigned',assignee='SENSITIVE_INPUT_ASSIGNEE'),headers={'Accept':'text/html'})
    assert response.status_code==409 and 'Review before saving again' in response.text
    assert 'SENSITIVE_INPUT_ASSIGNEE' not in response.text and 'view=unassigned' in response.text
    assert db.get(CleaningTask,task_id).cleaner_id is None
