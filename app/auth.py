"""
Session-cookie login gate for the whole app. Login accepts either the
shared ADMIN_USER/ADMIN_PASSWORD env-var account (kept for backward
compatibility) or a named StaffUser row — see main.py's /login route for
the actual credential check. Whichever one signs in, the session cookie
carries their username so routes can tell staff apart (e.g. the Staff
management page checking for the admin role).

Deliberately NOT HTTP Basic Auth: WeChat's in-app browser (and some other
embedded webviews) doesn't support the native Basic Auth credential prompt,
so a link shared in WeChat would just fail to load. A normal HTML login
form + signed cookie works everywhere a browser does.

If ADMIN_USER/ADMIN_PASSWORD aren't set, auth is skipped entirely (so local
dev needs no credentials) — but that means production MUST have them
configured, since there's nothing else gating access.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

SESSION_COOKIE = "omi_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
PUBLIC_PATHS = {"/login"}
# /api/public/ is read-only listing data meant to be fetched directly from
# the omiholiday.com marketing site — it must stay reachable without a
# session cookie, since the browser making that request is a site visitor's.
# /owner/, /cleaner/ and /guest/ are magic-link portals for people who
# don't have a staff login at all — the unguessable token in the URL *is*
# their auth. /ical/ is the outbound availability feed — other platforms
# (Airbnb etc.) fetch it directly, with no session cookie of their own;
# same unguessable-token-as-auth pattern.
PUBLIC_PREFIXES = ("/static/", "/api/public/", "/owner/", "/cleaner/", "/guest/", "/ical/")


def _secret() -> str:
    # Falls back to the admin password so a session cookie is still possible
    # without forcing a *third* env var — SECRET_KEY is the better option
    # when set, since rotating the password then invalidates old sessions too.
    return os.environ.get("SECRET_KEY") or os.environ.get("ADMIN_PASSWORD", "")


def _sign(value: str) -> str:
    mac = hmac.new(_secret().encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{mac}"


def make_session_cookie(username: str = "admin") -> str:
    expiry = int(time.time()) + SESSION_MAX_AGE
    payload = f"{expiry}|{username}"
    return _sign(payload)


def verify_session_cookie(cookie_value: str) -> str | None:
    """Returns the logged-in username, or None if the cookie is missing/invalid/expired."""
    if not cookie_value or "." not in cookie_value:
        return None
    payload, _, mac = cookie_value.rpartition(".")
    expected = _sign(payload)
    if not hmac.compare_digest(expected, cookie_value):
        return None
    expiry_str, _, username = payload.partition("|")
    try:
        if int(expiry_str) <= time.time():
            return None
    except ValueError:
        return None
    # Pre-multi-user cookies (issued before StaffUser existed) carry no
    # username segment at all — they can only have come from the shared
    # ADMIN_USER login, so that's what they map to. A literal "admin"
    # fallback would silently fail every _is_admin() check whenever the
    # real ADMIN_USER value isn't literally the string "admin".
    return username or os.environ.get("ADMIN_USER", "admin")


class SessionAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        admin_user = os.environ.get("ADMIN_USER")
        admin_password = os.environ.get("ADMIN_PASSWORD")

        if not admin_user or not admin_password:
            return await call_next(request)  # not configured — no gate (local dev)

        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        cookie = request.cookies.get(SESSION_COOKIE, "")
        username = verify_session_cookie(cookie)
        if username:
            request.state.username = username
            return await call_next(request)

        next_url = path if request.method == "GET" else "/"
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)
