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
    for index in range(49):db.add(PropertyPhoto(property_id=listing.id,url=f'https://images.example.com/{index}.jpg',alt_text='Synthetic',position=index+2,actor='synthetic-admin'))
    db.commit()
    with pytest.raises(TaskError):write(db,listing,url='https://images.example.com/overflow.jpg')
    assert db.query(PropertyPhoto).count()==50
