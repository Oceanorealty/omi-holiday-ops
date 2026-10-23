import json
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from app.db import get_db
from app.models import (AgreementAcceptance, AgreementPolicy, AgreementVersion, AgreementWithdrawal,
    Booking, BookingAgreement, Property)
from app.operations.agreements import (accept_agreement, create_version, document_state, download_text,
    issue_agreement, issue_token, policy_token, withdraw_agreement)
from app.operations.guest_checkin import find_guest_booking
from app.operations.parity_router import optional_property, same_origin
from app.operations.router import require_admin, templates
from app.operations.tasks import TaskError, form_token
from types import SimpleNamespace

router=APIRouter()


@router.get('/operations/agreements')
def agreements(request:Request,property_id:str='',booking_id:str='',db=Depends(get_db),admin=Depends(require_admin)):
    selected=optional_property(property_id);booking_id=optional_property(booking_id)
    properties=db.query(Property).order_by(Property.name).all()
    booking=db.get(Booking,booking_id) if booking_id else None
    if booking_id and (not booking or booking.is_block):raise HTTPException(404,'Reservation not found')
    if booking:
        if selected and selected!=booking.property_id:raise HTTPException(422,'Reservation is on a different property')
        selected=booking.property_id
    if selected is None and properties:selected=properties[0].id
    prop=db.get(Property,selected) if selected else None
    if selected and not prop:raise HTTPException(404,'Property not found')
    query=db.query(BookingAgreement).join(Booking,Booking.id==BookingAgreement.booking_id)
    if selected:query=query.filter(Booking.property_id==selected)
    if booking_id:query=query.filter(BookingAgreement.booking_id==booking_id)
    rows=query.order_by(BookingAgreement.id.desc()).limit(101).all()
    versions=db.query(AgreementVersion).filter_by(property_id=selected).order_by(AgreementVersion.revision.desc()).limit(20).all() if selected else []
    return templates.TemplateResponse(request=request,name='agreements.html',context={
        'properties':properties,'property':prop,'booking':booking,'versions':versions,
        'policy':db.get(AgreementPolicy,selected) if selected else None,
        'policy_token':policy_token(db,admin,selected) if selected else '',
        'issue_token':issue_token(db,admin,booking) if booking else '',
        'rows':rows[:100],'truncated':len(rows)>100,
        'states':{r.id:document_state(db,r,db.get(Booking,r.booking_id)) for r in rows[:100]}})


@router.post('/operations/agreements/versions')
def version_create(request:Request,property_id:int=Form(...),token:str=Form(...),title:str=Form(...),
                   body:str=Form(...),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:create_version(db,actor=admin,property_id=property_id,token=token,title=title,body=body,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/agreements?property_id={property_id}',status_code=303)


@router.post('/operations/agreements/issue')
def agreement_issue(request:Request,booking_id:int=Form(...),token:str=Form(...),note:str=Form(...),
                    db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:entity_id=issue_agreement(db,actor=admin,booking_id=booking_id,token=token,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/agreements/{entity_id}',status_code=303)


@router.get('/operations/agreements/{agreement_id}')
def agreement_detail(agreement_id:int,request:Request,db=Depends(get_db),admin=Depends(require_admin)):
    row=db.get(BookingAgreement,agreement_id)
    if not row:raise HTTPException(404,'Agreement not found')
    booking=db.get(Booking,row.booking_id)
    return templates.TemplateResponse(request=request,name='agreement_detail.html',context={
        'row':row,'data':json.loads(row.snapshot),'booking':booking,'state':document_state(db,row,booking),
        'acceptance':db.query(AgreementAcceptance).filter_by(agreement_id=row.id).first(),
        'withdrawal':db.query(AgreementWithdrawal).filter_by(agreement_id=row.id).first(),
        'withdraw_token':form_token(admin,SimpleNamespace(id=row.id,version=0),purpose='agreement-withdraw')})


def text_response(db,row):
    return Response(download_text(row,db.query(AgreementAcceptance).filter_by(agreement_id=row.id).first(),
        db.query(AgreementWithdrawal).filter_by(agreement_id=row.id).first()),media_type='text/plain',
        headers={'Content-Disposition':f'attachment; filename="agreement-{row.id}.txt"',
            'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'})


@router.get('/operations/agreements/{agreement_id}/download')
def admin_download(agreement_id:int,db=Depends(get_db),admin=Depends(require_admin)):
    row=db.get(BookingAgreement,agreement_id)
    if not row:raise HTTPException(404,'Agreement not found')
    return text_response(db,row)


@router.post('/operations/agreements/{agreement_id}/withdraw')
def agreement_withdraw(agreement_id:int,request:Request,token:str=Form(...),note:str=Form(...),
                       db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:withdraw_agreement(db,actor=admin,agreement_id=agreement_id,token=token,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/agreements/{agreement_id}',status_code=303)


@router.post('/guest/{portal_token}/agreements/{agreement_id}/accept')
def guest_accept(portal_token:str,agreement_id:int,request:Request,token:str=Form(...),typed_name:str=Form(...),
                 acknowledged:str=Form(''),db=Depends(get_db)):
    same_origin(request)
    if acknowledged not in ('','1'):raise HTTPException(422,'Invalid acknowledgement')
    try:accept_agreement(db,portal_token=portal_token,agreement_id=agreement_id,token=token,
        typed_name=typed_name,acknowledged=acknowledged=='1')
    except LookupError:raise HTTPException(404,'Agreement not available') from None
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/guest/{portal_token}#agreement',status_code=303,headers={'Referrer-Policy':'same-origin'})


@router.get('/guest/{portal_token}/agreements/{agreement_id}/download')
def guest_download(portal_token:str,agreement_id:int,db=Depends(get_db)):
    booking=find_guest_booking(db,portal_token);row=db.get(BookingAgreement,agreement_id)
    if not booking or not row or row.booking_id!=booking.id:raise HTTPException(404,'Agreement not available')
    return text_response(db,row)
