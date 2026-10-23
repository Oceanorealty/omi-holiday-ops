"""Disposable CI PostgreSQL only. Reject all production/remote database URLs."""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.db import Base
from app import migrate
from app.deployment import ensure_release_snapshot
from app.models import Booking, Property, WorkOrder, WorkOrderCheck, WorkOrderEvent
from app.operations.tasks import TaskError, form_token, save_task, change_checklist


@pytest.fixture
def pg(monkeypatch):
    url=os.environ.get('OPS_TEST_POSTGRES_URL')
    if not url: pytest.skip('Isolated PostgreSQL URL not configured')
    parsed=make_url(url)
    if parsed.host not in ('127.0.0.1','localhost') or parsed.database!='ops_release_test':
        pytest.fail('Refusing any database except local disposable ops_release_test')
    schema='test_'+uuid.uuid4().hex[:10]
    admin=create_engine(url)
    with admin.begin() as c:c.execute(text(f'CREATE SCHEMA {schema}'))
    engine=create_engine(url,connect_args={'options':f'-csearch_path={schema}'})
    monkeypatch.setenv('SECRET_KEY','synthetic-pg-release-secret-'+'x'*32)
    yield engine,schema,url
    engine.dispose()
    with admin.begin() as c:c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
    admin.dispose()


def base_schema(engine):
    path=Path(__file__).parent/'fixtures/production_models_bcedbba.py'
    spec=importlib.util.spec_from_file_location('release_baseline_'+uuid.uuid4().hex,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.Base.metadata.create_all(engine)
    with Session(engine) as s:
        p=module.Property(name='Synthetic migration property');s.add(p);s.flush()
        s.add(module.Booking(property_id=p.id,platform=module.PlatformName.direct,uid='synthetic-existing',
            check_in=datetime(2026,10,21),check_out=datetime(2026,10,23),status=module.BookingStatus.confirmed,amount=0));s.commit()


def test_postgres_snapshot_and_twice_applied_upgrade_preserve_rows(pg):
    engine,schema,_=pg;base_schema(engine)
    first=ensure_release_snapshot(engine);second=ensure_release_snapshot(engine)
    assert first['state']=='saved' and second['state']=='already_saved'
    for _ in range(2):migrate.run(engine);Base.metadata.create_all(engine)
    with Session(engine) as s:
        booking=s.query(Booking).one()
        assert booking.uid=='synthetic-existing' and booking.amount==0 and not booking.notifications_enabled
        assert s.query(Property).one().time_zone=='UTC'
        assert s.query(WorkOrder).count()==0
    with engine.connect() as c:
        snapshot=c.execute(text(f'SELECT uid FROM ops_backup_20261021.{schema}__bookings')).scalar_one()
        assert snapshot=='synthetic-existing'
    assert 'work_order_checks' in inspect(engine).get_table_names()


def args(actor, prop_id, row=None):
    return dict(actor=actor,entity_id=row.id if row else 0,token=form_token(actor,row),property_id=prop_id,
        title='Synthetic PG task',description='Disposable test record',category='maintenance',priority='normal',
        status='open',assignee_id=None,due_date='2026-10-22',note='Verified with isolated PostgreSQL data')


def test_postgres_checklist_and_competing_versions(pg):
    engine,_,_=pg;Base.metadata.create_all(engine)
    with Session(engine) as s:
        p=Property(name='Synthetic concurrency property');s.add(p);s.commit();prop_id=p.id
        task_id=save_task(s,**args('reviewer',prop_id));row=s.get(WorkOrder,task_id)
        change_checklist(s,actor='reviewer',entity_id=row.id,token=form_token('reviewer',row,purpose='checklist'),
            action='add',label='Check water flow',required=True,note='Required for a verified repair')
        item=s.query(WorkOrderCheck).one();item_id=item.id
        a=args('reviewer',prop_id,row);a['status']='in_progress';save_task(s,**a)
        a=args('reviewer',prop_id,row);a['status']='done'
        with pytest.raises(TaskError):save_task(s,**a)
        change_checklist(s,actor='reviewer',entity_id=row.id,token=form_token('reviewer',row,purpose='checklist'),
            action='check',item_id=item_id,note='Water flow tested and confirmed clear')
        a=args('reviewer',prop_id,row);a['status']='done';save_task(s,**a)
        assert row.completed_at and s.query(WorkOrderEvent).count()==5
        a=args('reviewer',prop_id,row);a['status']='open';save_task(s,**a)
        assert not s.get(WorkOrderCheck,item_id).completed
    barrier=threading.Barrier(2)
    def competing(title):
        with Session(engine) as s:
            row=s.get(WorkOrder,task_id);a=args('reviewer',prop_id,row);a['title']=title
            barrier.wait(timeout=5)
            try:save_task(s,**a);return 'saved'
            except TaskError:return 'stale'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(competing,['First change','Second change']))==['saved','stale']
    with Session(engine) as s:
        assert s.query(WorkOrder).count()==1 and s.query(WorkOrderEvent).count()==7


