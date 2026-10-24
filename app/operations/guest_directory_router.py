"""Private guest lookup and history; no guest marketing or automatic identity merge."""
from fastapi import APIRouter,Depends,HTTPException,Path,Query,Request
from fastapi.responses import Response
from app.db import get_db
from app.models import Booking,Guest,Property
from app.operations.router import require_admin,templates
from app.operations.guest_directory import directory_data,export_directory

router=APIRouter()


@router.get('/operations/guests')
def guests(request:Request,q:str='',repeat:bool=False,page:int=Query(1,ge=1,le=1000000),db=Depends(get_db),admin=Depends(require_admin)):
    return templates.TemplateResponse(request=request,name='guest_directory.html',context=directory_data(db,q,repeat,page),headers={'Cache-Control':'no-store'})


@router.get('/operations/guests/export.csv')
def export(q:str='',repeat:bool=False,db=Depends(get_db),admin=Depends(require_admin)):
    try:body=export_directory(db,q,repeat)
    except ValueError as exc:raise HTTPException(422,str(exc))
    return Response(body,media_type='text/csv; charset=utf-8',headers={'Cache-Control':'no-store','Content-Disposition':'attachment; filename="omi-guest-directory.csv"','X-Content-Type-Options':'nosniff'})


@router.get('/operations/guests/{guest_id}')
def profile(request:Request,guest_id:int=Path(...,ge=1,le=999999999999999999),page:int=Query(1,ge=1,le=1000000),db=Depends(get_db),admin=Depends(require_admin)):
    guest=db.get(Guest,guest_id)
    if not guest:raise HTTPException(404,'Guest not found')
    query=db.query(Booking).filter(Booking.guest_id==guest_id,Booking.is_block.is_(False))
    total=query.count();pages=max(1,(total+24)//25);page=min(page,pages)
    rows=query.order_by(Booking.check_in.desc(),Booking.id.desc()).offset((page-1)*25).limit(25).all()
    property_ids={b.property_id for b in rows}
    properties={p.id:p for p in db.query(Property).filter(Property.id.in_(property_ids)).all()} if property_ids else {}
    return templates.TemplateResponse(request=request,name='guest_profile.html',context={'guest':guest,'rows':rows,'total':total,'page':page,'pages':pages,'properties':properties},headers={'Cache-Control':'no-store'})
