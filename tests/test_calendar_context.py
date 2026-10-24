from datetime import date,datetime
from decimal import Decimal
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from app.db import get_db
from app.models import (InventoryDay,PriceRule,PriceRuleType,PricingLimit,BookingAccount,BookingLedgerEntry,WorkOrder,
    IcalFeed,PlatformName,PublicPaymentAccount)
from app.operations.calendar import calendar_data
from app.operations.calendar_context import add_context
from app.operations.router import router,require_admin
from app.pricing import suggested_price
from test_operations import db,prop,booking


def context(db,p):return add_context(db,calendar_data(db,date(2026,10,21),date(2026,10,24),p.id))


def test_rates_share_quote_arithmetic_and_stay_bounded_queries(db):
    p=prop(db,base_nightly_rate=Decimal('100'),pricing_mode='dynamic')
    db.add_all([PriceRule(name='Synthetic',property_id=None,rule_type=PriceRuleType.date_range,start_date=datetime(2026,10,1),end_date=datetime(2026,10,31),multiplier=Decimal('1.5'),active=True),
        PricingLimit(property_id=p.id,minimum_rate=Decimal('80'),maximum_rate=Decimal('130')),
        InventoryDay(property_id=p.id,night='2026-10-22',nightly_rate=Decimal('70'))]);db.commit()
    cells=context(db,p)['rows'][0]['days']
    assert [x['suggested_rate'] for x in cells]==['130.00','80.00','130.00']
    assert all(Decimal(x['suggested_rate'])==suggested_price(db,p,date.fromisoformat(x['date'])) for x in cells)
    data=calendar_data(db,date(2026,10,1),date(2026,11,30),p.id);queries=[]
    def count(*a):queries.append(1)
    event.listen(db.get_bind(),'before_cursor_execute',count)
    try:add_context(db,data)
    finally:event.remove(db.get_bind(),'before_cursor_execute',count)
    assert len(queries)<=8  # one bounded prefetch per source, not per cell


def test_balances_keep_currency_voids_unknowns_and_departures(db):
    p=prop(db);a=booking(db,p);b=booking(db,p);unknown=booking(db,p)
    db.add_all([BookingAccount(booking_id=a.id,currency='AUD'),BookingAccount(booking_id=b.id,currency='USD')]);db.flush()
    charge=BookingLedgerEntry(booking_id=a.id,kind='charge',amount=100,description='Synthetic',occurred_on='2026-10-21',actor='test')
    paid=BookingLedgerEntry(booking_id=a.id,kind='payment',amount=40,description='Synthetic',occurred_on='2026-10-21',actor='test')
    db.add_all([charge,paid]);db.flush();db.add(BookingLedgerEntry(booking_id=a.id,kind='void',amount=0,description='Synthetic correction',occurred_on='2026-10-21',actor='test',voids_id=paid.id));db.commit()
    cells=context(db,p)['rows'][0]['days'];values={x['booking_id']:x for x in cells[0]['accounts']}
    assert values[a.id]['balance']=='100.00' and values[a.id]['currency']=='AUD' and values[a.id]['state']=='unpaid'
    assert values[b.id]['balance']=='0.00' and values[b.id]['state']=='no balance due' and values[b.id]['currency']=='USD'
    assert values[unknown.id]['balance'] is None and values[unknown.id]['state']=='not tracked'
    assert cells[2]['booking_ids']==[] and {x['booking_id'] for x in cells[2]['accounts']}=={a.id,b.id,unknown.id}


def test_tasks_feeds_and_secrets_are_scoped_and_readonly(db):
    p=prop(db);q=prop(db,name='Other');booking(db,p,door_code='SECRET_DOOR')
    db.add_all([WorkOrder(property_id=p.id,title='<script>Task</script>',due_date='2026-10-21',status='open'),
        WorkOrder(property_id=p.id,title='Done task',due_date='2026-10-21',status='done'),
        WorkOrder(property_id=q.id,title='OTHER_PRIVATE_TASK',due_date='2026-10-21',status='open'),
        IcalFeed(property_id=p.id,platform=PlatformName.airbnb,url='https://private.invalid/SECRET_TOKEN',last_sync_error='SECRET_ERROR',active=True)])
    db.commit();data=context(db,p);row=data['rows'][0]
    assert len(row['days'][0]['tasks'])==1 and row['days'][1]['tasks']==[]
    assert row['feeds']==[{'platform':'airbnb','active':True,'needs_review':True,'last_synced_at':None}]
    assert 'SECRET' not in str(data) and 'OTHER_PRIVATE' not in str(data)
    assert not db.new and not db.deleted and not db.dirty


def test_invalid_timezone_hides_rate_and_no_base_is_unknown(db):
    p=prop(db,base_nightly_rate=100);p.time_zone='Unknown/Zone';db.commit()
    cell=context(db,p)['rows'][0]['days'][0];assert cell['suggested_rate'] is None and cell['rate_state']=='timezone needs review'
    p.time_zone='Australia/Brisbane';p.base_nightly_rate=None;db.commit()
    assert context(db,p)['rows'][0]['days'][0]['rate_state']=='not configured'


def test_context_is_opt_in_admin_only_and_links_escape(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-calendar-context-'+32*'x')
    p=prop(db,base_nightly_rate=100);booking(db,p)
    db.add(WorkOrder(property_id=p.id,title='<script>Task</script>',due_date='2026-10-21',status='open'));db.commit()
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    url=f'/operations/calendar?start=2026-10-21&end=2026-10-24&property_id={p.id}'
    with TestClient(app) as c:
        assert c.get(url+'&include_context=1').status_code==403
        app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
        default=c.get(url);assert 'Suggested AUD rate 100.00' not in default.text
        response=c.get(url+'&include_context=1');assert response.status_code==200 and 'Suggested AUD rate 100.00' in response.text
        assert '&lt;script&gt;Task&lt;/script&gt;' in response.text and '<script>Task' not in response.text
        assert 'include_context=1' in response.text and response.headers['cache-control']=='no-store'
        detailed=c.get(url+'&include_context=1&view=list');assert detailed.status_code==200 and 'Rates, accounts and tasks' in detailed.text
        api=c.get(url.replace('/operations/calendar','/api/ops/calendar')+'&include_context=1');assert api.status_code==200 and api.json()['context_enabled'] is True and api.headers['cache-control']=='no-store'


def test_large_context_falls_back_to_inventory_with_explicit_warning(db,monkeypatch):
    monkeypatch.setattr('app.operations.calendar_context.TASK_LIMIT',1)
    p=prop(db);booking(db,p)
    db.add_all([WorkOrder(property_id=p.id,title='Synthetic '+str(i),due_date='2026-10-21',status='open') for i in range(2)]);db.commit()
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db;app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with TestClient(app) as c:
        url=f'/operations/calendar?start=2026-10-21&end=2026-10-24&property_id={p.id}&include_context=1'
        page=c.get(url);assert page.status_code==200 and 'Inventory remains visible' in page.text
        data=c.get(url.replace('/operations/calendar','/api/ops/calendar')).json()
        assert data['context_enabled'] is False and 'Too many tasks' in data['context_error']
        assert data['rows'][0]['days'][0]['booking_ids']


def test_invalid_legacy_negative_price_is_withheld_without_losing_inventory(db):
    p=prop(db,base_nightly_rate=Decimal('-1'))
    row=context(db,p)['rows'][0];assert row['days'][0]['suggested_rate'] is None
    assert row['days'][0]['rate_state']=='pricing needs review' and row['days'][0]['available_units']==1
