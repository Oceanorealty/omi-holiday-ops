"""Synthetic cleaning selection and assignment display; no actual dispatch."""
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from app.db import get_db
from app.models import (Cleaner,CleaningTask,CleaningStatus,PlatformName,Booking,MessageLog,OpsCommand)
from app.operations.cleaning_queue import queue_data,assignment_label,queue_return_url
from app.operations.parity_router import router
from app.operations.router import require_admin,templates
from app.operations.tasks import TaskError
from test_operations import db,prop,booking

NOW=datetime(2026,10,30,16,30,tzinfo=timezone.utc)


@pytest.fixture
def sample(db):
    bris=prop(db,name='Synthetic Brisbane');utc=prop(db,name='Synthetic UTC');utc.time_zone='UTC'
    bad=prop(db,name='Synthetic invalid zone');bad.time_zone='Not/A_Zone'
    cleaner=Cleaner(name='Synthetic linked cleaner',active=True);db.add(cleaner);db.commit()
    def task(p,day,**kw):
        b=booking(db,p,platform=PlatformName.direct)
        row=CleaningTask(property_id=p.id,booking_id=b.id,due_date=datetime.fromisoformat(day),
            status=kw.pop('status',CleaningStatus.pending),**kw);db.add(row);db.commit();return row
    rows=dict(overdue=task(bris,'2026-10-30'),
        linked=task(bris,'2026-10-31',cleaner_id=cleaner.id,assignee='Old synthetic free text',
            status=CleaningStatus.assigned,schedule_review_required=True,schedule_locked=True),
        free=task(utc,'2026-10-30',assignee='Synthetic free-text cleaner'),
        blank=task(bris,'2026-11-01',assignee='  '),
        cancelled=task(bris,'2026-10-20',cancelled_at=datetime(2026,10,21)),
        done=task(bris,'2026-10-29',status=CleaningStatus.done,cleaner_id=cleaner.id,schedule_review_required=True),
        invalid_zone=task(bad,'2026-10-28'))
    return bris,utc,bad,cleaner,rows


def test_queue_views_respect_property_local_day_and_assignment_without_writes(db,sample):
    bris,utc,bad,cleaner,rows=sample
    result=queue_data(db,now=NOW)
    assert result['queue_counts']=={'open':5,'unassigned':3,'overdue':1}
    assert result['timezone_unknown_count']==1
    assert result['overdue_task_ids']=={rows['overdue'].id}
    assert rows['free'].id not in result['overdue_task_ids']
    assert result['assignment_labels'][rows['linked'].id]=='Synthetic linked cleaner'
    assert {t.id for t in queue_data(db,view='unassigned',now=NOW)['tasks']}=={
        rows[k].id for k in ['overdue','blank','invalid_zone']}
    assert [t.id for t in queue_data(db,view='overdue',now=NOW)['tasks']]==[rows['overdue'].id]
    selected=queue_data(db,property_id=str(bris.id),now=NOW)
    assert selected['queue_counts']=={'open':3,'unassigned':2,'overdue':1}
    assert [t.id for t in selected['done_tasks']]==[rows['done'].id]
    assert queue_data(db,property_id=utc.id,now=NOW)['done_tasks']==[]
    assert queue_data(db,property_id='',now=NOW)['queue_counts']==result['queue_counts']
    assert rows['linked'].assignee=='Old synthetic free text'
    assert db.query(Booking).count()==db.query(CleaningTask).count()==7
    assert db.query(MessageLog).count()==db.query(OpsCommand).count()==0


@pytest.mark.parametrize('value',['bad','0','-1','1.5','1 OR 1=1','1'*30,False])
def test_invalid_property_filters_fail_without_changes(db,sample,value):
    with pytest.raises(TaskError):queue_data(db,property_id=value,now=NOW)
    assert db.query(CleaningTask).count()==7


def test_unknown_property_or_view_is_not_silently_ignored(db,sample):
    with pytest.raises(LookupError):queue_data(db,property_id=999,now=NOW)
    with pytest.raises(TaskError):queue_data(db,view='unknown',now=NOW)


