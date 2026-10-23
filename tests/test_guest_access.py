from datetime import datetime,timedelta,timezone
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import BookingAccount,BookingAgreement,GuestAccessRule,OpsCommand
from app.operations.guest_access import (rule_token,save_rule,portal_fields,clearance_token,review_account,account_is_clear)
from app.operations.guest_access_router import router
from app.operations.guest_checkin_router import router as guest_router
from app.operations.guest_checkin import submit_checkin,checkin_token
from app.operations.billing import create_account,create_token,add_entry,account_token,issue_statement
from app.operations.agreements import create_version,policy_token,issue_agreement,issue_token,accept_agreement,acceptance_token
from app.operations.tasks import TaskError
from test_operations import db,prop,booking


@pytest.fixture
def stay(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','access-synthetic-'+32*'x')
    monkeypatch.setenv('ADMIN_USER','access-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    start=datetime.utcnow()+timedelta(days=7)
    return booking(db,prop(db,wifi_info='PRIVATE-WIFI',check_in_instructions='PRIVATE-INSTRUCTIONS',house_rules='Synthetic rules'),
        check_in=start,check_out=start+timedelta(days=2),door_code='PRIVATE-DOOR',guest_portal_token='synthetic-access-link')


def save(db,stay,field='door_code',**changes):
    row=db.query(GuestAccessRule).filter_by(property_id=stay.property_id,field=field).first()
    args=dict(actor='access-admin',property_id=stay.property_id,field=field,token=rule_token('access-admin',stay.property_id,field,row),
        opening_hours='',require_arrival=False,require_agreement=False,require_account_clear=False,hide_after_checkout=False,note='Configure synthetic portal rule only')
    args.update(changes);save_rule(db,**args)


def test_no_rule_preserves_current_display_and_per_field_time_gate(db,stay):
    assert portal_fields(db,stay)['released_fields']['door_code']=='PRIVATE-DOOR'
    save(db,stay,opening_hours='24',hide_after_checkout=True)
    start=stay.check_in.replace(tzinfo=timezone(timedelta(hours=10))).astimezone(timezone.utc)
    fields=portal_fields(db,stay,now=start-timedelta(hours=24,seconds=1))['released_fields']
    assert fields['door_code'] is None and fields['wifi_info']=='PRIVATE-WIFI'
    assert portal_fields(db,stay,now=start-timedelta(hours=24))['released_fields']['door_code']=='PRIVATE-DOOR'
    end=stay.check_out.replace(tzinfo=timezone(timedelta(hours=10))).astimezone(timezone.utc)
    assert portal_fields(db,stay,now=end)['released_fields']['door_code'] is None


@pytest.mark.parametrize('value',['-1','8761','1.5','abc','01'])
def test_invalid_hour_rules_rollback(db,stay,value):
    with pytest.raises(TaskError):save(db,stay,opening_hours=value)
    assert db.query(GuestAccessRule).count()==0


def test_rule_stale_version_and_purpose_are_rejected(db,stay):
    stale=rule_token('access-admin',stay.property_id,'door_code',None)
    save(db,stay,require_arrival=True)
    with pytest.raises(TaskError):save(db,stay,token=stale)
    with pytest.raises(TaskError):save(db,stay,field='wifi_info',token=stale)
    assert db.query(GuestAccessRule).count()==1


def test_current_checkin_and_agreement_both_required_and_rechecked(db,stay):
    save(db,stay,require_arrival=True,require_agreement=True)
    assert not portal_fields(db,stay)['guest_access_status']['door_code']['allowed']
    submit_checkin(db,portal_token=stay.guest_portal_token,token=checkin_token(db,stay),contact_name='Synthetic guest',
        contact_phone='',party_size=1,arrival_time='',requests='',acknowledged=True)
    create_version(db,actor='access-admin',property_id=stay.property_id,token=policy_token(db,'access-admin',stay.property_id),
        title='Synthetic non-binding example',body='Synthetic test only. This is not a real contractual agreement.',note='Prepare a demonstration policy')
    entity_id=issue_agreement(db,actor='access-admin',booking_id=stay.id,token=issue_token(db,'access-admin',stay),note='Issue a demonstration snapshot')
    row=db.get(BookingAgreement,entity_id)
    assert not portal_fields(db,stay)['guest_access_status']['door_code']['allowed']
    accept_agreement(db,portal_token=stay.guest_portal_token,agreement_id=row.id,token=acceptance_token(stay,row),typed_name='Synthetic tester',acknowledged=True)
    assert portal_fields(db,stay)['guest_access_status']['door_code']['allowed']
    stay.check_in+=timedelta(days=1);db.commit()
    assert not portal_fields(db,stay)['guest_access_status']['door_code']['allowed']


def setup_account(db,stay,opening='100'):
    create_account(db,actor='access-admin',booking_id=stay.id,token=create_token('access-admin',stay),currency='AUD',opening_amount=opening,note='Synthetic reviewed opening amount')


def payment(db,stay,**changes):
    account=db.get(BookingAccount,stay.id)
    args=dict(actor='access-admin',booking_id=stay.id,token=account_token('access-admin',account),kind='payment',amount='100',
        description='Synthetic external receipt',occurred_on=datetime.utcnow().date().isoformat(),reference='demo-access-payment',note='Verified synthetic external receipt')
    args.update(changes);return add_entry(db,**args)


def review(db,stay,**changes):
    args=dict(actor='access-admin',booking_id=stay.id,token=clearance_token(db,'access-admin',stay),note='Review charges against this synthetic stay')
    args.update(changes);review_account(db,**args)


def test_account_clearance_requires_charges_balance_and_current_operator_review(db,stay):
    save(db,stay,require_account_clear=True)
    assert not account_is_clear(db,stay)
    setup_account(db,stay)
    with pytest.raises(TaskError):review(db,stay)
    payment(db,stay);review(db,stay)
    assert portal_fields(db,stay)['released_fields']['door_code']=='PRIVATE-DOOR'
    account=db.get(BookingAccount,stay.id)
    issue_statement(db,actor='access-admin',booking_id=stay.id,token=account_token('access-admin',account,'account-statement'),note='Synthetic statement only no money changes')
    assert account_is_clear(db,stay)
    stale=clearance_token(db,'access-admin',stay)
    stay.amount=300;db.commit()
    assert not account_is_clear(db,stay)
    with pytest.raises(TaskError):review(db,stay,token=stale)
    review(db,stay)
    payment(db,stay,kind='charge',amount='20',reference='')
    assert not account_is_clear(db,stay)
    assert portal_fields(db,stay)['released_fields']['door_code'] is None


def test_empty_account_is_not_cleared_and_stale_ledger_review_rejected(db,stay):
    setup_account(db,stay,opening='')
    with pytest.raises(TaskError):review(db,stay)
    stale=clearance_token(db,'access-admin',stay)
    payment(db,stay,kind='charge',reference='')
    payment(db,stay)
    with pytest.raises(TaskError):review(db,stay,token=stale)
    review(db,stay)
    assert account_is_clear(db,stay)


def test_portal_masks_values_server_side_and_admin_write_requires_origin_and_actor(db,stay):
    save(db,stay,require_arrival=True)
    app=FastAPI();app.include_router(router);app.include_router(guest_router)
    app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        page=client.get('/guest/'+stay.guest_portal_token)
        assert page.status_code==200 and 'PRIVATE-DOOR' not in page.text and 'PRIVATE-WIFI' in page.text
        assert 'Submit current arrival details' in page.text
        assert page.headers['cache-control']=='private, no-store'
        assert client.get('/operations/guest-access').status_code==403
        assert client.post(f'/operations/guest-access/{stay.property_id}/door_code',data={}).status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('access-admin'))
        admin=client.get('/operations/guest-access');assert admin.status_code==200
        row=db.query(GuestAccessRule).one()
        data={'token':rule_token('access-admin',stay.property_id,'door_code',row),'note':'Synthetic reset for browser test'}
        assert client.post(f'/operations/guest-access/{stay.property_id}/door_code',data=data,headers={'Origin':'https://evil.invalid'}).status_code==403
        assert client.post(f'/operations/guest-access/{stay.property_id}/door_code',data=data).status_code==200
        assert 'PRIVATE-DOOR' in client.get('/guest/'+stay.guest_portal_token).text


def test_time_gate_uses_actual_hours_across_daylight_saving(db,stay):
    from zoneinfo import ZoneInfo
    stay.property.time_zone='Australia/Sydney'
    stay.check_in=datetime(2030,10,6,15);stay.check_out=datetime(2030,10,8,10);db.commit()
    save(db,stay,opening_hours='24')
    start=stay.check_in.replace(tzinfo=ZoneInfo('Australia/Sydney')).astimezone(timezone.utc)
    assert portal_fields(db,stay,now=start-timedelta(hours=24,seconds=1))['released_fields']['door_code'] is None
    assert portal_fields(db,stay,now=start-timedelta(hours=24))['released_fields']['door_code']=='PRIVATE-DOOR'


def test_invalid_timezone_fails_closed_for_configured_rule(db,stay):
    save(db,stay,opening_hours='24')
    stay.property.time_zone='Invalid/Zone';db.commit()
    assert portal_fields(db,stay)['released_fields']['door_code'] is None
    assert portal_fields(db,stay)['released_fields']['wifi_info']=='PRIVATE-WIFI'
