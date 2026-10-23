from concurrent.futures import ThreadPoolExecutor
from datetime import date,datetime
from decimal import Decimal
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from icalendar import Calendar
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import Booking,BookingStatus,OpsCommand,StockBinding,StockGuard,StockResource
from app.operations.stock import available_units,config_token,configure_stock,refresh_conflicts
from app.operations.stock_router import router
from app.operations.reservations import save_reservation
from app.operations.tasks import TaskError
from app.operations.calendar import calendar_data
from app.public_api import get_calendar,get_quote
from app.sync.ical_export import build_resource_ics
from app.sync import ical_sync
from test_operations import db,prop,booking,feed,event
from test_hostaway_parity import reservation_args


START=datetime(2026,11,10)
END=datetime(2026,11,12)


@pytest.fixture
def actor(monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-stock-secret-'+'x'*32)
    monkeypatch.setenv('ADMIN_USER','stock-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    return 'stock-admin'


def configure(db,actor,**changes):
    args=dict(actor=actor,token=config_token(db,actor),note='Verified physical resource inventory mapping')
    args.update(changes);configure_stock(db,**args)


def resource(db,actor,capacity=1):
    configure(db,actor,action='create',name='Synthetic stock',capacity=capacity,time_zone='Australia/Brisbane')
    return db.query(StockResource).order_by(StockResource.id.desc()).first()


def bind(db,actor,p,r,quantity=1):
    configure(db,actor,action='bind',property_id=p.id,resource_id=r.id,quantity=quantity)


def create(db,actor,p,**changes):
    args=reservation_args(db,actor,p,check_in='2026-11-10',check_out='2026-11-12')
    args.update(changes);return save_reservation(db,**args)


def cancel(db,actor,p,entity_id):
    row=db.get(Booking,entity_id)
    return save_reservation(db,**reservation_args(db,actor,p,row,action='cancel'))


def test_parent_names_do_not_implicitly_share_stock(db,actor):
    parent=prop(db,'Building');child=prop(db,'Room',parent_property_id=parent.id)
    booking(db,parent,check_in=START,check_out=END)
    assert available_units(db,child.id,START,END)==1


def test_entire_home_and_split_rooms_consume_explicit_shared_resources(db,actor):
    home=prop(db,'Entire home');a=prop(db,'Room A');b=prop(db,'Room B')
    ra,rb=resource(db,actor),resource(db,actor)
    for p,r in [(home,ra),(home,rb),(a,ra),(b,rb)]:bind(db,actor,p,r)
    first=create(db,actor,a)
    assert available_units(db,home.id,START,END)==0 and available_units(db,b.id,START,END)==1
    with pytest.raises(TaskError):create(db,actor,home)
    second=create(db,actor,b)
    cancel(db,actor,a,first)
    assert available_units(db,home.id,START,END)==0
    cancel(db,actor,b,second)
    assert available_units(db,home.id,START,END)==1
    whole=create(db,actor,home)
    assert available_units(db,a.id,START,END)==available_units(db,b.id,START,END)==0
    assert not db.get(Booking,whole).has_conflict


def test_pooled_capacity_allows_multiple_reservations_and_exports_only_full_dates(db,actor):
    p=prop(db,published=True,base_nightly_rate=200)
    r=resource(db,actor,capacity=3);bind(db,actor,p,r)
    first,second=create(db,actor,p),create(db,actor,p)
    assert available_units(db,p.id,START,END)==1
    days=get_calendar(property_id=p.id,start='2026-11-10',end='2026-11-12',db=db)['result']['days']
    assert all(d['status']=='available' and d['available_units']==1 for d in days)
    assert not Calendar.from_ical(build_resource_ics(db,p.id,start=date(2026,11,1))).walk('VEVENT')
    create(db,actor,p)
    with pytest.raises(TaskError):create(db,actor,p)
    assert not any(b.has_conflict for b in db.query(Booking))
    raw=build_resource_ics(db,p.id,start=date(2026,11,1));events=Calendar.from_ical(raw).walk('VEVENT')
    assert len(events)==1 and events[0]['dtstart'].dt==date(2026,11,10) and events[0]['dtend'].dt==date(2026,11,12)
    assert b'Synthetic guest' not in raw and b'guest@example.invalid' not in raw
    quote=get_quote(property_id=p.id,check_in='2026-11-10',check_out='2026-11-12',guests=1,db=db)['result']
    assert not quote['bookable']
    cancel(db,actor,p,second)
    assert available_units(db,p.id,START,END)==1


def test_capacity_uses_peak_nights_instead_of_summing_separate_stays(db,actor):
    p=prop(db);r=resource(db,actor,capacity=2);bind(db,actor,p,r)
    booking(db,p,check_in=START,check_out=datetime(2026,11,11,10))
    booking(db,p,check_in=datetime(2026,11,11,14),check_out=END)
    assert available_units(db,p.id,START,END)==1
    create(db,actor,p)
    assert available_units(db,p.id,START,END)==0


def test_departure_morning_does_not_block_arriving_night(db,actor):
    p=prop(db);r=resource(db,actor);bind(db,actor,p,r)
    booking(db,p,check_in=datetime(2026,11,8,14),check_out=datetime(2026,11,10,10))
    assert available_units(db,p.id,START,END)==1
    create(db,actor,p)


def test_blocks_close_full_pool_and_source_overbookings_remain_visible(db,actor,monkeypatch):
    p=prop(db);q=prop(db,'Linked alias');r=resource(db,actor,capacity=2)
    bind(db,actor,p,r);bind(db,actor,q,r)
    block=booking(db,q,is_block=True,check_in=START,check_out=END)
    assert available_units(db,p.id,START,END)==0
    f=feed(db,p)
    monkeypatch.setattr(ical_sync,'fetch_and_parse',lambda _:[event('source',check_in=START,check_out=END)])
    assert ical_sync.sync_feed(db,f)['added']==1
    imported=db.query(Booking).filter_by(uid='source').one()
    assert imported.has_conflict and block.has_conflict
    block.status=BookingStatus.cancelled;db.commit();ical_sync.recompute_conflicts(db)
    assert not imported.has_conflict and available_units(db,p.id,START,END)==1


def test_shared_conflicts_are_scoped_to_days_not_entire_stays(db,actor):
    p=prop(db);r=resource(db,actor,capacity=2);bind(db,actor,p,r)
    booking(db,p,check_in=START,check_out=END)
    booking(db,p,check_in=START,check_out=END)
    third=booking(db,p,check_in=datetime(2026,11,11),check_out=END)
    refresh_conflicts(db);db.commit()
    row=calendar_data(db,date(2026,11,10),date(2026,11,13))['rows'][0]
    assert row['inventory_capacity']==2 and row['reserved_night_pct'] is None
    assert row['days'][0]['conflict_ids']==[] and third.id in row['days'][1]['conflict_ids']
    assert row['conflict_days']==1


def test_parallel_linked_property_reservations_only_one_claims_last_resource(db,actor):
    p,q=prop(db),prop(db,'Other alias');r=resource(db,actor)
    bind(db,actor,p,r);bind(db,actor,q,r)
    args=[reservation_args(db,actor,s,check_in='2026-11-10',check_out='2026-11-12') for s in (p,q)]
    factory=sessionmaker(bind=db.bind,autoflush=False)
    def run(values):
        with factory() as session:
            try:save_reservation(session,**values);return 'saved'
            except TaskError:return 'held'
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(run,args))==['held','saved']
    assert db.query(Booking).count()==1


def test_stock_config_stale_invalid_timezone_and_audit_are_atomic(db,actor):
    p=prop(db);old=config_token(db,actor)
    r=resource(db,actor)
    with pytest.raises(TaskError):configure(db,actor,token=old,action='bind',property_id=p.id,resource_id=r.id)
    for changes in [dict(quantity=0),dict(quantity=2),dict(property_id=999)]:
        values=dict(action='bind',property_id=p.id,resource_id=r.id);values.update(changes)
        with pytest.raises(TaskError):configure(db,actor,**values)
    assert db.query(StockBinding).count()==0
    configure(db,actor,action='timezone',property_id=p.id,time_zone='UTC')
    with pytest.raises(TaskError):bind(db,actor,p,r)
    booking(db,p)
    with pytest.raises(TaskError):configure(db,actor,action='timezone',property_id=p.id,time_zone='Australia/Brisbane')


def test_resources_route_permissions_and_template_escaping(db,actor):
    p=prop(db);app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get('/operations/resources').status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie(actor))
        assert client.get('/operations/resources').status_code==200
        values=dict(token=config_token(db,actor),action='create',name='<script>unit</script>',capacity=1,time_zone='Australia/Brisbane',note='Create synthetic physical inventory unit')
        assert client.post('/operations/resources',data=values,headers={'origin':'https://elsewhere.invalid'}).status_code==403
        result=client.post('/operations/resources',data=values)
        assert result.status_code==200 and '&lt;script&gt;unit&lt;/script&gt;' in result.text
