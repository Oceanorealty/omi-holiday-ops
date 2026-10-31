import json
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter,Depends,Form,HTTPException,Request
from fastapi.responses import RedirectResponse,Response
from app.db import get_db
from app.models import Booking,BookingAccount,BookingAccountStatement,BookingLedgerEntry
from app.operations.billing import (CURRENCIES,account_token,add_entry,create_account,create_token,
    entries,issue_statement,totals,void_entry)
from app.operations.guest_checkin import find_guest_booking
from app.operations.guest_access import clearance_token,account_is_clear
from app.operations.parity_router import same_origin
from app.operations.router import require_admin,templates
from app.operations.tasks import TaskError

router=APIRouter()


@router.get('/operations/accounts/{booking_id}/owner-reconciliation')
def owner_reconciliation_page(booking_id:int,request:Request,db=Depends(get_db),admin=Depends(require_admin)):
    from app.operations.owner_reconciliation import reconciliation
    booking=db.get(Booking,booking_id)
    if not booking or booking.is_block:raise HTTPException(404,'Guest reservation not found')
    try:data=reconciliation(db,booking)
    except TaskError as exc:raise HTTPException(409,str(exc),headers={'Cache-Control':'private, no-store'}) from None
    return templates.TemplateResponse(request=request,name='owner_reconciliation.html',
        context=data,headers={'Cache-Control':'private, no-store'})


@router.get('/operations/accounts')
def account_index(request:Request,booking_id:int|None=None,db=Depends(get_db),admin=Depends(require_admin)):
    if booking_id is not None:return RedirectResponse(f'/operations/accounts/{booking_id}',status_code=303)
    rows=db.query(BookingAccount).order_by(BookingAccount.created_at.desc()).limit(101).all()
    return templates.TemplateResponse(request=request,name='booking_accounts.html',context={
        'rows':rows[:100],'truncated':len(rows)>100,'bookings':{r.booking_id:db.get(Booking,r.booking_id) for r in rows[:100]}})


@router.get('/operations/accounts/{booking_id}')
def account_page(booking_id:int,request:Request,db=Depends(get_db),admin=Depends(require_admin)):
    booking=db.get(Booking,booking_id)
    if not booking or booking.is_block:raise HTTPException(404,'Guest reservation not found')
    account=db.get(BookingAccount,booking_id);rows=entries(db,booking_id) if account else []
    return templates.TemplateResponse(request=request,name='booking_account.html',context={
        'account_clearance_token':clearance_token(db,admin,booking) if account else '', 'account_clearance_current':account_is_clear(db,booking) if account else False,
        'booking':booking,'account':account,'rows':rows,'summary':totals(rows),'currencies':CURRENCIES,
        'today':datetime.now(ZoneInfo(booking.property.time_zone)).date().isoformat(),
        'create_token':create_token(admin,booking) if not account else '',
        'entry_token':account_token(admin,account) if account else '',
        'void_token':account_token(admin,account,'account-void') if account else '',
        'statement_token':account_token(admin,account,'account-statement') if account else '',
        'statements':db.query(BookingAccountStatement).filter_by(booking_id=booking_id).order_by(BookingAccountStatement.id.desc()).limit(100).all()})


@router.post('/operations/accounts/{booking_id}/create')
def account_create(booking_id:int,request:Request,token:str=Form(...),currency:str=Form(...),
                   opening_amount:str=Form(''),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:create_account(db,actor=admin,booking_id=booking_id,token=token,currency=currency,opening_amount=opening_amount,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/accounts/{booking_id}',status_code=303)


@router.post('/operations/accounts/{booking_id}/entries')
def entry_create(booking_id:int,request:Request,token:str=Form(...),kind:str=Form(...),amount:str=Form(...),
                 description:str=Form(...),occurred_on:str=Form(...),reference:str=Form(''),payment_id:str=Form(''),
                 note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:add_entry(db,actor=admin,booking_id=booking_id,token=token,kind=kind,amount=amount,description=description,
        occurred_on=occurred_on,reference=reference,payment_id=int(payment_id) if payment_id else None,note=note)
    except ValueError as exc:raise HTTPException(409,str(exc) if isinstance(exc,TaskError) else 'Invalid payment selection') from None
    return RedirectResponse(f'/operations/accounts/{booking_id}',status_code=303)


@router.post('/operations/accounts/{booking_id}/void/{entry_id}')
def entry_void(booking_id:int,entry_id:int,request:Request,token:str=Form(...),note:str=Form(...),
               db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:void_entry(db,actor=admin,booking_id=booking_id,entry_id=entry_id,token=token,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/accounts/{booking_id}',status_code=303)


@router.post('/operations/accounts/{booking_id}/statements')
def statement_issue(booking_id:int,request:Request,token:str=Form(...),note:str=Form(...),
                    db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:entity_id=issue_statement(db,actor=admin,booking_id=booking_id,token=token,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/accounts/{booking_id}/statements/{entity_id}',status_code=303)


def statement_response(db,booking_id,entity_id,request,guest_url=''):
    row=db.get(BookingAccountStatement,entity_id)
    if not row or row.booking_id!=booking_id:raise HTTPException(404,'Statement not available')
    import hashlib
    if hashlib.sha256(row.snapshot.encode()).hexdigest()!=row.snapshot_hash:raise HTTPException(409,'Statement integrity needs review')
    return templates.TemplateResponse(request=request,name='booking_statement.html',context={
        'row':row,'data':json.loads(row.snapshot),'guest_url':guest_url},
        headers={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'})


@router.get('/operations/accounts/{booking_id}/statements/{entity_id}')
def admin_statement(booking_id:int,entity_id:int,request:Request,db=Depends(get_db),admin=Depends(require_admin)):
    return statement_response(db,booking_id,entity_id,request)


def receipt_response(db,booking_id,entry_id,request):
    row=db.get(BookingLedgerEntry,entry_id)
    if not row or row.booking_id!=booking_id or not row.receipt_snapshot:raise HTTPException(404,'Receipt record not available')
    import hashlib
    if hashlib.sha256(row.receipt_snapshot.encode()).hexdigest()!=row.receipt_snapshot_hash:
        raise HTTPException(409,'Receipt integrity needs review')
    voided=db.query(BookingLedgerEntry).filter_by(voids_id=row.id).first()
    return templates.TemplateResponse(request=request,name='booking_receipt.html',context={
        'row':row,'data':json.loads(row.receipt_snapshot),'voided':voided},
        headers={'Cache-Control':'private, no-store','Referrer-Policy':'same-origin'})


@router.get('/operations/accounts/{booking_id}/receipts/{entry_id}')
def admin_receipt(booking_id:int,entry_id:int,request:Request,db=Depends(get_db),admin=Depends(require_admin)):
    return receipt_response(db,booking_id,entry_id,request)


@router.get('/guest/{portal_token}/account/statements/{entity_id}')
def guest_statement(portal_token:str,entity_id:int,request:Request,db=Depends(get_db)):
    booking=find_guest_booking(db,portal_token)
    if not booking:raise HTTPException(404,'Statement not available')
    return statement_response(db,booking.id,entity_id,request,guest_url='/guest/'+portal_token)


@router.get('/guest/{portal_token}/account/receipts/{entry_id}')
def guest_receipt(portal_token:str,entry_id:int,request:Request,db=Depends(get_db)):
    booking=find_guest_booking(db,portal_token)
    if not booking:raise HTTPException(404,'Receipt record not available')
    return receipt_response(db,booking.id,entry_id,request)
