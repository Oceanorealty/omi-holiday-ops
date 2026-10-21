import re
from datetime import datetime, timedelta
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_operations import db, prop, booking, message_setup
from app.auth import SESSION_COOKIE, make_session_cookie
from app.db import get_db
from app.models import BookingStatus, CleaningStatus, CleaningTask, MessageDelivery, MessageLog, OperationsReview
from app.operations.reviews import ReviewError, apply_review, issue_token
from app.operations.router import router
from app.sync.cleaning import sync_cleaning_tasks
from app.comms.triggers import process_due_messages


@pytest.fixture
def actor(monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-review-key-'+'x'*32)
    return 'synthetic-admin'


def missing(db):
    return booking(db, source_missing_since=datetime(2026,10,20))


def test_review_source_is_audited_and_replay_denied(db,actor):
    b=missing(db)
    token=issue_token(db,actor,'source',b.id,'keep_active')
    args=dict(actor=actor,kind='source',entity_id=b.id,action='keep_active',token=token,note='Verified the original calendar record')
    apply_review(db,**args)
    assert b.source_missing_since is None and b.status==BookingStatus.confirmed
    assert db.query(OperationsReview).count()==1
    with pytest.raises(ReviewError):apply_review(db,**args)
    assert db.query(OperationsReview).count()==1


@pytest.mark.parametrize('bad', ['forged','expired','actor','changed','note'])
def test_invalid_review_does_not_change_record(db,actor,bad):
    b=missing(db)
    token=issue_token(db,actor,'source',b.id,'keep_active',now=1 if bad=='expired' else None)
    if bad=='forged':token=token[:-1]+('0' if token[-1]!='0' else '1')
    if bad=='changed':b.check_out+=timedelta(days=1);db.commit()
    with pytest.raises(ReviewError):
        apply_review(db,actor='different-user' if bad=='actor' else actor,kind='source',entity_id=b.id,
                     action='keep_active',token=token,note='short' if bad=='note' else 'Checked provider calendar records')
    assert b.source_missing_since and db.query(OperationsReview).count()==0


def test_confirm_cancel_archives_pending_cleaning(db,actor):
    b=missing(db);sync_cleaning_tasks(db);t=db.query(CleaningTask).one()
    token=issue_token(db,actor,'source',b.id,'confirm_cancelled')
    apply_review(db,actor=actor,kind='source',entity_id=b.id,action='confirm_cancelled',token=token,note='Provider confirmed the booking was cancelled')
    assert b.status==BookingStatus.cancelled and t.cancelled_at
    sync_cleaning_tasks(db)
    assert db.query(CleaningTask).count()==1


def test_cleaning_manual_schedule_acknowledgment_persists(db,actor):
    b=booking(db);sync_cleaning_tasks(db);t=db.query(CleaningTask).one();t.schedule_locked=True
    original=t.due_date;b.check_out+=timedelta(days=1);db.commit();sync_cleaning_tasks(db)
    token=issue_token(db,actor,'cleaning',t.id,'keep_schedule')
    apply_review(db,actor=actor,kind='cleaning',entity_id=t.id,action='keep_schedule',token=token,note='Supervisor confirmed the existing cleaning slot')
    sync_cleaning_tasks(db)
    assert t.due_date==original and not t.schedule_review_required
    b.check_out+=timedelta(days=1);db.commit();sync_cleaning_tasks(db)
    assert t.schedule_review_required


def test_completed_cleaning_cannot_be_rescheduled(db,actor):
    b=booking(db);sync_cleaning_tasks(db);t=db.query(CleaningTask).one();t.status=CleaningStatus.done
    b.check_out+=timedelta(days=1);db.commit();sync_cleaning_tasks(db)
    token=issue_token(db,actor,'cleaning',t.id,'use_checkout')
    with pytest.raises(ReviewError):apply_review(db,actor=actor,kind='cleaning',entity_id=t.id,action='use_checkout',token=token,note='Attempt to revise already completed work')
    assert t.due_date!=b.check_out


def test_unknown_delivery_record_sent_prevents_resend(db,actor,monkeypatch):
    b=message_setup(db,monkeypatch)
    d=MessageDelivery(booking_id=b.id,event='booking_confirmed',scheduled_for='2026-10-20',state='unknown')
    db.add(d);db.commit()
    token=issue_token(db,actor,'message',d.id,'record_sent')
    apply_review(db,actor=actor,kind='message',entity_id=d.id,action='record_sent',token=token,note='Verified the message in the sent mailbox')
    monkeypatch.setenv('OPS_MESSAGE_DISPATCH_ENABLED','1')
    monkeypatch.setattr('app.comms.triggers.send_email',lambda *a:pytest.fail('must not resend'))
    from datetime import date
    process_due_messages(db,date(2026,10,20))
    assert d.state=='sent' and db.query(MessageLog).count()==1


def test_active_send_cannot_be_reviewed(db,actor):
    b=booking(db);d=MessageDelivery(booking_id=b.id,event='booking_confirmed',scheduled_for='x',state='sending')
    db.add(d);db.commit()
    with pytest.raises(ReviewError):issue_token(db,actor,'message',d.id,'close_without_resend')


def test_review_route_requires_admin_token_and_same_origin(db,actor,monkeypatch):
    monkeypatch.setenv('ADMIN_USER',actor);monkeypatch.setenv('ADMIN_PASSWORD','synthetic')
    b=missing(db);app=FastAPI();app.include_router(router)
    def database():yield db
    app.dependency_overrides[get_db]=database
    path=f'/operations/reviews/source/{b.id}/keep_active'
    with TestClient(app) as client:
        assert client.post(path,data={'token':'fake','note':'Provider checked carefully'}).status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie(actor))
        token=issue_token(db,actor,'source',b.id,'keep_active')
        assert client.post(path,data={'token':token,'note':'Provider checked carefully'},headers={'Origin':'https://unrelated.invalid'}).status_code==403
        assert client.post(path,data={'token':'fake','note':'Provider checked carefully'}).status_code==409
        assert client.post(path,data={'token':token,'note':'Provider checked carefully'},follow_redirects=False).status_code==303
        assert client.get('/operations').status_code==200
