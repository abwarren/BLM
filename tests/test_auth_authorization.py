"""AUTHENTICATION — AUTHORIZATION (§6 protected routes, §9 "Authorization").

One named test per requirement:

    unauthenticated dashboard request  → rejected/redirected
    authenticated admin                → dashboard access
    authenticated user                 → dashboard access
    unauthenticated protected API      → rejected
    logout                             → session invalidated

plus direct-URL bypass attempts, role enforcement, CSRF, WebSocket
compatibility and the public allow-list the login page needs.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import ADMIN_USERNAME, login

#: every surface the directive lists as PROTECTED
PROTECTED_PATHS = [
    "/",
    "/dashboard",
    "/dashboard/",
    "/api/v4/live",
    "/api/v4/status",
    "/api/v4/betting/status",
    "/api/v2/status",
    "/metrics",
    "/docs",
    "/openapi.json",
]

#: surfaces that must stay reachable so the login flow can work at all
PUBLIC_PATHS = [
    "/login",
    "/static/login.css",
    "/static/login.js",
    "/static/dashboard.js",
    "/healthz",
]


# ── unauthenticated access ────────────────────────────────────────
def test_unauthenticated_dashboard_request_is_redirected_to_login(auth_stack):
    r = auth_stack["client"].get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_unauthenticated_dashboard_alias_is_redirected_to_login(auth_stack):
    r = auth_stack["client"].get("/dashboard", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_redirect_preserves_the_requested_destination(auth_stack):
    r = auth_stack["client"].get("/dashboard", follow_redirects=False)
    assert "next=/dashboard" in r.headers["location"]


def test_unauthenticated_protected_api_request_is_rejected(auth_stack):
    r = auth_stack["client"].get("/api/v4/live")
    assert r.status_code == 401
    assert r.json() == {"detail": "Authentication required"}
    assert r.headers.get("cache-control") == "no-store"


def test_unauthenticated_api_request_is_never_served_the_login_page(auth_stack):
    """An XHR client must get JSON, not HTML — no half-parsed responses."""
    r = auth_stack["client"].get("/api/v4/live")
    assert "text/html" not in r.headers.get("content-type", "")


def test_authentication_cannot_be_bypassed_by_direct_url_access(auth_stack):
    """The guard is on the app, not on the links — every protected path
    refuses an anonymous caller (no route opts out)."""
    client = auth_stack["client"]
    for path in PROTECTED_PATHS:
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (401, 303), f"{path} served {r.status_code}"
        if r.status_code == 303:
            assert r.headers["location"].startswith("/login"), path


def test_public_paths_do_not_require_a_session(auth_stack):
    client = auth_stack["client"]
    for path in PUBLIC_PATHS:
        r = client.get(path, follow_redirects=False)
        assert r.status_code != 401, f"{path} unexpectedly required auth"
        assert r.status_code != 303, f"{path} unexpectedly redirected"


# ── authenticated access ──────────────────────────────────────────
def test_authenticated_admin_is_granted_dashboard_access(auth_stack):
    client = auth_stack["client"]
    assert login(client, auth_stack["admin"],
                 auth_stack["admin_pw"]).status_code == 200
    r = client.get("/")
    assert r.status_code == 200
    assert "BLM DASHBOARD" in r.text


def test_authenticated_user_is_granted_dashboard_access(auth_stack):
    client = auth_stack["client"]
    assert login(client, auth_stack["user"],
                 auth_stack["user_pw"]).status_code == 200
    r = client.get("/")
    assert r.status_code == 200
    assert "BLM DASHBOARD" in r.text


def test_authenticated_caller_reaches_the_protected_api(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["user"], auth_stack["user_pw"])
    r = client.get("/api/v4/live")
    assert r.status_code == 200
    assert "games" in r.json()


# ── roles (§4) ────────────────────────────────────────────────────
def test_admin_role_is_granted_administrative_access(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["admin"], auth_stack["admin_pw"])
    r = client.get("/api/auth/admin/probe")
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_user_role_is_denied_administrative_access(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["user"], auth_stack["user_pw"])
    r = client.get("/api/auth/admin/probe")
    assert r.status_code == 403
    assert r.json()["detail"] == "Insufficient role"


def test_role_is_enforced_server_side_and_ignores_client_claims(auth_stack):
    """A user session cannot promote itself with a header, body or query."""
    client = auth_stack["client"]
    login(client, auth_stack["user"], auth_stack["user_pw"])
    for attempt in (
            {"X-BLM-Role": "admin"},
            {"X-User-Role": "admin"},
            {"X-Forwarded-User": "admin"},
    ):
        r = client.get("/api/auth/admin/probe", headers=attempt)
        assert r.status_code == 403, attempt
    r = client.get("/api/auth/admin/probe?role=admin")
    assert r.status_code == 403


def test_role_is_not_taken_from_the_session_snapshot_after_a_role_change(
        auth_stack):
    """The user row is the authority: demoting an admin takes effect on the
    very next request, without a re-login."""
    client = auth_stack["client"]
    login(client, auth_stack["admin"], auth_stack["admin_pw"])
    assert client.get("/api/auth/admin/probe").status_code == 200
    store = auth_stack["state"]["store"]
    row = store.get_user_by_username(auth_stack["admin"])
    store.set_role(row["id"], "user")
    assert client.get("/api/auth/admin/probe").status_code == 403


# ── CSRF (§8) ─────────────────────────────────────────────────────
def test_mutating_request_without_a_csrf_token_is_rejected(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["admin"], auth_stack["admin_pw"])
    r = client.post("/api/v4/betting/settings", json={"unit_price": 2.0})
    assert r.status_code == 403
    assert r.json() == {"detail": "CSRF validation failed"}


def test_mutating_request_with_a_wrong_csrf_token_is_rejected(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["admin"], auth_stack["admin_pw"])
    r = client.post("/api/v4/betting/settings", json={"unit_price": 2.0},
                    headers={"X-CSRF-Token": "not-the-token"})
    assert r.status_code == 403


def test_mutating_request_with_the_session_csrf_token_is_accepted(auth_stack):
    client = auth_stack["client"]
    resp = login(client, auth_stack["admin"], auth_stack["admin_pw"])
    r = client.post("/api/v4/betting/settings", json={"unit_price": 2.0},
                    headers={"X-CSRF-Token": resp.json()["csrf_token"]})
    assert r.status_code == 200


def test_read_requests_do_not_require_a_csrf_token(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["user"], auth_stack["user_pw"])
    assert client.get("/api/v4/live").status_code == 200


# ── logout (§3) ───────────────────────────────────────────────────
def test_logout_invalidates_the_session(auth_stack):
    client = auth_stack["client"]
    resp = login(client, auth_stack["admin"], auth_stack["admin_pw"])
    csrf = resp.json()["csrf_token"]
    assert client.get("/api/v4/live").status_code == 200

    r = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["revoked"] is True

    # the SAME client (same cookie jar) is now anonymous
    assert client.get("/api/v4/live").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_logout_clears_the_cookie(auth_stack):
    client = auth_stack["client"]
    resp = login(client, auth_stack["admin"], auth_stack["admin_pw"])
    r = client.post("/api/auth/logout",
                    headers={"X-CSRF-Token": resp.json()["csrf_token"]})
    set_cookie = r.headers.get("set-cookie", "")
    assert "blm_session=" in set_cookie
    assert "Max-Age=0" in set_cookie or 'expires=' in set_cookie.lower()


def test_logout_revokes_the_server_side_session_row(auth_stack):
    client = auth_stack["client"]
    resp = login(client, auth_stack["admin"], auth_stack["admin_pw"])
    store = auth_stack["state"]["store"]
    assert store.active_session_count() == 1
    client.post("/api/auth/logout",
                headers={"X-CSRF-Token": resp.json()["csrf_token"]})
    assert store.active_session_count() == 0
    row = store.recent_audit(5)
    assert any(a["event"] == "logout" for a in row)


def test_logout_requires_csrf_so_it_cannot_be_forced_cross_site(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["admin"], auth_stack["admin_pw"])
    r = client.post("/api/auth/logout")
    assert r.status_code == 403
    # and the session still works — the forced logout did not take effect
    assert client.get("/api/v4/live").status_code == 200


# ── WebSocket compatibility (§6) ──────────────────────────────────
def test_unauthenticated_websocket_handshake_is_rejected(auth_stack):
    from starlette.websockets import WebSocketDisconnect
    try:
        with auth_stack["client"].websocket_connect("/ws") as ws:
            ws.receive_text()
        raise AssertionError("anonymous WebSocket was accepted")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008, exc.code


def test_authenticated_websocket_handshake_is_accepted(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["admin"], auth_stack["admin_pw"])
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "hello"


# ── session lifecycle (§3) ────────────────────────────────────────
def test_deactivating_a_user_revokes_their_live_session(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["user"], auth_stack["user_pw"])
    assert client.get("/api/v4/live").status_code == 200
    store = auth_stack["state"]["store"]
    row = store.get_user_by_username(auth_stack["user"])
    store.set_active(row["id"], False)
    assert client.get("/api/v4/live").status_code == 401


def test_a_deactivated_account_cannot_authenticate(auth_stack):
    store = auth_stack["state"]["store"]
    row = store.get_user_by_username(auth_stack["user"])
    store.set_active(row["id"], False)
    r = login(auth_stack["client"], auth_stack["user"], auth_stack["user_pw"])
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid username or password"


def test_an_unknown_random_cookie_is_not_a_session(auth_stack):
    client = auth_stack["client"]
    client.cookies.set("blm_session", "a" * 43)
    assert client.get("/api/v4/live").status_code == 401


# ── composition order parity with server.py ───────────────────────
def test_guard_covers_mounted_sub_apps_and_leaves_static_public(
        auth_root, tmp_path):
    """server.py mounts /static and a /dashboard sub-app BEFORE installing
    the guard.  Reproduce that exact order and prove the mounted app is
    protected while the static mount stays public (the login page needs its
    CSS/JS before a session exists).
    """
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.testclient import TestClient

    from blm_v4.auth import install as install_auth
    from blm_v4.auth.config import AuthConfig
    from blm_v4.auth.seed import seed
    from blm_v4.auth.store import AuthStore

    root = auth_root["root"]
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "login.css").write_text("/* css */")
    (static_dir / "dashboard.js").write_text("// js")

    sub = FastAPI(title="v2 dashboard")

    @sub.get("/")
    def sub_home():
        return HTMLResponse("<h1>V2 DASHBOARD</h1>")

    app = FastAPI()
    app.mount("/dashboard", sub)                    # mounted BEFORE install
    app.mount("/static", StaticFiles(directory=str(static_dir)),
              name="blm_static")

    @app.get("/")
    def home():
        return HTMLResponse("<h1>BLM DASHBOARD</h1>")

    config = AuthConfig.from_env(root)
    seed(AuthStore(config.db_path), rounds=4,
         accounts=[{"username": ADMIN_USERNAME, "password": auth_root["admin_pw"],
                    "role": "admin", "email": None, "label": "admin"}],
         out=open(os.devnull, "w"))
    install_auth(app, root, trust_proxy=False)
    client = TestClient(app)

    # the mounted sub-app is behind the guard …
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/dashboard/api/anything",
                      follow_redirects=False).status_code in (401, 303)
    # … the static mount is not (the login page must be able to load)
    assert client.get("/static/login.css").status_code == 200
    assert client.get("/static/dashboard.js").status_code == 200
    # … and the guard is applied to the page route too
    assert client.get("/", follow_redirects=False).status_code == 303

    # after login everything is reachable
    resp = login(client, ADMIN_USERNAME, auth_root["admin_pw"])
    assert resp.status_code == 200
    assert client.get("/dashboard").status_code == 200
    assert client.get("/").status_code == 200
