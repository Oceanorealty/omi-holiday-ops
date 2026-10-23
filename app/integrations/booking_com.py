"""Booking.com Connectivity room discovery; no Hostaway dependency.

Uses official token authentication and the read-only POST /hotels/xml/rooms.
No reservation/card retrieval, inventory write or messaging is implemented here.
"""
import json
import re
import threading
import time
from functools import lru_cache
from xml.etree import ElementTree as ET

import requests

AUTH_URL = 'https://connectivity-authentication.booking.com/token-based-authentication/exchange'
ROOMS_URL = 'https://supply-xml.booking.com/hotels/xml/rooms'
MAX_RESPONSE = 1024 * 1024


class ChannelError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class BookingConnectivity:
    def __init__(self, client_id, client_secret, *, session=None, clock=time.monotonic):
        if not client_id or not client_secret:
            raise ChannelError('credentials_missing')
        self._client_id, self._client_secret = client_id, client_secret
        self._session = session or requests.Session()
        if session is None:
            self._session.trust_env = False
        self._clock, self._token, self._expires = clock, None, 0
        self._lock = threading.Lock()

    def _post(self, url, **kwargs):
        try:
            response = self._session.post(url, timeout=(5, 15), allow_redirects=False, stream=True, **kwargs)
            try:
                if response.status_code == 429:
                    raise ChannelError('rate_limited')
                if response.status_code in (401, 403):
                    self._token = None
                    raise ChannelError('authorization_required')
                if response.status_code != 200:
                    raise ChannelError('provider_request_failed')
                content = bytearray()
                for chunk in response.iter_content(chunk_size=16384):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise ChannelError('response_too_large')
                return bytes(content)
            finally:
                response.close()
        except requests.RequestException:
            raise ChannelError('network_unavailable') from None

    def _access_token(self):
        with self._lock:
            if self._token and self._clock() < self._expires:
                return self._token
            content = self._post(AUTH_URL, json={'client_id': self._client_id, 'client_secret': self._client_secret},
                                 headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
            try:
                value = json.loads(content)['jwt']
                if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', value) or len(value) > 16000:
                    raise ValueError()
            except (KeyError, ValueError, TypeError, UnicodeDecodeError):
                raise ChannelError('invalid_auth_response') from None
            # Official tokens expire after an hour; refresh early without trusting claims.
            self._token, self._expires = value, self._clock() + 3000
            return value

    def rooms(self, hotel_id):
        hotel_id = str(hotel_id)
        if not re.fullmatch(r'[1-9][0-9]{0,19}', hotel_id):
            raise ChannelError('invalid_property_id')
        token = self._access_token()
        root = ET.Element('request')
        ET.SubElement(root, 'hotel_id').text = hotel_id
        content = self._post(ROOMS_URL, data=ET.tostring(root, encoding='utf-8'),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/xml', 'Accept': 'application/xml'})
        try:
            document = content.decode('utf-8')
        except UnicodeDecodeError:
            raise ChannelError('invalid_room_response') from None
        if '\x00' in document or '<!DOCTYPE' in document.upper() or '<!ENTITY' in document.upper():
            raise ChannelError('invalid_room_response')
        try:
            root = ET.fromstring(document)
        except ET.ParseError:
            raise ChannelError('invalid_room_response') from None
        if root.tag != 'rooms' or len(root) > 10000:
            raise ChannelError('invalid_room_response')
        rows, seen = [], set()
        for room in root:
            room_id = room.get('id', '')
            if room.tag != 'room' or room.get('hotel_id') != hotel_id:
                raise ChannelError('property_scope_mismatch')
            if not re.fullmatch(r'[1-9][0-9]{0,19}', room_id) or room_id in seen:
                raise ChannelError('invalid_room_response')
            seen.add(room_id)
            rows.append({'room_id': room_id, 'hotel_id': hotel_id, 'name': (room.text or '')[:240]})
        return rows


@lru_cache(maxsize=4)
def configured_client(client_id, client_secret):
    """Process-local token reuse; credentials are never rendered or persisted by OPS."""
    return BookingConnectivity(client_id, client_secret)
