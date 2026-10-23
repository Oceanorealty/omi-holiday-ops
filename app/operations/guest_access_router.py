from fastapi import APIRouter,Depends,Form,HTTPException,Request
from fastapi.responses import RedirectResponse
from app.db import get_db
from app.models import GuestAccessRule,Property
from app.operations.guest_access import FIELDS,rule_token,save_rule,review_account
from app.operations.parity_router import same_origin,optional_property
from app.operations.router import require_admin,templates
from app.operations.tasks import TaskError

router=APIRouter()


@router.get('/operations/guest-access')
def access_page(request:Request,property_id:str='',db=Depends(get_db),admin=Depends(require_admin)):
    selected=optional_property(property_id)
    properties=db.query(Property).order_by(Property.name,Property.id).all()
    prop=db.get(Property,selected) if selected else (properties[0] if properties else None)
    if selected and not prop:raise HTTPException(404,'Property not found')
    rules={r.field:r for r in db.query(GuestAccessRule).filter_by(property_id=prop.id).all()} if prop else {}
    return templates.TemplateResponse(request=request,name='guest_access.html',context={
        'properties':properties,'property':prop,'fields':FIELDS,'rules':rules,
        'tokens':{field:rule_token(admin,prop.id,field,rules.get(field)) for field in FIELDS} if prop else {}})


@router.post('/operations/guest-access/{property_id}/{field}')
def access_save(property_id:int,field:str,request:Request,token:str=Form(...),opening_hours:str=Form(''),
                require_arrival:str=Form(''),require_agreement:str=Form(''),require_account_clear:str=Form(''),
                hide_after_checkout:str=Form(''),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    flags=[require_arrival,require_agreement,require_account_clear,hide_after_checkout]
    if any(v not in ('','1') for v in flags):raise HTTPException(422,'Invalid rule condition')
    try:save_rule(db,actor=admin,property_id=property_id,field=field,token=token,opening_hours=opening_hours,
        require_arrival=require_arrival=='1',require_agreement=require_agreement=='1',require_account_clear=require_account_clear=='1',
        hide_after_checkout=hide_after_checkout=='1',note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/operations/guest-access?property_id='+str(property_id),status_code=303)


@router.post('/operations/guest-access/reservations/{booking_id}/account-review')
def account_review(booking_id:int,request:Request,token:str=Form(...),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:review_account(db,actor=admin,booking_id=booking_id,token=token,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/operations/accounts/'+str(booking_id),status_code=303)
