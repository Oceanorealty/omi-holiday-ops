from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from app.auth import SESSION_COOKIE, make_session_cookie
from app.db import get_db
from app.models import (ChecklistTemplate, Expense, OpsCommand, ScheduledTaskOccurrence, StaffRole, StaffUser,
    TaskSchedule, TaskTemplateApplication, WorkOrder, WorkOrderCheck, WorkOrderCost, WorkOrderEvent)
from app.operations import task_plans as plans
from app.operations.tasks import TaskError, form_token, save_task, change_checklist
from app.operations.task_plan_router import router
from test_operations import db, prop
from test_operations_tasks import data, update_data


@pytest.fixture
def actor(monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'synthetic-plan-secret-'+'x'*32)
    monkeypatch.setenv('ADMIN_USER', 'plan-admin'); monkeypatch.setenv('ADMIN_PASSWORD', 'synthetic-only')
    monkeypatch.setattr(plans, 'today', lambda: date(2026, 10, 23))
    return 'plan-admin'


def template(db, actor, **changes):
    args = dict(actor=actor, token=form_token(actor,purpose='template-create'), name='Monthly inspection',
        lines='Check taps\nCheck smoke detector\nCheck windows', note='Reviewed property inspection checklist')
    args.update(changes)
    return plans.create_template(db, **args)


def schedule_args(db, actor, p=None, **changes):
    p = p or prop(db)
    args = dict(actor=actor, token=form_token(actor,purpose='schedule-create'), property_id=p.id,
        title='Recurring property inspection', description='Inspect the property after checkout', category='inspection',
        priority='normal', interval_days=7, start_date='2026-10-23', end_date='', note='Create regular weekly property inspections')
    args.update(changes)
    return args


def test_immutable_template_copied_once_and_task_revision_protected(db,actor):
    t_id=template(db,actor)
    task=db.get(WorkOrder,save_task(db,**data(db,actor)))
    old=update_data(db,actor,task)
    token=form_token(actor,task,purpose='template-apply')
    plans.apply_template(db,actor=actor,entity_id=task.id,token=token,template_id=t_id,note='Apply standard property inspection checks')
    assert db.query(WorkOrderCheck).count()==3 and db.query(TaskTemplateApplication).count()==1
    with pytest.raises(TaskError): save_task(db,**old)
    with pytest.raises(TaskError): plans.apply_template(db,actor=actor,entity_id=task.id,token=token,template_id=t_id,note='Attempted repeated template application')
    with pytest.raises(TaskError): plans.apply_template(db,actor=actor,entity_id=task.id,token=form_token(actor,task,purpose='template-apply'),template_id=t_id,note='Attempted another template application')
    assert db.query(WorkOrderCheck).count()==3
    assert all(c.required and not c.completed for c in db.query(WorkOrderCheck))


@pytest.mark.parametrize('changes',[dict(lines=''),dict(lines='Check\ncheck'),dict(lines='x'*241),dict(lines='\n'.join(str(i) for i in range(51))),dict(name=''),dict(note='short'),dict(token='forged')])
def test_bad_templates_leave_no_definition_or_audit(db,actor,changes):
    with pytest.raises(TaskError): template(db,actor,**changes)
    assert db.query(ChecklistTemplate).count()==db.query(OpsCommand).count()==0


def test_schedule_generation_inclusive_end_snapshots_and_idempotency(db,actor):
    t_id=template(db,actor)
    args=schedule_args(db,actor,template_id=t_id,end_date='2026-11-06')
    entity_id=plans.create_schedule(db,**args)
    with pytest.raises(TaskError): plans.create_schedule(db,**args)
    result=plans.materialize_schedules(db,date(2026,11,6))
    assert result=={'generated':3,'needs_another_run':0}
    assert [o.due_date for o in db.query(ScheduledTaskOccurrence).order_by(ScheduledTaskOccurrence.due_date)]==['2026-10-23','2026-10-30','2026-11-06']
    assert db.query(WorkOrder).count()==3 and db.query(WorkOrderCheck).count()==9
    assert db.query(TaskTemplateApplication).count()==3
    assert not db.get(TaskSchedule,entity_id).active
    assert plans.materialize_schedules(db,date(2026,11,6))['generated']==0
    assert all(e.actor=='scheduler' for e in db.query(WorkOrderEvent))


def test_pause_resume_retains_existing_work_and_due_cursor(db,actor):
    entity_id=plans.create_schedule(db,**schedule_args(db,actor))
    row=db.get(TaskSchedule,entity_id)
    token=form_token(actor,row,purpose='schedule-state')
    plans.set_schedule_active(db,actor=actor,entity_id=entity_id,token=token,active=False,note='Pause upcoming maintenance during review')
    assert plans.materialize_schedules(db)['generated']==0
    assert row.next_due=='2026-10-23'
    with pytest.raises(TaskError): plans.set_schedule_active(db,actor=actor,entity_id=entity_id,token=token,active=True,note='Attempt stale state transition again')
    plans.set_schedule_active(db,actor=actor,entity_id=entity_id,token=form_token(actor,row,purpose='schedule-state'),active=True,note='Resume maintenance after review completed')
    assert plans.materialize_schedules(db)['generated']==3


def test_catchup_is_bounded_and_does_not_skip_unprocessed_occurrences(db,actor):
    args=schedule_args(db,actor,start_date='2026-09-22',interval_days=1)
    entity_id=plans.create_schedule(db,**args)
    result=plans.materialize_schedules(db,date(2026,11,22))
    assert result['generated']==50 and result['needs_another_run']==1
    row=db.get(TaskSchedule,entity_id)
    assert row.next_due=='2026-11-11'
    assert plans.materialize_schedules(db,date(2026,11,22))['generated']==12
    assert db.query(WorkOrder).count()==62


