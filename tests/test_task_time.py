from datetime import datetime,timedelta
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import StaffUser,WorkOrder,TaskTimeEntry,OpsCommand,WorkOrderCheck,WorkOrderEvent
from app.operations.task_time import record_time,time_token,time_report,time_csv
from app.operations.task_time_router import router
from app.operations.task_router import router as tasks_router
from app.operations.tasks import TaskError,save_task,form_token
from test_operations import db,prop


@pytest.fixture
def work(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','time-synthetic-'+32*'x')
    monkeypatch.setenv('ADMIN_USER','time-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    staff=StaffUser(name='Synthetic operator',username='synthetic-worker',password_hash='not-a-real-login');db.add(staff);db.flush()
    task=WorkOrder(property_id=prop(db).id,title='Synthetic inspection',status='in_progress',assignee_id=staff.id)
    db.add(task);db.commit();return task,staff


def timer(db,work,**changes):
    task,staff=work
    args=dict(actor='time-admin',task_id=task.id,token=time_token('time-admin',task),action='start',worker_id=staff.id,
        note='Record synthetic work interval',now=datetime(2026,10,21,23,30))
    args.update(changes);return record_time(db,**args)


def edit(db,task,**changes):
    args=dict(actor='time-admin',entity_id=task.id,token=form_token('time-admin',task),property_id=task.property_id,
        title=task.title,description=task.description,category=task.category,priority=task.priority,status=task.status,
        assignee_id=task.assignee_id,due_date=task.due_date or '',note='Synthetic task transition with timer')
    args.update(changes);return save_task(db,**args)


def test_pause_resume_and_utc_report_clips_intervals(db,work):
    task,staff=work
    first=timer(db,work)
    timer(db,work,action='stop',entry_id=first,now=datetime(2026,10,22,1))
    second=timer(db,work,now=datetime(2026,10,22,2))
    report=time_report(db,start='2026-10-22',end='2026-10-23',as_of=datetime(2026,10,22,2,30))
    assert report['closed_seconds']=={staff.id:3600} and report['running_seconds']=={staff.id:1800}
    assert report['rows'][0]['seconds']==3600 and report['rows'][1]['provisional']
    timer(db,work,action='stop',entry_id=second,now=datetime(2026,10,22,3))
    assert time_report(db,start='2026-10-21',end='2026-10-23')['closed_seconds'][staff.id]==9000


def test_staff_cannot_run_overlapping_timers_on_other_tasks(db,work):
    task,staff=work;entry=timer(db,work)
    other=WorkOrder(property_id=task.property_id,title='Other synthetic task',status='in_progress');db.add(other);db.commit()
    with pytest.raises(TaskError):timer(db,(other,staff))
    assert db.query(TaskTimeEntry).count()==1 and db.get(WorkOrder,other.id).version==1
    timer(db,work,action='stop',entry_id=entry,now=datetime(2026,10,22))
    timer(db,(other,staff),now=datetime(2026,10,22,1))
    assert db.query(TaskTimeEntry).filter(TaskTimeEntry.active_worker_id.is_not(None)).count()==1


@pytest.mark.parametrize('status',['open','done','cancelled'])
def test_leaving_in_progress_stops_timer_in_same_transaction(db,work,status):
    task,staff=work;entry=timer(db,work,now=datetime.utcnow()-timedelta(minutes=5))
    edit(db,task,status=status)
    row=db.get(TaskTimeEntry,entry);assert row.stopped_at and row.active_worker_id is None
    event=db.query(WorkOrderEvent).order_by(WorkOrderEvent.id.desc()).first()
    assert json.loads(event.snapshot)['stopped_timer_ids']==[entry]
    if status!='open':edit(db,task,status='open')
    assert db.get(TaskTimeEntry,entry).stopped_at==row.stopped_at


def test_failed_completion_rolls_back_timer_stop(db,work):
    task,staff=work;entry=timer(db,work,now=datetime.utcnow()-timedelta(minutes=5))
    db.add(WorkOrderCheck(work_order_id=task.id,label='Required synthetic inspection',required=True,completed=False));db.commit()
    with pytest.raises(TaskError):edit(db,task,status='done')
    assert db.get(TaskTimeEntry,entry).stopped_at is None


def test_assignee_change_stops_timer_and_void_retains_record(db,work):
    task,staff=work;entry=timer(db,work,now=datetime.utcnow()-timedelta(minutes=5))
    edit(db,task,assignee_id=None)
    assert db.get(TaskTimeEntry,entry).stopped_at is not None
    timer(db,work,action='void',entry_id=entry,now=datetime.utcnow())
    assert db.get(TaskTimeEntry,entry).voided
    assert time_report(db,start='2020-01-01',end='2020-02-01')['rows']==[]
    assert db.query(OpsCommand).filter_by(purpose='task-time').count()==2


def test_stale_forms_inactive_staff_wrong_task_and_clock_reversal(db,work):
    task,staff=work;stale=time_token('time-admin',task);entry=timer(db,work)
    with pytest.raises(TaskError):timer(db,work,token=stale,action='stop',entry_id=entry)
    with pytest.raises(TaskError):timer(db,work,action='stop',entry_id=entry,now=datetime(2026,10,20))
    other=WorkOrder(property_id=task.property_id,title='Other',status='in_progress');db.add(other);db.commit()
    with pytest.raises(TaskError):timer(db,(other,staff),action='stop',entry_id=entry)
    timer(db,work,action='stop',entry_id=entry,now=datetime(2026,10,22))
    staff.active=False;db.commit()
    with pytest.raises(TaskError):timer(db,work)


def test_report_authorization_filters_csv_and_origin(db,work):
    task,staff=work;entry=timer(db,work);timer(db,work,action='stop',entry_id=entry,now=datetime(2026,10,22,1))
    staff.name='=DANGEROUS_FORMULA()';db.commit()
    report=time_report(db,start='2026-10-21',end='2026-10-23',property_id=task.property_id,worker_id=staff.id)
    assert "'=DANGEROUS_FORMULA()" in time_csv(report,{staff.id:staff.name})
    other=prop(db,name='Other listing')
    assert time_report(db,start='2026-10-21',end='2026-10-23',property_id=other.id)['rows']==[]
    app=FastAPI();app.include_router(router);app.include_router(tasks_router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get('/operations/time-report').status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('time-admin'))
        assert client.get('/operations/time-report?start=2026-10-21&end=2026-10-23').status_code==200
        csv=client.get('/operations/time-report?start=2026-10-21&end=2026-10-23&format=csv')
        assert csv.status_code==200 and 'seconds_in_range' in csv.text
        assert client.get(f'/operations/tasks/{task.id}').status_code==200
        assert client.get('/operations/time-report?start=bad').status_code==422
        data={'token':time_token('time-admin',task),'action':'start','worker_id':staff.id,'note':'Synthetic origin check'}
        assert client.post(f'/operations/tasks/{task.id}/time',data=data,headers={'Origin':'https://evil.invalid'}).status_code==403
