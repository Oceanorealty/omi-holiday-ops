"""UTC work intervals, recorded by administrators for named staff. No payroll."""
import csv,io,json
from datetime import datetime,date,timedelta
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from app.models import TaskTimeEntry,WorkOrder,StaffUser,Property
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError,form_token,verify_token


def time_token(actor,task):
    return form_token(actor,task,purpose='task-time')


def close_task_timers(db,task_id,at):
    rows=db.query(TaskTimeEntry).filter_by(work_order_id=task_id,stopped_at=None,voided=False).all()
    for row in rows:
        if at<row.started_at:raise TaskError('System clock precedes timer start; review before changing task')
        row.stopped_at=at;row.active_worker_id=None
    return [r.id for r in rows]


def record_time(db,*,actor,task_id,token,action,worker_id=None,entry_id=None,note,now=None):
    if action not in ('start','stop','void'):raise TaskError('Choose start, stop or void')
    expected=verify_token(token,actor,task_id,purpose='task-time')
    now=now or datetime.utcnow()
    if now.tzinfo is not None:raise TaskError('Timer clock must be naive UTC')
    try:
        lock_stock(db);db.expire_all();task=db.get(WorkOrder,task_id)
        if not task or task.version!=expected:raise TaskError('Task changed; reload before recording time')
        from sqlalchemy import update
        claimed=db.execute(update(WorkOrder).where(WorkOrder.id==task_id,WorkOrder.version==expected).values(version=expected+1,updated_at=now).execution_options(synchronize_session=False))
        if claimed.rowcount!=1:raise TaskError('Task changed; reload before recording time')
        if action=='start':
            if task.status!='in_progress':raise TaskError('Start the task before running a timer')
            worker=db.get(StaffUser,worker_id) if worker_id else None
            if not worker or not worker.active:raise TaskError('Choose an active named staff member')
            if db.query(TaskTimeEntry).filter_by(active_worker_id=worker_id).first():raise TaskError('This staff member already has a running timer')
            row=TaskTimeEntry(work_order_id=task_id,worker_id=worker_id,active_worker_id=worker_id,started_at=now,actor=actor)
            db.add(row);db.flush()
        else:
            row=db.get(TaskTimeEntry,entry_id) if entry_id else None
            if not row or row.work_order_id!=task_id or row.voided:raise TaskError('Choose a current time entry on this task')
            if now<row.started_at:raise TaskError('System clock precedes timer start')
            if action=='stop':
                if row.stopped_at is not None:raise TaskError('This timer is already stopped')
                row.stopped_at=now;row.active_worker_id=None
            else:
                if row.stopped_at is None:row.stopped_at=now
                row.active_worker_id=None;row.voided=True
        record_command(db,token,'task-time',actor,note,{'task_id':task_id,'entry_id':row.id,'worker_id':row.worker_id,
            'action':action,'started_at':row.started_at.isoformat(),'stopped_at':row.stopped_at.isoformat() if row.stopped_at else None,
            'voided':row.voided,'task_version':expected+1})
        db.commit();db.expire_all();return row.id
    except IntegrityError:
        db.rollback();raise TaskError('Timer changed or submission already recorded; reload') from None
    except Exception:
        db.rollback();raise


def task_time_context(db,actor,task):
    rows=db.query(TaskTimeEntry).filter_by(work_order_id=task.id).order_by(TaskTimeEntry.id.desc()).limit(201).all()
    return {'time_rows':rows[:200],'time_truncated':len(rows)>200,'time_token':time_token(actor,task),
        'time_staff':{s.id:s.name for s in db.query(StaffUser).all()}}


def time_report(db,*,start,end,property_id=None,worker_id=None,as_of=None):
    try:
        first,last=date.fromisoformat(start),date.fromisoformat(end)
        if first.isoformat()!=start or last.isoformat()!=end or not 1<=(last-first).days<=366:raise ValueError()
    except (ValueError,TypeError):raise TaskError('Choose 1–366 UTC dates, end excluded') from None
    if property_id and not db.get(Property,property_id):raise TaskError('Property not found')
    if worker_id and not db.get(StaffUser,worker_id):raise TaskError('Staff member not found')
    lo,hi=datetime.combine(first,datetime.min.time()),datetime.combine(last,datetime.min.time())
    as_of=as_of or datetime.utcnow()
    if as_of.tzinfo is not None:raise TaskError('Use naive UTC report time')
    query=db.query(TaskTimeEntry,WorkOrder).join(WorkOrder,WorkOrder.id==TaskTimeEntry.work_order_id).filter(
        TaskTimeEntry.voided.is_(False),TaskTimeEntry.started_at<hi,
        or_(TaskTimeEntry.stopped_at>lo,TaskTimeEntry.stopped_at.is_(None)))
    if property_id:query=query.filter(WorkOrder.property_id==property_id)
    if worker_id:query=query.filter(TaskTimeEntry.worker_id==worker_id)
    records=query.order_by(TaskTimeEntry.started_at,TaskTimeEntry.id).limit(10001).all()
    if len(records)>10000:raise TaskError('Too many time entries; narrow the dates or staff filter')
    rows=[];closed={};running={}
    for entry,task in records:
        stop=min(entry.stopped_at if entry.stopped_at else as_of,hi)
        seconds=max(0,int((stop-max(entry.started_at,lo)).total_seconds()))
        if not seconds:continue
        target=closed if entry.stopped_at else running
        target[entry.worker_id]=target.get(entry.worker_id,0)+seconds
        rows.append({'entry':entry,'task':task,'seconds':seconds,'provisional':entry.stopped_at is None})
    return {'rows':rows,'closed_seconds':closed,'running_seconds':running,'as_of':as_of,'start':start,'end':end}


def time_csv(data,names):
    stream=io.StringIO();writer=csv.writer(stream);writer.writerow(['entry_id','task_id','property_id','worker_id','worker','start_utc','stop_utc','seconds_in_range','provisional'])
    for row in data['rows']:
        e=row['entry'];name=names.get(e.worker_id,'Unknown')
        if name.lstrip().startswith(('=','+','-','@')):name="'"+name
        writer.writerow([e.id,e.work_order_id,row['task'].property_id,e.worker_id,name,e.started_at.isoformat(),e.stopped_at.isoformat() if e.stopped_at else '',row['seconds'],row['provisional']])
    return stream.getvalue()
