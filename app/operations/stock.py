"""Explicit resource consumption for linked listings and pooled room types.

Bindings consume ALL attached resources, not alternative physical-room choices.
The shared write guard deliberately serializes inventory changes for this small PMS.
It does not provide a distributed lock on external OTA inventories.
"""
from collections import defaultdict
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from app.models import Booking, BookingStatus, Property, StockBinding, StockGuard, StockResource
from app.operations.task_plans import record_command
from app.operations.tasks import TaskError, form_token, verify_token


def lock_stock(db):
    dialect=db.bind.dialect.name
    if dialect=='sqlite':
        from sqlalchemy.dialects.sqlite import insert
    elif dialect=='postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:raise TaskError('Inventory locking requires SQLite or PostgreSQL')
    db.execute(insert(StockGuard).values(id=1,revision=0).on_conflict_do_nothing(index_elements=['id']))
    db.execute(update(StockGuard).where(StockGuard.id==1).values(revision=StockGuard.revision))


def config_token(db,actor):
    guard=db.get(StockGuard,1)
    return form_token(actor,SimpleNamespace(id=1,version=guard.revision if guard else 0),purpose='stock-config')


def stock_context(db,property_id,start,end):
    prop=db.get(Property,property_id)
    if not prop:raise TaskError('Property not found')
    requested=db.query(StockBinding).filter_by(property_id=property_id).all()
    if requested:
        ids=[b.resource_id for b in requested]
        bindings=db.query(StockBinding).filter(StockBinding.resource_id.in_(ids)).all()
        resources={r.id:r for r in db.query(StockResource).filter(StockResource.id.in_(ids)).all()}
        groups=[]
        for link in requested:
            resource=resources.get(link.resource_id)
            if not resource or resource.time_zone!=prop.time_zone:raise TaskError('Resource timezone needs review')
            amounts={b.property_id:b.quantity for b in bindings if b.resource_id==link.resource_id}
            groups.append({'capacity':resource.capacity,'quantity':link.quantity,'amounts':amounts})
    else:
        groups=[{'capacity':1,'quantity':1,'amounts':{property_id:1}}]
    property_ids=set().union(*(g['amounts'] for g in groups))
    related=db.query(Property).filter(Property.id.in_(property_ids)).all()
    if any(p.time_zone!=prop.time_zone for p in related):raise TaskError('Linked property timezones need review')
    records=db.query(Booking).filter(Booking.property_id.in_(property_ids),Booking.status==BookingStatus.confirmed,
        Booking.check_in<end,or_(Booking.check_out>start,Booking.check_out<=Booking.check_in)).limit(5001).all()
    if len(records)>5000:raise TaskError('Inventory needs a narrower review before booking')
    return {'groups':groups,'records':records,'shared':bool(requested),
        'capacity':min(g['capacity']//g['quantity'] for g in groups)}


def units_from_context(context,start,end,exclude_id=0):
    if end<=start:return 0
    remaining=[]
    for group in context['groups']:
        changes=defaultdict(int)
        for booking in context['records']:
            if booking.id==exclude_id or booking.property_id not in group['amounts']:continue
            if booking.check_out<=booking.check_in:return 0
            booking_start,booking_end=inventory_interval(booking)
            if booking_start>=end or booking_end<=start:continue
            quantity=group['capacity'] if booking.is_block else group['amounts'][booking.property_id]
            changes[max(start,booking_start)]+=quantity
            changes[min(end,booking_end)]-=quantity
        used=peak=0
        for point,change in sorted(changes.items()):
            used+=change
            if point<end:peak=max(peak,used)
        remaining.append(max(0,(group['capacity']-peak)//group['quantity']))
    return min(remaining)


def inventory_interval(booking):
    # Overnight stock uses local nights, so departure morning permits same-day arrival.
    if booking.check_in.date()<booking.check_out.date():
        return (datetime.combine(booking.check_in.date(),datetime.min.time()),
                datetime.combine(booking.check_out.date(),datetime.min.time()))
    return booking.check_in,booking.check_out


def available_units(db,property_id,start,end,exclude_id=0):
    return units_from_context(stock_context(db,property_id,start,end),start,end,exclude_id)


def conflict_ids(db):
    records=db.query(Booking).filter(Booking.status==BookingStatus.confirmed).all()
    bindings=db.query(StockBinding).all()
    resources={r.id:r for r in db.query(StockResource).all()}
    bound={b.property_id for b in bindings}
    groups=[]
    for resource in resources.values():
        amounts={b.property_id:b.quantity for b in bindings if b.resource_id==resource.id}
        if amounts:groups.append((resource.capacity,amounts))
    for property_id in {r.property_id for r in records}-bound:
        groups.append((1,{property_id:1}))
    return group_conflict_ids(records,groups)


def context_conflicts(context,start=None,end=None):
    return group_conflict_ids(context['records'],[(g['capacity'],g['amounts']) for g in context['groups']],start,end)


def group_conflict_ids(records,groups,lower=None,upper=None):
    conflicts=set()
    for capacity,amounts in groups:
        starts,ends=defaultdict(list),defaultdict(list)
        for booking in records:
            if booking.property_id not in amounts:continue
            if booking.check_out<=booking.check_in:
                if lower is None:conflicts.add(booking.id)
                continue
            weight=capacity if booking.is_block else amounts[booking.property_id]
            start,end=inventory_interval(booking)
            if lower is not None:
                if start>=upper or end<=lower:continue
                start,end=max(start,lower),min(end,upper)
            starts[start].append((booking.id,weight))
            ends[end].append(booking.id)
        active={}
        for point in sorted(set(starts)|set(ends)):
            for entity_id in ends[point]:active.pop(entity_id,None)
            active.update(starts[point])
            if sum(active.values())>capacity:conflicts.update(active)
    return conflicts


def refresh_conflicts(db):
    db.flush()
    conflicts=conflict_ids(db)
    for booking in db.query(Booking).all():
        booking.has_conflict=booking.id in conflicts
    db.flush()


def configure_stock(db,*,actor,token,action,name='',capacity=1,time_zone='UTC',property_id=None,resource_id=None,quantity=1,note):
    expected=verify_token(token,actor,1,purpose='stock-config')
    try:
        lock_stock(db);db.expire_all()
        guard=db.get(StockGuard,1)
        if guard.revision!=expected:raise TaskError('Inventory configuration changed; reload before saving')
        if action=='create':
            try:ZoneInfo(time_zone)
            except (ZoneInfoNotFoundError,ValueError):raise TaskError('Use a valid IANA timezone') from None
            if not 1<=len(name.strip())<=160 or not 1<=capacity<=1000:raise TaskError('Use a resource name and capacity of 1–1000')
            row=StockResource(name=name.strip(),capacity=capacity,time_zone=time_zone)
            db.add(row);db.flush();payload={'resource_id':row.id,'name':row.name,'capacity':capacity,'time_zone':time_zone}
        elif action in ('bind','unbind'):
            prop=db.get(Property,property_id);resource=db.get(StockResource,resource_id)
            if not prop or not resource:raise TaskError('Select an existing property and resource')
            if action=='bind' and prop.time_zone!=resource.time_zone:raise TaskError('Property and resource must use the same stored timezone')
            row=db.query(StockBinding).filter_by(property_id=property_id,resource_id=resource_id).first()
            before=row.quantity if row else None
            if action=='bind':
                if not 1<=quantity<=resource.capacity:raise TaskError('Consumption must be between one and resource capacity')
                if not row:row=StockBinding(property_id=property_id,resource_id=resource_id);db.add(row)
                row.quantity=quantity
            else:
                if not row:raise TaskError('No such resource binding')
                db.delete(row)
            payload={'property_id':property_id,'resource_id':resource_id,'before':before,'after':quantity if action=='bind' else None}
        elif action=='timezone':
            prop=db.get(Property,property_id)
            if not prop:raise TaskError('Property not found')
            try:ZoneInfo(time_zone)
            except (ZoneInfoNotFoundError,ValueError):raise TaskError('Use a valid IANA timezone') from None
            if db.query(Booking).filter_by(property_id=property_id).first() or db.query(StockBinding).filter_by(property_id=property_id).first():
                raise TaskError('Existing booking times or resource bindings need a reviewed timezone migration')
            payload={'property_id':property_id,'before':prop.time_zone,'after':time_zone}
            prop.time_zone=time_zone
        else:raise TaskError('Unknown inventory action')
        guard.revision+=1
        record_command(db,token,'stock-config',actor,note,{'action':action,**payload})
        refresh_conflicts(db)
        db.commit()
    except IntegrityError:
        db.rollback();raise TaskError('Inventory request already saved; reload before trying again') from None
    except Exception:
        db.rollback();raise
