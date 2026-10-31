"""Read-only cleaning queue; never assigns work or changes task state."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from sqlalchemy.orm import joinedload
from app.models import CleaningTask, CleaningStatus, Property
from app.operations.tasks import TaskError


def assignment_label(task):
    if task.cleaner:
        return task.cleaner.name + ('' if task.cleaner.active else ' (inactive)')
    if task.cleaner_id:
        return f'Cleaner #{task.cleaner_id} (record unavailable)'
    return (task.assignee or '').strip() or 'Unassigned'


def queue_return_url(property_id='', view='open'):
    """Construct only an internal queue URL, never a supplied destination."""
    value = '' if property_id is None else str(property_id)
    if view not in ('open', 'unassigned', 'overdue') or (value and
            (not value.isascii() or not value.isdigit() or len(value) > 18 or int(value) < 1)):
        return '/cleaning'
    if not value and view == 'open':
        return '/cleaning'
    return '/cleaning?' + urlencode({'property_id': int(value) if value else '', 'view': view})


def queue_data(db, *, property_id=None, view='open', now=None):
    if view not in ('open', 'unassigned', 'overdue'):
        raise TaskError('Choose all open, unassigned or overdue work')
    if property_id is None or property_id == '':
        property_id = None
    else:
        value = str(property_id)
        if not value.isascii() or not value.isdigit() or len(value) > 18 or int(value) < 1:
            raise TaskError('Choose a valid property')
        property_id = int(value)
        if not db.get(Property, property_id):
            raise LookupError('Property not found')
    properties = db.query(Property).order_by(Property.name).all()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = {}
    for prop in properties:
        try:
            today[prop.id] = now.astimezone(ZoneInfo(prop.time_zone)).date()
        except (TypeError, ValueError, KeyError):
            today[prop.id] = None
    query = db.query(CleaningTask).options(joinedload(CleaningTask.cleaner))
    if property_id is not None:
        query = query.filter(CleaningTask.property_id == property_id)
    active = query.filter(CleaningTask.status != CleaningStatus.done,
        CleaningTask.cancelled_at.is_(None)).order_by(CleaningTask.due_date, CleaningTask.id).all()
    done = query.filter(CleaningTask.status == CleaningStatus.done).order_by(
        CleaningTask.due_date.desc(), CleaningTask.id.desc()).limit(20).all()
    unassigned = {task.id for task in active if not task.cleaner_id and not (task.assignee or '').strip()}
    overdue = {task.id for task in active if today.get(task.property_id) is not None
        and task.due_date.date() < today[task.property_id]}
    rows = active if view == 'open' else [task for task in active if task.id in
        (unassigned if view == 'unassigned' else overdue)]
    return {'tasks': rows, 'done_tasks': done, 'properties': properties,
        'selected_property_id': property_id, 'queue_view': view,
        'queue_counts': {'open': len(active), 'unassigned': len(unassigned), 'overdue': len(overdue)},
        'overdue_task_ids': overdue,
        'assignment_labels': {task.id: assignment_label(task) for task in active + done},
        'timezone_unknown_count': sum(today.get(task.property_id) is None for task in active)}
