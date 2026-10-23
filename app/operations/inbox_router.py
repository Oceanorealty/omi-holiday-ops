from types import SimpleNamespace
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from app.auth import _secret
from app.db import get_db
from app.models import Booking, PortalConversation, StaffUser
from app.operations.guest_checkin import find_guest_booking, guest_actor
from app.operations.inbox import (can_guest_post, issue_link, link_purpose, manage_conversation, mark_read, message_token,
    post_message, read_token, unread_count, visible_messages)
from app.operations.parity_router import optional_property, same_origin
from app.operations.router import require_admin, templates
from app.operations.tasks import TaskError, form_token

router=APIRouter()


def portal_context(db,booking,before=None):
    if not booking:return {}
    conversation=db.query(PortalConversation).filter_by(booking_id=booking.id).first()
    rows=visible_messages(db,conversation,guest=True,before=before)
    actor=guest_actor(booking)
    return {'portal_messages':rows, 'portal_message_token':message_token(actor,booking) if _secret() and can_guest_post(booking) else '',
        'portal_read_token':read_token(actor,booking.id,rows[-1].id) if rows and _secret() else '',
        'portal_unread':unread_count(db,conversation,actor,guest=True) if conversation else 0,
        'portal_older':rows[0].id if len(rows)==100 else None}


@router.get('/operations/inbox')
def inbox(request:Request,property_id:str='',state:str='',db=Depends(get_db),admin=Depends(require_admin)):
    from app.models import Property
    prop=optional_property(property_id)
    if state not in ('','open','waiting','resolved'):raise HTTPException(422,'Invalid state')
    query=db.query(PortalConversation).join(Booking,Booking.id==PortalConversation.booking_id)
    if prop is not None:query=query.filter(Booking.property_id==prop)
    if state:query=query.filter(PortalConversation.state==state)
    rows=query.order_by(PortalConversation.updated_at.desc(),PortalConversation.id.desc()).limit(201).all()
    return templates.TemplateResponse(request=request,name='inbox.html',context={
        'conversations':rows[:200],'truncated':len(rows)>200,'state':state,'property_id':prop,
        'bookings':{r.booking_id:db.get(Booking,r.booking_id) for r in rows[:200]},
        'unread':{r.id:unread_count(db,r,admin) for r in rows[:200]},
        'staff':{s.id:s for s in db.query(StaffUser).all()},'properties':db.query(Property).order_by(Property.name).all()})


@router.get('/operations/inbox/{booking_id}')
def conversation(booking_id:int,request:Request,before:int|None=None,db=Depends(get_db),admin=Depends(require_admin)):
    booking=db.get(Booking,booking_id)
    if not booking or booking.is_block:raise HTTPException(404,'Reservation not found')
    row=db.query(PortalConversation).filter_by(booking_id=booking_id).first()
    messages=visible_messages(db,row,before=before)
    return templates.TemplateResponse(request=request,name='inbox_conversation.html',context={
        'booking':booking,'conversation':row,'messages':messages,'older':messages[0].id if len(messages)==100 else None,
        'staff':db.query(StaffUser).filter_by(active=True).order_by(StaffUser.name).all(),
        'message_token':message_token(admin,booking),
        'link_token':form_token(admin,SimpleNamespace(id=booking_id,version=0),purpose=link_purpose(booking)),
        'manage_token':form_token(admin,SimpleNamespace(id=booking_id,version=row.version),purpose='portal-manage') if row else '',
        'read_token':read_token(admin,booking_id,messages[-1].id) if messages else '',
        'unread':unread_count(db,row,admin) if row else 0})


@router.post('/operations/inbox/{booking_id}/link')
def admin_link(booking_id:int,request:Request,token:str=Form(...),note:str=Form(...),
               db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:issue_link(db,actor=admin,booking_id=booking_id,token=token,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/inbox/{booking_id}',status_code=303)


@router.post('/operations/inbox/{booking_id}/message')
def admin_message(booking_id:int,request:Request,token:str=Form(...),kind:str=Form(...),body:str=Form(...),
                  db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    if kind not in ('reply','internal'):raise HTTPException(422,'Invalid message kind')
    try:post_message(db,actor=admin,booking_id=booking_id,token=token,kind=kind,body=body)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/inbox/{booking_id}',status_code=303)


@router.post('/operations/inbox/{booking_id}/manage')
def admin_manage(booking_id:int,request:Request,token:str=Form(...),state:str=Form(...),assignee_id:str=Form(''),
                 note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:manage_conversation(db,actor=admin,booking_id=booking_id,token=token,state=state,
        assignee_id=int(assignee_id) if assignee_id else None,note=note)
    except ValueError as exc:raise HTTPException(409,str(exc) if isinstance(exc,TaskError) else 'Invalid assignee') from None
    return RedirectResponse(f'/operations/inbox/{booking_id}',status_code=303)


@router.post('/operations/inbox/{booking_id}/read')
def admin_read(booking_id:int,request:Request,token:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:mark_read(db,actor=admin,booking_id=booking_id,token=token)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/operations/inbox/{booking_id}',status_code=303)


@router.post('/guest/{portal_token}/message')
def guest_message(portal_token:str,request:Request,token:str=Form(...),body:str=Form(...),db=Depends(get_db)):
    same_origin(request)
    booking=find_guest_booking(db,portal_token)
    if not booking:raise HTTPException(404,'Stay link not available')
    try:post_message(db,actor=guest_actor(booking),booking_id=booking.id,token=token,kind='guest',body=body,portal_token=portal_token)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/guest/{portal_token}#messages',status_code=303,headers={'Referrer-Policy':'same-origin'})


@router.post('/guest/{portal_token}/read')
def guest_read(portal_token:str,request:Request,token:str=Form(...),db=Depends(get_db)):
    same_origin(request)
    booking=find_guest_booking(db,portal_token)
    if not booking:raise HTTPException(404,'Stay link not available')
    try:mark_read(db,actor=guest_actor(booking),booking_id=booking.id,token=token,guest=True)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/guest/{portal_token}#messages',status_code=303,headers={'Referrer-Policy':'same-origin'})
