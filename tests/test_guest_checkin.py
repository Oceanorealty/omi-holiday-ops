from datetime import date, datetime, timedelta
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE, make_session_cookie
from app.db import get_db
from app.models import BookingStatus, GuestCheckin, GuestCheckinEvent
from app.operations.guest_checkin import checkin_token, find_guest_booking, is_current, submit_checkin
from app.operations.guest_checkin_router import router
from app.operations.tasks import TaskError
from test_operations import db, prop, booking


@pytest.fixture
def stay(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-checkin-key-'+'x'*32)
    monkeypatch.setenv('ADMIN_USER','checkin-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    p=prop(db,max_guests=3,house_rules='Synthetic rule: keep shared areas quiet')
    # Relative future dates make tests independent of the host's wall clock.
    start=datetime.utcnow()+timedelta(days=7)
    return booking(db,p,check_in=start,check_out=start+timedelta(days=2),guest_portal_token='synthetic-stay-link')


def args(db,stay,**changes):
    values=dict(portal_token=stay.guest_portal_token,token=checkin_token(db,stay),contact_name='Synthetic guest',
        contact_phone='+61 400 000 000',party_size=2,arrival_time='14:30',requests='Please confirm luggage drop-off',acknowledged=True)
    values.update(changes)
    return values


def test_arrival_submission_versions_and_audit_are_not_actual_attendance(db,stay):
    first=args(db,stay)
    submit_checkin(db,**first)
    row=db.query(GuestCheckin).one()
    assert row.version==1 and row.party_size==2 and is_current(stay,row)
    with pytest.raises(TaskError):submit_checkin(db,**first)
    submit_checkin(db,**args(db,stay,arrival_time='16:00'))
    assert row.version==2 and db.query(GuestCheckinEvent).count()==2
    payload=json.loads(db.query(GuestCheckinEvent).order_by(GuestCheckinEvent.id).first().payload)
    assert payload['arrival_time']=='14:30' and 'guest_portal_token' not in payload
    assert stay.status==BookingStatus.confirmed


def test_dates_rules_and_cancelled_stays_invalidate_old_forms(db,stay):
    old=args(db,stay)
    stay.check_out+=timedelta(days=1);db.commit()
    with pytest.raises(TaskError):submit_checkin(db,**old)
    submit_checkin(db,**args(db,stay))
    row=db.query(GuestCheckin).one();old=args(db,stay)
    stay.property.house_rules='Updated synthetic house rule';db.commit()
    assert not is_current(stay,row)
    with pytest.raises(TaskError):submit_checkin(db,**old)
    assert row.rules_snapshot=='Synthetic rule: keep shared areas quiet'
    submit_checkin(db,**args(db,stay))
    assert is_current(stay,row) and row.rules_snapshot=='Updated synthetic house rule'
    old=args(db,stay);stay.status=BookingStatus.cancelled;db.commit()
    with pytest.raises(LookupError):submit_checkin(db,**old)
    assert find_guest_booking(db,stay.guest_portal_token) is None


@pytest.mark.parametrize('changes',[dict(party_size=0),dict(party_size=4),dict(contact_name=' '),dict(contact_name='x'*161),
    dict(arrival_time='24:00'),dict(arrival_time='9:30'),dict(contact_phone='not a phone'),dict(requests='x'*2001),
    dict(acknowledged=False),dict(token='forged')])
def test_invalid_guest_submission_has_no_partial_write(db,stay,changes):
    with pytest.raises(TaskError):submit_checkin(db,**args(db,stay,**changes))
    assert db.query(GuestCheckin).count()==db.query(GuestCheckinEvent).count()==0


def test_another_guest_token_and_expired_stay_are_denied(db,stay):
    other=booking(db,guest_portal_token='different-synthetic-link')
    values=args(db,stay);values['portal_token']=other.guest_portal_token
    with pytest.raises(TaskError):submit_checkin(db,**values)
    stay.check_in=datetime(2020,1,1);stay.check_out=datetime(2020,1,3);db.commit()
    with pytest.raises(TaskError):submit_checkin(db,**args(db,stay))


def test_guest_routes_do_not_require_staff_cookie_and_hide_other_stays(db,stay):
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        url='/guest/'+stay.guest_portal_token
        result=client.get(url)
        assert result.status_code==200 and result.headers['Referrer-Policy']=='same-origin'
        assert client.get('/guest/unknown').status_code==404
        assert client.get('/operations/guest-checkins').status_code==403
        values=args(db,stay,contact_name='<script>guest</script>');values.pop('portal_token');values['acknowledged']='1'
        assert client.post(url+'/check-in',data=values,headers={'origin':'https://elsewhere.invalid'}).status_code==403
        result=client.post(url+'/check-in',data=values)
        assert result.status_code==200 and '&lt;script&gt;guest&lt;/script&gt;' in result.text
        client.cookies.set(SESSION_COOKIE,make_session_cookie('checkin-admin'))
        result=client.get('/operations/guest-checkins?property_id=')
        assert result.status_code==200 and 'Synthetic' in result.text
        assert stay.guest_portal_token not in result.text
        stay.status=BookingStatus.cancelled;db.commit()
        result=client.get(url)
        assert result.status_code==404 and 'Door code' not in result.text
