"""
Session-cookie login gate for the whole app. One shared username/password
(env vars ADMIN_USER / ADMIN_PASSWORD) — this is an internal team tool, not
a multi-tenant product, so per-user accounts would be overkill for now.

Deliberately NOT HTTP Basic Auth: WeChat's in-app browser (and some other
embedded webviews) doesn't support the native Basic Auth credential prompt,
so a link shared in WeChat would just fail to load. A normal HTML login
form + signed cookie works everywhere a browser does.

If ADMIN_USER/ADMIN_PASSWORD aren't set, auth is skipped entirely (so local
dev needs no credentials) — but that means production MUST have them
configured, since there's nothing else gating access.
"""

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
PUBLIC_PREFIXES = ("/static/",)


def _secret() -> str:
    # Falls back to the admin password so a session cookie is still possible
    # without forcing a *third* env var — SECRET_KEY is the better option
    # when set, since rotating the password then invalidates old sessions too.
    return os.environ.get("SECRET_KEY") or os.environ.get("ADMIN_PASSWORD", "")


def _sign(value: str) -> str:
    mac = hmac.new(_secret().encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{mac}"


def make_session_cookie() -> str:
    payload = str(int(time.time()) + SESSION_MAX_AGE)  # expiry timestamp
    return _sign(payload)


def verify_session_cookie(cookie_value: str) -> bool:
    if not cookie_value or "." not in cookie_value:
        return False
    payload, _, mac = cookie_value.rpartition(".")
    expected = _sign(payload)
    if not hmac.compare_digest(expected, cookie_value):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


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
        if verify_session_cookie(cookie):
            return await call_next(request)

        next_url = path if request.method == "GET" else "/"
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)
