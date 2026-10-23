from datetime import datetime,timedelta
from types import SimpleNamespace
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import (AgreementAcceptance,AgreementPolicy,AgreementVersion,AgreementWithdrawal,
    BookingAgreement,BookingStatus,OpsCommand)
from app.operations.agreements import (accept_agreement,acceptance_token,booking_fingerprint,create_version,
    document_state,download_text,guest_agreement_context,issue_agreement,issue_token,policy_token,withdraw_agreement)
from app.operations.agreement_router import router
from app.operations.guest_checkin_router import router as guest_router
from app.operations.tasks import TaskError,form_token
from test_operations import db,prop,booking


@pytest.fixture
def stay(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-agreement-'+32*'x')
    monkeypatch.setenv('ADMIN_USER','agreement-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    start=datetime.utcnow()+timedelta(days=7)
    return booking(db,prop(db),check_in=start,check_out=start+timedelta(days=2),amount=200,
        guest_portal_token='synthetic-agreement-link')


def version(db,stay,**changes):
    values=dict(actor='agreement-admin',property_id=stay.property_id,token=policy_token(db,'agreement-admin',stay.property_id),
        title='Synthetic non-binding test document',body='Synthetic demonstration text only. This test is not a real contract.',note='Prepare a synthetic agreement version')
    values.update(changes);return create_version(db,**values)


def issue(db,stay,**changes):
    values=dict(actor='agreement-admin',booking_id=stay.id,token=issue_token(db,'agreement-admin',stay),note='Issue synthetic demonstration document')
    values.update(changes);return issue_agreement(db,**values)


def accept(db,stay,row,**changes):
    values=dict(portal_token=stay.guest_portal_token,agreement_id=row.id,token=acceptance_token(stay,row),
        typed_name='Synthetic portal holder',acknowledged=True)
    values.update(changes);return accept_agreement(db,**values)


def test_versions_and_issued_snapshots_are_immutable_and_not_replaced_by_new_text(db,stay):
    first=version(db,stay);entity_id=issue(db,stay);row=db.get(BookingAgreement,entity_id)
    snapshot=row.snapshot;fingerprint=row.snapshot_hash
    version(db,stay,body='Changed synthetic demonstration text for future documents only.')
    assert row.snapshot==snapshot and row.snapshot_hash==fingerprint
    assert json.loads(row.snapshot)['revision']==1 and row.version_id==first
    assert db.query(AgreementVersion).count()==2
    assert document_state(db,row,stay)=='awaiting acknowledgement'
    newer=db.get(BookingAgreement,issue(db,stay))
    assert json.loads(newer.snapshot)['revision']==2 and document_state(db,row,stay)=='superseded'


def test_acknowledgement_retry_is_once_only_and_preserves_name_time_hash(db,stay):
    version(db,stay);row=db.get(BookingAgreement,issue(db,stay));token=acceptance_token(stay,row)
    first=accept(db,stay,row,token=token)
    assert accept(db,stay,row,token=token)==first
    record=db.query(AgreementAcceptance).one()
    assert record.snapshot_hash==row.snapshot_hash and record.typed_name=='Synthetic portal holder'
    assert document_state(db,row,stay)=='acknowledged' and record.accepted_at is not None
    with pytest.raises(TaskError):accept(db,stay,row,token=token,typed_name='Changed signer')
    with pytest.raises(TaskError):accept(db,stay,row)
    assert db.query(AgreementAcceptance).count()==1 and db.query(OpsCommand).count()==2
    exported=download_text(row,record)
    assert 'Synthetic portal holder' in exported and row.snapshot_hash in exported
    assert exported.splitlines()[-1]==row.snapshot
    assert stay.guest_portal_token not in exported and 'agreement-admin' not in exported


@pytest.mark.parametrize('change',[{'typed_name':' '},{'typed_name':'x'*161},{'typed_name':'A\nB'},
    {'acknowledged':False},{'token':'forged'}])
def test_invalid_acknowledgement_writes_no_acceptance(db,stay,change):
    version(db,stay);row=db.get(BookingAgreement,issue(db,stay))
    with pytest.raises(TaskError):accept(db,stay,row,**change)
    assert db.query(AgreementAcceptance).count()==0


@pytest.mark.parametrize('field',['dates','amount','guest'])
def test_changed_reservation_invalidates_old_document_and_form_without_rewriting_history(db,stay,field):
    from app.models import Guest
    version(db,stay);row=db.get(BookingAgreement,issue(db,stay));token=acceptance_token(stay,row);saved=row.snapshot
    if field=='dates':stay.check_out+=timedelta(days=1)
    elif field=='amount':stay.amount=250
    else:stay.guest=Guest(name='Changed guest',email='synthetic@example.invalid')
    db.commit()
    with pytest.raises(TaskError):accept(db,stay,row,token=token)
    assert 'changed' in document_state(db,row,stay) and row.snapshot==saved
    newer=db.get(BookingAgreement,issue(db,stay));accept(db,stay,newer)
    assert document_state(db,newer,stay)=='acknowledged'


def test_withdraw_and_reissue_preserve_original_acknowledgement(db,stay):
    version(db,stay);row=db.get(BookingAgreement,issue(db,stay));accept(db,stay,row)
    token=form_token('agreement-admin',SimpleNamespace(id=row.id,version=0),purpose='agreement-withdraw')
    values=dict(actor='agreement-admin',agreement_id=row.id,token=token,note='Withdraw synthetic demonstration only')
    withdraw_agreement(db,**values)
    assert document_state(db,row,stay)=='withdrawn' and db.query(AgreementAcceptance).count()==1
    with pytest.raises(TaskError):withdraw_agreement(db,**values)
    assert db.query(AgreementWithdrawal).count()==1
    newer=db.get(BookingAgreement,issue(db,stay))
    assert newer.id!=row.id and document_state(db,newer,stay)=='awaiting acknowledgement'
    assert db.query(AgreementAcceptance).count()==1
    assert [r.id for r in guest_agreement_context(db,stay)['stay_agreement_history']]==[row.id]


def test_policy_and_issue_stale_forms_reject_concurrent_changes(db,stay):
    old_policy=policy_token(db,'agreement-admin',stay.property_id)
    version(db,stay)
    with pytest.raises(TaskError):version(db,stay,token=old_policy)
    old_issue=issue_token(db,'agreement-admin',stay)
    version(db,stay,body='A new synthetic version invalidates a pending administrator issue form.')
    with pytest.raises(TaskError):issue(db,stay,token=old_issue)
    issue(db,stay)
    with pytest.raises(TaskError):issue(db,stay)
    assert db.query(BookingAgreement).count()==1


def test_unknown_other_cancelled_and_past_stays_cannot_acknowledge(db,stay):
    version(db,stay);row=db.get(BookingAgreement,issue(db,stay))
    other=booking(db,guest_portal_token='different-synthetic-agreement-link')
    with pytest.raises(LookupError):accept(db,stay,row,portal_token=other.guest_portal_token)
    stay.status=BookingStatus.cancelled;db.commit()
    with pytest.raises(LookupError):accept(db,stay,row)
    stay.status=BookingStatus.confirmed;stay.check_in=datetime(2020,1,1);stay.check_out=datetime(2020,1,3);db.commit()
    newer=db.get(BookingAgreement,issue(db,stay))
    assert not guest_agreement_context(db,stay)['stay_agreement_token']
    with pytest.raises(TaskError):accept(db,stay,newer)


def test_document_integrity_mismatch_cannot_be_acknowledged(db,stay):
    version(db,stay);row=db.get(BookingAgreement,issue(db,stay));row.snapshot=row.snapshot.replace('demonstration','modified');db.commit()
    assert 'integrity' in document_state(db,row,stay)
    with pytest.raises(TaskError):accept(db,stay,row)


def test_unconfigured_secret_cannot_accept_a_form_signed_with_an_empty_key(db,stay,monkeypatch):
    import hashlib,hmac
    version(db,stay);row=db.get(BookingAgreement,issue(db,stay))
    body=acceptance_token(stay,row).rsplit('|',1)[0]
    monkeypatch.delenv('SECRET_KEY');monkeypatch.delenv('ADMIN_PASSWORD')
    forged=body+'|'+hmac.new(b'',body.encode(),hashlib.sha256).hexdigest()
    with pytest.raises(TaskError):accept(db,stay,row,token=forged)
    assert db.query(AgreementAcceptance).count()==0


@pytest.mark.parametrize('change',[{'body':'too short'},{'body':'x'*20001},{'title':' '},{'note':'short'}])
def test_invalid_policy_rolls_back_all_tables(db,stay,change):
    with pytest.raises(TaskError):version(db,stay,**change)
    assert db.query(AgreementVersion).count()==db.query(AgreementPolicy).count()==db.query(OpsCommand).count()==0


def test_guest_routes_scope_downloads_and_escape_text_without_leaking_internal_evidence(db,stay):
    version(db,stay,title='<script>test document</script>');row=db.get(BookingAgreement,issue(db,stay))
    app=FastAPI();app.include_router(router);app.include_router(guest_router);app.dependency_overrides[get_db]=lambda:db
    base='/guest/'+stay.guest_portal_token
    with TestClient(app) as client:
        assert client.get('/operations/agreements').status_code==403
        response=client.get(base)
        assert response.status_code==200 and '&lt;script&gt;test document&lt;/script&gt;' in response.text
        assert 'agreement-admin' not in response.text
        download=f'{base}/agreements/{row.id}/download'
        assert client.get(download).headers['Cache-Control']=='private, no-store'
        assert client.get(f'/guest/other-link/agreements/{row.id}/download').status_code==404
        values=dict(token=acceptance_token(stay,row),typed_name='Synthetic holder',acknowledged='1')
        post=f'{base}/agreements/{row.id}/accept'
        assert client.post(post,data=values,headers={'Origin':'https://other.invalid'}).status_code==403
        assert client.post(post,data=values).status_code==200
        assert 'Synthetic holder' in client.get(download).text
        client.cookies.set(SESSION_COOKIE,make_session_cookie('agreement-admin'))
        assert client.get('/operations/agreements?booking_id='+str(stay.id)).status_code==200
        assert client.get('/operations/agreements/'+str(row.id)).status_code==200
        assert client.get('/operations/agreements/'+str(row.id)+'/download').status_code==200
        stay.status=BookingStatus.cancelled;db.commit()
        assert client.get(download).status_code==404
        assert client.get('/operations/agreements/'+str(row.id)).status_code==200
