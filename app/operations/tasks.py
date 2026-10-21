"""Audited admin tasks with expiring actor/version-bound submissions."""
import hashlib
import hmac
import json
import secrets
import time
from datetime import date, datetime
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from app.auth import _secret
from app.models import Property, StaffUser, WorkOrder, WorkOrderEvent, WorkOrderCheck

CATEGORIES = ('maintenance', 'inspection', 'guest_request', 'other')
PRIORITIES = ('low', 'normal', 'high', 'urgent')
TRANSITIONS = {'open': {'open','in_progress','cancelled'}, 'in_progress': {'in_progress','open','done','cancelled'},
               'done': {'done','open'}, 'cancelled': {'cancelled','open'}}
FIELDS = ('property_id','title','description','category','priority','status','assignee_id','due_date','version')


class TaskError(ValueError):
    pass


def form_token(actor, row=None, purpose="save"):
    if not _secret(): raise TaskError('Login configuration required')
    body=json.dumps({'actor':actor,'id':row.id if row else 0,'version':row.version if row else 0,'purpose':purpose,
                     'expires':int(time.time())+900,'nonce':secrets.token_hex(16)},sort_keys=True,separators=(',',':'))
    return body+'|'+hmac.new(_secret().encode(),body.encode(),hashlib.sha256).hexdigest()


def verify_token(token, actor, entity_id, purpose="save"):
    try:
        body,signature=token.rsplit('|',1)
        expected=hmac.new(_secret().encode(),body.encode(),hashlib.sha256).hexdigest()
        data=json.loads(body)
        if not hmac.compare_digest(signature,expected) or data['actor']!=actor or data['id']!=entity_id or data['expires']<=time.time() or data.get('purpose')!=purpose: raise ValueError()
        return data['version']
    except (ValueError,KeyError,TypeError,AttributeError):
        raise TaskError('Invalid or expired form; reload this page') from None


def save_task(db, *, actor, entity_id, token, property_id, title, description, category, priority,
              status, assignee_id, due_date, note):
    version=verify_token(token,actor,entity_id)
    title,description,note=title.strip(),description.strip(),note.strip()
    if not 1<=len(title)<=160 or len(description)>4000 or not 10<=len(note)<=1000:
        raise TaskError('Use a title of 1–160 characters, details up to 4000, and a change note of 10–1000')
    if category not in CATEGORIES or priority not in PRIORITIES or status not in TRANSITIONS:
        raise TaskError('Unknown category, priority or status')
    if due_date:
        try:
            if date.fromisoformat(due_date).isoformat()!=due_date: raise ValueError()
        except ValueError: raise TaskError('Use a valid YYYY-MM-DD due date') from None
    due_date=due_date or None
    if not db.get(Property,property_id): raise TaskError('Property not found')
    if assignee_id is not None:
        user=db.get(StaffUser,assignee_id)
        if not user or not user.active: raise TaskError('Choose an active staff member')
    try:
        row=db.get(WorkOrder,entity_id) if entity_id else None
        if entity_id and (not row or row.version!=version): raise TaskError('Task changed; reload before saving')
        if row and status not in TRANSITIONS[row.status]: raise TaskError('Start work before completing it; reopen closed work before changing state')
        if row and row.property_id!=property_id: raise TaskError('A task cannot be moved to another property')
        if not row and (version!=0 or status!='open'): raise TaskError('New tasks must start open')
        checks=db.query(WorkOrderCheck).filter_by(work_order_id=entity_id).order_by(WorkOrderCheck.id).all() if row else []
        if status=='done' and any(c.required and not c.completed for c in checks):
            raise TaskError('Complete every required checklist item before closing the task')
        reset_checks = bool(row and row.status in ('done','cancelled') and status=='open')
        values=dict(property_id=property_id,title=title,description=description,category=category,priority=priority,
                    status=status,assignee_id=assignee_id,due_date=due_date,version=version+1,updated_at=datetime.utcnow(),
                    completed_at=(row.completed_at if row and row.status=='done' else datetime.utcnow()) if status=='done' else None)
        if row:
            result=db.execute(update(WorkOrder).where(WorkOrder.id==entity_id,WorkOrder.version==version).values(**values).execution_options(synchronize_session=False))
            if result.rowcount!=1: raise TaskError('Task changed; reload before saving')
        else:
            row=WorkOrder(**values);db.add(row);db.flush();entity_id=row.id
        if reset_checks:
            for check in checks:
                check.completed=False;check.checked_by=None;check.checked_at=None
        snapshot={k:values[k] for k in FIELDS}
        snapshot['checklist']=checklist_snapshot(checks)
        db.add(WorkOrderEvent(work_order_id=entity_id,token_digest=hashlib.sha256(token.encode()).hexdigest(),
            actor=actor,note=note,snapshot=json.dumps(snapshot,sort_keys=True)))
        db.commit();db.expire_all()
        return entity_id
    except IntegrityError:
        db.rollback();raise TaskError('This form was already submitted; reload the task') from None
    except Exception:
        db.rollback();raise


def checklist_snapshot(checks):
    return [{'id':c.id,'label':c.label,'required':c.required,'completed':c.completed,
             'checked_by':c.checked_by,'checked_at':c.checked_at.isoformat() if c.checked_at else None} for c in checks]


def change_checklist(db, *, actor, entity_id, token, action, note, label='', required=True, item_id=None):
    version=verify_token(token,actor,entity_id,purpose='checklist')
    if action not in ('add','check','uncheck') or not 10<=len(note.strip())<=1000:
        raise TaskError('Choose an action and provide a note of 10–1000 characters')
    if action=='add' and not 1<=len(label.strip())<=240:
        raise TaskError('Use a checklist label of 1–240 characters')
    try:
        row=db.get(WorkOrder,entity_id)
        if not row or row.version!=version: raise TaskError('Task changed; reload before saving')
        if row.status not in ('open','in_progress'): raise TaskError('Reopen the task before changing its checklist')
        # Parent version update serializes all checklist and task mutations together.
        result=db.execute(update(WorkOrder).where(WorkOrder.id==entity_id,WorkOrder.version==version)
            .values(version=version+1,updated_at=datetime.utcnow()).execution_options(synchronize_session=False))
        if result.rowcount!=1: raise TaskError('Task changed; reload before saving')
        checks=db.query(WorkOrderCheck).filter_by(work_order_id=entity_id).order_by(WorkOrderCheck.id).all()
        if action=='add':
            if len(checks)>=50: raise TaskError('A task supports up to 50 checklist items')
            item=WorkOrderCheck(work_order_id=entity_id,label=label.strip(),required=bool(required),completed=False)
            db.add(item);db.flush();checks.append(item)
        else:
            item=next((c for c in checks if c.id==item_id),None)
            if not item: raise TaskError('Checklist item not found on this task')
            target=action=='check'
            if item.completed==target: raise TaskError('Item already has this state; reload the task')
            item.completed=target;item.checked_by=actor if target else None
            item.checked_at=datetime.utcnow() if target else None
        db.add(WorkOrderEvent(work_order_id=entity_id,token_digest=hashlib.sha256(token.encode()).hexdigest(),
            actor=actor,note=note.strip(),snapshot=json.dumps({'action':action,'item_id':item.id,
                'version':version+1,'checklist':checklist_snapshot(checks)},sort_keys=True)))
        db.commit();db.expire_all()
    except IntegrityError:
        db.rollback();raise TaskError('This form was already submitted; reload the task') from None
    except Exception:
        db.rollback();raise
