"""Named staff self-service for current assignments; financial fields are excluded."""
from datetime import datetime,timedelta
from fastapi import APIRouter,Depends,Form,File,HTTPException,Query,Request,UploadFile
from fastapi.responses import RedirectResponse
from app.auth import SESSION_COOKIE,_secret,verify_session_cookie
from app.db import get_db
from app.models import StaffUser,WorkOrder,WorkOrderCheck,TaskTimeEntry,TaskAttachment,Property
from app.operations.router import templates
from app.operations.parity_router import same_origin
from app.operations.tasks import TaskError,form_token,save_task,change_checklist
from app.operations.task_time import record_time,time_token
from app.operations.task_attachments import save_attachment,MAX_FILE
from app.operations.task_attachment_router import BoundedUploadRoute,download
router=APIRouter(route_class=BoundedUploadRoute)


def require_named_user(request:Request,db=Depends(get_db)):
    if not _secret():raise HTTPException(403,'Named staff login required')
    name=verify_session_cookie(request.cookies.get(SESSION_COOKIE,''))
    user=db.query(StaffUser).filter_by(username=name,active=True).first() if name else None
    if not user:raise HTTPException(403,'Named active staff login required')
    return user


def assigned_task(db,user,task_id):
    task=db.query(WorkOrder).filter_by(id=task_id,assignee_id=user.id).first()
    if not task:raise HTTPException(404,'Assigned task not found')
    return task


@router.get('/my-work')
def work_index(request:Request,scope:str='active',page:int=Query(1,ge=1,le=1000000),db=Depends(get_db),user=Depends(require_named_user)):
    if scope not in ('active','all'):raise HTTPException(422,'Choose active or all')
    query=db.query(WorkOrder).filter_by(assignee_id=user.id)
    if scope=='active':query=query.filter(WorkOrder.status.in_(['open','in_progress']))
    total=query.count();pages=max(1,(total+49)//50);page=min(page,pages)
    rows=query.order_by(WorkOrder.due_date.is_(None),WorkOrder.due_date,WorkOrder.id.desc()).offset((page-1)*50).limit(50).all()
    properties={p.id:p.name for p in db.query(Property.id,Property.name).filter(Property.id.in_([r.property_id for r in rows])).all()}
    return templates.TemplateResponse(request=request,name='my_work.html',context={'user':user,'rows':rows,'properties':properties,
        'scope':scope,'page':page,'pages':pages,'total':total},headers={'Cache-Control':'no-store'})


@router.get('/my-work/{task_id}')
def own_detail(task_id:int,request:Request,db=Depends(get_db),user=Depends(require_named_user)):
    task=assigned_task(db,user,task_id)
    return templates.TemplateResponse(request=request,name='my_work_detail.html',context={'user':user,'task':task,'property':db.get(Property,task.property_id),
        'checks':db.query(WorkOrderCheck).filter_by(work_order_id=task.id).order_by(WorkOrderCheck.id).all(),
        'times':db.query(TaskTimeEntry).filter_by(work_order_id=task.id,worker_id=user.id,voided=False).order_by(TaskTimeEntry.id.desc()).limit(100).all(),
        'attachments':db.query(TaskAttachment.id,TaskAttachment.filename,TaskAttachment.created_at).filter_by(work_order_id=task.id,actor=user.username,archived=False).all(),
        'save_token':form_token(user.username,task),'check_token':form_token(user.username,task,purpose='checklist'),
        'timer_token':time_token(user.username,task),'file_token':form_token(user.username,task,purpose='task-attachment')},headers={'Cache-Control':'no-store'})


@router.post('/my-work/{task_id}/state')
def own_state(task_id:int,request:Request,token:str=Form(...),action:str=Form(...),note:str=Form(...),db=Depends(get_db),user=Depends(require_named_user)):
    same_origin(request);task=assigned_task(db,user,task_id)
    if action not in ('start','complete'):raise HTTPException(422,'Choose start or complete')
    if task.status!=('open' if action=='start' else 'in_progress'):raise HTTPException(409,'Task state changed; reload first')
    try:save_task(db,actor=user.username,entity_id=task.id,token=token,property_id=task.property_id,title=task.title,
        description=task.description,category=task.category,priority=task.priority,status='in_progress' if action=='start' else 'done',
        assignee_id=user.id,due_date=task.due_date or '',note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/my-work/{task_id}',303)


@router.post('/my-work/{task_id}/check')
def own_check(task_id:int,request:Request,token:str=Form(...),action:str=Form(...),item_id:int=Form(...),note:str=Form(...),db=Depends(get_db),user=Depends(require_named_user)):
    same_origin(request);assigned_task(db,user,task_id)
    if action not in ('check','uncheck'):raise HTTPException(422,'Choose check or uncheck')
    try:change_checklist(db,actor=user.username,entity_id=task_id,token=token,action=action,item_id=item_id,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/my-work/{task_id}',303)


@router.post('/my-work/{task_id}/time')
def own_time(task_id:int,request:Request,token:str=Form(...),action:str=Form(...),entry_id:int|None=Form(None),note:str=Form(...),db=Depends(get_db),user=Depends(require_named_user)):
    same_origin(request);assigned_task(db,user,task_id)
    if action not in ('start','stop'):raise HTTPException(422,'Choose start or stop')
    if action=='stop' and not db.query(TaskTimeEntry).filter_by(id=entry_id,work_order_id=task_id,worker_id=user.id).first():
        raise HTTPException(404,'Your timer was not found')
    try:record_time(db,actor=user.username,task_id=task_id,token=token,action=action,worker_id=user.id,entry_id=entry_id,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/my-work/{task_id}',303)


@router.post('/my-work/{task_id}/attachments')
async def own_upload(task_id:int,request:Request,token:str=Form(...),note:str=Form(...),file:UploadFile=File(...),db=Depends(get_db),user=Depends(require_named_user)):
    same_origin(request);assigned_task(db,user,task_id)
    data=await file.read(MAX_FILE+1)
    try:save_attachment(db,actor=user.username,task_id=task_id,token=token,filename=file.filename or '',data=data,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/my-work/{task_id}',303)


@router.get('/my-work/{task_id}/attachments/{entry_id}')
def own_download(task_id:int,entry_id:int,db=Depends(get_db),user=Depends(require_named_user)):
    assigned_task(db,user,task_id)
    row=db.query(TaskAttachment.id).filter_by(id=entry_id,work_order_id=task_id,actor=user.username,archived=False).first()
    if not row:raise HTTPException(404,'Your attachment was not found')
    return download(task_id,entry_id,db,user.username)
