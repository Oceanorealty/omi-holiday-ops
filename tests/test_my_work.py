import hashlib,html,re
from datetime import datetime
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import StaffUser,StaffRole,WorkOrder,WorkOrderCheck,TaskTimeEntry,TaskAttachment
from app.operations.my_work_router import router
from app.operations.task_router import router as admin_router
from test_operations import db,prop


def form_token(response,ending):
    body=re.search(r'<form[^>]*action="[^"]*'+re.escape(ending)+r'"[^>]*>(.*?)</form>',response.text,re.S).group(1)
    return html.unescape(re.search(r'name="token" value="([^"]+)"',body).group(1))


@pytest.fixture
def work(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-my-work-'+32*'x')
    p=prop(db,name='Own synthetic listing',owner_email='OWNER_PRIVATE@example.invalid',wifi_info='WIFI_PRIVATE',address='PRIVATE ADDRESS')
    users=[StaffUser(name='Synthetic worker '+str(i),username='worker'+str(i),password_hash='synthetic-not-used',active=True,role=StaffRole.cleaner) for i in (1,2)]
    db.add_all(users);db.flush()
    tasks=[WorkOrder(property_id=p.id,title='Own repair' if i==0 else 'OTHER_PRIVATE_TASK',description='Check the synthetic kitchen',
        category='maintenance',priority='normal',status='open',assignee_id=u.id) for i,u in enumerate(users)]
    db.add_all(tasks);db.flush();db.add(WorkOrderCheck(work_order_id=tasks[0].id,label='Required test check',required=True,completed=False));db.commit()
    app=FastAPI();app.include_router(router);app.include_router(admin_router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE,make_session_cookie(users[0].username))
        yield client,users,tasks


def test_named_staff_only_sees_current_assignments_not_finance_or_private_property_data(work):
    client,users,tasks=work
    index=client.get('/my-work');assert index.status_code==200 and 'Own repair' in index.text
    assert 'OTHER_PRIVATE_TASK' not in index.text
    detail=client.get('/my-work/'+str(tasks[0].id));assert detail.status_code==200
    for value in ['OWNER_PRIVATE','WIFI_PRIVATE','PRIVATE ADDRESS','OTHER_PRIVATE_TASK','Reservation accounts']:
        assert value not in detail.text
    assert client.get('/my-work/'+str(tasks[1].id)).status_code==404
    assert client.get('/operations/tasks/'+str(tasks[0].id)).status_code==403
    assert detail.headers['cache-control']=='no-store'


def test_start_check_and_complete_require_current_task_state_and_checklist(work,db):
    client,users,tasks=work;path='/my-work/'+str(tasks[0].id)
    page=client.get(path);start=form_token(page,'/state')
    response=client.post(path+'/state',data={'token':start,'action':'start','note':'Starting the assigned synthetic repair'},follow_redirects=False)
    assert response.status_code==303
    assert client.post(path+'/state',data={'token':start,'action':'start','note':'Replay of synthetic start request'}).status_code==409
    page=client.get(path)
    assert client.post(path+'/state',data={'token':form_token(page,'/state'),'action':'complete','note':'Attempt before required checklist'}).status_code==409
    check=db.query(WorkOrderCheck).filter_by(work_order_id=tasks[0].id).one()
    assert client.post(path+'/check',data={'token':form_token(client.get(path),'/check'),'action':'add','item_id':check.id,'note':'Cannot add required work checks'}).status_code==422
    response=client.post(path+'/check',data={'token':form_token(client.get(path),'/check'),'action':'check','item_id':check.id,'note':'Checked the required synthetic item'})
    assert response.status_code==200
    assert client.post(path+'/state',data={'token':form_token(client.get(path),'/state'),'action':'complete','note':'Finished the synthetic repair and checks'}).status_code==200
    db.expire_all();assert db.get(WorkOrder,tasks[0].id).status=='done'


def test_reassignment_revokes_old_page_and_inactive_login_is_rejected(work,db):
    client,users,tasks=work;path='/my-work/'+str(tasks[0].id);old=form_token(client.get(path),'/state')
    task=db.get(WorkOrder,tasks[0].id);task.assignee_id=users[1].id;task.version+=1;db.commit()
    assert client.post(path+'/state',data={'token':old,'action':'start','note':'Old page after supervisor reassigned'}).status_code==404
    users[0].active=False;db.commit()
    assert client.get('/my-work').status_code==403
    client.cookies.clear();assert client.get('/my-work').status_code==403


def test_timer_cannot_impersonate_worker_or_stop_another_workers_time(work,db):
    client,users,tasks=work;task=db.get(WorkOrder,tasks[0].id);task.status='in_progress';db.commit();path='/my-work/'+str(task.id)
    response=client.post(path+'/time',data={'token':form_token(client.get(path),'/time'),'action':'start','worker_id':users[1].id,'note':'Record own synthetic working interval'})
    assert response.status_code==200
    entry=db.query(TaskTimeEntry).one();assert entry.worker_id==users[0].id
    other=TaskTimeEntry(work_order_id=task.id,worker_id=users[1].id,active_worker_id=users[1].id,started_at=datetime.utcnow(),actor='supervisor')
    db.add(other);db.commit()
    assert client.post(path+'/time',data={'token':form_token(client.get(path),'/time'),'action':'stop','entry_id':other.id,'note':'Cannot stop another worker timer'}).status_code==404
    assert client.post(path+'/time',data={'token':form_token(client.get(path),'/time'),'action':'void','entry_id':entry.id,'note':'Worker cannot void a recorded timer'}).status_code==422


def test_staff_evidence_is_private_to_current_assignment_and_uploader(work,db):
    client,users,tasks=work;path='/my-work/'+str(tasks[0].id)
    response=client.post(path+'/attachments',data={'token':form_token(client.get(path),'/attachments'),'note':'Upload synthetic task completion evidence'},files={'file':('proof.txt',b'SYNTHETIC EVIDENCE','text/plain')})
    assert response.status_code==200
    row=db.query(TaskAttachment).one()
    download=client.get(path+'/attachments/'+str(row.id));assert download.status_code==200 and download.content==b'SYNTHETIC EVIDENCE'
    assert download.headers['x-content-type-options']=='nosniff'
    row.actor='supervisor';db.commit()
    assert client.get(path+'/attachments/'+str(row.id)).status_code==404
    task=db.get(WorkOrder,tasks[0].id);task.assignee_id=users[1].id;task.version+=1;db.commit()
    assert client.get(path+'/attachments/'+str(row.id)).status_code==404
