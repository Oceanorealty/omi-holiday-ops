from datetime import timedelta
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from app.db import get_db
from app.models import ChecklistTemplate, Property, StaffUser, TaskSchedule
from app.operations.parity_router import same_origin
from app.operations.router import require_admin, templates
from app.operations.tasks import CATEGORIES, PRIORITIES, TaskError, form_token, verify_token
from app.operations.task_plans import (apply_template, create_schedule, create_template, iso_date,
    materialize_schedules, set_schedule_active, set_task_cost, today)

router = APIRouter()


@router.get('/operations/task-plans')
def plans(request: Request, db=Depends(get_db), admin=Depends(require_admin)):
    schedules = db.query(TaskSchedule).order_by(TaskSchedule.id.desc()).limit(101).all()
    return templates.TemplateResponse(request=request, name='task_plans.html', context={
        'schedules': schedules[:100], 'truncated': len(schedules)>100,
        'properties': db.query(Property).order_by(Property.name).all(),
        'staff': db.query(StaffUser).filter_by(active=True).order_by(StaffUser.name).all(),
        'checklist_templates': db.query(ChecklistTemplate).order_by(ChecklistTemplate.name).all(),
        'categories': CATEGORIES, 'priorities': PRIORITIES, 'today': today(), 'through': today()+timedelta(days=14),
        'template_token': form_token(admin, purpose='template-create'), 'schedule_token': form_token(admin, purpose='schedule-create'),
        'run_token': form_token(admin, purpose='schedule-run'),
        'state_tokens': {s.id:form_token(admin, s, purpose='schedule-state') for s in schedules[:100]}})


@router.post('/operations/task-plans/templates')
def template_create(request: Request, token: str=Form(...), name: str=Form(...), lines: str=Form(...),
                    note: str=Form(...), db=Depends(get_db), admin=Depends(require_admin)):
    same_origin(request)
    try:
        create_template(db, actor=admin, token=token, name=name, lines=lines, note=note)
    except TaskError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse('/operations/task-plans', status_code=303)


@router.post('/operations/task-plans/schedules')
def schedule_create(request: Request, token: str=Form(...), property_id: int=Form(...), title: str=Form(...),
                    description: str=Form(''), category: str=Form(...), priority: str=Form(...), interval_days: int=Form(...),
                    start_date: str=Form(...), end_date: str=Form(''), assignee_id: str=Form(''), template_id: str=Form(''),
                    note: str=Form(...), db=Depends(get_db), admin=Depends(require_admin)):
    same_origin(request)
    try:
        create_schedule(db, actor=admin, token=token, property_id=property_id, title=title, description=description,
            category=category, priority=priority, interval_days=interval_days, start_date=start_date, end_date=end_date,
            assignee_id=int(assignee_id) if assignee_id else None, template_id=int(template_id) if template_id else None, note=note)
    except ValueError as exc:
        raise HTTPException(409, str(exc) if isinstance(exc, TaskError) else 'Invalid staff or template selection') from None
    return RedirectResponse('/operations/task-plans', status_code=303)


@router.post('/operations/task-plans/schedules/{entity_id}/state')
def schedule_state(entity_id: int, request: Request, token: str=Form(...), active: str=Form(...), note: str=Form(...),
                   db=Depends(get_db), admin=Depends(require_admin)):
    same_origin(request)
    if active not in ('0', '1'):
        raise HTTPException(422, 'Invalid state')
    try:
        set_schedule_active(db, actor=admin, entity_id=entity_id, token=token, active=active=='1', note=note)
    except TaskError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse('/operations/task-plans', status_code=303)


@router.post('/operations/task-plans/generate')
def schedule_generate(request: Request, token: str=Form(...), through: str=Form(...),
                      db=Depends(get_db), admin=Depends(require_admin)):
    same_origin(request)
    try:
        verify_token(token, admin, 0, purpose='schedule-run')
        result = materialize_schedules(db, iso_date(through))
    except TaskError as exc:
        raise HTTPException(409, str(exc)) from None
    return templates.TemplateResponse(request=request, name='task_plan_result.html', context=result)


@router.post('/operations/tasks/{entity_id}/template')
def task_template(entity_id: int, request: Request, token: str=Form(...), template_id: int=Form(...), note: str=Form(...),
                  db=Depends(get_db), admin=Depends(require_admin)):
    same_origin(request)
    try:
        apply_template(db, actor=admin, entity_id=entity_id, token=token, template_id=template_id, note=note)
    except TaskError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse(f'/operations/tasks/{entity_id}', status_code=303)


@router.post('/operations/tasks/{entity_id}/cost')
def task_cost(entity_id: int, request: Request, token: str=Form(...), amount: str=Form(''), category: str=Form(...),
              note: str=Form(...), db=Depends(get_db), admin=Depends(require_admin)):
    same_origin(request)
    try:
        set_task_cost(db, actor=admin, entity_id=entity_id, token=token, amount=amount, category=category, note=note)
    except TaskError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse(f'/operations/tasks/{entity_id}', status_code=303)
