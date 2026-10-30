import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db import get_db
from app.models import Cleaner,Property,OpsCommand,Booking,CleaningTask,MessageLog
from app.operations.property_details import details_token,save_details
from app.operations.router import router,require_admin
from app.operations.tasks import TaskError
from test_operations import db,prop,booking


@pytest.fixture
def listing(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-core-details-secret')
    p=prop(db,name='Synthetic source',published=False,photo_url='https://images.example.com/synthetic.jpg')
    p.time_zone='UTC';db.commit();return p


def write(db,p,**changes):
    args={'actor':'synthetic-admin','property_id':p.id,'token_value':details_token('synthetic-admin',p),
        'name':p.name,'address':p.address or '', 'time_zone':'Australia/Brisbane','default_cleaner':p.default_cleaner or '',
        'note':'Correct verified synthetic timezone'}
    args.update(changes);return save_details(db,**args)


def test_timezone_and_core_details_are_audited_without_external_side_effects(db,listing):
    old_cover=listing.photo_url;cleaner=Cleaner(name='Synthetic existing cleaner',active=True);db.add(cleaner);db.commit()
    write(db,listing,address='Synthetic test address',default_cleaner=cleaner.name)
    assert listing.time_zone=='Australia/Brisbane' and listing.address=='Synthetic test address'
    assert listing.default_cleaner==cleaner.name and listing.photo_url==old_cover and listing.published is False
    assert listing.owner_portal_token is None
    assert db.query(OpsCommand).filter_by(purpose='property-details').count()==1
    assert db.query(Booking).count()==db.query(CleaningTask).count()==db.query(MessageLog).count()==0


@pytest.mark.parametrize('change',[{'name':''},{'time_zone':'Not/A_Zone'},{'time_zone':'../UTC'},
    {'default_cleaner':'Unknown cleaner'},{'address':'x'*501},{'note':'bad'}])
def test_invalid_details_rollback_every_field(db,listing,change):
    with pytest.raises(TaskError):write(db,listing,**change)
    db.refresh(listing);assert listing.time_zone=='UTC' and listing.address is None
    assert db.query(OpsCommand).count()==0


def test_stale_cross_actor_and_cross_property_tokens_fail_closed(db,listing):
    old=details_token('synthetic-admin',listing);listing.name='Changed elsewhere';db.commit()
    with pytest.raises(TaskError):write(db,listing,token_value=old)
    with pytest.raises(TaskError):write(db,listing,token_value=details_token('different-admin',listing))
    other=prop(db,name='Synthetic other')
    with pytest.raises(TaskError):write(db,listing,token_value=details_token('synthetic-admin',other))
    assert listing.time_zone=='UTC' and db.query(OpsCommand).count()==0


def test_existing_reservation_blocks_timezone_change_but_allows_other_metadata(db,listing):
    booking(db,listing)
    with pytest.raises(TaskError):write(db,listing)
    assert listing.time_zone=='UTC'
    write(db,listing,time_zone='UTC',address='Synthetic corrected address')
    assert listing.address=='Synthetic corrected address' and db.query(Booking).count()==1


def test_core_details_route_requires_admin_origin_and_current_token(db,listing):
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    data={'token_value':details_token('synthetic-admin',listing),'name':listing.name,'time_zone':'Australia/Brisbane',
          'address':'','default_cleaner':'','note':'Verified synthetic timezone correction'}
    path=f'/operations/properties/{listing.id}/details'
    with TestClient(app,follow_redirects=False) as c:
        assert c.post(path,data=data,headers={'Origin':'http://testserver'}).status_code==403
        app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
        assert c.post(path,data=data).status_code==403
        assert c.post(path,data=data,headers={'Origin':'https://other.invalid'}).status_code==403
        assert c.post(path,data=data,headers={'Origin':'http://testserver'}).status_code==303
        assert c.post(path,data=data,headers={'Origin':'http://testserver'}).status_code==409
    assert listing.time_zone=='Australia/Brisbane'
