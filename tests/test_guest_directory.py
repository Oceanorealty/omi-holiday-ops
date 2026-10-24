import csv,io
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE,make_session_cookie
from app.db import get_db
from app.models import Guest,BookingStatus
from app.operations.guest_directory import directory_data,export_directory
from app.operations.guest_directory_router import router
from test_operations import db,prop,booking


def guest(db,**values):
    row=Guest(**values);db.add(row);db.commit();return row


def test_identity_history_counts_and_literal_search(db):
    p=prop(db);a=guest(db,name='Same name',email='same@example.invalid');b=guest(db,name='Same name',email=a.email)
    for _ in range(2):booking(db,p,guest_id=a.id)
    booking(db,p,guest_id=b.id);booking(db,p,guest_id=b.id,status=BookingStatus.cancelled);booking(db,p,guest_id=b.id,is_block=True)
    rows=directory_data(db,repeat=True)['rows'];assert [(g.id,n) for g,n in rows]==[(a.id,2)]
    assert directory_data(db,q=a.email)['total']==2
    special=guest(db,name='Literal %_ guest');assert directory_data(db,q='%_')['rows'][0][0].id==special.id
    assert directory_data(db,q='9'*120)['total']==0


def test_filter_pagination_and_safe_csv(db,monkeypatch):
    for i in range(27):guest(db,name='Synthetic '+str(i),email='contact@example.invalid')
    result=directory_data(db,q='Synthetic',page=2);assert len(result['rows'])==2 and result['pages']==2
    assert 'q=Synthetic' in result['previous'] and result['next'] is None
    row=guest(db,name='=2+2',email='@formula',phone='+61400000000')
    body=export_directory(db,q=str(row.id));values=list(csv.reader(io.StringIO(body.lstrip('\ufeff'))));assert values[1][1:4]==["'=2+2","'@formula","'+61400000000"]
    monkeypatch.setattr('app.operations.guest_directory.EXPORT_LIMIT',1)
    with pytest.raises(ValueError):export_directory(db)


def test_private_routes_escape_and_keep_history_on_exact_id(db,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','synthetic-directory-'+32*'x');monkeypatch.setenv('ADMIN_USER','synthetic-admin');monkeypatch.setenv('ADMIN_PASSWORD','synthetic-only')
    a=guest(db,name='<script>private name</script>',email='private@example.invalid');b=guest(db,name='OTHER_PRIVATE_GUEST')
    p=prop(db);own=booking(db,p,guest_id=a.id);other=booking(db,p,guest_id=b.id)
    app=FastAPI();app.include_router(router)
    def database():yield db
    app.dependency_overrides[get_db]=database
    with TestClient(app) as client:
        for path in ['/operations/guests','/operations/guests/export.csv',f'/operations/guests/{a.id}']:
            assert client.get(path).status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('unknown-worker'))
        assert client.get('/operations/guests/export.csv').status_code==403
        client.cookies.set(SESSION_COOKIE,make_session_cookie('synthetic-admin'))
        response=client.get('/operations/guests');assert response.status_code==200 and '<script>private name' not in response.text and '&lt;script&gt;' in response.text
        profile=client.get(f'/operations/guests/{a.id}');assert profile.status_code==200 and f'/operations/reservations/{own.id}' in profile.text and f'/operations/reservations/{other.id}' not in profile.text
        assert profile.headers['cache-control']=='no-store'
        export=client.get('/operations/guests/export.csv');assert export.status_code==200 and 'attachment;' in export.headers['content-disposition'] and export.headers['cache-control']=='no-store'
        assert client.get('/operations/guests/999999').status_code==404
        assert client.get('/operations/guests/'+('9'*120)).status_code==422
