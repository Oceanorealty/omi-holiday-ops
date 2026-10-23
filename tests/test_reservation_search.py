"""Synthetic admin search: scope, literal matching, nights and pagination."""
from datetime import datetime
from html import unescape
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db import get_db
from app.models import Booking, Guest, PlatformName
from app.operations.parity_router import router
from app.operations.router import require_admin
from test_operations import db,prop,booking


def client_for(db):
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    return TestClient(app)


def test_search_literal_guest_email_reference_and_night_scope(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','search-test-'+'x'*32)
    p=prop(db);q=prop(db,name='Other')
    guest=Guest(name='Alice_100%',email='alice@example.invalid');db.add(guest);db.commit()
    wanted=booking(db,p,guest_id=guest.id,uid='REF-literal_%',platform=PlatformName.direct)
    booking(db,q,uid='Different listing')
    booking(db,p,uid='Cancelled-ignored',status='cancelled')
    with client_for(db) as client:
        base='/operations/reservations'
        for term in ['alice@example.invalid','alice_100%','REF-literal_%',str(wanted.id)]:
            response=client.get(base,params={'q':term,'property_id':p.id,'channel':'direct','start':'2026-10-22','end':'2026-10-23'})
            assert response.status_code==200
            assert f'href="/operations/reservations/{wanted.id}"' in response.text
        for params in [{'start':'2026-10-23','end':'2026-10-24'}, {'q':'not-present'}, {'channel':'airbnb','property_id':p.id}]:
            response=client.get(base,params=params)
            assert f'href="/operations/reservations/{wanted.id}"' not in response.text
        # SQL wildcards in a search must be treated literally.
        literal=client.get(base,params={'q':'%'}).text
        assert f'href="/operations/reservations/{wanted.id}"' in literal
        assert 'Different listing' not in literal
        for params in [{'start':'bad'},{'start':'2026-10-24','end':'2026-10-23'}, {'channel':'invalid'}, {'page':0},{'page':501},{'q':'x'*121}]:
            assert client.get(base,params=params).status_code==422


def test_pagination_preserves_filters_and_does_not_drop_older_orders(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','search-test-'+'x'*32)
    p=prop(db)
    db.add_all([Booking(property_id=p.id,platform=PlatformName.direct,uid=f'Archive-{i}',
        check_in=datetime(2026,10,21),check_out=datetime(2026,10,23)) for i in range(201)])
    db.commit()
    with client_for(db) as client:
        params={'property_id':p.id,'q':'Archive-','channel':'direct','start':'2026-10-21','end':'2026-10-24'}
        first=client.get('/operations/reservations',params=params)
        assert first.status_code==200 and 'Next page' in first.text
        assert f'property_id={p.id}&status=confirmed&q=Archive-&channel=direct&start=2026-10-21&end=2026-10-24&page=2' in unescape(first.text)
        second=client.get('/operations/reservations',params={**params,'page':2})
        assert second.status_code==200 and 'Previous page' in second.text and 'Next page' not in second.text
        assert 'href="/operations/reservations/1"' in second.text
        assert 'href="/operations/reservations/1"' not in first.text
        assert db.query(Booking).count()==201
