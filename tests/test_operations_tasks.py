import re
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE, make_session_cookie
from app.db import get_db
from app.models import StaffUser, StaffRole, WorkOrder, WorkOrderEvent
from app.operations.router import overview
from app.operations.task_router import router
from app.operations.tasks import TaskError, form_token, save_task
from test_operations import db, prop


@pytest.fixture
def actor(monkeypatch):
    monkeypatch.setenv('SECRET_KEY','task-synthetic-'+32*'x')
    return 'task-admin'


def data(db, actor):
    p=prop(db)
    return dict(actor=actor,entity_id=0,token=form_token(actor),property_id=p.id,title='Repair leaking tap',
        description='Kitchen cold tap leaks',category='maintenance',priority='high',status='open',assignee_id=None,
        due_date='2000-01-01',note='Reported by inspection after departure')


def update_data(db,actor,row,**kw):
    values={k:getattr(row,k) for k in ['property_id','title','description','category','priority','status','assignee_id','due_date']}
    values.update(actor=actor,entity_id=row.id,token=form_token(actor,row),note='Staff checked and recorded progress')
    values.update(kw);return values


def test_task_lifecycle_audit_completion_and_reopen(db,actor):
    args=data(db,actor);id=save_task(db,**args);row=db.get(WorkOrder,id)
    assert row.version==1 and db.query(WorkOrderEvent).count()==1
    assert overview(db)['overdue_work_orders']==1
    with pytest.raises(TaskError):save_task(db,**update_data(db,actor,row,status='done'))
    save_task(db,**update_data(db,actor,row,status='in_progress'))
    save_task(db,**update_data(db,actor,row,status='done',note='Replaced tap washer and tested for leaks'))
    assert row.completed_at and row.version==3 and overview(db)['overdue_work_orders']==0
    completed=row.completed_at
    save_task(db,**update_data(db,actor,row,note='Completion checked by the supervisor'))
    assert row.completed_at==completed
    save_task(db,**update_data(db,actor,row,status='open'))
    assert row.completed_at is None and db.query(WorkOrderEvent).count()==5


def test_replay_create_and_stale_update_are_atomic(db,actor):
    args=data(db,actor);id=save_task(db,**args)
    with pytest.raises(TaskError):save_task(db,**args)
    assert db.query(WorkOrder).count()==1 and db.query(WorkOrderEvent).count()==1
    row=db.get(WorkOrder,id);first=update_data(db,actor,row,title='Supervisor updated issue')
    stale=update_data(db,actor,row,title='Old page')
    save_task(db,**first)
    with pytest.raises(TaskError):save_task(db,**stale)
    assert row.title=='Supervisor updated issue' and db.query(WorkOrderEvent).count()==2


@pytest.mark.parametrize('change',[{'actor':'other'},{'token':'forged'},{'title':' '},{'due_date':'2026-02-30'},
    {'note':'short'},{'category':'unknown'},{'priority':'bad'},{'property_id':999},{'assignee_id':999},{'status':'done'}])
def test_invalid_submissions_do_not_create(db,actor,change):
    args=data(db,actor);args.update(change)
    with pytest.raises(TaskError):save_task(db,**args)
    assert db.query(WorkOrder).count()==0 and db.query(WorkOrderEvent).count()==0


def test_expired_form_and_property_change_denied(db,actor,monkeypatch):
    args=data(db,actor)
    import app.operations.tasks as service
    clock=service.time.time()
    with monkeypatch.context() as m:
        m.setattr(service.time,'time',lambda:clock+901)
        with pytest.raises(TaskError):save_task(db,**args)
    id=save_task(db,**args);row=db.get(WorkOrder,id);other=prop(db)
    with pytest.raises(TaskError):save_task(db,**update_data(db,actor,row,property_id=other.id))


def test_staff_assignment_validated_and_archived_tasks_retained(db,actor):
    user=StaffUser(name='Synthetic staff',username='synthetic',password_hash='not-a-real-hash',role=StaffRole.staff,active=True)
    db.add(user);db.commit();args=data(db,actor);args['assignee_id']=user.id
    row=db.get(WorkOrder,save_task(db,**args));assert row.assignee_id==user.id
    user.active=False;db.commit()
    with pytest.raises(TaskError):save_task(db,**update_data(db,actor,row))
    save_task(db,**update_data(db,actor,row,status='cancelled',assignee_id=None))
    assert db.get(WorkOrder,row.id).status=='cancelled' and db.query(WorkOrderEvent).count()==2


def test_task_routes_filters_escape_and_origin(db,actor,monkeypatch):
    monkeypatch.setenv('ADMIN_USER',actor);monkeypatch.setenv('ADMIN_PASSWORD','synthetic-pass')
    p=prop(db);app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as c:
        assert c.get('/operations/tasks').status_code==403
        c.cookies.set(SESSION_COOKIE,make_session_cookie(actor))
        page=c.get('/operations/tasks?property_id=&status=active');assert page.status_code==200
        token=re.search(r'name="token" value="([^"]+)"',page.text).group(1)
        import html
        args=data(db,actor);args.pop('actor');args.pop('entity_id');args['token']=html.unescape(token)
        args['assignee_id']='';args['title']='<script>unsafe</script>'
        assert c.post('/operations/tasks/0',data=args,headers={'origin':'https://elsewhere.invalid'}).status_code==403
        r=c.post('/operations/tasks/0',data=args);assert r.status_code==200
        assert '&lt;script&gt;unsafe&lt;/script&gt;' in r.text and '<script>unsafe</script>' not in r.text
        assert 'Recorded task state' in r.text
        assert c.get('/operations/tasks?status=bogus').status_code==422
        assert c.get('/operations/tasks?property_id=bad').status_code==422
        assert c.get('/operations/tasks/9999').status_code==404
        user=StaffUser(name='Limited',username='limited',password_hash='none',active=True,role=StaffRole.staff);db.add(user);db.commit()
        c.cookies.set(SESSION_COOKIE,make_session_cookie('limited'))
        assert c.get('/operations/tasks').status_code==403
