"""Audited named-account lifecycle. Disabling preserves work and financial history."""
import hashlib,json,os,re
from datetime import datetime
from types import SimpleNamespace
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from app.models import StaffUser,StaffRole,TaskTimeEntry,WorkOrder
from app.operations.stock import lock_stock
from app.operations.tasks import TaskError,form_token,verify_token
from app.operations.task_plans import record_command
from app.security import hash_password


def snapshot(row):
    return {'id':row.id,'name':row.name,'username':row.username,'role':row.role.value,'active':row.active} if row else None


def staff_token(actor,row=None):
    digest=hashlib.sha256(json.dumps(snapshot(row),sort_keys=True).encode()).hexdigest()
    return form_token(actor,SimpleNamespace(id=row.id if row else 0,version=0),purpose='staff-control:'+digest)


def verify(actor,row,token):
    digest=hashlib.sha256(json.dumps(snapshot(row),sort_keys=True).encode()).hexdigest()
    verify_token(token,actor,row.id if row else 0,purpose='staff-control:'+digest)


def create(db,*,actor,token,name,username,password,role,note):
    name=name.strip();username=username.strip()
    if not 1<=len(name)<=160 or not re.fullmatch(r'[a-z0-9][a-z0-9_.@-]{2,79}',username):raise TaskError('Use a name up to 160 characters and a lowercase username of 3–80 letters, digits or . _ @ -')
    if not 12<=len(password)<=256:raise TaskError('Use a password of 12–256 characters')
    if username==os.environ.get('ADMIN_USER'):raise TaskError('The shared administrator username is reserved')
    try:role=StaffRole(role)
    except ValueError:raise TaskError('Choose a supported role') from None
    try:
        lock_stock(db);verify(actor,None,token)
        row=StaffUser(name=name,username=username,password_hash=hash_password(password),role=role,active=True)
        db.add(row);db.flush()
        record_command(db,token,'staff-control',actor,note,{'action':'create','after':snapshot(row)})
        db.commit();return row.id
    except IntegrityError:db.rollback();raise TaskError('Username exists or this form was already submitted') from None
    except Exception:db.rollback();raise


def set_active(db,*,actor,staff_id,token,active,note):
    try:
        lock_stock(db);db.expire_all();row=db.get(StaffUser,staff_id)
        if not row:raise TaskError('Staff account not found')
        verify(actor,row,token)
        if row.username==actor:raise TaskError('Use another administrator to change your own login access')
        if row.active==active:raise TaskError('Account state already changed; reload first')
        before=snapshot(row);row.active=active;stopped=[]
        if not active:
            for entry in db.query(TaskTimeEntry).filter_by(active_worker_id=row.id).all():
                entry.stopped_at=datetime.utcnow();entry.active_worker_id=None;stopped.append(entry.id)
                db.execute(update(WorkOrder).where(WorkOrder.id==entry.work_order_id).values(version=WorkOrder.version+1))
        record_command(db,token,'staff-control',actor,note,{'action':'enable' if active else 'disable','before':before,'after':snapshot(row),'stopped_timer_ids':stopped})
        db.commit()
    except IntegrityError:db.rollback();raise TaskError('Account request already saved; reload first') from None
    except Exception:db.rollback();raise
