from datetime import datetime,timedelta
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import BookingStatus,OpsCommand,PortalConversation,PortalMessage,PortalReadCursor,StaffUser,StaffRole
from app.operations.guest_checkin import guest_actor
from app.operations.guest_checkin_router import router as guest_router
from app.operations.inbox import (issue_link,link_purpose,manage_conversation,mark_read,message_token,post_message,read_token,
    unread_count,visible_messages)
from app.operations.inbox_router import router
from app.operations.tasks import TaskError,form_token
from test_operations import db,prop,booking


@pytest.fixture
def stay(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-inbox-'+32*'x')
    monkeypatch.setenv('ADMIN_USER','inbox-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    start=datetime.utcnow()+timedelta(days=7)
    return booking(db,prop(db),check_in=start,check_out=start+timedelta(days=2),guest_portal_token='synthetic-inbox-link')


def send(db,stay,kind='guest',body='Synthetic question',**changes):
    actor=guest_actor(stay) if kind=='guest' else 'inbox-admin'
    values=dict(actor=actor,booking_id=stay.id,token=message_token(actor,stay),kind=kind,body=body,
        portal_token=stay.guest_portal_token if kind=='guest' else None)
    values.update(changes)
    return post_message(db,**values)


def test_guest_reply_and_notes_are_persistent_and_isolated(db,stay):
    guest_id=send(db,stay)
    reply_id=send(db,stay,'reply','Synthetic public reply')
    note_id=send(db,stay,'internal','Do not show internal note')
    row=db.query(PortalConversation).one()
    assert [m.id for m in visible_messages(db,row,guest=True)]==[guest_id,reply_id]
    assert [m.id for m in visible_messages(db,row)]==[guest_id,reply_id,note_id]
    assert unread_count(db,row,'inbox-admin')==1
    assert unread_count(db,row,guest_actor(stay),guest=True)==1
    assert unread_count(db,row,'another-admin')==1
    mark_read(db,actor='inbox-admin',booking_id=stay.id,token=read_token('inbox-admin',stay.id,guest_id))
    assert unread_count(db,row,'inbox-admin')==0
    send(db,stay,body='A newer question')
    # Replaying a prior read cannot mark a subsequently posted question read.
    mark_read(db,actor='inbox-admin',booking_id=stay.id,token=read_token('inbox-admin',stay.id,guest_id))
    assert unread_count(db,row,'inbox-admin')==1
    assert db.query(PortalReadCursor).count()==1


def test_message_retry_is_once_only_and_cannot_change_audience(db,stay):
    actor=guest_actor(stay);token=message_token(actor,stay)
    first=send(db,stay,token=token)
    assert send(db,stay,token=token)==first
    with pytest.raises(TaskError):send(db,stay,body='Changed body',token=token)
    with pytest.raises(TaskError):send(db,stay,'reply',token=token)
    assert db.query(PortalMessage).count()==1


def test_resolution_assignment_and_guest_reopen_keep_history(db,stay):
    send(db,stay)
    staff=StaffUser(name='Synthetic operator',username='operator',password_hash='unused',role=StaffRole.staff)
    db.add(staff);db.commit()
    row=db.query(PortalConversation).one()
    token=form_token('inbox-admin',SimpleNamespace(id=stay.id,version=row.version),purpose='portal-manage')
    values=dict(actor='inbox-admin',booking_id=stay.id,token=token,state='resolved',assignee_id=staff.id,note='Handled synthetic question')
    manage_conversation(db,**values)
    assert row.state=='resolved' and row.assignee_id==staff.id
    assert db.query(OpsCommand).count()==1
    with pytest.raises(TaskError):manage_conversation(db,**values)
    send(db,stay,body='Synthetic follow-up')
    assert row.state=='open' and row.assignee_id==staff.id
    token=form_token('inbox-admin',SimpleNamespace(id=stay.id,version=row.version),purpose='portal-manage')
    staff.active=False;db.commit()
    with pytest.raises(TaskError):manage_conversation(db,**{**values,'token':token})
    assert db.query(OpsCommand).count()==1


@pytest.mark.parametrize('change',[{'token':'forged'},{'body':' '},{'body':'x'*4001},{'kind':'email'},
    {'portal_token':'another-link'},{'actor':'another-guest'}])
def test_invalid_post_has_no_partial_rows(db,stay,change):
    with pytest.raises(TaskError):send(db,stay,**change)
    assert db.query(PortalConversation).count()==db.query(PortalMessage).count()==0


def test_cancelled_guest_cannot_post_and_admin_retains_internal_history(db,stay):
    token=message_token(guest_actor(stay),stay)
    send(db,stay,'internal','History retained')
    stay.status=BookingStatus.cancelled;db.commit()
    with pytest.raises(TaskError):send(db,stay,token=token)
    with pytest.raises(TaskError):send(db,stay,'reply')
    send(db,stay,'internal','Cancellation context')
    assert db.query(PortalMessage).count()==2


def test_guest_rate_limit_and_expired_stay(db,stay):
    for n in range(5):send(db,stay,body=f'Synthetic message {n}')
    with pytest.raises(TaskError):send(db,stay)
    assert db.query(PortalMessage).count()==5
    stay.check_in=datetime(2020,1,1);stay.check_out=datetime(2020,1,3);db.commit()
    with pytest.raises(TaskError):send(db,stay)


def test_read_cursor_is_scoped_to_actor_booking_and_visibility(db,stay):
    note=send(db,stay,'internal')
    actor=guest_actor(stay)
    with pytest.raises(TaskError):mark_read(db,actor=actor,booking_id=stay.id,token=read_token(actor,stay.id,note),guest=True)
    other=booking(db,guest_portal_token='other-synthetic-guest')
    with pytest.raises(TaskError):mark_read(db,actor=actor,booking_id=other.id,token=read_token(actor,stay.id,note))
    assert db.query(PortalReadCursor).count()==0


def test_routes_escape_text_protect_internal_notes_and_do_not_mark_read_on_get(db,stay):
    app=FastAPI();app.include_router(router);app.include_router(guest_router)
    app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get('/operations/inbox').status_code==403
        url='/guest/'+stay.guest_portal_token
        assert client.get(url).status_code==200
        token=message_token(guest_actor(stay),stay)
        assert client.post(url+'/message',data={'token':token,'body':'<script>question</script>'},
            headers={'Origin':'https://elsewhere.invalid'}).status_code==403
        response=client.post(url+'/message',data={'token':token,'body':'<script>question</script>'})
        assert response.status_code==200 and '&lt;script&gt;question&lt;/script&gt;' in response.text
        send(db,stay,'internal','secret-internal-test-note')
        send(db,stay,'reply','Portal reply available')
        response=client.get(url)
        assert 'secret-internal-test-note' not in response.text and 'inbox-admin' not in response.text
        assert 'Portal reply available' in response.text
        assert db.query(PortalReadCursor).count()==0
        client.cookies.set(SESSION_COOKIE,make_session_cookie('inbox-admin'))
        assert client.get('/operations/inbox').status_code==200
        response=client.get(f'/operations/inbox/{stay.id}')
        assert 'secret-internal-test-note' in response.text and 'Guest stay portal' in response.text
        assert client.post(url+'/message',data={'token':token,'body':'different'}).status_code==409
        stay.status=BookingStatus.cancelled;db.commit()
        assert client.post(url+'/message',data={'token':token,'body':'different'}).status_code==404


def test_visible_history_paginates_without_exposing_internal_notes(db,stay):
    send(db,stay,'internal')
    row=db.query(PortalConversation).one()
    for n in range(110):db.add(PortalMessage(conversation_id=row.id,kind='reply',actor='admin',body=f'reply-{n}',token_digest=f'{n:064x}'))
    db.commit()
    page=visible_messages(db,row,guest=True)
    assert len(page)==100 and page[-1].body=='reply-109'
    earlier=visible_messages(db,row,guest=True,before=page[0].id)
    assert len(earlier)==10 and earlier[0].body=='reply-0'
    assert not any(m.kind=='internal' for m in earlier+page)


def test_private_link_rotation_revokes_old_link_forms_but_retains_history(db,stay):
    send(db,stay)
    old_link=stay.guest_portal_token;old_message=message_token(guest_actor(stay),stay)
    token=form_token('inbox-admin',SimpleNamespace(id=stay.id,version=0),purpose=link_purpose(stay))
    values=dict(actor='inbox-admin',booking_id=stay.id,token=token,note='Replace synthetic guest private link')
    issue_link(db,**values)
    assert stay.guest_portal_token!=old_link and db.query(PortalMessage).count()==1
    with pytest.raises(TaskError):issue_link(db,**values)
    with pytest.raises(TaskError):send(db,stay,portal_token=old_link,token=old_message)
    with pytest.raises(TaskError):send(db,stay,token=old_message)
    assert old_link not in db.query(OpsCommand).one().payload
