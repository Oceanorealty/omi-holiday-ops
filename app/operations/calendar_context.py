"""Bounded, administrator-only calendar context. Never writes or moves money."""
from datetime import date,datetime
from decimal import Decimal
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from app.models import (Property,InventoryDay,PriceRule,PricingLimit,BookingAccount,BookingLedgerEntry,
    PublicPaymentAccount,WorkOrder,IcalFeed)
from app.operations.billing import totals
from app.pricing import calculate_price


RULE_LIMIT=1000
ENTRY_LIMIT=20000
TASK_LIMIT=2000
FEED_LIMIT=1000


def add_context(db,data):
    ids=[row['property_id'] for row in data['rows']]
    if not ids:return data
    start,end=data['start'],data['end']
    props={p.id:p for p in db.query(Property).filter(Property.id.in_(ids)).all()}
    overrides={(r.property_id,r.night):r for r in db.query(InventoryDay).filter(InventoryDay.property_id.in_(ids),InventoryDay.night>=start,InventoryDay.night<end).all()}
    rules=db.query(PriceRule).filter(PriceRule.active.is_(True),(PriceRule.property_id.in_(ids))|(PriceRule.property_id.is_(None))).order_by(PriceRule.id).limit(RULE_LIMIT+1).all()
    if len(rules)>RULE_LIMIT:raise ValueError('Too many pricing rules for calendar context; narrow the selection or hide context.')
    limits={r.property_id:r for r in db.query(PricingLimit).filter(PricingLimit.property_id.in_(ids)).all()}
    booking_ids={bid for row in data['rows'] for cell in row['days'] for key in ('booking_ids','arrivals','departures') for bid in cell[key]}
    accounts={r.booking_id:r for r in db.query(BookingAccount).filter(BookingAccount.booking_id.in_(booking_ids)).all()} if booking_ids else {}
    entries=db.query(BookingLedgerEntry).filter(BookingLedgerEntry.booking_id.in_(accounts)).order_by(BookingLedgerEntry.id).limit(ENTRY_LIMIT+1).all() if accounts else []
    if len(entries)>ENTRY_LIMIT:raise ValueError('Too much account history for calendar context; narrow the selection or hide context.')
    grouped={bid:[] for bid in accounts}
    for entry in entries:grouped[entry.booking_id].append(entry)
    sandbox={r.booking_id for r in db.query(PublicPaymentAccount).filter(PublicPaymentAccount.booking_id.in_(accounts)).all()} if accounts else set()
    balances={}
    for bid in booking_ids:
        if bid not in accounts:balances[bid]={'state':'not tracked','balance':None,'currency':None,'sandbox':False};continue
        summary=totals(grouped[bid]);balances[bid]={'state':summary['state'],'balance':str(summary['balance'].quantize(Decimal('.01'))),'currency':accounts[bid].currency,'sandbox':bid in sandbox}
    tasks=db.query(WorkOrder).filter(WorkOrder.property_id.in_(ids),WorkOrder.due_date>=start,WorkOrder.due_date<end,WorkOrder.status.in_(['open','in_progress'])).order_by(WorkOrder.id).limit(TASK_LIMIT+1).all()
    if len(tasks)>TASK_LIMIT:raise ValueError('Too many tasks for calendar context; narrow the selection or hide context.')
    task_days={}
    for task in tasks:task_days.setdefault((task.property_id,task.due_date),[]).append({'id':task.id,'title':task.title,'status':task.status,'priority':task.priority})
    feeds={pid:[] for pid in ids}
    feed_rows=db.query(IcalFeed).filter(IcalFeed.property_id.in_(ids)).order_by(IcalFeed.id).limit(FEED_LIMIT+1).all()
    if len(feed_rows)>FEED_LIMIT:raise ValueError('Too many calendar feeds; narrow the selection or hide context.')
    for feed in feed_rows:
        feeds[feed.property_id].append({'platform':feed.platform.value,'active':feed.active,'needs_review':bool(feed.last_sync_error),'last_synced_at':feed.last_synced_at.isoformat()+'Z' if feed.last_synced_at else None})
    for row in data['rows']:
        prop=props[row['property_id']];matching=[r for r in rules if r.property_id in (None,prop.id)]
        try:today=datetime.now(ZoneInfo(prop.time_zone)).date()
        except (ZoneInfoNotFoundError,ValueError,TypeError):today=None
        row['feeds']=feeds[prop.id]
        for cell in row['days']:
            state='timezone needs review' if today is None else 'not configured'
            value=None
            if today:
                try:
                    value=calculate_price(prop,date.fromisoformat(cell['date']),today,overrides.get((prop.id,cell['date'])),matching,limits.get(prop.id))
                    if value is not None and (not value.is_finite() or value<0):raise ArithmeticError()
                    state='not configured' if value is None else 'local suggestion'
                except (ArithmeticError,TypeError):
                    value=None;state='pricing needs review'
            cell['suggested_rate']=str(value) if value is not None else None
            cell['rate_state']=state
            cell['accounts']=[{'booking_id':bid,**balances[bid]} for bid in sorted(set(cell['booking_ids']+cell['arrivals']+cell['departures']))]
            cell['tasks']=task_days.get((prop.id,cell['date']),[])
    data['context_enabled']=True
    return data
