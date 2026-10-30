"""Actual original-route acceptance checks on a NEW local isolated database.

Accepts a private local source JSON path; never embeds those records in code.
"""
import argparse
from datetime import date
from html.parser import HTMLParser
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.run_internal_module_trial import prepare

class Forms(HTMLParser):
    def __init__(self,html):
        super().__init__();self.forms=[];self.current=None;self.feed(html)
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='form':self.current={'action':a.get('action',''),'data':{}};self.forms.append(self.current)
        if tag=='input' and self.current is not None and a.get('name'):self.current['data'][a['name']]=a.get('value','')
    def handle_endtag(self,tag):
        if tag=='form':self.current=None


def verify(source,root):
    import os
    pg = os.environ.get('OPS_TRIAL_PG_LOCAL_CHECK') == '1'
    if pg:
        Path(root).mkdir(parents=True,exist_ok=False)
        from tools.run_internal_module_trial import prepare_postgres
        app,engine,added=prepare_postgres(source)
    else:
        app,engine,added=prepare(root,source)
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from sqlalchemy import inspect
    from app.db import SessionLocal
    from app.models import (Property,Booking,CleaningTask,Cleaner,WorkOrder,BookingAccount,BookingLedgerEntry,Guest,IcalFeed,MessageLog,BookingStatus,CleaningStatus)
    from app.internal_module_trial import import_source_properties
    from app.operations.billing import totals
    assert not __import__('app.main',fromlist=['scheduler']).scheduler.running
    checks=[]
    def ok(name):checks.append({'feature':name,'status':'passed'})
    def form(client,path,action):
        page=client.get(path);assert page.status_code==200,(path,page.status_code,page.text[:150])
        return next(f['data'] for f in Forms(page.text).forms if f['action']==action)
    def submit(client,path,data,status=303):
        r=client.post(path,data=data,follow_redirects=False)
        assert r.status_code==status,(path,r.status_code,r.text[:300])
        return r
    rows=json.loads(Path(source).read_text());n=len(rows)
    assert len(added)==n
    assert import_source_properties(engine,Path(source).read_bytes())==[]
    ok('Verified source identities imported idempotently into ORIGINAL properties table')
    with TestClient(app) as c:
        assert c.get('/operations/reservations',follow_redirects=False).status_code==303
        submit(c,'/login',{'username':os.environ['ADMIN_USER'],'password':os.environ['ADMIN_PASSWORD'],'next':'/'})
        page=c.get('/properties');assert page.status_code==200 and all(r['title'] in page.text for r in rows)
        assert '独立数据区' in page.text
        setup=c.get('/operations/module-trial/setup');assert setup.status_code==200
        upload=next(f for f in Forms(setup.text).forms if f['action']=='/operations/module-trial/sources')
        repeated=c.post('/operations/module-trial/sources',data={'token':upload['data']['token']},files={'file':('source.json',Path(source).read_bytes(),'application/json')},follow_redirects=False)
        assert repeated.status_code==303
        assert c.post('/operations/module-trial/sources',headers={'origin':'https://synthetic-other.invalid'},data={'token':upload['data']['token']},files={'file':('source.json',Path(source).read_bytes(),'application/json')}).status_code==403
        ok('Authenticated source intake reuses original properties, deduplicates and rejects cross-origin submission')
        ok('Original /properties lists every supplied source title with internal-test banner')
        with SessionLocal() as db:
            props=db.query(Property).order_by(Property.id).all();pids=[p.id for p in props]
            assert len(props)==n and all(p.address is None and not p.published for p in props)
        bids=[]
        for index,pid in enumerate(pids):
            data=form(c,'/operations/reservations','/operations/reservations/0')
            data.update(property_id=pid,check_in='2026-11-01',check_out='2026-11-03',amount='200.00',
                        guest_name='SYNTHETIC TEST GUEST '+str(index+1),guest_email='',note='Synthetic original module functional test',action='save')
            r=submit(c,'/operations/reservations/0',data);bids.append(int(r.headers['location'].rsplit('/',1)[1]))
        ok('Original reservation POST creates real Booking and Guest models for every source property')
        data=form(c,'/operations/reservations','/operations/reservations/0');data.update(property_id=pids[0],check_in='2026-11-02',check_out='2026-11-04',amount='200',guest_name='SYNTHETIC CONFLICT',guest_email='',note='Synthetic overlap must be rejected',action='save')
        submit(c,'/operations/reservations/0',data,409);ok('Original inventory conflict rejects overlapping booking')
        calendar=c.get('/operations/calendar?start=2026-11-01&end=2026-11-07');assert calendar.status_code==200 and all(r['title'] in calendar.text for r in rows)
        assert all(f'/operations/reservations/{bid}' in calendar.text for bid in bids)
        checks.append({'feature':'Guest names directly on original calendar grid','status':'not satisfied','reason':'Existing grid renders reservation IDs, not guest names; calendar redesign remains paused'})
        assert all(r['title'] in c.get('/').text for r in rows)
        ok('Original calendar and Bookings homepage read and display created Booking records')
        with SessionLocal() as db:
            assert db.query(CleaningTask).count()==n
            cleaner=Cleaner(name='SYNTHETIC TEST CLEANER',active=True);db.add(cleaner);db.commit();cid=cleaner.id
            cleaning_ids=[t.id for t in db.query(CleaningTask).all()]
        for tid in cleaning_ids:
            submit(c,f'/cleaning/{tid}/assign',{'cleaner_id':cid,'assignee':'SYNTHETIC TEST CLEANER'})
            submit(c,f'/cleaning/{tid}/done',{})
        assert c.get('/cleaning').status_code==200
        ok('Original reservation-to-CleaningTask creation, cleaner assignment and completion')
        for pid in pids:
            data=form(c,'/operations/tasks','/operations/tasks/0');data.update(property_id=pid,title='SYNTHETIC TEST maintenance',description='Synthetic only',category='maintenance',priority='normal',status='open',assignee_id='',due_date='2026-11-03',note='Synthetic task acceptance check')
            r=submit(c,'/operations/tasks/0',data);url=r.headers['location']
            for status in ('in_progress','done'):
                data=form(c,url,url);data.update(property_id=pid,title='SYNTHETIC TEST maintenance',description='Synthetic only',category='maintenance',priority='normal',status=status,assignee_id='',due_date='2026-11-03',note='Synthetic task state transition');submit(c,url,data)
        ok('Original WorkOrder UI create -> in_progress -> done for every property')
        bid=bids[0];url=f'/operations/accounts/{bid}'
        data=form(c,url,url+'/create');data.update(currency='AUD',opening_amount='200.00',note='Synthetic ledger; no real money');submit(c,url+'/create',data)
        data=form(c,url,url+'/entries');data.update(kind='payment',amount='200.00',description='SYNTHETIC TEST manual payment record',occurred_on=date.today().isoformat(),reference='synthetic-local-manual-1',payment_id='',note='Synthetic ledger no funds movement');submit(c,url+'/entries',data)
        with SessionLocal() as db:
            ledger=db.query(BookingLedgerEntry).filter_by(booking_id=bid).all();assert totals(ledger)['balance']==0
            payment=next(r for r in ledger if r.kind=='payment');payment_id=payment.id
        assert c.get(url+f'/receipts/{payment_id}').status_code==200
        data=form(c,url,url+'/statements');data.update(note='Synthetic statement acceptance no external send');r=submit(c,url+'/statements',data);assert c.get(r.headers['location']).status_code==200
        ok('Original BookingAccount charge/payment totals, receipt and statement rendering')
        # Every external public surface and manual sync is blocked, even while authenticated.
        for path in ('/api/public/properties','/api/public/listings','/owner/fake','/cleaner/fake','/guest/fake','/ical/1/fake.ics','/operations/internal-test','/operations/integrations'):
            assert c.get(path).status_code==403,path
        for path in ('/sync',f'/properties/{pids[0]}/listing',f'/properties/{pids[0]}/feeds','/api/public/checkout','/api/public/stripe-webhook'):
            assert c.post(path,data={}).status_code==403,path
        ok('Guard blocks public APIs, portals, feeds, publication, sync and external checkout/webhook routes')
    import socket,subprocess,os
    for attempt in (lambda:socket.create_connection(('127.0.0.1',9)),lambda:socket.getaddrinfo('example.invalid',443),lambda:subprocess.run(['/usr/bin/true'])):
        try:attempt()
        except PermissionError:pass
        else:raise AssertionError('Egress/child process guard did not fail closed')
    ok('Process audit guard denies connections, DNS and child processes independently of env switches')
    import sqlite3
    try: sqlite3.connect(str(Path(root)/'wrong-database.sqlite'))
    except PermissionError: pass
    else: raise AssertionError('Other database connection was allowed')
    assert not (Path(root)/'wrong-database.sqlite').exists()
    ok('SQLite audit guard rejects every database other than the dedicated trial file')
    with SessionLocal() as db:
        p=db.get(Property,pids[0]);p.published=True
        try:db.commit()
        except ValueError:db.rollback()
        else:raise AssertionError('ORM publication guard failed')
        b=db.get(Booking,bids[0]);b.notifications_enabled=True
        try:db.commit()
        except ValueError:db.rollback()
        else:raise AssertionError('Notification guard failed')
        assert db.query(Booking).count()==n and db.query(CleaningTask).filter_by(status=CleaningStatus.done).count()==n
        assert db.query(WorkOrder).filter_by(status='done').count()==n
        assert db.query(IcalFeed).count()==db.query(MessageLog).count()==0
        assert all(not g.email for g in db.query(Guest).all())
        counts={'properties':db.query(Property).count(),'bookings':db.query(Booking).count(),'cleaning_tasks_done':db.query(CleaningTask).filter_by(status=CleaningStatus.done).count(),'work_orders_done':db.query(WorkOrder).filter_by(status='done').count(),'booking_accounts':db.query(BookingAccount).count(),'ledger_entries':db.query(BookingLedgerEntry).count(),'message_logs':db.query(MessageLog).count(),'ical_feeds':db.query(IcalFeed).count(),'published_properties':db.query(Property).filter_by(published=True).count()}
    ok('ORM guards reject publication/notification writes; no contact emails, feeds or outgoing message records')
    checks += [{'feature':'Real OTA reservations/availability or physical-unit reconciliation','status':'blocked','reason':'Source dataset contains basic invitation-derived listings only'}, {'feature':'Real payment, external delivery, cleaner dispatch or channel writeback','status':'intentionally disabled','reason':'No external effects authorized'}]
    result={'scope':'LOCAL original OPS routes/models, dedicated PostgreSQL role/schema; not production acceptance' if pg else 'LOCAL original OPS routes/models, independent SQLite DB; not production acceptance', 'source_count':n,'counts':counts,'matrix':checks,'production_modified':False,'database':'dedicated local PostgreSQL schema' if pg else str(Path(root)/'ops-module-trial.sqlite')}
    (Path(root)/'verification.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2));return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--root',required=True);a=p.parse_args();verify(a.source,a.root)