def test_postgres_full_app_startup_and_database_readiness(pg):
    engine,schema,url=pg;base_schema(engine)
    parsed=make_url(url).update_query_dict({'options':f'-csearch_path={schema}'})
    env={'PATH':os.environ.get('PATH',''),'DATABASE_URL':parsed.render_as_string(hide_password=False),
         'OPS_SCHEDULER_ENABLED':'0','OPS_MESSAGE_DISPATCH_ENABLED':'0','OPS_OWNER_EMAIL_ENABLED':'0',
         'ADMIN_USER':'release-admin','ADMIN_PASSWORD':'synthetic-only','SECRET_KEY':'synthetic-app-pg-key-'+'x'*32}
    script='''
from fastapi.testclient import TestClient
from app.main import app
from app.auth import SESSION_COOKIE,make_session_cookie
with TestClient(app) as c:
 c.cookies.set(SESSION_COOKIE,make_session_cookie('release-admin'))
 for path in ['/healthz','/','/transactions','/cleaning','/operations','/operations/calendar','/operations/tasks','/operations/readiness','/operations/setup','/operations/reservations','/operations/inventory','/operations/reports','/operations/channels','/operations/task-plans','/operations/guest-checkins','/operations/reservation-requests','/operations/resources','/operations/inbox','/operations/agreements','/operations/accounts']:
  response=c.get(path)
  assert response.status_code==200,(path,response.status_code)
  assert 'no-store' in response.headers['Cache-Control']
 assert c.get('/operations/readiness').json()['database']=='ready'
'''
    result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=35)
    assert result.returncode==0,result.stderr[-2000:]


def test_postgres_new_release_snapshot_and_reservation_serialization(pg):
    from app.operations.reservations import reservation_token, save_reservation
    engine, schema, _ = pg
    base_schema(engine)
    ensure_release_snapshot(engine)
    current = ensure_release_snapshot(engine, snapshot_schema='ops_backup_20261023')
    assert current['state'] == 'saved'
    assert ensure_release_snapshot(engine, snapshot_schema='ops_backup_20261023')['state'] == 'already_saved'
    migrate.run(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        property_id = s.query(Property).one().id
    barrier = threading.Barrier(2)
    def competing(_):
        with Session(engine) as s:
            token = reservation_token('reviewer')
            barrier.wait(timeout=5)
            try:
                save_reservation(s, actor='reviewer', entity_id=0, token=token, property_id=property_id,
                    check_in='2026-11-10', check_out='2026-11-12', amount='200',
                    note='Reviewed disposable PostgreSQL reservation')
                return 'saved'
            except TaskError:
                return 'held'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(competing, range(2))) == ['held', 'saved']
    with Session(engine) as s:
        assert s.query(Booking).count() == 2
    with engine.connect() as c:
        count = c.execute(text(f'SELECT count(*) FROM ops_backup_20261023.{schema}__bookings')).scalar_one()
        assert count == 1


