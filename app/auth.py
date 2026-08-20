"""
HTTP Basic Auth gate for the whole app. One shared username/password (env
vars ADMIN_USER / ADMIN_PASSWORD) — this is an internal team tool, not a
multi-tenant product, so per-user accounts would be overkill for now.

If the env vars aren't set, auth is skipped entirely (so local dev doesn't
need credentials) — but that means production MUST have them configured,
since there's nothing else gating access.
"""

import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        admin_user = os.environ.get("ADMIN_USER")
        admin_password = os.environ.get("ADMIN_PASSWORD")

        if not admin_user or not admin_password:
            return await call_next(request)  # not configured — no gate (local dev)

        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Basic "):
            import base64

            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                username, _, password = decoded.partition(":")
            except Exception:  # noqa: BLE001 — malformed header, treat as unauthenticated
                username, password = "", ""

            user_ok = secrets.compare_digest(username, admin_user)
            pass_ok = secrets.compare_digest(password, admin_password)
            if user_ok and pass_ok:
                return await call_next(request)

        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Omi Holiday Operations"'},
        )
