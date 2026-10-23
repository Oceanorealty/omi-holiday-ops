"""Internal property/night notes. They never change stock, rates or bookings."""
import hashlib,json
from datetime import date,timedelta,datetime
from types import SimpleNamespace
from sqlalchemy.exc import IntegrityError
from app.models import CalendarNote,CalendarNoteChange,OpsCommand,Property
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError,form_token,verify_token


def dates(start,end):
    try:
        first,last=date.fromisoformat(start),date.fromisoformat(end)
        if first.isoformat()!=start or last.isoformat()!=end or not 1<=(last-first).days<=62:raise ValueError()
        return [(first+timedelta(days=i)).isoformat() for i in range((last-first).days)]
    except (ValueError,TypeError):raise TaskError('Choose 1–62 nights with the end date excluded') from None


def rows(db,property_id,start,end):
    dates(start,end)
    return db.query(CalendarNote).filter(CalendarNote.property_id==property_id,CalendarNote.night>=start,CalendarNote.night<end).order_by(CalendarNote.night).all()


def purpose(db,property_id,start,end):
    existing=rows(db,property_id,start,end)
    digest=hashlib.sha256(json.dumps([[r.night,r.version,r.body] for r in existing],sort_keys=True).encode()).hexdigest()
    return 'calendar-notes:'+start+':'+end+':'+digest


def notes_token(db,actor,property_id,start,end):
    return form_token(actor,SimpleNamespace(id=property_id,version=0),purpose=purpose(db,property_id,start,end))


def save_notes(db,*,actor,property_id,start,end,token,action,body,note):
    nights=dates(start,end);body=body.strip()
    if action not in ('replace','clear'):raise TaskError('Choose replace or clear')
    if action=='replace' and not 1<=len(body)<=1000:raise TaskError('Use a note of 1–1000 characters')
    if action=='clear':body=''
    try:
        lock_stock(db);db.expire_all()
        if not db.get(Property,property_id):raise TaskError('Property not found')
        verify_token(token,actor,property_id,purpose=purpose(db,property_id,start,end))
        existing={r.night:r for r in rows(db,property_id,start,end)};changes=[]
        for night in nights:
            row=existing.get(night)
            if (row.body if row else '')==body:continue
            before={'body':row.body,'version':row.version} if row else {'body':'','version':0}
            if not row:row=CalendarNote(property_id=property_id,night=night,version=0);db.add(row)
            row.body=body;row.version+=1;row.actor=actor;row.updated_at=datetime.utcnow()
            changes.append({'night':night,'before':before,'after':{'body':body,'version':row.version}})
        record_command(db,token,'calendar-notes',actor,note,{'property_id':property_id,'start':start,'end':end,'action':action,'changes':changes})
        db.flush()
        command=db.query(OpsCommand).filter_by(token_digest=hashlib.sha256(token.encode()).hexdigest()).one()
        db.add(CalendarNoteChange(property_id=property_id,start=start,end=end,command_id=command.id))
        db.commit();return len(changes)
    except IntegrityError:
        db.rollback();raise TaskError('This note submission was already recorded; reload before saving') from None
    except Exception:
        db.rollback();raise
