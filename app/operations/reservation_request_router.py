from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from app.auth import _secret
from app.db import get_db
from app.models import Property, ReservationRequest
from app.operations.parity_router import optional_property, same_origin
from app.operations.reservation_requests import review_request, submit_request
from app.operations.router import require_admin, templates
from app.operations.tasks import TaskError, form_token
from app.public_api import get_quote

router=APIRouter()


@router.get('/api/public/reservation-request')
def public_request_form(request: Request,property_id: int,check_in: str='',check_out: str='',guests: int=1,db=Depends(get_db)):
    prop=db.get(Property,property_id)
    if not prop or not prop.published:raise HTTPException(404,'Listing not found')
    if not _secret():raise HTTPException(503,'Reservation requests are not configured')
    quote=get_quote(property_id=property_id,check_in=check_in,check_out=check_out,guests=guests,db=db)['result'] if check_in and check_out else None
    return templates.TemplateResponse(request=request,name='reservation_request_form.html',context={
        'property':prop,'quote':quote,'check_in':check_in,'check_out':check_out,'guests':guests},
        headers={'Cache-Control':'private, no-store','Referrer-Policy':'no-referrer'})


@router.post('/api/public/reservation-request')
def public_request_submit(request: Request,quote_token: str=Form(...),guest_name: str=Form(...),guest_email: str=Form(...),db=Depends(get_db)):
    same_origin(request)
    try:reference=submit_request(db,quote_token=quote_token,guest_name=guest_name,guest_email=guest_email)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return templates.TemplateResponse(request=request,name='reservation_request_received.html',context={'reference':reference},
        headers={'Cache-Control':'private, no-store','Referrer-Policy':'no-referrer'})


@router.get('/operations/reservation-requests')
def requests_page(request: Request,state: str='pending',property_id: str='',db=Depends(get_db),admin=Depends(require_admin)):
    if state not in ('pending','approved','rejected','all'):raise HTTPException(422,'Invalid request state')
    property_id=optional_property(property_id)
    query=db.query(ReservationRequest)
    if state!='all':query=query.filter_by(state=state)
    if property_id is not None:query=query.filter_by(property_id=property_id)
    rows=query.order_by(ReservationRequest.id.desc()).limit(201).all()
    props=db.query(Property).order_by(Property.name).all()
    return templates.TemplateResponse(request=request,name='reservation_requests.html',context={
        'rows':rows[:200],'truncated':len(rows)>200,'state':state,'properties':props,
        'property_names':{p.id:p.name for p in props},'property_id':property_id,
        'tokens':{r.id:form_token(admin,r,purpose='reservation-request-review') for r in rows[:200]}})


@router.post('/operations/reservation-requests/{entity_id}')
def request_review(entity_id: int,request: Request,token: str=Form(...),action: str=Form(...),note: str=Form(...),
                   db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:booking_id=review_request(db,actor=admin,entity_id=entity_id,token=token,action=action,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/reservations/{booking_id}' if booking_id else '/operations/reservation-requests',status_code=303)
