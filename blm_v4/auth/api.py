"""Authentication API + the login page route.

Endpoints
---------
    GET  /login                  the login page (public; redirects when a
                                 session is already live)
    POST /api/auth/login         authenticate; sets the session cookie
    POST /api/auth/logout        revoke the session; clears the cookie
    GET  /api/auth/me            current identity + CSRF token
    GET  /api/auth/admin/probe   role-enforcement probe (admin only)

Response discipline
-------------------
* A failed login ALWAYS returns ``{"detail": "Invalid username or
  password"}`` (401).  Unknown user, wrong password, inactive account and a
  malformed stored hash are indistinguishable.
* An exhausted throttle returns 429 with a generic retry message.
* Unexpected server faults return 502 with a generic message; the detail is
  logged server-side only and never echoed to the caller.
* No endpoint can return a password or a password hash — there is no code
  path that selects ``password_hash`` into a response.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)

from blm_v4.auth.config import AuthConfig
from blm_v4.auth.middleware import (SESSION_SCOPE_KEY, client_ip, safe_next)
from blm_v4.auth.service import (GENERIC_LOGIN_ERROR, RATE_LIMIT_ERROR,
                                 AuthService, SessionView)

_log = logging.getLogger("blm_v4.auth")

router = APIRouter(tags=["blm-auth"])

_service: Optional[AuthService] = None
_config: Optional[AuthConfig] = None
_trust_proxy: bool = False

_STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"
_LOGIN_HTML = _STATIC_DIR / "login.html"


def configure_auth(service: AuthService, config: AuthConfig, *,
                   trust_proxy: bool = False) -> None:
    """Wire the process-wide auth service (called from the composition root)."""
    global _service, _config, _trust_proxy
    _service = service
    _config = config
    _trust_proxy = bool(trust_proxy)


def get_service() -> AuthService:
    if _service is None:
        raise HTTPException(status_code=503,
                            detail="authentication not configured")
    return _service


def get_config() -> AuthConfig:
    if _config is None:
        raise HTTPException(status_code=503,
                            detail="authentication not configured")
    return _config


# ── dependencies ──────────────────────────────────────────────────
def current_session(request: Request) -> SessionView:
    """The authenticated session attached by ``SessionGuard``.

    A missing session here means the route was reached without the guard
    (e.g. a router included standalone) — fail closed with 401 rather than
    assuming identity.
    """
    session = request.scope.get(SESSION_SCOPE_KEY)
    if session is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return session


def require_role(*roles: str):
    """Dependency factory: server-side role enforcement.

    Role comes from the live session (which itself re-reads the user row),
    never from a client-supplied header, body field or query parameter.
    """
    allowed = tuple(roles)

    def _dep(request: Request) -> SessionView:
        session = current_session(request)
        if session.role not in allowed:
            _log.warning("auth_role_denied username=%r role=%s need=%s",
                         session.username, session.role, allowed)
            raise HTTPException(status_code=403, detail="Insufficient role")
        return session

    return _dep


require_admin = require_role("admin")


# ── cookie plumbing ───────────────────────────────────────────────
def _set_session_cookie(response, config: AuthConfig, token: str,
                        *, max_age: int) -> None:
    response.set_cookie(
        key=config.cookie_name,
        value=token,
        max_age=int(max_age),
        path="/",
        httponly=True,
        secure=bool(config.cookie_secure),
        samesite=config.cookie_samesite,
    )


def _clear_session_cookie(response, config: AuthConfig) -> None:
    response.delete_cookie(
        key=config.cookie_name,
        path="/",
        httponly=True,
        secure=bool(config.cookie_secure),
        samesite=config.cookie_samesite,
    )


def _client_meta(request: Request) -> tuple[str, str]:
    ip = client_ip(request.scope, trust_proxy=_trust_proxy)
    ua = request.headers.get("user-agent", "") or ""
    return ip, ua


# ── the login page ────────────────────────────────────────────────
def _inject_banner(html: str, banner: str) -> str:
    """Inject an operator banner (TEST/STAGING marker) into the page.

    Lives here rather than in a template engine so the login page stays a
    plain static asset that production serves byte-for-byte.
    """
    if not banner:
        return html
    mark = (
        '<div id="envBanner" style="position:fixed;top:0;left:0;right:0;'
        'z-index:99999;background:#7f1d1d;color:#fff;'
        'font:700 12px/1.5 monospace;padding:7px 12px;text-align:center;'
        'letter-spacing:2px;">' + banner + '</div>'
    )
    if "</body>" in html:
        return html.replace("</body>", mark + "</body>", 1)
    return mark + html


@router.get("/login", include_in_schema=False)
def login_page(request: Request, next: str = "/"):
    """Serve the login page.  A live session short-circuits to the target."""
    config = get_config()
    target = safe_next(next, fallback="/")
    token = request.cookies.get(config.cookie_name)
    service = _service
    if service is not None and token:
        try:
            if service.validate(token) is not None:
                return RedirectResponse(target, status_code=303)
        except Exception:  # pragma: no cover - fall through to the page
            _log.exception("auth_login_page_session_check_failed")
    if _LOGIN_HTML.is_file():
        resp = FileResponse(str(_LOGIN_HTML), media_type="text/html")
        resp.headers["Cache-Control"] = "no-store"
        if config.login_banner:
            html = _LOGIN_HTML.read_text(encoding="utf-8")
            return HTMLResponse(
                content=_inject_banner(html, config.login_banner),
                status_code=200,
                headers={"Cache-Control": "no-store"})
        return resp
    raise HTTPException(status_code=404, detail="login page missing")


# ── login ─────────────────────────────────────────────────────────
def _login_payload(payload: dict) -> tuple[str, str, bool, str]:
    identifier = (payload.get("username") or payload.get("email")
                  or payload.get("identifier") or "")
    password = payload.get("password") or ""
    remember = bool(payload.get("remember"))
    nxt = safe_next(payload.get("next"), fallback="/")
    return str(identifier), str(password), remember, nxt


async def _read_login_payload(request: Request) -> tuple[dict, bool]:
    """Parse the login body as JSON or as an HTML form POST.

    Returns ``(payload, is_form)``.  Accepting ``application/x-www-form-urlencoded``
    keeps the page working with JavaScript disabled (and makes the endpoint
    reachable from curl for smoke tests) without pulling in a multipart
    dependency.
    """
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype == "application/x-www-form-urlencoded":
        # Parsed with parse_qsl rather than ``request.form()`` so the login
        # endpoint needs no multipart dependency — the deployment's package
        # set is deliberately left untouched by this feature.
        body = (await request.body()).decode("utf-8", "replace")
        allowed = {"username", "email", "identifier", "password",
                   "remember", "next"}
        pairs = parse_qsl(body, keep_blank_values=True)
        return {k: v for k, v in pairs if k in allowed}, True
    try:
        body = await request.json()
        return (body if isinstance(body, dict) else {}), False
    except Exception:
        return {}, False


def _wants_html(request: Request, is_form: bool) -> bool:
    if is_form:
        return True
    accept = request.headers.get("accept", "") or ""
    return "text/html" in accept and "application/json" not in accept


@router.post("/api/auth/login")
async def auth_login(request: Request):
    service = get_service()
    config = get_config()
    raw, is_form = await _read_login_payload(request)
    html_mode = _wants_html(request, is_form)

    if is_form:
        raw = dict(raw)
        raw["remember"] = str(raw.get("remember", "")).lower() in (
            "1", "true", "yes", "on")
    identifier, password, remember, nxt = _login_payload(raw or {})

    def _html_fail(code: str, nxt_value: str = "/"):
        target = f"{config.login_path}?error={code}"
        if nxt_value and nxt_value != "/":
            target += f"&next={quote(nxt_value, safe='/')}"
        return RedirectResponse(target, status_code=303,
                                headers={"Cache-Control": "no-store"})

    if not identifier.strip() or not password:
        # a validation failure, not a credential failure — but still generic
        # about *which* field was empty beyond naming the pair
        if html_mode:
            return _html_fail("validation", nxt)
        return JSONResponse(
            status_code=400,
            content={"detail": "Username and password are required"},
            headers={"Cache-Control": "no-store"})

    ip, ua = _client_meta(request)
    try:
        result = service.login(identifier, password, remember=remember,
                               ip=ip, user_agent=ua)
    except Exception:
        _log.exception("auth_login_failed_unexpected")
        if html_mode:
            return _html_fail("unavailable", nxt)
        return JSONResponse(
            status_code=502,
            content={"detail": "Authentication service unavailable"},
            headers={"Cache-Control": "no-store"})

    if not result.ok:
        if html_mode:
            return _html_fail("rate" if result.reason == "rate_limited"
                              else "invalid", nxt)
        status = 429 if result.reason == "rate_limited" else 401
        body = {"detail": result.message or GENERIC_LOGIN_ERROR}
        headers = {"Cache-Control": "no-store"}
        if result.retry_after_s:
            headers["Retry-After"] = str(int(result.retry_after_s))
        return JSONResponse(status_code=status, content=body, headers=headers)

    if html_mode:
        resp = RedirectResponse(nxt, status_code=303,
                                headers={"Cache-Control": "no-store"})
        _set_session_cookie(resp, config, result.token or "",
                            max_age=result.expires_in)
        return resp
    resp = JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "user": result.user,
            "role": (result.user or {}).get("role"),
            "csrf_token": result.csrf_token,
            "expires_in": result.expires_in,
            "redirect": nxt,
        },
        headers={"Cache-Control": "no-store"})
    _set_session_cookie(resp, config, result.token or "",
                        max_age=result.expires_in)
    return resp


# ── logout ────────────────────────────────────────────────────────
@router.post("/api/auth/logout")
def auth_logout(request: Request):
    config = get_config()
    service = get_service()
    token = request.cookies.get(config.cookie_name)
    revoked = False
    try:
        revoked = service.logout(token)
    except Exception:  # pragma: no cover
        _log.exception("auth_logout_failed")
    resp = JSONResponse(
        status_code=200,
        content={"ok": True, "revoked": bool(revoked)},
        headers={"Cache-Control": "no-store"})
    # clear regardless: an unreadable/absent cookie must still be removed
    _clear_session_cookie(resp, config)
    return resp


# ── identity ──────────────────────────────────────────────────────
@router.get("/api/auth/me")
def auth_me(request: Request):
    session = current_session(request)
    return JSONResponse(status_code=200,
                        content=session.as_public(),
                        headers={"Cache-Control": "no-store"})


@router.get("/api/auth/admin/probe")
def auth_admin_probe(request: Request):
    """Minimal role-enforcement surface.

    Deliberately carries no administrative functionality — it exists so the
    admin role can be proven to be enforced server-side.  A ``user`` session
    receives 403.
    """
    session: SessionView = require_admin(request)
    return {"ok": True, "username": session.username, "role": session.role}
