from datetime import datetime,timedelta,date
from fastapi import APIRouter,Depends,Form,HTTPException,Request
from fastapi.responses import RedirectResponse,Response
from app.db import get_db
from app.models import Property,StaffUser
from app.operations.parity_router import optional_property,same_origin
from app.operations.router import require_admin,templates
from app.operations.task_time import record_time,time_report,time_csv
from app.operations.tasks import TaskError

router=APIRouter()


@router.post('/operations/tasks/{task_id}/time')
def timer_save(task_id:int,request:Request,token:str=Form(...),action:str=Form(...),worker_id:str=Form(''),
               entry_id:str=Form(''),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:record_time(db,actor=admin,task_id=task_id,token=token,action=action,
        worker_id=optional_property(worker_id),entry_id=optional_property(entry_id),note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/operations/tasks/'+str(task_id),status_code=303)


@router.get('/operations/time-report')
def report(request:Request,start:str='',end:str='',property_id:str='',worker_id:str='',format:str='html',
           db=Depends(get_db),admin=Depends(require_admin)):
    end=end or (datetime.utcnow().date()+timedelta(days=1)).isoformat()
    try:start=start or (date.fromisoformat(end)-timedelta(days=31)).isoformat()
    except (ValueError,OverflowError):raise HTTPException(422,'Invalid report dates') from None
    prop,worker=optional_property(property_id),optional_property(worker_id)
    try:data=time_report(db,start=start,end=end,property_id=prop,worker_id=worker)
    except TaskError as exc:raise HTTPException(422,str(exc)) from None
    staff=db.query(StaffUser).order_by(StaffUser.name).all();names={s.id:s.name for s in staff}
    if format=='csv':return Response(time_csv(data,names),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="ops-task-time.csv"','Cache-Control':'private, no-store'})
    if format!='html':raise HTTPException(422,'Choose html or csv')
    return templates.TemplateResponse(request=request,name='task_time_report.html',context={**data,'names':names,'staff':staff,
        'properties':db.query(Property).order_by(Property.name).all(),'property_id':prop,'worker_id':worker})
