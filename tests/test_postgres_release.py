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
 for path in ['/healthz','/','/transactions','/cleaning','/operations','/operations/calendar','/operations/tasks','/operations/readiness']:
  response=c.get(path)
  assert response.status_code==200,(path,response.status_code)
  assert 'no-store' in response.headers['Cache-Control']
 assert c.get('/operations/readiness').json()['database']=='ready'
'''
    result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=35)
    assert result.returncode==0,result.stderr[-2000:]
