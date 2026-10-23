import json
from fastapi import APIRouter,Depends,Form,HTTPException,Request
from fastapi.responses import RedirectResponse,Response
from app.db import get_db
from app.models import Booking,Property,GuestRegistration,GuestRegistrationEvent,OpsCommand
from app.operations.registration import configuration,config_token,configure,context,save_registration,registration_export
from app.operations.guest_checkin import find_guest_booking
from app.operations.parity_router import same_origin,optional_property
from app.operations.router import require_admin,templates
from app.operations.tasks import TaskError

router=APIRouter()
PRIVATE={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'}

@router.get('/operations/registration-settings')
def settings(request:Request,property_id:str='',db=Depends(get_db),admin=Depends(require_admin)):
    properties=db.query(Property).order_by(Property.name).all();id=optional_property(property_id)
    prop=db.get(Property,id) if id is not None else (properties[0] if properties else None)
    if id is not None and not prop:raise HTTPException(404,'Property not found')
    config=configuration(db,prop.id)[0] if prop else None
    return templates.TemplateResponse(request=request,name='registration_settings.html',context={'properties':properties,'property':prop,'config':config,'questions':'\n'.join(('*' if q['required'] else '')+q['label'] for q in config['questions']) if config else '', 'token':config_token(db,admin,prop.id) if prop else ''})

@router.post('/operations/registration-settings/{property_id}')
def settings_save(property_id:int,request:Request,token:str=Form(...),require_adults:str=Form(''),questions:str=Form(''),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    if require_adults not in ('','1'):raise HTTPException(422,'Invalid requirement')
    try:configure(db,actor=admin,property_id=property_id,token=token,require_adults=require_adults=='1',questions=questions,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/registration-settings?property_id={property_id}',status_code=303)

@router.get('/guest/{portal_token}/registration')
def guest_page(portal_token:str,request:Request,db=Depends(get_db)):
    booking=find_guest_booking(db,portal_token)
    if not booking:raise HTTPException(404,'Stay link not available')
    return templates.TemplateResponse(request=request,name='guest_registration.html',context={**context(db,booking),'readonly':False},headers=PRIVATE)

@router.post('/guest/{portal_token}/registration')
async def guest_save(portal_token:str,request:Request,db=Depends(get_db)):
    same_origin(request);data=await request.form()
    try:
        if str(data.get('confirmed','')) not in ('','1'):raise TaskError('Invalid confirmation')
        save_registration(db,portal_token=portal_token,token=str(data.get('token','')),action=str(data.get('action','')),adult_names=str(data.get('adult_names','')),children=int(str(data.get('children','0'))),party_size=int(str(data.get('party_size','1'))),answers={k[2:]:str(v) for k,v in data.items() if k.startswith('q_')},confirmed=data.get('confirmed')=='1')
    except LookupError:raise HTTPException(404,'Stay link not available') from None
    except (TaskError,ValueError) as exc:raise HTTPException(409,str(exc) if isinstance(exc,TaskError) else 'Use whole numbers for guest counts') from None
    return RedirectResponse(f'/guest/{portal_token}/registration',status_code=303,headers=PRIVATE)

@router.get('/operations/guest-registrations/export')
def export(start:str,end:str,property_id:str='',db=Depends(get_db),admin=Depends(require_admin)):
    try:body=registration_export(db,optional_property(property_id),start,end)
    except TaskError as exc:raise HTTPException(422,str(exc)) from None
    return Response('\ufeff'+body,media_type='text/csv',headers={**PRIVATE,'Content-Disposition':'attachment; filename="guest-registration.csv"','X-Content-Type-Options':'nosniff'})

@router.get('/operations/guest-registrations/{booking_id}')
def review(booking_id:int,request:Request,db=Depends(get_db),admin=Depends(require_admin)):
    booking=db.get(Booking,booking_id)
    if not booking or booking.is_block:raise HTTPException(404,'Reservation not found')
    row=db.get(GuestRegistration,booking_id)
    history=db.query(GuestRegistrationEvent).filter_by(booking_id=booking_id).order_by(GuestRegistrationEvent.id.desc()).limit(51).all()
    return templates.TemplateResponse(request=request,name='guest_registration.html',context={**context(db,booking),'readonly':True,'data':json.loads(row.payload) if row else {},'history':history[:50],'history_limited':len(history)>50},headers=PRIVATE)
