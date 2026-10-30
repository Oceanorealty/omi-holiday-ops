from decimal import Decimal
import pytest
from sqlalchemy import create_engine,MetaData,Table,Column,Integer,String,Boolean,Numeric,ForeignKey,DateTime,select,func
from app.operations.property_cleanup import preview,remove,parse_ids,CleanupError


@pytest.fixture
def store():
    engine=create_engine('sqlite://')
    meta=MetaData()
    prop=Table('properties',meta,Column('id',Integer,primary_key=True),Column('name',String),Column('published',Boolean))
    statement=Table('owner_statements',meta,Column('id',Integer,primary_key=True),Column('property_id',ForeignKey('properties.id')),
        Column('period',String),Column('finalized',Boolean),Column('sent_at',DateTime),Column('missing_amount_count',Integer),
        *(Column(n,Numeric) for n in ('gross_revenue','total_expenses','commission_amount','adjustment_amount','net_payout')))
    deps=Table('future_property_dependency',meta,Column('id',Integer,primary_key=True),Column('property_id',ForeignKey('properties.id',ondelete='CASCADE')))
    delivery=Table('statement_deliveries',meta,Column('id',Integer,primary_key=True),Column('statement_id',ForeignKey('owner_statements.id')))
    raw=Table('unconstrained_property_reference',meta,Column('id',Integer,primary_key=True),Column('property_id',Integer))
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(prop.insert(),[{'id':101,'name':'Synthetic old A','published':False},{'id':102,'name':'Synthetic old B','published':False},{'id':201,'name':'Synthetic retained source','published':False}])
        conn.execute(statement.insert(),dict(id=501,property_id=102,period='2030-01',finalized=False,sent_at=None,missing_amount_count=0,
            gross_revenue=0,total_expenses=0,commission_amount=0,adjustment_amount=0,net_payout=0))
    yield engine,prop,statement,deps,delivery,raw
    engine.dispose()


def test_preview_is_read_only_and_lists_exact_statement(store):
    engine,prop,statement,*_=store
    with engine.begin() as conn:
        report=preview(conn,[101,102])
        assert report['blockers']==[]
        assert report['statements']==[dict(id=501,property_id=102,period='2030-01',all_amounts_zero=True,eligible=True)]
        assert conn.scalar(select(func.count()).select_from(prop))==3
        assert conn.scalar(select(func.count()).select_from(statement))==1


def test_exact_delete_preserves_unselected_property(store):
    engine,prop,statement,*_=store
    with engine.begin() as conn:
        report=preview(conn,[101,102]);remove(conn,[101,102],report['digest'],[501])
        assert conn.execute(select(prop.c.id)).scalars().all()==[201]
        assert conn.scalar(select(func.count()).select_from(statement))==0


@pytest.mark.parametrize('column,value',[('gross_revenue',Decimal('0.01')),('total_expenses',1),('commission_amount',1),('adjustment_amount',1),('net_payout',1),('finalized',True),('missing_amount_count',1)])
def test_nonzero_or_finalized_statement_blocks_delete(store,column,value):
    engine,prop,statement,*_=store
    with engine.begin() as conn:
        conn.execute(statement.update().values(**{column:value}));r=preview(conn,[101,102]);assert r['blockers']
        with pytest.raises(CleanupError):remove(conn,[101,102],r['digest'],[501])
        assert conn.scalar(select(func.count()).select_from(prop))==3


@pytest.mark.parametrize('dependency',[3,4,5])
def test_fk_cascade_delivery_and_unconstrained_reference_block(store,dependency):
    engine,prop,statement,*_=store
    with engine.begin() as conn:
        table=store[dependency]
        conn.execute(table.insert(),{'id':1,('statement_id' if dependency==4 else 'property_id'):(501 if dependency==4 else 101)})
        r=preview(conn,[101,102]);assert r['blockers']
        with pytest.raises(CleanupError):remove(conn,[101,102],r['digest'],[501])
        assert conn.scalar(select(func.count()).select_from(prop))==3


def test_stale_preview_and_wrong_statement_selection_rejected(store):
    engine,prop,*_=store
    with engine.begin() as conn:
        r=preview(conn,[101,102])
        with pytest.raises(CleanupError):remove(conn,[101,102],r['digest'],[])
        conn.execute(prop.update().where(prop.c.id==101).values(name='Changed fixture'))
        with pytest.raises(CleanupError):remove(conn,[101,102],r['digest'],[501])
        assert conn.scalar(select(func.count()).select_from(prop))==3


