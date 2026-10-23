from fastapi import APIRouter,Depends,Form,HTTPException,Request
from fastapi.responses import RedirectResponse
from app.db import get_db
from app.models import BookingWindow,Property
from app.operations.booking_windows import window_token,save_window
from app.operations.parity_router import same_origin,optional_property
from app.operations.router import require_admin,templates
from app.operations.tasks import TaskError
router=APIRouter()

@router.get('/operations/booking-windows')
def window_page(request:Request,property_id:str='',db=Depends(get_db),admin=Depends(require_admin)):
    props=db.query(Property).order_by(Property.name).all();id=optional_property(property_id)
    prop=db.get(Property,id) if id is not None else (props[0] if props else None)
    if id is not None and not prop:raise HTTPException(404,'Property not found')
    return templates.TemplateResponse(request=request,name='booking_windows.html',context={'properties':props,'property':prop,'window':db.get(BookingWindow,prop.id) if prop else None,'token':window_token(db,admin,prop.id) if prop else ''})

@router.post('/operations/booking-windows/{property_id}')
def window_save(property_id:int,request:Request,token:str=Form(...),minimum_notice_days:int=Form(...),maximum_advance_days:str=Form(''),same_day_cutoff:str=Form(''),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:
        maximum=int(maximum_advance_days) if maximum_advance_days.strip() else None
        save_window(db,actor=admin,property_id=property_id,token=token,minimum_notice_days=minimum_notice_days,maximum_advance_days=maximum,same_day_cutoff=same_day_cutoff,note=note)
    except (TaskError,ValueError) as exc:raise HTTPException(409,str(exc) if isinstance(exc,TaskError) else 'Use whole days for maximum advance') from None
    return RedirectResponse(f'/operations/booking-windows?property_id={property_id}',status_code=303)
