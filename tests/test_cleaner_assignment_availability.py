"""Exercise the actual assignment POST with disposable synthetic records."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.models import Cleaner, CleaningTask, CleaningStatus
from test_operations import db, prop, booking


def setup_client(db, monkeypatch, existing=False):
    import app.main as main
    monkeypatch.setattr(main,'SessionLocal',lambda:db)
    p=prop(db);b=booking(db,p)
    cleaner=Cleaner(name='SYNTHETIC CLEANER',active=True);db.add(cleaner);db.flush()
    task=CleaningTask(booking_id=b.id,property_id=p.id,due_date=b.check_out,
                      assignee='Prior synthetic assignment',cleaner_id=cleaner.id if existing else None,
                      status=CleaningStatus.assigned if existing else CleaningStatus.pending)
    db.add(task);db.commit()
    app=FastAPI()
    app.router.routes.extend(r for r in main.app.routes if getattr(r,'path','')=='/cleaning/{task_id}/assign')
    return TestClient(app),task.id,cleaner.id


def test_active_assignment_keeps_queue_filters(db,monkeypatch):
    client,task_id,cleaner_id=setup_client(db,monkeypatch)
    with client:
        r=client.post(f'/cleaning/{task_id}/assign',data={'cleaner_id':cleaner_id,'queue_property_id':'1','queue_view':'unassigned'},follow_redirects=False)
    assert r.status_code==303 and r.headers['location']=='/cleaning?property_id=1&view=unassigned'
    assert db.get(CleaningTask,task_id).cleaner_id==cleaner_id


def test_stale_form_cannot_assign_newly_inactive_cleaner_or_change_existing_fields(db,monkeypatch):
    client,task_id,cleaner_id=setup_client(db,monkeypatch)
    db.get(Cleaner,cleaner_id).active=False;db.commit()
    with client:r=client.post(f'/cleaning/{task_id}/assign',data={'cleaner_id':cleaner_id,'assignee':'Should not overwrite'})
    assert r.status_code==409 and 'no longer available' in r.text
    row=db.get(CleaningTask,task_id)
    assert row.cleaner_id is None and row.assignee=='Prior synthetic assignment' and row.status==CleaningStatus.pending


def test_existing_inactive_assignment_can_be_retained_without_reassigning(db,monkeypatch):
    client,task_id,cleaner_id=setup_client(db,monkeypatch,existing=True)
    db.get(Cleaner,cleaner_id).active=False;db.commit()
    with client:r=client.post(f'/cleaning/{task_id}/assign',data={'cleaner_id':cleaner_id},follow_redirects=False)
    assert r.status_code==303 and db.get(CleaningTask,task_id).cleaner_id==cleaner_id


@pytest.mark.parametrize('value,status',[('invalid',422),('999999',409)])
def test_missing_or_malformed_cleaner_does_not_create_orphan_assignment(db,monkeypatch,value,status):
    client,task_id,_=setup_client(db,monkeypatch)
    with client:r=client.post(f'/cleaning/{task_id}/assign',data={'cleaner_id':value})
    assert r.status_code==status and db.get(CleaningTask,task_id).cleaner_id is None