def test_postgres_shared_inventory_and_recurring_plan_concurrency(pg,monkeypatch):
    from datetime import date,timedelta
    from app.models import ChecklistTemplate,ScheduledTaskOccurrence,StockResource
    from app.operations.stock import config_token,configure_stock
    from app.operations.reservations import reservation_token,save_reservation
    from app.operations import task_plans
    engine,_,_=pg;Base.metadata.create_all(engine)
    monkeypatch.setattr(task_plans,'today',lambda:date(2026,10,23))
    with Session(engine) as s:
        p=Property(name='Synthetic A',time_zone='Australia/Brisbane');q=Property(name='Synthetic B',time_zone='Australia/Brisbane')
        s.add_all([p,q]);s.commit();ids=[p.id,q.id]
        configure_stock(s,actor='reviewer',token=config_token(s,'reviewer'),action='create',name='One shared unit',capacity=1,time_zone='Australia/Brisbane',note='Confirmed shared physical room mapping')
        resource_id=s.query(StockResource).one().id
        for property_id in ids:
            configure_stock(s,actor='reviewer',token=config_token(s,'reviewer'),action='bind',property_id=property_id,resource_id=resource_id,note='Confirmed listing uses the shared room')
        template_id=task_plans.create_template(s,actor='reviewer',token=form_token('reviewer',purpose='template-create'),name='Inspection',lines='Check smoke detector',note='Create synthetic PostgreSQL inspection template')
        task_plans.create_schedule(s,actor='reviewer',token=form_token('reviewer',purpose='schedule-create'),property_id=ids[0],title='Inspect unit',description='',category='inspection',priority='normal',interval_days=7,start_date='2026-10-23',template_id=template_id,note='Create recurring PostgreSQL inspection schedule')
    barrier=threading.Barrier(2)
    def reserve(property_id):
        with Session(engine) as s:
            token=reservation_token('reviewer');barrier.wait(timeout=5)
            try:
                save_reservation(s,actor='reviewer',entity_id=0,token=token,property_id=property_id,check_in='2026-11-10',check_out='2026-11-12',amount='200',note='Book last shared room using synthetic data')
                return 'saved'
            except TaskError:return 'held'
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(reserve,ids))==['held','saved']
    def generate(_):
        with Session(engine) as s:return task_plans.materialize_schedules(s,date(2026,11,6))['generated']
    with ThreadPoolExecutor(max_workers=2) as pool:assert sum(pool.map(generate,range(2)))==3
    with Session(engine) as s:
        assert s.query(Booking).count()==1 and s.query(ScheduledTaskOccurrence).count()==3


def test_postgres_portal_concurrent_retry_creates_one_conversation_and_message(pg):
    from datetime import timedelta
    from app.models import PortalConversation,PortalMessage
    from app.operations.inbox import message_token,post_message
    engine,_,_=pg;Base.metadata.create_all(engine)
    with Session(engine) as s:
        p=Property(name='Synthetic inbox property');s.add(p);s.flush()
        start=datetime.utcnow()+timedelta(days=7)
        b=Booking(property_id=p.id,platform='direct',uid='synthetic-inbox',check_in=start,check_out=start+timedelta(days=2),status='confirmed')
        s.add(b);s.commit();booking_id=b.id;token=message_token('reviewer',b)
    barrier=threading.Barrier(2)
    def post(_):
        with Session(engine) as s:
            barrier.wait(timeout=5)
            return post_message(s,actor='reviewer',booking_id=booking_id,token=token,kind='reply',body='One synthetic portal reply')
    with ThreadPoolExecutor(max_workers=2) as pool:assert len(set(pool.map(post,range(2))))==1
    with Session(engine) as s:assert s.query(PortalConversation).count()==s.query(PortalMessage).count()==1


