import json
from datetime import datetime
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from app.auth import SessionAuthMiddleware,SESSION_COOKIE,make_session_cookie
from app.models import StaffUser,StaffRole,WorkOrder,TaskTimeEntry,OpsCommand
from app.operations.staff_control import create,set_active,staff_token
from app.operations.tasks import TaskError
from test_operations import db,prop


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-staff-access-'+32*'x')
    monkeypatch.setenv('ADMIN_USER','shared-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-shared-only')


def user(db,role=StaffRole.staff,username='synthetic-worker'):
    row=StaffUser(name='Synthetic worker',username=username,password_hash='unused-synthetic',role=role,active=True)
    db.add(row);db.commit();return row


@pytest.mark.parametrize('role',[StaffRole.staff,StaffRole.manager,StaffRole.cleaner])
def test_non_admin_sessions_cannot_reach_legacy_financial_or_management_routes(db,configured,role):
    row=user(db,role)
    app=FastAPI()
    for path in ['/','/my-work','/my-work/1','/transactions','/properties','/staff','/operations/calendar','/logout','/healthz','/api/public/listings']:
        app.add_api_route(path,lambda:{'response':'synthetic'},methods=['GET','POST'])
    app.add_middleware(SessionAuthMiddleware,session_factory=sessionmaker(bind=db.get_bind()))
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE,make_session_cookie(row.username))
        assert client.get('/my-work').status_code==200 and client.get('/my-work/1').status_code==200
        assert client.get('/',follow_redirects=False).headers['location']=='/my-work'
        for path in ['/transactions','/properties','/staff','/operations/calendar']:
            assert client.get(path).status_code==403 and client.post(path).status_code==403
        row.active=False;db.commit()
        assert client.get('/my-work').status_code==403
        assert client.post('/logout').status_code==200
        assert client.get('/healthz').status_code==200 and client.get('/api/public/listings').status_code==200
        client.cookies.set(SESSION_COOKIE,make_session_cookie('shared-admin'))
        assert client.get('/transactions').status_code==200


def test_named_admin_works_without_shared_account_and_public_health_skips_database(db,configured,monkeypatch):
    row=user(db,StaffRole.admin)
    monkeypatch.delenv('ADMIN_USER');monkeypatch.delenv('ADMIN_PASSWORD')
    app=FastAPI();app.add_api_route('/transactions',lambda:{'ok':True});app.add_api_route('/healthz',lambda:{'ok':True})
    app.add_middleware(SessionAuthMiddleware,session_factory=sessionmaker(bind=db.get_bind()))
    with TestClient(app) as client:
        assert client.get('/transactions',follow_redirects=False).status_code==303
        client.cookies.set(SESSION_COOKIE,make_session_cookie(row.username))
        assert client.get('/transactions').status_code==200
    app2=FastAPI();app2.add_api_route('/healthz',lambda:{'ok':True})
    app2.add_middleware(SessionAuthMiddleware,session_factory=lambda:(_ for _ in ()).throw(RuntimeError('No DB for public health')))
    assert TestClient(app2).get('/healthz').status_code==200


def test_create_and_disable_are_audited_without_passwords_and_preserve_history(db,configured):
    secret='Synthetic-password-not-for-production'
    id=create(db,actor='shared-admin',token=staff_token('shared-admin'),name='Synthetic new worker',username='new-worker',password=secret,role='cleaner',note='Create a synthetic assigned-work login')
    row=db.get(StaffUser,id);assert row.password_hash!=secret
    command=db.query(OpsCommand).one();assert secret not in command.payload and row.password_hash not in command.payload
    p=prop(db);task=WorkOrder(property_id=p.id,title='Synthetic work',description='',category='maintenance',priority='normal',status='in_progress',assignee_id=id)
    db.add(task);db.flush();entry=TaskTimeEntry(work_order_id=task.id,worker_id=id,active_worker_id=id,started_at=datetime.utcnow(),actor=row.username)
    db.add(entry);db.commit();old=staff_token('shared-admin',row)
    set_active(db,actor='shared-admin',staff_id=id,token=old,active=False,note='Disable synthetic worker after assignment ended')
    assert db.get(StaffUser,id) and not row.active and entry.stopped_at and entry.active_worker_id is None
    assert db.get(WorkOrder,task.id).assignee_id==id
    with pytest.raises(TaskError):set_active(db,actor='shared-admin',staff_id=id,token=old,active=True,note='Stale state request must be rejected')
    assert db.query(OpsCommand).count()==2


def test_reserved_username_self_disable_and_weak_password_do_not_write(db,configured):
    for changes in [{'username':'shared-admin'},{'password':'short'},{'username':'Bad Space'},{'role':'root'}]:
        values={'actor':'shared-admin','token':staff_token('shared-admin'),'name':'Synthetic','username':'new-worker','password':'Synthetic-long-password','role':'staff','note':'Synthetic invalid account attempt'};values.update(changes)
        with pytest.raises(TaskError):create(db,**values)
    row=user(db,StaffRole.admin)
    with pytest.raises(TaskError):set_active(db,actor=row.username,staff_id=row.id,token=staff_token(row.username,row),active=False,note='Do not disable the current administrator')
    assert row.active and db.query(OpsCommand).count()==0