def test_inactive_assignee_is_not_assigned_to_generated_tasks(db,actor):
    staff=StaffUser(name='Synthetic',username='synthetic-staff',password_hash='not-real',role=StaffRole.staff,active=True)
    db.add(staff);db.commit()
    plans.create_schedule(db,**schedule_args(db,actor,assignee_id=staff.id))
    staff.active=False;db.commit()
    plans.materialize_schedules(db,date(2026,10,23))
    assert db.query(WorkOrder).one().assignee_id is None
    assert 'inactive' in db.query(WorkOrderEvent).one().note


def test_parallel_generators_do_not_duplicate_tasks(db,actor):
    plans.create_schedule(db,**schedule_args(db,actor))
    factory=sessionmaker(bind=db.bind,autoflush=False)
    def run(_):
        with factory() as session:
            return plans.materialize_schedules(session,date(2026,11,6))['generated']
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(run,range(2)))==3
    assert db.query(WorkOrder).count()==3 and db.query(ScheduledTaskOccurrence).count()==3


@pytest.mark.parametrize('changes',[dict(interval_days=0),dict(interval_days=366),dict(start_date='2020-01-01'),dict(end_date='2026-10-22'),dict(property_id=999),dict(template_id=999),dict(assignee_id=999),dict(category='bad'),dict(note='short')])
def test_invalid_schedules_are_atomic(db,actor,changes):
    args=schedule_args(db,actor);args.update(changes)
    with pytest.raises(TaskError): plans.create_schedule(db,**args)
    assert db.query(TaskSchedule).count()==db.query(OpsCommand).count()==0


def test_task_expense_generated_once_and_history_retained_on_reopen(db,actor):
    task=db.get(WorkOrder,save_task(db,**data(db,actor)))
    plans.set_task_cost(db,actor=actor,entity_id=task.id,token=form_token(actor,task,purpose='task-cost'),amount='125.50',category='maintenance',note='Cost confirmed against the supplier invoice')
    assert db.query(Expense).count()==0
    save_task(db,**update_data(db,actor,task,status='in_progress'))
    save_task(db,**update_data(db,actor,task,status='done',title='Repaired and inspected kitchen tap'))
    cost=db.query(WorkOrderCost).one();expense=db.get(Expense,cost.expense_id)
    assert expense.amount==Decimal('125.50') and expense.description.endswith('Repaired and inspected kitchen tap')
    assert json.loads(db.query(WorkOrderEvent).order_by(WorkOrderEvent.id.desc()).first().snapshot)['expense_id']==expense.id
    save_task(db,**update_data(db,actor,task,status='open'))
    with pytest.raises(TaskError): plans.set_task_cost(db,actor=actor,entity_id=task.id,token=form_token(actor,task,purpose='task-cost'),amount='999',category='maintenance',note='Cannot silently rewrite existing task expense')
    save_task(db,**update_data(db,actor,task,status='in_progress'))
    save_task(db,**update_data(db,actor,task,status='done'))
    assert db.query(Expense).count()==1 and expense.amount==Decimal('125.50')


def test_required_checks_block_cost_creation_and_failed_edits_roll_back(db,actor):
    task=db.get(WorkOrder,save_task(db,**data(db,actor)))
    plans.apply_template(db,actor=actor,entity_id=task.id,token=form_token(actor,task,purpose='template-apply'),template_id=template(db,actor),note='Apply required inspection before completion')
    plans.set_task_cost(db,actor=actor,entity_id=task.id,token=form_token(actor,task,purpose='task-cost'),amount='25',category='materials',note='Record purchased materials for this repair')
    save_task(db,**update_data(db,actor,task,status='in_progress'))
    with pytest.raises(TaskError): save_task(db,**update_data(db,actor,task,status='done'))
    assert db.query(Expense).count()==0 and task.status=='in_progress'
    before=task.version
    with pytest.raises(TaskError): plans.set_task_cost(db,actor=actor,entity_id=task.id,token=form_token(actor,task,purpose='task-cost'),amount='30',category='materials',note='short')
    assert task.version==before and db.query(WorkOrderCost).one().amount==25


def test_plan_routes_are_authorized_and_have_no_external_send(db,actor):
    p=prop(db);app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get('/operations/task-plans').status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie(actor))
        assert client.get('/operations/task-plans').status_code==200
        args=dict(token=form_token(actor,purpose='template-create'),name='<script>unsafe</script>',lines='Check windows',note='Create a synthetic inspection checklist')
        assert client.post('/operations/task-plans/templates',data=args,headers={'origin':'https://elsewhere.invalid'}).status_code==403
        result=client.post('/operations/task-plans/templates',data=args)
        assert result.status_code==200 and '&lt;script&gt;unsafe&lt;/script&gt;' in result.text
        args=schedule_args(db,actor,p);args.pop('actor')
        assert client.post('/operations/task-plans/schedules',data=args).status_code==200
        result=client.post('/operations/task-plans/generate',data={'token':form_token(actor,purpose='schedule-run'),'through':'2026-11-06'})
        assert result.status_code==200 and '3 tasks generated' in result.text
        assert client.post('/operations/task-plans/generate',data={'token':form_token(actor,purpose='schedule-run'),'through':'2027-01-01'}).status_code==409