def test_postgres_agreement_acknowledgement_retry_is_atomic(pg):
    from datetime import timedelta
    from app.models import AgreementAcceptance,BookingAgreement
    from app.operations.agreements import (accept_agreement,acceptance_token,create_version,
        issue_agreement,issue_token,policy_token)
    engine,_,_=pg;Base.metadata.create_all(engine)
    with Session(engine) as s:
        p=Property(name='Synthetic agreement property');s.add(p);s.flush()
        start=datetime.utcnow()+timedelta(days=7)
        b=Booking(property_id=p.id,platform='direct',uid='synthetic-agreement',check_in=start,
            check_out=start+timedelta(days=2),status='confirmed',guest_portal_token='synthetic-pg-agreement-link')
        s.add(b);s.commit()
        create_version(s,actor='reviewer',property_id=p.id,token=policy_token(s,'reviewer',p.id),
            title='Synthetic demonstration',body='Synthetic non-binding document for a database test only.',note='Prepare synthetic PostgreSQL agreement')
        entity_id=issue_agreement(s,actor='reviewer',booking_id=b.id,token=issue_token(s,'reviewer',b),note='Issue synthetic PostgreSQL agreement')
        token=acceptance_token(b,s.get(BookingAgreement,entity_id))
    barrier=threading.Barrier(2)
    def acknowledge(_):
        with Session(engine) as s:
            barrier.wait(timeout=5)
            return accept_agreement(s,portal_token='synthetic-pg-agreement-link',agreement_id=entity_id,
                token=token,typed_name='Synthetic holder',acknowledged=True)
    with ThreadPoolExecutor(max_workers=2) as pool:assert len(set(pool.map(acknowledge,range(2))))==1
    with Session(engine) as s:assert s.query(AgreementAcceptance).count()==1


def test_postgres_global_payment_reference_and_refund_concurrency(pg):
    from datetime import timedelta
    from app.models import BookingAccount,BookingLedgerEntry
    from app.operations.billing import account_token,add_entry,create_account,create_token,entries,totals
    engine,_,_=pg;Base.metadata.create_all(engine)
    with Session(engine) as s:
        p=Property(name='Synthetic billing property');s.add(p);s.flush()
        start=datetime.utcnow()+timedelta(days=7)
        stays=[Booking(property_id=p.id,platform='direct',uid=f'synthetic-billing-{i}',check_in=start,
            check_out=start+timedelta(days=2),status='confirmed') for i in range(2)]
        s.add_all(stays);s.commit();ids=[b.id for b in stays]
        for b in stays:create_account(s,actor='reviewer',booking_id=b.id,token=create_token('reviewer',b),currency='AUD',opening_amount='300',note='Confirmed synthetic currency and opening amount')
    barrier=threading.Barrier(2)
    def payment(booking_id):
        with Session(engine) as s:
            token=account_token('reviewer',s.get(BookingAccount,booking_id));barrier.wait(timeout=5)
            try:
                entity_id=add_entry(s,actor='reviewer',booking_id=booking_id,token=token,kind='payment',amount='100',description='Synthetic payment',
                    occurred_on=datetime.utcnow().date().isoformat(),reference='globally-unique-synthetic-ref',note='Verified synthetic external receipt')
                return booking_id,entity_id
            except TaskError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(payment,ids))
    saved=[r for r in results if r];assert len(saved)==1
    booking_id,payment_id=saved[0];barrier=threading.Barrier(2)
    def refund(i):
        with Session(engine) as s:
            token=account_token('reviewer',s.get(BookingAccount,booking_id));barrier.wait(timeout=5)
            try:
                add_entry(s,actor='reviewer',booking_id=booking_id,token=token,kind='refund',amount='60',description='Synthetic refund',
                    occurred_on=datetime.utcnow().date().isoformat(),reference=f'synthetic-refund-{i}',payment_id=payment_id,note='Verified synthetic external refund')
                return 'saved'
            except TaskError:return 'held'
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(refund,range(2)))==['held','saved']
    with Session(engine) as s:
        assert s.query(BookingLedgerEntry).filter_by(kind='payment').count()==1
        assert totals(entries(s,booking_id))['refunds']==60
