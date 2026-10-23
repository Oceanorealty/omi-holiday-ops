"""Bounded database-backed task files, always downloaded, never executed inline."""
import hashlib
from pathlib import PurePosixPath
from sqlalchemy import func,update
from sqlalchemy.exc import IntegrityError
from app.models import TaskAttachment,WorkOrder
from app.operations.stock import lock_stock
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError,form_token,verify_token

MAX_FILE=5*1024*1024
MAX_TOTAL=100*1024*1024
MAX_PER_TASK=20


def file_type(filename,data):
    name=filename.replace('\\','/').split('/')[-1].strip()
    if not 1<=len(name)<=180 or any(ord(c)<32 or ord(c)==127 for c in name):raise TaskError('Use a filename up to 180 characters without control characters')
    if not data or len(data)>MAX_FILE:raise TaskError('Upload a non-empty file up to 5 MiB')
    suffix=PurePosixPath(name).suffix.lower()
    if suffix=='.pdf' and data.startswith(b'%PDF-'):media='application/pdf'
    elif suffix=='.png' and data.startswith(b'\x89PNG\r\n\x1a\n'):media='image/png'
    elif suffix in ('.jpg','.jpeg') and data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9'):media='image/jpeg'
    elif suffix=='.txt':
        try:data.decode('utf-8')
        except UnicodeDecodeError:raise TaskError('Text attachments must use UTF-8') from None
        media='text/plain'
    else:raise TaskError('Use PDF, PNG, JPEG or UTF-8 text with a matching file header')
    return name,media


def attachment_context(db,actor,task):
    # File bytes are deliberately excluded from task-page queries.
    rows=db.query(TaskAttachment.id,TaskAttachment.filename,TaskAttachment.size,TaskAttachment.sha256,
        TaskAttachment.archived,TaskAttachment.actor,TaskAttachment.created_at).filter_by(work_order_id=task.id).order_by(TaskAttachment.id.desc()).all()
    return {'attachments':rows,'attachment_token':form_token(actor,task,purpose='task-attachment')}


def save_attachment(db,*,actor,task_id,token,filename,data,note):
    name,media=file_type(filename,data);expected=verify_token(token,actor,task_id,purpose='task-attachment')
    try:
        lock_stock(db);db.expire_all();task=db.get(WorkOrder,task_id)
        if not task or task.version!=expected:raise TaskError('Task changed; reload before uploading')
        if db.query(TaskAttachment.id).filter_by(work_order_id=task_id).count()>=MAX_PER_TASK:
            raise TaskError('This task has reached its 20-file limit, including archived files')
        size=db.query(func.coalesce(func.sum(TaskAttachment.size),0)).scalar()
        if size+len(data)>MAX_TOTAL:raise TaskError('Attachment storage limit reached; arrange a reviewed storage migration')
        result=db.execute(update(WorkOrder).where(WorkOrder.id==task_id,WorkOrder.version==expected).values(version=expected+1).execution_options(synchronize_session=False))
        if result.rowcount!=1:raise TaskError('Task changed; reload before uploading')
        row=TaskAttachment(work_order_id=task_id,filename=name,media_type=media,data=data,size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),actor=actor);db.add(row);db.flush()
        record_command(db,token,'task-attachment',actor,note,{'task_id':task_id,'attachment_id':row.id,'filename':name,'size':len(data),'sha256':row.sha256})
        db.commit();db.expire_all();return row.id
    except IntegrityError:
        db.rollback();raise TaskError('This upload was already recorded; reload the task') from None
    except Exception:
        db.rollback();raise


def archive_attachment(db,*,actor,task_id,entry_id,token,archived,note):
    expected=verify_token(token,actor,task_id,purpose='task-attachment')
    try:
        lock_stock(db);db.expire_all();task=db.get(WorkOrder,task_id);row=db.get(TaskAttachment,entry_id)
        if not task or task.version!=expected:raise TaskError('Task changed; reload before saving')
        if not row or row.work_order_id!=task_id or row.archived==archived:raise TaskError('Choose a file on this task with a different archive state')
        result=db.execute(update(WorkOrder).where(WorkOrder.id==task_id,WorkOrder.version==expected).values(version=expected+1).execution_options(synchronize_session=False))
        if result.rowcount!=1:raise TaskError('Task changed; reload before saving')
        row.archived=archived
        record_command(db,token,'task-attachment-archive',actor,note,{'task_id':task_id,'attachment_id':entry_id,'archived':archived,'sha256':row.sha256})
        db.commit();db.expire_all()
    except IntegrityError:
        db.rollback();raise TaskError('This submission was already recorded') from None
    except Exception:
        db.rollback();raise