def test_template_shows_actionable_scoped_queue_and_correct_cleaner(db,sample):
    bris,utc,bad,cleaner,rows=sample
    context=queue_data(db,property_id=bris.id,now=NOW)
    body=templates.env.get_template('cleaning.html').render(
        request=Request({'type':'http','path':'/cleaning','headers':[]}),cleaners=[cleaner],
        linen_rows=[],now=NOW,**context)
    assert 'All open (3)' in body and 'Unassigned (2)' in body and 'Overdue (1)' in body
    assert f'property_id={bris.id}&amp;view=unassigned' in body
    assert f'<option value="{bris.id}" selected>' in body
    assert 'Schedule needs review' in body and 'Schedule locked' in body
    assert 'Synthetic linked cleaner' in body and 'Recently completed · selected property' in body
    unknown=queue_data(db,property_id=bad.id,view='overdue',now=NOW)
    body=templates.env.get_template('cleaning.html').render(
        request=Request({'type':'http','path':'/cleaning','headers':[]}),cleaners=[],linen_rows=[],now=NOW,**unknown)
    assert 'overdue status is unknown' in body and 'No open cleaning tasks match this selection' in body


def test_linked_cleaner_display_is_consistent_on_reservation_page(db,sample,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-cleaning-queue-key')
    bris,utc,bad,cleaner,rows=sample
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with TestClient(app) as c:
        body=c.get(f'/operations/reservations/{rows["linked"].booking_id}').text
        assert 'Synthetic linked cleaner' in body and 'Old synthetic free text' not in body
        cleaner.active=False;db.commit()
        body=c.get(f'/operations/reservations/{rows["linked"].booking_id}').text
        assert 'Synthetic linked cleaner (inactive)' in body
    assert db.query(MessageLog).count()==0


def test_missing_linked_record_is_not_mislabeled_unassigned():
    task=SimpleNamespace(cleaner=None,cleaner_id=12,assignee='Old synthetic text')
    assert assignment_label(task)=='Cleaner #12 (record unavailable)'


@pytest.mark.parametrize('property_id,view,expected',[
    ('','open','/cleaning'),('','unassigned','/cleaning?property_id=&view=unassigned'),
    ('002','overdue','/cleaning?property_id=2&view=overdue'),
    ('//other.invalid','open','/cleaning'),('1','https://other.invalid','/cleaning'),
    ('-1','open','/cleaning')])
def test_return_context_cannot_redirect_outside_the_queue(property_id,view,expected):
    assert queue_return_url(property_id,view)==expected


def test_real_cleaning_handlers_preserve_filters_and_reopen_linked_assignment(db,sample,monkeypatch):
    from app import main
    bris,utc,bad,cleaner,rows=sample
    monkeypatch.setattr(main,'SessionLocal',lambda:db)
    app=FastAPI()
    app.add_api_route('/cleaning',main.cleaning_page,methods=['GET'])
    for action,handler in [('assign',main.assign_cleaning_task),('quality-check',main.toggle_quality_check),
            ('done',main.complete_cleaning_task),('reopen',main.reopen_cleaning_task)]:
        app.add_api_route('/cleaning/{task_id}/'+action,handler,methods=['POST'])
    data={'queue_property_id':str(bris.id),'queue_view':'unassigned'}
    location=f'/cleaning?property_id={bris.id}&view=unassigned'
    task_id=rows['overdue'].id
    cancelled_id=rows['cancelled'].id
    with TestClient(app,follow_redirects=False) as c:
        assert c.get('/cleaning?property_id=&view=open').status_code==200
        assert c.get('/cleaning?view=invalid').status_code==422
        assert c.get('/cleaning?property_id=999').status_code==404
        response=c.post(f'/cleaning/{task_id}/assign',data={**data,'cleaner_id':str(cleaner.id),'assignee':''})
        assert response.status_code==303 and response.headers['location']==location
        task=db.get(CleaningTask,task_id)
        assert task.cleaner_id==cleaner.id and task.assignee is None and task.status==CleaningStatus.assigned
        for action in ['quality-check','done','reopen']:
            response=c.post(f'/cleaning/{task_id}/{action}',data=data)
            assert response.status_code==303 and response.headers['location']==location
        task=db.get(CleaningTask,task_id)
        assert task.status==CleaningStatus.assigned and task.quality_checked
        response=c.post(f'/cleaning/{cancelled_id}/done',data={'queue_view':'//other.invalid'})
        assert response.headers['location']=='/cleaning'
        assert db.get(CleaningTask,cancelled_id).status==CleaningStatus.pending
    assert db.query(MessageLog).count()==db.query(OpsCommand).count()==0
