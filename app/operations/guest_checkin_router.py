from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from datetime import datetime
from zoneinfo import ZoneInfo
from app.auth import _secret
from app.db import get_db
from app.models import Booking, GuestCheckin, Property
from app.operations.guest_checkin import checkin_token, find_guest_booking, is_current, submit_checkin
from app.operations.parity_router import optional_property, same_origin
from app.operations.router import require_admin, templates
from app.operations.tasks import TaskError

router = APIRouter()


def guest_context(db, booking, before=None):
    from app.operations.inbox_router import portal_context
    from app.operations.agreements import guest_agreement_context
    from app.operations.billing import guest_billing_context
    from app.operations.guest_access import portal_fields
    row = db.query(GuestCheckin).filter_by(booking_id=booking.id).first() if booking else None
    can_update=bool(booking and datetime.now(ZoneInfo(booking.property.time_zone)).date()<=booking.check_out.date())
    return {**portal_fields(db,booking), **portal_context(db,booking,before), **guest_agreement_context(db,booking), **guest_billing_context(db,booking), 'booking': booking, 'arrival_details': row, 'details_current': is_current(booking,row) if booking else False,
        'checkin_form_token': checkin_token(db,booking) if can_update and _secret() else ''}


@router.get('/guest/{portal_token}')
def guest_page(portal_token: str, request: Request, before: int|None=None, db=Depends(get_db)):
    booking = find_guest_booking(db,portal_token)
    return templates.TemplateResponse(request=request,name='guest_portal.html',context=guest_context(db,booking,before),
        status_code=200 if booking else 404, headers={'Cache-Control':'private, no-store', 'Referrer-Policy':'same-origin'})


@router.post('/guest/{portal_token}/check-in')
def checkin_save(portal_token: str, request: Request, token: str=Form(...), contact_name: str=Form(...),
                 contact_phone: str=Form(''), party_size: int=Form(...), arrival_time: str=Form(''),
                 requests: str=Form(''), acknowledged: str=Form(''), db=Depends(get_db)):
    same_origin(request)
    if acknowledged not in ('','1'):
        raise HTTPException(422,'Invalid acknowledgement')
    try:
        submit_checkin(db,portal_token=portal_token,token=token,contact_name=contact_name,contact_phone=contact_phone,
            party_size=party_size,arrival_time=arrival_time,requests=requests,acknowledged=acknowledged=='1')
    except LookupError:
        raise HTTPException(404,'Stay link not available') from None
    except TaskError as exc:
        raise HTTPException(409,str(exc)) from None
    return RedirectResponse(f'/guest/{portal_token}',status_code=303,headers={'Referrer-Policy':'same-origin'})


@router.get('/operations/guest-checkins')
def checkins(request: Request, property_id: str='', db=Depends(get_db), admin=Depends(require_admin)):
    property_id=optional_property(property_id)
    query=db.query(GuestCheckin).join(Booking,Booking.id==GuestCheckin.booking_id)
    if property_id is not None:
        query=query.filter(Booking.property_id==property_id)
    rows=query.order_by(GuestCheckin.submitted_at.desc(),GuestCheckin.id.desc()).limit(201).all()
    bookings={r.booking_id:db.get(Booking,r.booking_id) for r in rows[:200]}
    return templates.TemplateResponse(request=request,name='guest_checkins.html',context={
        'rows':rows[:200], 'truncated':len(rows)>200, 'bookings':bookings,
        'current':{r.id:is_current(bookings[r.booking_id],r) for r in rows[:200]},
        'properties':db.query(Property).order_by(Property.name).all(), 'property_id':property_id})