def test_transaction_rollback_restores_exact_deleted_rows(store):
    engine,prop,statement,*_=store
    with pytest.raises(RuntimeError):
        with engine.begin() as conn:
            r=preview(conn,[101,102]);remove(conn,[101,102],r['digest'],[501]);raise RuntimeError('Injected failure after delete')
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(prop))==3
        assert conn.scalar(select(func.count()).select_from(statement))==1


def test_published_missing_or_invalid_targets_fail_closed(store):
    engine,prop,*_=store
    with engine.begin() as conn:
        conn.execute(prop.update().where(prop.c.id==101).values(published=True));assert preview(conn,[101])['blockers']
        with pytest.raises(CleanupError):preview(conn,[999])
    for value in ('','0','1; DROP TABLE properties','1,2,3,4,5,6'):
        with pytest.raises(CleanupError):parse_ids(value)
    assert parse_ids('102,101,102')==[101,102]


@pytest.fixture
def web(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from app.db import Base,get_db
    from app.models import Property,OwnerStatement
    from app.operations.router import router
    from app.auth import make_session_cookie,SESSION_COOKIE
    monkeypatch.setenv('SECRET_KEY','synthetic-cleanup-test-secret')
    monkeypatch.setenv('ADMIN_USER','synthetic-admin')
    monkeypatch.setenv('ADMIN_PASSWORD','synthetic-test-password')
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db=Session(engine)
    db.add_all([Property(id=101,name='Synthetic fixture A'),Property(id=102,name='Synthetic fixture B'),Property(id=201,name='Synthetic retained source')])
    db.add(OwnerStatement(id=501,property_id=102,period='2030-01'));db.commit()
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app,follow_redirects=False)
    yield client,db,SESSION_COOKIE,make_session_cookie('synthetic-admin')
    db.close();engine.dispose()


def _review_form(client,cookie_name,cookie):
    import re,html
    client.cookies.set(cookie_name,cookie)
    response=client.get('/operations/property-cleanup?ids=101,102')
    assert response.status_code==200
    assert '#501' in response.text and 'Synthetic fixture A' in response.text
    return {name:html.unescape(value) for name,value in re.findall(r'name="([^"]+)" value="([^"]*)"',response.text)}


def test_review_requires_admin_and_post_rejects_cross_origin_or_bad_confirmation(web):
    from app.models import Property
    client,db,key,cookie=web
    assert client.get('/operations/property-cleanup?ids=101,102').status_code==403
    data=_review_form(client,key,cookie);data['confirmation']='DELETE THESE TEST RECORDS'
    assert client.post('/operations/property-cleanup',data=data).status_code==403
    assert client.post('/operations/property-cleanup',data=data,headers={'Origin':'https://elsewhere.invalid'}).status_code==403
    data['confirmation']='yes'
    assert client.post('/operations/property-cleanup',data=data,headers={'Origin':'http://testserver'}).status_code==409
    assert db.query(Property).count()==3


def test_online_route_exact_action_creates_audit_and_preserves_other_property(web):
    from app.models import Property,OwnerStatement,OpsCommand
    client,db,key,cookie=web;data=_review_form(client,key,cookie)
    data['confirmation']='DELETE THESE TEST RECORDS'
    response=client.post('/operations/property-cleanup',data=data,headers={'Origin':'http://testserver'})
    assert response.status_code==303 and response.headers['location']=='/properties'
    assert [p.id for p in db.query(Property).all()]==[201]
    assert db.query(OwnerStatement).count()==0
    assert db.query(OpsCommand).filter_by(purpose='property-fixture-cleanup').count()==1
    assert client.post('/operations/property-cleanup',data=data,headers={'Origin':'http://testserver'}).status_code==409


def test_review_token_bound_to_dependencies_and_actor(web):
    from app.models import Property,WorkOrder
    from app.auth import make_session_cookie
    client,db,key,cookie=web;data=_review_form(client,key,cookie)
    data['confirmation']='DELETE THESE TEST RECORDS'
    db.add(WorkOrder(property_id=101,title='Synthetic new dependency',description='',category='other',priority='normal',status='open',version=1));db.commit()
    assert client.post('/operations/property-cleanup',data=data,headers={'Origin':'http://testserver'}).status_code==409
    assert db.query(Property).count()==3
    client.cookies.set(key,make_session_cookie('different-user'))
    assert client.post('/operations/property-cleanup',data=data,headers={'Origin':'http://testserver'}).status_code==403
