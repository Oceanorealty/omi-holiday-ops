import json
from datetime import datetime,timedelta,date
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from urllib.parse import urlencode
from fastapi import APIRouter,Depends,Form,HTTPException,Request
from fastapi.responses import RedirectResponse
from app.db import get_db
from app.models import Property,OpsCommand,CalendarNoteChange
from app.operations.calendar_notes import notes_token,rows,save_notes,dates
from app.operations.parity_router import optional_property,same_origin
from app.operations.router import require_admin,templates
from app.operations.tasks import TaskError

router=APIRouter()


@router.get('/operations/calendar-notes')
def notes_page(request:Request,property_id:str='',start:str='',end:str='',db=Depends(get_db),admin=Depends(require_admin)):
    selected=optional_property(property_id);properties=db.query(Property).order_by(Property.name,Property.id).all()
    prop=db.get(Property,selected) if selected else (properties[0] if properties else None)
    if selected and not prop:raise HTTPException(404,'Property not found')
    try:
        start=start or datetime.now(ZoneInfo(prop.time_zone if prop else 'Australia/Brisbane')).date().isoformat()
        end=end or (date.fromisoformat(start)+timedelta(days=14)).isoformat()
        nights=dates(start,end)
    except (ValueError,TaskError,ZoneInfoNotFoundError,TypeError,OverflowError):raise HTTPException(422,'Choose valid dates and a valid property timezone') from None
    history=[]
    if prop:
        commands=db.query(OpsCommand).join(CalendarNoteChange,CalendarNoteChange.command_id==OpsCommand.id).filter(
            CalendarNoteChange.property_id==prop.id,CalendarNoteChange.start<end,CalendarNoteChange.end>start
        ).order_by(OpsCommand.id.desc()).limit(51).all()
        history=[{'command':c,'data':json.loads(c.payload)} for c in commands[:50]]
    return templates.TemplateResponse(request=request,name='calendar_notes.html',context={'property':prop,'properties':properties,
        'start':start,'end':end,'nights':nights,'notes':{r.night:r for r in rows(db,prop.id,start,end)} if prop else {},
        'token':notes_token(db,admin,prop.id,start,end) if prop else '', 'history':history,
        'history_limited':len(commands)>50 if prop else False})


@router.post('/operations/calendar-notes/{property_id}')
def notes_save(property_id:int,request:Request,start:str=Form(...),end:str=Form(...),token:str=Form(...),
               action:str=Form(...),body:str=Form(''),note:str=Form(...),db=Depends(get_db),admin=Depends(require_admin)):
    same_origin(request)
    try:save_notes(db,actor=admin,property_id=property_id,start=start,end=end,token=token,action=action,body=body,note=note)
    except TaskError as exc:raise HTTPException(409,str(exc)) from None
    return RedirectResponse('/operations/calendar-notes?'+urlencode({'property_id':property_id,'start':start,'end':end}),status_code=303)
