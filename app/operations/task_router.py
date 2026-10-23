from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Query
from sqlalchemy import or_
from urllib.parse import urlencode
from fastapi.responses import RedirectResponse
from app.db import get_db
from app.models import ChecklistTemplate, Property, StaffUser, WorkOrder, WorkOrderCost, WorkOrderEvent, WorkOrderCheck
from app.operations.router import require_admin, templates
from app.operations.tasks import CATEGORIES, PRIORITIES, TRANSITIONS, TaskError, form_token, save_task, change_checklist

router=APIRouter()


@router.get('/operations/tasks')
def task_list(request: Request, status: str='active', property_id: str='', q: str=Query('',max_length=120),
              priority: str='', assignee: str='', due: str='', page: int=Query(1,ge=1,le=1000000),
              db=Depends(get_db), admin=Depends(require_admin)):
    try:
        property_id=int(property_id) if property_id else None
        if property_id is not None and property_id<1: raise ValueError()
    except ValueError: raise HTTPException(422,'Invalid property') from None
    query=db.query(WorkOrder)
    if status=='active': query=query.filter(WorkOrder.status.in_(['open','in_progress']))
    elif status!='all':
        if status not in TRANSITIONS: raise HTTPException(422,'Unknown status')
        query=query.filter(WorkOrder.status==status)
    if property_id is not None: query=query.filter(WorkOrder.property_id==property_id)
    q=q.strip()
    if q:
        match=or_(WorkOrder.title.contains(q,autoescape=True),WorkOrder.description.contains(q,autoescape=True))
        if q.isascii() and q.isdigit() and len(q)<=18: match=or_(match,WorkOrder.id==int(q))
        query=query.filter(match)
    if priority:
        if priority not in PRIORITIES: raise HTTPException(422,'Unknown priority')
        query=query.filter(WorkOrder.priority==priority)
    if assignee=='unassigned': query=query.filter(WorkOrder.assignee_id.is_(None))
    elif assignee:
        if not assignee.isascii() or not assignee.isdigit() or len(assignee)>18 or int(assignee)<1: raise HTTPException(422,'Invalid assignee')
        query=query.filter(WorkOrder.assignee_id==int(assignee))
    today=datetime.now(ZoneInfo('Australia/Brisbane')).date().isoformat()
    if due not in ('','overdue','today','unscheduled'): raise HTTPException(422,'Unknown due filter')
    if due=='overdue': query=query.filter(WorkOrder.due_date<today,WorkOrder.status.in_(['open','in_progress']))
    elif due=='today': query=query.filter(WorkOrder.due_date==today)
    elif due=='unscheduled': query=query.filter(or_(WorkOrder.due_date.is_(None),WorkOrder.due_date==''))
    total=query.count(); pages=max(1,(total+49)//50);page=min(page,pages)
    rows=query.order_by(WorkOrder.due_date.is_(None),WorkOrder.due_date,WorkOrder.id.desc()).offset((page-1)*50).limit(50).all()
    filters={'status':status,'property_id':property_id or '', 'q':q,'priority':priority,'assignee':assignee,'due':due}
    page_url=lambda number:'/operations/tasks?'+urlencode({**filters,'page':number})
    return templates.TemplateResponse(request=request,name='operations_tasks.html',context={
        'staff_names':{s.id:s.name for s in db.query(StaffUser).all()},'rows':rows,'truncated':False,'status':status,'property_id':property_id,
        'q':q,'priority_filter':priority,'assignee_filter':assignee,'due_filter':due,'page':page,'pages':pages,'total':total,
        'previous_url':page_url(page-1) if page>1 else None,'next_url':page_url(page+1) if page<pages else None,
        'today':datetime.now(ZoneInfo('Australia/Brisbane')).date().isoformat(),
        'properties':db.query(Property).order_by(Property.name).all(),
        'staff':db.query(StaffUser).filter_by(active=True).order_by(StaffUser.name).all(),
        'categories':CATEGORIES,'priorities':PRIORITIES,'token':form_token(admin),'task':None})


@router.get('/operations/tasks/{entity_id}')
def task_detail(entity_id: int, request: Request, db=Depends(get_db), admin=Depends(require_admin)):
    task=db.get(WorkOrder,entity_id)
    if not task: raise HTTPException(404,'Task not found')
    from app.operations.task_time import task_time_context
    from app.operations.task_attachments import attachment_context
    return templates.TemplateResponse(request=request,name='operations_task_detail.html',context={
        **task_time_context(db,admin,task), **attachment_context(db,admin,task),
        'checklist_templates':db.query(ChecklistTemplate).order_by(ChecklistTemplate.name).all(),
        'template_token':form_token(admin,task,purpose='template-apply'),
        'cost_token':form_token(admin,task,purpose='task-cost'),
        'cost':db.query(WorkOrderCost).filter_by(work_order_id=entity_id).first(),
        'checks':db.query(WorkOrderCheck).filter_by(work_order_id=entity_id).order_by(WorkOrderCheck.id).all(),
        'check_token':form_token(admin,task,purpose='checklist'),'task':task,'token':form_token(admin,task),'categories':CATEGORIES,'priorities':PRIORITIES,
        'statuses':sorted(TRANSITIONS[task.status]),'property':db.get(Property,task.property_id),
        'staff':db.query(StaffUser).filter_by(active=True).order_by(StaffUser.name).all(),
        'events':db.query(WorkOrderEvent).filter_by(work_order_id=entity_id).order_by(WorkOrderEvent.id.desc()).limit(100).all()})


@router.post('/operations/tasks/{entity_id}')
def task_save(entity_id: int, request: Request, token: str=Form(...), property_id: int=Form(...),
              title: str=Form(...), description: str=Form(''), category: str=Form(...), priority: str=Form(...),
              status: str=Form('open'), assignee_id: str=Form(''), due_date: str=Form(''), note: str=Form(...),
              db=Depends(get_db), admin=Depends(require_admin)):
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'): raise HTTPException(403,'Submit from this site')
    try:
        assignee=int(assignee_id) if assignee_id else None
        if entity_id<0: raise TaskError('Invalid task')
        saved=save_task(db,actor=admin,entity_id=entity_id,token=token,property_id=property_id,title=title,
            description=description,category=category,priority=priority,status=status,assignee_id=assignee,due_date=due_date,note=note)
    except ValueError as exc:
        raise HTTPException(409,str(exc) if isinstance(exc,TaskError) else 'Invalid assignee') from None
    return RedirectResponse(f'/operations/tasks/{saved}',status_code=303)


@router.post('/operations/tasks/{entity_id}/checklist')
def checklist_save(entity_id: int, request: Request, token: str=Form(...), action: str=Form(...),
                   note: str=Form(...), label: str=Form(''), required: str=Form(''), item_id: int=Form(None),
                   db=Depends(get_db), admin=Depends(require_admin)):
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'): raise HTTPException(403,'Submit from this site')
    if required not in ('','1'): raise HTTPException(422,'Invalid requirement')
    try:
        change_checklist(db,actor=admin,entity_id=entity_id,token=token,action=action,note=note,
                         label=label,required=required=='1',item_id=item_id)
    except TaskError as exc: raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/tasks/{entity_id}',status_code=303)
