import json
import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import SESSION_COOKIE, make_session_cookie
from app.db import get_db
from app.models import IntegrationRun
from app.integrations.booking_com import AUTH_URL, ROOMS_URL, BookingConnectivity, ChannelError
from app.operations.channel_router import router
from app.operations.tasks import form_token
from test_operations import db


class Response:
    def __init__(self, content=b'', status=200):
        self.content, self.status_code, self.closed = content, status, False
    def iter_content(self, chunk_size):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start+chunk_size]
    def close(self):
        self.closed = True


class Transport:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def token():
    return Response(json.dumps({'jwt': 'synthetic.jwt.signature'}).encode())


def room():
    return Response(b'<rooms><room id="1234" hotel_id="123">Synthetic studio</room></rooms>')


def test_token_auth_cached_and_rooms_response_scoped_to_property():
    transport = Transport([token(), room(), room(), token(), room()])
    now = [100]
    client = BookingConnectivity('synthetic-client', 'synthetic-secret', session=transport, clock=lambda: now[0])
    assert client.rooms('123') == [{'room_id': '1234', 'hotel_id': '123', 'name': 'Synthetic studio'}]
    client.rooms('123')
    now[0] += 3001
    client.rooms('123')
    assert [url for url, _ in transport.calls] == [AUTH_URL, ROOMS_URL, ROOMS_URL, AUTH_URL, ROOMS_URL]
    assert all(kwargs['allow_redirects'] is False and kwargs['timeout'] == (5, 15) for _, kwargs in transport.calls)
    assert transport.calls[0][1]['json']['client_secret'] == 'synthetic-secret'
    assert 'client_secret' not in str(transport.calls[1][1])
    assert transport.calls[1][1]['headers']['Authorization'] == 'Bearer synthetic.jwt.signature'
    assert b'<hotel_id>123</hotel_id>' in transport.calls[1][1]['data']


@pytest.mark.parametrize('response,expected', [
    (Response(status=401), 'authorization_required'), (Response(status=403), 'authorization_required'),
    (Response(status=429), 'rate_limited'), (Response(status=302), 'provider_request_failed'),
    (Response(b'not json'), 'invalid_auth_response'), (Response(b'{"jwt":"bad token"}'), 'invalid_auth_response'),
    (requests.Timeout('credential-must-not-leak'), 'network_unavailable')])
def test_auth_errors_do_not_echo_remote_content_or_follow_redirects(response, expected):
    client = BookingConnectivity('id', 'secret', session=Transport([response]))
    with pytest.raises(ChannelError) as error:
        client.rooms('123')
    assert str(error.value) == expected and 'credential-must-not-leak' not in str(error.value)
    if isinstance(response, Response):
        assert response.closed


@pytest.mark.parametrize('document,expected', [
    (b'<rooms><room id="1" hotel_id="999">Wrong property</room></rooms>', 'property_scope_mismatch'),
    (b'<rooms><room id="1" hotel_id="123"/><room id="1" hotel_id="123"/></rooms>', 'invalid_room_response'),
    (b'<!DOCTYPE rooms [<!ENTITY x "secret">]><rooms>&x;</rooms>', 'invalid_room_response'),
    ('<!DOCTYPE rooms [<!ENTITY x "secret">]><rooms>&x;</rooms>'.encode('utf-16'), 'invalid_room_response'),
    (b'<errors><error>must-not-leak</error></errors>', 'invalid_room_response'),
    (b'<rooms>', 'invalid_room_response'), (b'x'* (1024*1024+1), 'response_too_large')])
def test_room_schema_size_and_entity_validation(document, expected):
    client = BookingConnectivity('id', 'secret', session=Transport([token(), Response(document)]))
    with pytest.raises(ChannelError) as error:
        client.rooms('123')
    assert str(error.value) == expected and 'must-not-leak' not in str(error.value)


def test_property_id_is_validated_before_any_network_request():
    transport = Transport([])
    client = BookingConnectivity('id', 'secret', session=transport)
    for invalid in ('https://other.invalid', '0', '-1', '<request/>', '１２３'):
        with pytest.raises(ChannelError):
            client.rooms(invalid)
    assert transport.calls == []


def test_direct_channel_page_requires_no_hostaway_or_provider_credentials(db, monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'synthetic-channel-key-'+'x'*32)
    monkeypatch.setenv('ADMIN_USER', 'channel-admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'synthetic-only')
    for key in ('BOOKING_CONNECTIVITY_APPROVED', 'BOOKING_CONNECTIVITY_CLIENT_ID', 'BOOKING_CONNECTIVITY_CLIENT_SECRET', 'HOSTAWAY_ACCESS_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        assert client.get('/operations/channels').status_code == 403
        client.cookies.set(SESSION_COOKIE, make_session_cookie('channel-admin'))
        result = client.get('/operations/channels')
        assert result.status_code == 200 and 'No Hostaway account or API is required' in result.text
        assert 'No live direct-channel tests recorded.' in result.text
        values = {'token': form_token('channel-admin', purpose='booking-room-test'), 'hotel_id': '123'}
        assert client.post('/operations/channels/booking-com/rooms', data=values).status_code == 409
        assert db.query(IntegrationRun).count() == 0
        monkeypatch.setenv('BOOKING_CONNECTIVITY_APPROVED', '1')
        monkeypatch.setenv('BOOKING_CONNECTIVITY_CLIENT_ID', 'synthetic-id')
        monkeypatch.setenv('BOOKING_CONNECTIVITY_CLIENT_SECRET', 'do-not-render')
        from app.operations import channel_router
        class FakeClient:
            def rooms(self, hotel_id):
                return [{'room_id': '1', 'hotel_id': hotel_id, 'name': '<script>unsafe</script>'}]
        monkeypatch.setattr(channel_router, 'configured_client', lambda *_: FakeClient())
        assert client.post('/operations/channels/booking-com/rooms', data=values, headers={'origin': 'https://elsewhere.invalid'}).status_code == 403
        result = client.post('/operations/channels/booking-com/rooms', data=values)
        assert result.status_code == 200 and '&lt;script&gt;unsafe&lt;/script&gt;' in result.text
        assert 'do-not-render' not in result.text
        assert db.query(IntegrationRun).one().state == 'rooms_read_verified'
        assert 'does not certify full channel connectivity' in result.text
