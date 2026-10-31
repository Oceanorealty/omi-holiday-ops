"""Synthetic tests for the compact date picker; never writes reservations."""
from html.parser import HTMLParser
from urllib.parse import urlencode
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db import get_db
from app.models import Booking
from app.operations.router import router, require_admin
from test_operations import db, prop


class Forms(HTMLParser):
    def __init__(self):
        super().__init__(); self.ids=[]; self.forms=[]; self.current=None

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if 'id' in attrs:self.ids.append(attrs['id'])
        if tag=='form':
            self.current={'attrs':attrs,'inputs':[]};self.forms.append(self.current)
        if tag=='input' and self.current is not None:self.current['inputs'].append(attrs)

    def handle_endtag(self, tag):
        if tag=='form':self.current=None


def test_mobile_date_form_preserves_property_scope_without_writing(db):
    selected=prop(db,name='Selected synthetic listing');prop(db,name='Unselected synthetic listing')
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with TestClient(app) as client:
        page=client.get(f'/operations/calendar?start=2026-11-01&end=2026-11-08&property_id={selected.id}')
        parsed=Forms();parsed.feed(page.text)
        form=next(f for f in parsed.forms if any(i.get('id')=='mobile-board-start' for i in f['inputs']))
        assert form['attrs']['method']=='get' and form['attrs']['action']=='/operations/calendar'
        params={i['name']:i.get('value','') for i in form['inputs']}
        assert params=={'start':'2026-11-01','end':'2026-11-08','property_id':str(selected.id)}
        params.update(start='2026-11-04',end='2026-11-06')
        response=client.get(form['attrs']['action']+'?'+urlencode(params))
        assert response.status_code==200
        assert '2026-11-04 → 2026-11-06 · Change dates' in response.text
        assert 'Selected synthetic listing' in response.text and 'Unselected synthetic listing' not in response.text
        assert 'Live availability is not verified.' in response.text
    assert db.query(Booking).count()==0


def test_responsive_date_inputs_have_unique_accessible_ids_and_validate_dates(db):
    prop(db)
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_admin]=lambda:'synthetic-admin'
    with TestClient(app) as client:
        parsed=Forms();parsed.feed(client.get('/operations/calendar').text)
        assert len(parsed.ids)==len(set(parsed.ids))
        assert client.get('/operations/calendar?start=2026-11-04&end=2026-11-03').status_code==422
        assert client.get('/operations/calendar?start=2026-11-04&end=2027-11-03').status_code==422
    assert db.query(Booking).count()==0
