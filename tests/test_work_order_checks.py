import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import WorkOrder,WorkOrderCheck,WorkOrderEvent
from app.operations.tasks import form_token,save_task,change_checklist,TaskError
from app.operations.task_router import router
from test_operations import db
from test_operations_tasks import actor,data,update_data


def setup(db,actor):
    return db.get(WorkOrder,save_task(db,**data(db,actor)))


def change(db,actor,row,**kw):
    args=dict(actor=actor,entity_id=row.id,token=form_token(actor,row,purpose='checklist'),action='add',
        note='Supervisor defined the acceptance check',label='Verify leak stopped',required=True)
    args.update(kw);change_checklist(db,**args)


def test_required_checks_block_completion_and_reopen_resets(db,actor):
    row=setup(db,actor);change(db,actor,row)
    item=db.query(WorkOrderCheck).one()
    save_task(db,**update_data(db,actor,row,status='in_progress'))
    with pytest.raises(TaskError):save_task(db,**update_data(db,actor,row,status='done'))
    assert row.status=='in_progress'
    change(db,actor,row,action='check',item_id=item.id,note='Ran the tap and verified no water leak')
    assert item.completed and item.checked_by==actor and item.checked_at
    save_task(db,**update_data(db,actor,row,status='done'))
    with pytest.raises(TaskError):change(db,actor,row,action='uncheck',item_id=item.id)
    save_task(db,**update_data(db,actor,row,status='open'))
    assert not item.completed and item.checked_by is None and item.checked_at is None
    snapshots=[json.loads(e.snapshot) for e in db.query(WorkOrderEvent).all()]
    assert any(any(c['completed'] for c in s.get('checklist',[])) for s in snapshots)
    assert not snapshots[-1]['checklist'][0]['completed']


def test_optional_item_does_not_block_and_pending_is_audited(db,actor):
    row=setup(db,actor);change(db,actor,row,required=False)
    item=db.query(WorkOrderCheck).one()
    change(db,actor,row,action='check',item_id=item.id)
    change(db,actor,row,action='uncheck',item_id=item.id)
    assert not item.completed
    save_task(db,**update_data(db,actor,row,status='in_progress'))
    save_task(db,**update_data(db,actor,row,status='done'))
    assert row.status=='done'


def test_checklist_invalidates_old_task_form_and_replay(db,actor):
    row=setup(db,actor);old=update_data(db,actor,row)
    token=form_token(actor,row,purpose='checklist')
    change(db,actor,row,token=token)
    with pytest.raises(TaskError):save_task(db,**old)
    with pytest.raises(TaskError):change(db,actor,row,token=token)
    assert db.query(WorkOrderCheck).count()==1 and db.query(WorkOrderEvent).count()==2


@pytest.mark.parametrize('bad',['scope','actor','purpose','label','note','state'])
def test_invalid_checklist_request_does_not_change_version(db,actor,bad):
    row=setup(db,actor);version=row.version;kw={}
    if bad=='scope':kw.update(action='check',item_id=999)
    if bad=='actor':kw['token']=form_token('someone-else',row,purpose='checklist')
    if bad=='purpose':kw['token']=form_token(actor,row)
    if bad=='label':kw['label']=' '
    if bad=='note':kw['note']='short'
    if bad=='state':kw['action']='delete'
    with pytest.raises(TaskError):change(db,actor,row,**kw)
    assert db.get(WorkOrder,row.id).version==version
    assert db.query(WorkOrderCheck).count()==0 and db.query(WorkOrderEvent).count()==1


def test_task_cannot_change_other_task_check(db,actor):
    row=setup(db,actor);other=setup(db,actor);change(db,actor,other)
    item=db.query(WorkOrderCheck).one()
    with pytest.raises(TaskError):change(db,actor,row,action='check',item_id=item.id)
    assert not item.completed


def test_checklist_limit_and_safe_render(db,actor,monkeypatch):
    row=setup(db,actor)
    for i in range(50):db.add(WorkOrderCheck(work_order_id=row.id,label=f'Check {i}',required=True))
    db.commit()
    with pytest.raises(TaskError):change(db,actor,row)
    assert db.query(WorkOrderCheck).count()==50
    item=db.query(WorkOrderCheck).first();item.label='<script>unsafe</script>';db.commit()
    monkeypatch.setenv('ADMIN_USER',actor);monkeypatch.setenv('ADMIN_PASSWORD','synthetic')
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as c:
        assert c.get(f'/operations/tasks/{row.id}').status_code==403
        c.cookies.set(SESSION_COOKIE,make_session_cookie(actor))
        page=c.get(f'/operations/tasks/{row.id}')
        assert page.status_code==200 and '&lt;script&gt;unsafe&lt;/script&gt;' in page.text
        assert 'Add checklist item' not in page.text
        args=dict(token=form_token(actor,row,purpose='checklist'),action='check',item_id=item.id,note='Verified by synthetic inspection')
        assert c.post(f'/operations/tasks/{row.id}/checklist',data=args,headers={'origin':'https://other.invalid'}).status_code==403
        response=c.post(f'/operations/tasks/{row.id}/checklist',data=args)
        assert response.status_code==200 and item.completed
