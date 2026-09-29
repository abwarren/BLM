"""AUTHENTICATION — LOGIN (directive §9 "Login" + §3).

One named test per requirement:

    valid admin credentials   → success
    valid user credentials    → success
    invalid username          → failure
    invalid password          → failure
    empty username            → validation failure
    empty password            → validation failure

plus the behaviours §3/§8 require of the endpoint (generic error, cookie
flags, throttling, no credential in the response).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# repo convention: make tests/ importable so the shared auth harness in
# conftest.py can be reused by name
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import login

GENERIC = "Invalid username or password"
VALIDATION = "Username and password are required"


# ── valid credentials ─────────────────────────────────────────────
def test_login_valid_admin_credentials_succeed(auth_stack):
    r = login(auth_stack["client"], auth_stack["admin"], auth_stack["admin_pw"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["user"]["username"] == auth_stack["admin"]
    assert body["role"] == "admin"


def test_login_valid_user_credentials_succeed(auth_stack):
    r = login(auth_stack["client"], auth_stack["user"], auth_stack["user_pw"])
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "user"


# ── invalid credentials ───────────────────────────────────────────
def test_login_invalid_username_fails(auth_stack):
    r = login(auth_stack["client"], "no-such-operator", "whatever")
    assert r.status_code == 401
    assert r.json()["detail"] == GENERIC


def test_login_invalid_password_fails(auth_stack):
    r = login(auth_stack["client"], auth_stack["admin"], "definitely-wrong")
    assert r.status_code == 401
    assert r.json()["detail"] == GENERIC


# ── empty field validation ────────────────────────────────────────
def test_login_empty_username_is_validation_failure(auth_stack):
    r = login(auth_stack["client"], "", auth_stack["admin_pw"])
    assert r.status_code == 400
    assert r.json()["detail"] == VALIDATION


def test_login_empty_password_is_validation_failure(auth_stack):
    r = login(auth_stack["client"], auth_stack["admin"], "")
    assert r.status_code == 400
    assert r.json()["detail"] == VALIDATION


def test_login_whitespace_only_username_is_validation_failure(auth_stack):
    r = login(auth_stack["client"], "   ", auth_stack["admin_pw"])
    assert r.status_code == 400
    assert r.json()["detail"] == VALIDATION


# ── §3 generic error: every failure mode is indistinguishable ─────
def test_login_failure_message_is_identical_for_unknown_user_and_bad_password(
        auth_stack):
    client = auth_stack["client"]
    unknown = login(client, "ghost-operator", "x")
    wrong = login(client, auth_stack["admin"], "x")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json() == {"detail": GENERIC}
    # byte-identical bodies: no size side-channel either
    assert unknown.content == wrong.content


def test_login_account_without_password_hash_fails_generically(auth_stack):
    """A damaged/absent stored hash must not become a distinct error."""
    store = auth_stack["state"]["store"]
    row = store.get_user_by_username(auth_stack["user"])
    store.set_password(row["id"], "")
    r = login(auth_stack["client"], auth_stack["user"], auth_stack["user_pw"])
    assert r.status_code == 401 and r.json()["detail"] == GENERIC


# ── §8 no credential material in the response ─────────────────────
def test_login_success_response_contains_no_password_or_hash(auth_stack):
    r = login(auth_stack["client"], auth_stack["admin"], auth_stack["admin_pw"])
    raw = r.text.lower()
    assert auth_stack["admin_pw"].lower() not in raw
    for forbidden in ("password", "password_hash", "hash", "bcrypt",
                      "token_hash", "$2b$", "blm_session="):
        assert forbidden not in raw, f"{forbidden!r} leaked in the login body"


def test_login_error_response_contains_no_internal_detail(auth_stack):
    r = login(auth_stack["client"], auth_stack["admin"], "nope")
    body = r.json()
    assert set(body) == {"detail"}
    for forbidden in ("traceback", "sqlite", "bcrypt", "hash", "file \""):
        assert forbidden not in json.dumps(body).lower()


# ── cookie contract ───────────────────────────────────────────────
def test_login_sets_httponly_samesite_session_cookie(auth_stack):
    r = login(auth_stack["client"], auth_stack["admin"], auth_stack["admin_pw"])
    raw = r.headers.get("set-cookie", "")
    assert "HttpOnly" in raw
    assert "SameSite=lax" in raw.lower().replace("samesite=lax", "SameSite=lax")
    assert "Path=/" in raw


def test_login_cookie_is_secure_when_secure_cookies_are_configured(
        auth_stack, monkeypatch):
    """In production (HTTPS behind the reverse proxy) the cookie is Secure."""
    from blm_v4.auth.config import AuthConfig
    monkeypatch.setenv("BLM_ENV", "production")
    monkeypatch.setenv("BLM_AUTH_COOKIE_SECURE", "1")
    assert AuthConfig.from_env("/tmp").cookie_secure is True


def test_login_remember_me_extends_the_absolute_lifetime(auth_stack):
    short = login(auth_stack["client"], auth_stack["admin"],
                  auth_stack["admin_pw"])
    long_ = login(auth_stack["client"], auth_stack["admin"],
                  auth_stack["admin_pw"], remember=True)
    assert long_.json()["expires_in"] > short.json()["expires_in"]


def test_remember_me_cookie_is_persistent(auth_stack):
    r = login(auth_stack["client"], auth_stack["admin"], auth_stack["admin_pw"],
              remember=True)
    assert "Max-Age=" in r.headers.get("set-cookie", "")


# ── identifier handling ───────────────────────────────────────────
def test_login_accepts_an_email_address_as_the_identifier(auth_stack):
    r = login(auth_stack["client"], "admin@blm.test", auth_stack["admin_pw"])
    assert r.status_code == 200
    assert r.json()["user"]["username"] == auth_stack["admin"]


def test_login_username_is_case_insensitive(auth_stack):
    r = login(auth_stack["client"], auth_stack["admin"].upper(),
              auth_stack["admin_pw"])
    assert r.status_code == 200


# ── throttling (§8) ───────────────────────────────────────────────
def test_login_throttles_repeated_failures(auth_stack):
    client = auth_stack["client"]
    codes = [login(client, auth_stack["user"], "wrong").status_code
             for _ in range(10)]
    assert 429 in codes, f"no throttle after 10 failures: {codes}"
    assert codes[-1] == 429
    r = login(client, auth_stack["user"], "wrong")
    assert r.status_code == 429
    assert r.headers.get("Retry-After")
    assert "Invalid username or password" not in r.json()["detail"]


def test_throttling_is_case_insensitive_on_the_identity(auth_stack):
    client = auth_stack["client"]
    for _ in range(10):
        login(client, auth_stack["user"].upper(), "wrong")
    assert login(client, auth_stack["user"], "wrong").status_code == 429


# ── failure isolation ─────────────────────────────────────────────
def test_internal_login_failure_returns_a_generic_message(auth_stack,
                                                          monkeypatch):
    """An unexpected server fault must not surface internals to the caller."""
    service = auth_stack["state"]["service"]

    def _boom(*a, **k):
        raise RuntimeError("internal detail /etc/blm/secret-path")

    monkeypatch.setattr(service, "login", _boom)
    r = login(auth_stack["client"], auth_stack["admin"], auth_stack["admin_pw"])
    assert r.status_code == 502
    assert r.json() == {"detail": "Authentication service unavailable"}


# ── post-login navigation ─────────────────────────────────────────
def test_authenticated_visit_to_login_redirects_to_the_dashboard(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["admin"], auth_stack["admin_pw"])
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_login_rejects_an_offsite_next_parameter(auth_stack):
    r = login(auth_stack["client"], auth_stack["admin"], auth_stack["admin_pw"],
              next="//evil.example.com/steal")
    assert r.json()["redirect"] == "/"


def test_login_honours_a_same_site_next_parameter(auth_stack):
    r = login(auth_stack["client"], auth_stack["admin"], auth_stack["admin_pw"],
              next="/api/v4/live")
    assert r.json()["redirect"] == "/api/v4/live"


# ── no-JavaScript form POST ───────────────────────────────────────
def test_form_post_login_redirects_and_sets_the_cookie(auth_stack):
    client = auth_stack["client"]
    r = client.post("/api/auth/login",
                    data={"username": auth_stack["admin"],
                          "password": auth_stack["admin_pw"]},
                    headers={"Content-Type":
                             "application/x-www-form-urlencoded"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "HttpOnly" in r.headers.get("set-cookie", "")


def test_form_post_login_failure_redirects_with_a_generic_error_code(
        auth_stack):
    client = auth_stack["client"]
    r = client.post("/api/auth/login",
                    data={"username": auth_stack["admin"], "password": "nope"},
                    headers={"Content-Type":
                             "application/x-www-form-urlencoded"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?error=invalid"
    # the password never travels back in the URL
    assert "nope" not in r.headers["location"]


# ── §7 already-authenticated flow ─────────────────────────────────
def test_me_endpoint_reports_the_authenticated_identity(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["admin"], auth_stack["admin_pw"])
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["username"] == auth_stack["admin"]
    assert body["role"] == "admin"
    assert body["csrf_token"]


def test_me_endpoint_is_rejected_without_a_session(auth_stack):
    r = auth_stack["client"].get("/api/auth/me")
    assert r.status_code == 401
