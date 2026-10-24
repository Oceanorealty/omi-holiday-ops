"""Immutable checklist templates, repeatable schedules and once-only task expenses."""
import calendar
import hashlib
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from app.models import (ChecklistTemplate, Expense, OpsCommand, Property, ScheduledTaskOccurrence,
    StaffUser, TaskSchedule, TaskScheduleCalendarRule, TaskTemplateApplication, WorkOrder, WorkOrderCheck, WorkOrderCost, WorkOrderEvent)
from app.operations.tasks import CATEGORIES, PRIORITIES, TaskError, checklist_snapshot, verify_token
from app.operations.reservations import money


def today():
    return datetime.now(ZoneInfo('Australia/Brisbane')).date()


def iso_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise TaskError('Use a valid YYYY-MM-DD date') from None


def record_command(db, token, purpose, actor, note, payload):
    if not 10 <= len(note.strip()) <= 1000:
        raise TaskError('Record a note of 10–1000 characters')
    db.add(OpsCommand(token_digest=hashlib.sha256(token.encode()).hexdigest(), purpose=purpose,
        actor=actor, note=note.strip(), payload=json.dumps(payload, sort_keys=True)))


def create_template(db, *, actor, token, name, lines, note):
    verify_token(token, actor, 0, purpose='template-create')
    name = name.strip()
    labels = [line.strip() for line in lines.splitlines() if line.strip()]
    if not 1 <= len(name) <= 120 or not 1 <= len(labels) <= 50 or any(len(s) > 240 for s in labels):
        raise TaskError('Use a name up to 120 characters and 1–50 checks up to 240 characters each')
    if len({label.casefold() for label in labels}) != len(labels):
        raise TaskError('Remove duplicate checklist labels')
    items = [{'label': label, 'required': True} for label in labels]
    try:
        row = ChecklistTemplate(name=name, items=json.dumps(items), created_by=actor)
        db.add(row); db.flush()
        record_command(db, token, 'template-create', actor, note, {'template_id': row.id, 'name': name, 'items': items})
        db.commit()
        return row.id
    except IntegrityError:
        db.rollback(); raise TaskError('Request already saved; reload before submitting again') from None
    except Exception:
        db.rollback(); raise


