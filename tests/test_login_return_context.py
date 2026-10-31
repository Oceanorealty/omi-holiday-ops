"""Original login/middleware must retain safe GET filters after session expiry."""
from urllib.parse import urlsplit,parse_qs
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SessionAuthMiddleware
from app.db import get_db
from test_operations import db,prop


def client_for(db,monkeypatch):
    import app.main as main
    monkeypatch.setenv('SECRET_KEY','synthetic-login-context-key-only')
    monkeypatch.setenv('ADMIN_USER','synthetic-context-admin')
    monkeypatch.setenv('ADMIN_PASSWORD','synthetic-context-password')
    monkeypatch.setattr(main,'SessionLocal',lambda:db)
    app=FastAPI()
    from app.operations.router import router
    app.include_router(router)
    app.router.routes.extend(r for r in main.app.routes if getattr(r,'path','') in ('/login','/operations/calendar','/cleaning/{task_id}/assign'))
    app.dependency_overrides[get_db]=lambda:db
    app.add_middleware(SessionAuthMiddleware)
    return TestClient(app,follow_redirects=False)


def test_original_login_retains_calendar_property_dates_and_view(db,monkeypatch):
    p=prop(db)
    target=f'/operations/calendar?property_id={p.id}&start=2026-11-03&end=2026-11-08&view=list'
    with client_for(db,monkeypatch) as client:
        redirect=client.get(target)
        assert redirect.status_code==303
        query=parse_qs(urlsplit(redirect.headers['location']).query)
        assert query=={'next':[target]}
        login=client.post('/login',data={'username':'synthetic-context-admin','password':'synthetic-context-password','next':query['next'][0]})
        assert login.status_code==303 and login.headers['location']==target
        assert client.get(login.headers['location']).status_code==200


def test_empty_filter_and_reserved_query_characters_stay_inside_next(db,monkeypatch):
    target='/operations/calendar?property_id=&view=board&note=a%26b%3Dc'
    with client_for(db,monkeypatch) as client:
        location=client.get(target).headers['location']
        assert parse_qs(urlsplit(location).query)=={'next':[target]}


def test_unauthenticated_post_does_not_copy_query_or_body_to_login_return(db,monkeypatch):
    with client_for(db,monkeypatch) as client:
        response=client.post('/cleaning/1/assign?ignored=value',data={'assignee':'SYNTHETIC-PRIVATE-BODY'})
        assert response.status_code==303
        assert parse_qs(urlsplit(response.headers['location']).query)=={'next':['/']}
        assert 'SYNTHETIC' not in response.headers['location']


@pytest.mark.parametrize('unsafe',['//example.invalid/','https://example.invalid/','/\\example.invalid/'])
def test_existing_login_open_redirect_guard_remains_intact(db,monkeypatch,unsafe):
    with client_for(db,monkeypatch) as client:
        response=client.post('/login',data={'username':'synthetic-context-admin','password':'synthetic-context-password','next':unsafe})
        assert response.status_code==303 and response.headers['location']=='/'
