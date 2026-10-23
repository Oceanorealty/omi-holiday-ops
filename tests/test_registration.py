import json
from datetime import timedelta
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import GuestRegistration,GuestRegistrationEvent,GuestCheckin
from app.operations.registration import configuration,config_token,configure,registration_token,save_registration,registration_current,registration_export
from app.operations.registration_router import router
from app.operations.guest_checkin import submit_checkin,is_current
from app.operations.tasks import TaskError
from test_guest_checkin import db,stay,args


def setup(db,stay,questions='*Expected arrival transport\nOptional requests',require_adults=True):
    configure(db,actor='checkin-admin',property_id=stay.property_id,token=config_token(db,'checkin-admin',stay.property_id),require_adults=require_adults,questions=questions,note='Synthetic registration requirements')


def save(db,stay,**changes):
    config,_=configuration(db,stay.property_id)
    values=dict(portal_token=stay.guest_portal_token,token=registration_token(db,stay),action='submit',adult_names='Synthetic adult one\nSynthetic adult two',children=0,party_size=2,answers={q['key']:'Synthetic answer' for q in config['questions']},confirmed=True)
    values.update(changes);return save_registration(db,**values)


def test_draft_is_partial_and_submit_satisfies_only_registration_gate(db,stay):
    setup(db,stay);save(db,stay,action='draft',adult_names='',answers={},confirmed=False)
    assert not registration_current(db,stay)
    row=db.get(GuestRegistration,stay.id);assert row.state=='draft'
    old=registration_token(db,stay)
    save(db,stay)
    assert row.state=='submitted' and row.version==2 and registration_current(db,stay)
    assert not is_current(stay,None)
    submit_checkin(db,**args(db,stay))
    assert is_current(stay,db.query(GuestCheckin).one())
    with pytest.raises(TaskError):save(db,stay,token=old)
    assert db.query(GuestRegistrationEvent).count()==2


@pytest.mark.parametrize('changes',[{'adult_names':'Only one'},{'children':-1},{'party_size':4},{'answers':{}},{'confirmed':False},{'adult_names':'Same\nsame'},{'action':'unknown'},{'answers':{'wrong':'No'}},{'adult_names':'x'*161}])
def test_invalid_submit_is_atomic(db,stay,changes):
    setup(db,stay)
    with pytest.raises(TaskError):save(db,stay,**changes)
    assert db.query(GuestRegistration).count()==db.query(GuestRegistrationEvent).count()==0


def test_changes_invalidate_both_registration_and_old_form_and_keep_history(db,stay):
    setup(db,stay);save(db,stay);submit_checkin(db,**args(db,stay))
    assert is_current(stay,db.query(GuestCheckin).one())
    old=registration_token(db,stay)
    setup(db,stay,questions='*Updated required question')
    assert not registration_current(db,stay) and not is_current(stay,db.query(GuestCheckin).one())
    with pytest.raises(TaskError):save(db,stay,token=old)
    save(db,stay)
    stay.check_out+=timedelta(days=1);db.commit()
    assert not registration_current(db,stay)
    assert db.query(GuestRegistrationEvent).count()==2
    prior=json.loads(db.query(GuestRegistrationEvent).order_by(GuestRegistrationEvent.id).first().payload)
    assert prior['data']['questions'][0]['label']=='Expected arrival transport'


def test_required_questions_removed_and_guest_counts(db,stay):
    setup(db,stay);save(db,stay,adult_names='Only adult',children=1)
    assert registration_current(db,stay)
    setup(db,stay,questions='',require_adults=False)
    assert registration_current(db,stay)
    save(db,stay,adult_names='',children=0)


def test_export_only_submitted_scope_dates_and_formula_protection(db,stay):
    setup(db,stay);save(db,stay,adult_names='=SYNTHETIC()\nSecond adult')
    start=stay.check_in.date().isoformat();end=(stay.check_in.date()+timedelta(days=1)).isoformat()
    csv=registration_export(db,stay.property_id,start,end)
    assert "'=SYNTHETIC()" in csv and 'Synthetic answer' in csv
    assert '=SYNTHETIC' not in registration_export(db,999999,start,end)
    save(db,stay,action='draft')
    assert '=SYNTHETIC' not in registration_export(db,None,start,end)
    with pytest.raises(TaskError):registration_export(db,None,start,start)


def test_routes_private_admin_review_drafts_and_cross_origin(db,stay):
    setup(db,stay)
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        url='/guest/'+stay.guest_portal_token+'/registration'
        page=client.get(url);assert page.status_code==200 and page.headers['Cache-Control']=='private, no-store'
        assert client.get('/guest/unknown/registration').status_code==404
        assert client.get('/operations/registration-settings').status_code==403
        assert client.get(f'/operations/guest-registrations/{stay.id}').status_code==403
        data={'token':registration_token(db,stay),'action':'draft','party_size':'2','children':'0','adult_names':'<script>synthetic</script>'}
        assert client.post(url,data=data,headers={'Origin':'https://evil.invalid'}).status_code==403
        response=client.post(url,data=data)
        assert response.status_code==200 and '&lt;script&gt;synthetic&lt;/script&gt;' in response.text
        client.cookies.set(SESSION_COOKIE,make_session_cookie('checkin-admin'))
        assert client.get('/operations/registration-settings').status_code==200
        review=client.get(f'/operations/guest-registrations/{stay.id}')
        assert review.status_code==200 and 'History' in review.text and '<script>synthetic</script>' not in review.text
        assert stay.guest_portal_token not in review.text


def test_expired_and_cancelled_stays_cannot_submit(db,stay):
    setup(db,stay);stay.check_out-=timedelta(days=20);db.commit()
    with pytest.raises(TaskError):save(db,stay)
    from app.models import BookingStatus
    stay.status=BookingStatus.cancelled;db.commit()
    with pytest.raises(LookupError):save(db,stay)


def test_mismatched_arrival_guest_count_keeps_arrival_gate_closed(db,stay):
    setup(db,stay);save(db,stay)
    submit_checkin(db,**args(db,stay,party_size=1))
    assert not is_current(stay,db.query(GuestCheckin).one())
    submit_checkin(db,**args(db,stay,party_size=2))
    assert is_current(stay,db.query(GuestCheckin).one())
