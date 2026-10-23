import calendar
from datetime import date,datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter,Depends,HTTPException,Request
from app.db import get_db
from app.models import Property
from app.operations.calendar import calendar_data
from app.operations.parity_router import optional_property
from app.operations.router import require_admin,templates

router=APIRouter()


def period_month(db,property_id,year,month):
    first=date(year,month,1);end=date(year+1,1,1) if month==12 else date(year,month+1,1)
    data=calendar_data(db,first,end,property_id);row=data['rows'][0]
    lookup={cell['date']:cell for cell in row['days']}
    weeks=[[lookup.get(day.isoformat()) if day.month==month else None for day in week]
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year,month)]
    return {'year':year,'month':month,'name':calendar.month_name[month],'weeks':weeks,'row':row,'limitations':data['limitations']}


@router.get('/operations/calendar-period')
def period_page(request:Request,view:str='month',year:int|None=None,month:int|None=None,property_id:str='',
                db=Depends(get_db),admin=Depends(require_admin)):
    today=datetime.now(ZoneInfo('Australia/Brisbane')).date();year=year if year is not None else today.year;month=month if month is not None else today.month
    if view not in ('month','year') or not 2<=year<=9998 or not 1<=month<=12:
        raise HTTPException(422,'Choose month or year, a year from 2–9998 and a month from 1–12')
    selected=optional_property(property_id);properties=db.query(Property).order_by(Property.name,Property.id).all()
    prop=db.get(Property,selected) if selected else (properties[0] if properties else None)
    if selected and not prop:raise HTTPException(404,'Property not found')
    try:months=[period_month(db,prop.id,year,m) for m in (range(1,13) if view=='year' else [month])] if prop else []
    except ValueError as exc:raise HTTPException(422,str(exc)) from None
    previous=(year-1,month) if view=='year' else ((year-1,12) if month==1 else (year,month-1))
    following=(year+1,month) if view=='year' else ((year+1,1) if month==12 else (year,month+1))
    return templates.TemplateResponse(request=request,name='calendar_period.html',context={'property':prop,'properties':properties,
        'view':view,'year':year,'month':month,'months':months,'weekdays':['Mon','Tue','Wed','Thu','Fri','Sat','Sun'],
        'previous':previous if previous[0]>=2 else None,'following':following if following[0]<=9998 else None})
