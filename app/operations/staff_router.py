from fastapi import APIRouter,Depends,Form,HTTPException,Request
from fastapi.responses import RedirectResponse
from app.db import get_db
from app.models import StaffUser,StaffRole
from app.operations.router import require_admin,templates
from app.operations.parity_router import same_origin
from app.operations.tasks import TaskError
from app.operations.staff_control import staff_token,create,set_active
router=APIRouter()


@router.get('/staff')
def staff_page(request:Request,db=Depends(get_db),admin=Depends(require_admin)):
    rows=db.query(StaffUser).order_by(StaffUser.name,StaffUser.id).all()
    return templates.TemplateResponse(request=request,name='staff.html',context={'denied':False,'staff':rows,'roles':list(StaffRole),
        'create_token':staff_token(admin),'state_tokens':{row.id:staff_token(admin,row) for row in rows}},headers={'Cache-Control':'no-store'})


@router.post('/staff')
def create_staff(request:Request,token:str=Form(...),name:str=Form(...),username:str=Form(...),password:str=Form(...),role:str=Form('staff'),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:create(db,actor=admin,token=token,name=name,username=username,password=password,role=role,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/staff',303)


@router.post('/staff/{staff_id}/toggle')
def toggle(request:Request,staff_id:int,token:str=Form(...),active:str=Form(...),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    if active not in ('0','1'):raise HTTPException(422,'Choose an account state')
    try:set_active(db,actor=admin,staff_id=staff_id,token=token,active=active=='1',note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/staff',303)


@router.post('/staff/{staff_id}/delete')
def legacy_disable(request:Request,staff_id:int,token:str=Form(...),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:set_active(db,actor=admin,staff_id=staff_id,token=token,active=False,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/staff',303)
