"""The session guard — a pure-ASGI middleware that is the single gate.

Why middleware and not per-route dependencies: the requirement is that the
protection cannot be bypassed by asking for a URL directly, and must cover
routes that do not opt in.  A route-level dependency is opt-in; a guard
placed around the whole application is not.

Behaviour
---------
* PUBLIC paths (``config.PUBLIC_EXACT`` / ``PUBLIC_PREFIX``) pass through.
* Everything else needs a live session cookie.
* Unauthenticated **API** request   → 401 JSON (never an HTML login page to
  an XHR client, and never an internal error string).
* Unauthenticated **page** request  → 303 redirect to the login page with a
  ``next`` parameter (validated on the way back in — see ``api.py``).
* Unauthenticated **WebSocket**     → closed with 1008 (policy violation);
  a WS handshake therefore cannot be used to reach live data.
* Mutating methods (POST/PUT/PATCH/DELETE) additionally require the
  per-session CSRF header.  SameSite cookies are the baseline; this is the
  second lock.
* The resolved session is attached to the ASGI scope under
  ``scope["blm_session"]`` for dependencies to read — it is never taken from
  a request header or query string that a client could forge.
"""
from __future__ import annotations

import json
import logging
from typing import Optional
from urllib.parse import quote

from blm_v4.auth.config import AuthConfig
from blm_v4.auth.service import AuthService, SessionView

_log = logging.getLogger("blm_v4.auth")

SESSION_SCOPE_KEY = "blm_session"
_CSRF_HEADER = b"x-csrf-token"
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_JSON_401 = {"detail": "Authentication required"}
_JSON_CSRF = {"detail": "CSRF validation failed"}


def client_ip(scope: dict, *, trust_proxy: bool) -> str:
    """Best-effort client address for throttle/lockout bucketing.

    Behind a reverse proxy the socket peer is always the proxy, so every
    client would share one bucket and a single attacker could lock everyone
    out.  When (and only when) the deployment declares a trusted proxy, the
    LAST ``X-Forwarded-For`` entry is used — Caddy *appends* the peer to any
    inbound header, so the last element is the one the proxy actually
    observed and cannot be spoofed by the client.
    """
    headers = {k.lower(): v for k, v in scope.get("headers") or []}
    if trust_proxy:
        real = headers.get(b"x-real-ip")
        if real:
            return real.decode("latin-1").strip()[:64]
        xff = headers.get(b"x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.decode("latin-1").split(",")
                     if p.strip()]
            if parts:
                return parts[-1][:64]
    client = scope.get("client")
    if client:
        return str(client[0])[:64]
    return "-"


def _cookie_value(scope: dict, name: str) -> Optional[str]:
    headers = {k.lower(): v for k, v in scope.get("headers") or []}
    raw = headers.get(b"cookie")
    if not raw:
        return None
    target = name + "="
    for chunk in raw.decode("latin-1").split(";"):
        chunk = chunk.strip()
        if chunk.startswith(target):
            return chunk[len(target):]
    return None


def _header_value(scope: dict, name: bytes) -> Optional[str]:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


class SessionGuard:
    """ASGI middleware enforcing authentication on every non-public request."""

    def __init__(self, app, service: AuthService, config: AuthConfig, *,
                 trust_proxy: bool = False):
        self.app = app
        self.service = service
        self.config = config
        self.trust_proxy = trust_proxy

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        path = scope.get("path", "") or "/"
        if not self.config.enabled or self.config.is_public(path):
            return await self.app(scope, receive, send)

        token = _cookie_value(scope, self.config.cookie_name)
        try:
            session = self.service.validate(token)
        except Exception:  # pragma: no cover - fail CLOSED, never open
            _log.exception("auth_session_lookup_failed")
            session = None

        if session is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            return await self._reject(scope, receive, send, path)

        if scope["type"] == "http" and scope.get("method", "GET").upper() in _MUTATING:
            supplied = _header_value(scope, _CSRF_HEADER)
            if not self.service.verify_csrf(session, supplied):
                return await self._send_json(send, 403, _JSON_CSRF)

        scope[SESSION_SCOPE_KEY] = session
        return await self.app(scope, receive, send)

    # ── rejection shapes ──────────────────────────────────────────
    async def _reject(self, scope, receive, send, path):
        method = scope.get("method", "GET").upper()
        accepts_json = False
        accept = _header_value(scope, b"accept") or ""
        if "application/json" in accept:
            accepts_json = True
        wants_json = path.startswith("/api/") or accepts_json
        if method not in ("GET", "HEAD") or wants_json:
            return await self._send_json(send, 401, _JSON_401)
        target = self.config.login_path
        nxt = path if path not in ("", "/") else "/"
        location = f"{target}?next={quote(nxt, safe='/')}"
        body = json.dumps({"detail": "Authentication required"}).encode()
        await send({
            "type": "http.response.start",
            "status": 303,
            "headers": [
                (b"location", location.encode("latin-1")),
                (b"cache-control", b"no-store"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _send_json(send, status: int, payload: dict):
        body = json.dumps(payload).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def safe_next(next_value: Optional[str], *, fallback: str = "/") -> str:
    """Only ever return a same-site, absolute-path redirect target.

    Blocks ``//evil.com``, ``https://evil.com`` and any other open-redirect
    shape; anything that is not a plain rooted path falls back to the
    dashboard.
    """
    if not next_value:
        return fallback
    value = next_value.strip()
    if not value.startswith("/") or value.startswith("//"):
        return fallback
    if "\\" in value or "\n" in value or "\r" in value:
        return fallback
    if value.startswith("/login"):
        return fallback
    return value