def claim_task(db, actor, entity_id, token, purpose):
    expected = verify_token(token, actor, entity_id, purpose=purpose)
    result = db.execute(update(WorkOrder).where(WorkOrder.id == entity_id, WorkOrder.version == expected,
        WorkOrder.status.in_(['open', 'in_progress'])).values(version=expected+1, updated_at=datetime.utcnow())
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise TaskError('Task changed or is closed; reload before saving')
    db.expire_all()
    return db.get(WorkOrder, entity_id)


def apply_template(db, *, actor, entity_id, token, template_id, note):
    try:
        task = claim_task(db, actor, entity_id, token, 'template-apply')
        template = db.get(ChecklistTemplate, template_id)
        if not template:
            raise TaskError('Checklist template not found')
        if db.query(TaskTemplateApplication).filter_by(work_order_id=entity_id, template_id=template_id).first():
            raise TaskError('This template is already applied to the task')
        checks = db.query(WorkOrderCheck).filter_by(work_order_id=entity_id).order_by(WorkOrderCheck.id).all()
        items = json.loads(template.items)
        if len(checks) + len(items) > 50:
            raise TaskError('A task supports at most 50 checks')
        for item in items:
            check = WorkOrderCheck(work_order_id=entity_id, label=item['label'], required=item['required'])
            db.add(check); checks.append(check)
        db.add(TaskTemplateApplication(work_order_id=entity_id, template_id=template_id))
        db.flush()
        record_command(db, token, 'template-apply', actor, note, {'task_id': entity_id, 'template_id': template_id})
        db.add(WorkOrderEvent(work_order_id=entity_id, actor=actor, token_digest=hashlib.sha256(token.encode()).hexdigest(),
            note=note.strip(), snapshot=json.dumps({'action': 'apply_template', 'template_id': template_id,
                'version': task.version, 'checklist': checklist_snapshot(checks)}, sort_keys=True)))
        db.commit()
    except IntegrityError:
        db.rollback(); raise TaskError('Request already saved; reload before submitting again') from None
    except Exception:
        db.rollback(); raise


def create_schedule(db, *, actor, token, property_id, title, description, category, priority,
                    interval_days, start_date, end_date='', assignee_id=None, template_id=None, note, frequency='days'):
    verify_token(token, actor, 0, purpose='schedule-create')
    start = iso_date(start_date)
    end = iso_date(end_date) if end_date else None
    title, description = title.strip(), description.strip()
    if not 1 <= len(title) <= 160 or len(description) > 4000 or category not in CATEGORIES or priority not in PRIORITIES:
        raise TaskError('Choose a valid title, category and priority; details are limited to 4000 characters')
    limits={'days':365,'weeks':52,'months':12}
    if frequency not in limits or not 1 <= interval_days <= limits[frequency]:
        raise TaskError('Choose 1–365 days, 1–52 weeks or 1–12 calendar months')
    repeat_every=interval_days
    interval_days=interval_days*7 if frequency=='weeks' else interval_days
    if frequency=='months':interval_days=31*repeat_every
    if start < today() - timedelta(days=31) or (end and end < start):
        raise TaskError('Use a 1–365 day interval, a start no earlier than 31 days ago, and an end on or after the start')
    if not db.get(Property, property_id):
        raise TaskError('Property not found')
    staff = db.get(StaffUser, assignee_id) if assignee_id is not None else None
    if assignee_id is not None and (not staff or not staff.active):
        raise TaskError('Choose an active staff member')
    template = db.get(ChecklistTemplate, template_id) if template_id is not None else None
    if template_id is not None and not template:
        raise TaskError('Checklist template not found')
    try:
        row = TaskSchedule(property_id=property_id, title=title, description=description, category=category,
            priority=priority, interval_days=interval_days, next_due=start.isoformat(), end_date=end.isoformat() if end else None,
            assignee_id=assignee_id, template_id=template_id, checklist_snapshot=template.items if template else '[]', created_by=actor)
        db.add(row); db.flush()
        if frequency=='months':
            db.add(TaskScheduleCalendarRule(schedule_id=row.id,anchor_date=start.isoformat(),interval_months=repeat_every))
        record_command(db, token, 'schedule-create', actor, note, {'schedule_id': row.id, 'start': start.isoformat(),
            'end': row.end_date, 'interval_days': interval_days, 'template_id': template_id,
            'frequency':frequency,'repeat_every':repeat_every})
        db.commit()
        return row.id
    except IntegrityError:
        db.rollback(); raise TaskError('Request already saved; reload before submitting again') from None
    except Exception:
        db.rollback(); raise


def set_schedule_active(db, *, actor, entity_id, token, active, note):
    expected = verify_token(token, actor, entity_id, purpose='schedule-state')
    try:
        result = db.execute(update(TaskSchedule).where(TaskSchedule.id == entity_id, TaskSchedule.version == expected)
            .values(active=active, version=expected+1).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise TaskError('Schedule changed; reload before saving')
        record_command(db, token, 'schedule-state', actor, note, {'schedule_id': entity_id, 'active': active})
        db.commit(); db.expire_all()
    except IntegrityError:
        db.rollback(); raise TaskError('Request already saved; reload before submitting again') from None
    except Exception:
        db.rollback(); raise


def next_calendar_due(anchor, current, every):
    """Preserve the original day even after a short month or a leap year."""
    elapsed=(current.year-anchor.year)*12+current.month-anchor.month
    month_index=anchor.year*12+anchor.month-1+(elapsed//every+1)*every
    year,month0=divmod(month_index,12)
    month=month0+1
    return date(year,month,min(anchor.day,calendar.monthrange(year,month)[1]))


def materialize_schedules(db, through=None):
    through = through or today() + timedelta(days=14)
    if not today() <= through <= today() + timedelta(days=30):
        raise TaskError('Generate through a date between today and 30 days ahead')
    ids = [r.id for r in db.query(TaskSchedule).filter(TaskSchedule.active.is_(True),
        TaskSchedule.next_due <= through.isoformat()).order_by(TaskSchedule.id).limit(101).all()]
    generated, held = 0, max(0, len(ids)-100)
    for entity_id in ids[:100]:
        try:
            # A no-op write serializes each rule against other generators, including SQLite.
            locked = db.execute(update(TaskSchedule).where(TaskSchedule.id == entity_id, TaskSchedule.active.is_(True))
                .values(version=TaskSchedule.version).execution_options(synchronize_session=False))
            db.expire_all()
            row = db.get(TaskSchedule, entity_id)
            if locked.rowcount != 1 or not row or not row.active:
                db.rollback(); continue
            due = iso_date(row.next_due)
            rule=db.get(TaskScheduleCalendarRule,entity_id)
            anchor=iso_date(rule.anchor_date) if rule else None
            end = iso_date(row.end_date) if row.end_date else None
            added, processed = 0, 0
            staff = db.get(StaffUser, row.assignee_id) if row.assignee_id else None
            assignee = row.assignee_id if staff and staff.active else None
            while due <= through and (end is None or due <= end) and processed < 50:
                processed += 1
                occurrence = db.query(ScheduledTaskOccurrence).filter_by(schedule_id=entity_id, due_date=due.isoformat()).first()
                if not occurrence:
                    task = WorkOrder(property_id=row.property_id, title=row.title, description=row.description,
                        category=row.category, priority=row.priority, assignee_id=assignee, due_date=due.isoformat(), status='open')
                    db.add(task); db.flush()
                    checks = []
                    for item in json.loads(row.checklist_snapshot):
                        check = WorkOrderCheck(work_order_id=task.id, label=item['label'], required=item['required'])
                        db.add(check); checks.append(check)
                    if row.template_id:
                        db.add(TaskTemplateApplication(work_order_id=task.id, template_id=row.template_id))
                    db.flush()
                    db.add(ScheduledTaskOccurrence(schedule_id=entity_id, due_date=due.isoformat(), work_order_id=task.id))
                    note = 'Created from recurring schedule; no external notification sent'
                    if row.assignee_id and assignee is None:
                        note += '; original assignee is inactive, task left unassigned'
                    digest = hashlib.sha256(f'schedule:{entity_id}:{due}'.encode()).hexdigest()
                    db.add(WorkOrderEvent(work_order_id=task.id, token_digest=digest, actor='scheduler', note=note,
                        snapshot=json.dumps({'schedule_id': entity_id, 'due_date': due.isoformat(), 'title': row.title,
                            'assignee_id': assignee, 'checklist': checklist_snapshot(checks)}, sort_keys=True)))
                    added += 1
                due = next_calendar_due(anchor,due,rule.interval_months) if rule else due+timedelta(days=row.interval_days)
            if processed:
                row.next_due = due.isoformat()
                row.version += 1
            if end and due > end:
                row.active = False
                if not processed:
                    row.version += 1
            if due <= through and (end is None or due <= end):
                held += 1
            db.commit()
            generated += added
        except IntegrityError:
            db.rollback(); held += 1
        except Exception:
            db.rollback(); raise
    return {'generated': generated, 'needs_another_run': held}


def set_task_cost(db, *, actor, entity_id, token, amount, category, note):
    total = money(amount)
    if not 1 <= len(category.strip()) <= 60:
        raise TaskError('Use a cost category of 1–60 characters')
    try:
        task = claim_task(db, actor, entity_id, token, 'task-cost')
        cost = db.query(WorkOrderCost).filter_by(work_order_id=entity_id).first()
        if cost and cost.expense_id is not None:
            raise TaskError('A recorded task expense is immutable; use a separate reviewed accounting adjustment')
        if cost is None:
            cost = WorkOrderCost(work_order_id=entity_id)
            db.add(cost)
        cost.amount, cost.category = total, category.strip()
        record_command(db, token, 'task-cost', actor, note, {'task_id': entity_id,
            'amount': str(total) if total is not None else None, 'category': cost.category})
        db.add(WorkOrderEvent(work_order_id=entity_id, token_digest=hashlib.sha256(token.encode()).hexdigest(),
            actor=actor, note=note.strip(), snapshot=json.dumps({'action': 'set_cost', 'version': task.version,
                'amount': str(total) if total is not None else None, 'category': cost.category})))
        db.commit()
    except IntegrityError:
        db.rollback(); raise TaskError('Request already saved; reload before submitting again') from None
    except Exception:
        db.rollback(); raise


def generate_task_expense(db, task):
    """Called within the parent task completion transaction; never commits."""
    cost = db.query(WorkOrderCost).filter_by(work_order_id=task.id).first()
    if not cost or cost.amount is None or cost.expense_id is not None:
        return cost.expense_id if cost else None
    prop = db.get(Property, task.property_id)
    local_date = datetime.now(ZoneInfo(prop.time_zone)).date()
    expense = Expense(property_id=task.property_id, category=cost.category,
        description=f'Task #{task.id}: {task.title}', amount=cost.amount,
        occurred_at=datetime.combine(local_date, datetime.min.time()))
    db.add(expense); db.flush()
    cost.expense_id = expense.id
    return expense.id
