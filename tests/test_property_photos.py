import json,re
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db import get_db
from app.models import PropertyPhoto,OpsCommand
from app.operations.property_photos import photo_url,token,save,public_gallery
from app.operations.property_photo_router import router
from app.operations.router import require_admin
from app.public_api import router as public_router
from app.operations.tasks import TaskError
from test_operations import db,prop


@pytest.fixture
def listing(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-photo-'+32*'x')
    return prop(db,published=True,photo_url='https://images.example.com/legacy.jpg')


def write(db,listing,**changes):
    args={'actor':'synthetic-admin','property_id':listing.id,'token_value':token(db,'synthetic-admin',listing),
        'action':'add','url':'https://images.example.com/room.jpg','caption':'Synthetic room','alt_text':'A bright synthetic studio',
        'note':'Update synthetic listing gallery'}
    args.update(changes);return save(db,**args)


def test_gallery_order_cover_archive_restore_and_audit(db,listing):
    first=write(db,listing);second=write(db,listing,url='https://images.example.com/balcony.jpg')
    assert listing.photo_url.endswith('legacy.jpg')
    write(db,listing,action='up',photo_id=second)
    assert public_gallery(db,listing.id)[0]['url'].endswith('balcony.jpg')
    write(db,listing,action='cover',photo_id=second)
    with pytest.raises(TaskError):write(db,listing,action='archive',photo_id=second)
    write(db,listing,action='cover',photo_id=first)
    write(db,listing,action='archive',photo_id=second)
    assert len(public_gallery(db,listing.id))==1
    write(db,listing,action='restore',photo_id=second)
    assert len(public_gallery(db,listing.id))==2
    assert db.query(OpsCommand).count()==7
    assert json.loads(db.query(OpsCommand).order_by(OpsCommand.id.desc()).first().payload)['action']=='restore'


def test_stale_gallery_and_cover_changes_do_not_overwrite(db,listing):
    old=token(db,'synthetic-admin',listing);first=write(db,listing)
    with pytest.raises(TaskError):write(db,listing,token_value=old,url='https://images.example.com/stale.jpg')
    current=token(db,'synthetic-admin',listing);listing.photo_url='https://images.example.com/changed-elsewhere.jpg';db.commit()
    with pytest.raises(TaskError):write(db,listing,token_value=current,action='cover',photo_id=first)
    assert db.query(PropertyPhoto).count()==1


@pytest.mark.parametrize('url',['javascript:alert(1)','http://images.example.com/a','https://localhost/a',
    'https://127.0.0.1/a','https://10.1.1.1/a','https://[::1]/a','https://user:pass@images.example.com/a',
    'https://images.example.com:8080/a','https://images.example.com\\@127.0.0.1/a'])
def test_invalid_urls_are_rejected_without_writes(db,listing,url):
    with pytest.raises(TaskError):write(db,listing,url=url)
    assert db.query(PropertyPhoto).count()==db.query(OpsCommand).count()==0


def test_public_gallery_hides_archived_records_and_internal_history(db,listing):
    first=write(db,listing,caption='<script>synthetic</script>')
    write(db,listing,url='https://images.example.com/private-archive.jpg')
    archived=db.query(PropertyPhoto).filter(PropertyPhoto.id!=first).one()
    write(db,listing,action='archive',photo_id=archived.id)
    app=FastAPI();app.include_router(router);app.include_router(public_router);app.dependency_overrides[get_db]=lambda:db
    with TestClient(app) as client:
        assert client.get(f'/operations/properties/{listing.id}/photos').status_code==403
        response=client.get(f'/api/public/listings/{listing.id}')
        assert len(response.json()['result']['photos'])==1
        assert 'private-archive' not in response.text and 'Update synthetic listing gallery' not in response.text
        app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
        html=client.get(f'/operations/properties/{listing.id}/photos').text
        assert '<script>synthetic</script>' not in html and '&lt;script&gt;synthetic' in html
        listing.published=False;db.commit()
        assert client.get(f'/api/public/listings/{listing.id}').status_code==404


def test_gallery_limit_duplicate_and_cross_property_id(db,listing):
    first=write(db,listing)
    with pytest.raises(TaskError):write(db,listing)
    other=prop(db)
    with pytest.raises(TaskError):write(db,other,action='cover',photo_id=first)
    for index in range(99):db.add(PropertyPhoto(property_id=listing.id,url=f'https://images.example.com/{index}.jpg',alt_text='Synthetic',position=index+2,actor='synthetic-admin'))
    db.commit()
    with pytest.raises(TaskError):write(db,listing,url='https://images.example.com/overflow.jpg')
    assert db.query(PropertyPhoto).count()==100


def manifest(listing,count=3,**changes):
    data={'schema_version':1,'property_id':listing.id,'source_url':listing.listing_url,
        'photos':[{'url':f'https://images.example.com/import-{i}.jpg','caption':'Synthetic source photo','alt_text':f'Synthetic photo {i+1}'} for i in range(count)]}
    data.update(changes);return json.dumps(data).encode()


def bulk(db,listing,content=None,**changes):
    from app.operations.property_photos import import_photos
    args={'actor':'synthetic-admin','property_id':listing.id,'token_value':token(db,'synthetic-admin',listing),
          'content':manifest(listing) if content is None else content,'note':'Import synthetic verified source photos'}
    args.update(changes);return import_photos(db,**args)


@pytest.fixture
def private_listing(db,listing):
    listing.published=False;listing.listing_url='https://source.example.com/synthetic-room';db.commit();return listing


def test_bulk_largest_observed_album_order_idempotency_and_no_outbound_records(db,private_listing):
    from app.models import Booking,CleaningTask,MessageLog
    r=bulk(db,private_listing,manifest(private_listing,87));assert r=={'added':87,'already_present':0}
    photos=db.query(PropertyPhoto).order_by(PropertyPhoto.position).all()
    assert len(photos)==87 and photos[0].url.endswith('import-0.jpg') and photos[-1].position==87
    assert private_listing.photo_url.endswith('legacy.jpg') and private_listing.published is False
    assert bulk(db,private_listing,manifest(private_listing,87))=={'added':0,'already_present':87}
    assert db.query(OpsCommand).count()==1
    assert db.query(Booking).count()==db.query(CleaningTask).count()==db.query(MessageLog).count()==0


@pytest.mark.parametrize('change',[{'property_id':999},{'source_url':'https://other.example.com/source'},
    {'photos':[{'url':'http://images.example.com/a','caption':'','alt_text':'Synthetic'}]},
    {'photos':[{'url':'https://images.example.com/a?access_token=synthetic','caption':'','alt_text':'Synthetic'}]},
    {'photos':[{'url':'https://images.example.com/a','caption':'','alt_text':'Synthetic','unexpected':'extra data'}]},
    {'photos':[]},{'extra':'data'}])
def test_bulk_mismatch_and_invalid_manifest_make_no_changes(db,private_listing,change):
    with pytest.raises(TaskError):bulk(db,private_listing,manifest(private_listing,**change))
    assert db.query(PropertyPhoto).count()==db.query(OpsCommand).count()==0


def test_bulk_published_stale_archived_overflow_and_atomic_rollback(db,private_listing):
    old=token(db,'synthetic-admin',private_listing);write(db,private_listing,url='https://images.example.com/elsewhere.jpg')
    with pytest.raises(TaskError):bulk(db,private_listing,token_value=old)
    private_listing.published=True;db.commit()
    with pytest.raises(TaskError):bulk(db,private_listing)
    private_listing.published=False;db.commit()
    first=db.query(PropertyPhoto).one();write(db,private_listing,action='archive',photo_id=first.id)
    data=json.loads(manifest(private_listing));data['photos'][0]['url']=first.url
    with pytest.raises(TaskError):bulk(db,private_listing,json.dumps(data).encode())
    with pytest.raises(TaskError):bulk(db,private_listing,manifest(private_listing,100))
    with pytest.raises(TaskError):bulk(db,private_listing,note='bad')
    assert db.query(PropertyPhoto).count()==1
    assert db.query(OpsCommand).count()==2


def test_bulk_router_auth_origin_and_normal_gallery_entry(db,private_listing):
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    path=f'/operations/properties/{private_listing.id}/photos'
    with TestClient(app,follow_redirects=False) as client:
        assert client.get(path).status_code==403
        app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
        page=client.get(path);assert 'Import verified source gallery' in page.text
        tok=re.search(r'name="token_value" value="([^"]+)"',page.text).group(1)
        import html
        data={'token_value':html.unescape(tok),'note':'Import synthetic source gallery'}
        files={'file':('synthetic.json',manifest(private_listing),'application/json')}
        assert client.post(path+'/import',data=data,files=files).status_code==403
        assert client.post(path+'/import',data=data,files=files,headers={'Origin':'https://other.invalid'}).status_code==403
        response=client.post(path+'/import',data=data,files=files,headers={'Origin':'http://testserver'})
        assert response.status_code==303 and response.headers['location']==path
        assert db.query(PropertyPhoto).count()==3
