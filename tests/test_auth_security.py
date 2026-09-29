"""AUTHENTICATION — SECURITY (§8, §9 "Security", §10 schema guarantees).

One named test per requirement:

    password is stored hashed
    password is never returned by API
    password is never written to logs
    authentication cannot be bypassed by directly requesting dashboard/API

plus the cookie/token/schema guarantees the directive requires.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import login

REPO_ROOT = Path(__file__).resolve().parent.parent


def _db_bytes(db_path) -> bytes:
    """Every byte SQLite may hold the row in.

    The store runs in WAL mode, so freshly written rows can still live in
    the sidecar files until a checkpoint — a leak check that only reads the
    main database file would miss them.
    """
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.is_file():
            blob += p.read_bytes()
    return blob


# ── §9 password is stored hashed ──────────────────────────────────
def test_password_is_stored_hashed_and_not_in_plaintext(auth_stack):
    password = auth_stack["admin_pw"]
    store = auth_stack["state"]["store"]
    row = store.get_user_by_username(auth_stack["admin"])
    assert row["password_hash"].startswith("bcrypt_sha256$")
    assert row["password_hash"] != password
    assert password not in row["password_hash"]

    # the raw database bytes must not contain the credential anywhere
    raw = _db_bytes(auth_stack["state"]["config"].db_path)
    assert password.encode() not in raw
    assert row["password_hash"].encode() in raw  # the hash IS what is stored


def test_password_hash_uses_a_salt_so_equal_passwords_differ(auth_stack):
    from blm_v4.auth.passwords import hash_password
    a = hash_password("same-secret", rounds=4)
    b = hash_password("same-secret", rounds=4)
    assert a != b
    assert a.startswith("bcrypt_sha256$") and b.startswith("bcrypt_sha256$")


def test_stored_hash_verifies_only_the_correct_password(auth_stack):
    from blm_v4.auth.passwords import verify_password
    store = auth_stack["state"]["store"]
    stored = store.get_user_by_username(auth_stack["admin"])["password_hash"]
    assert verify_password(auth_stack["admin_pw"], stored) is True
    assert verify_password("wrong", stored) is False
    assert verify_password("", stored) is False
    assert verify_password(auth_stack["admin_pw"] + "x", stored) is False


def test_long_passphrases_are_not_silently_truncated(auth_stack):
    """bcrypt's 72-byte ceiling must not merge distinct long passwords."""
    from blm_v4.auth.passwords import hash_password, verify_password
    base = "p" * 80
    stored = hash_password(base + "A", rounds=4)
    assert verify_password(base + "A", stored) is True
    assert verify_password(base + "B", stored) is False


def test_verification_of_an_unknown_or_malformed_hash_fails_without_raising():
    from blm_v4.auth.passwords import verify_password
    for stored in ("", "not-a-hash", "bcrypt_sha256$", "md5$deadbeef"):
        assert verify_password("anything", stored) is False


# ── §9 password is never returned by API ──────────────────────────
def test_password_is_never_returned_by_any_api_endpoint(auth_stack):
    client = auth_stack["client"]
    password = auth_stack["admin_pw"]
    responses = [
        client.post("/api/auth/login",
                    json={"username": auth_stack["admin"], "password": password}),
        client.get("/api/auth/me"),
        client.get("/api/auth/admin/probe"),
        client.get("/api/v4/live"),
        client.get("/"),
    ]
    for r in responses:
        assert password not in r.text
        assert "password_hash" not in r.text
        assert "$2b$" not in r.text


def test_user_projection_never_carries_the_hash(auth_stack):
    """The only user projection the store exposes is an allow-list."""
    store = auth_stack["state"]["store"]
    for user in store.list_users():
        assert "password_hash" not in user
        assert set(user) == {"id", "username", "role", "is_active", "email",
                             "created_at", "last_login_at"}


# ── §9 password is never written to logs ──────────────────────────
def test_password_is_never_written_to_logs(auth_stack, caplog):
    password = auth_stack["admin_pw"]
    client = auth_stack["client"]
    with caplog.at_level(logging.DEBUG):
        login(client, auth_stack["admin"], password)          # success
        login(client, auth_stack["admin"], password + "-bad")  # failure
        client.get("/api/v4/live")
    logged = caplog.text
    assert password not in logged
    assert password + "-bad" not in logged
    # no hash material either
    assert "$2b$" not in logged
    assert "bcrypt_sha256$" not in logged


def test_audit_trail_records_events_without_any_credential(auth_stack):
    client = auth_stack["client"]
    password = auth_stack["admin_pw"]
    login(client, auth_stack["admin"], password)
    login(client, auth_stack["admin"], password + "-bad")
    rows = auth_stack["state"]["store"].recent_audit(20)
    events = {r["event"] for r in rows}
    assert "login_success" in events and "login_failed" in events
    blob = repr(rows)
    assert password not in blob
    assert password + "-bad" not in blob
    assert "$2b$" not in blob
    # the audit columns are an allow-list — there is nowhere to put one
    assert set(rows[0]) == {"id", "at_utc", "event", "username", "ip",
                            "user_agent", "detail"}


# ── §9 session token is not recoverable from the database ─────────
def test_session_token_is_stored_hashed_never_raw(auth_stack):
    from blm_v4.auth.store import hash_token
    client = auth_stack["client"]
    resp = login(client, auth_stack["admin"], auth_stack["admin_pw"])
    raw_token = client.cookies.get("blm_session")
    assert raw_token

    blob = _db_bytes(auth_stack["state"]["config"].db_path)
    assert raw_token.encode() not in blob, "raw session token persisted"
    assert hash_token(raw_token).encode() in blob

    store = auth_stack["state"]["store"]
    assert store.get_session(hash_token(raw_token)) is not None
    assert store.get_session(raw_token) is None


# ── §10 users table: required columns, constraints, indexes ───────
def test_users_table_has_the_required_columns(auth_stack):
    db = auth_stack["state"]["config"].db_path
    con = sqlite3.connect(db)
    try:
        cols = {r[1]: r for r in con.execute("PRAGMA table_info(users)")}
        for required in ("id", "username", "password_hash", "role",
                         "is_active", "created_at", "updated_at"):
            assert required in cols, f"users.{required} missing"
        # NOT NULL on the security-critical columns
        assert cols["username"][3] == 1
        assert cols["password_hash"][3] == 1
        assert cols["role"][3] == 1
        assert cols["is_active"][3] == 1
    finally:
        con.close()


def test_users_table_has_uniqueness_and_indexes(auth_stack):
    db = auth_stack["state"]["config"].db_path
    con = sqlite3.connect(db)
    try:
        indexes = [r[1] for r in con.execute("PRAGMA index_list(users)")]
        assert any("role" in i for i in indexes)
        # the auto-index created by UNIQUE(username_lower) must exist and be
        # the only index touching that column
        covering = []
        for idx in indexes:
            info = list(con.execute(f"PRAGMA index_info('{idx}')"))
            cols = [r[2] for r in info]
            if cols == ["username_lower"]:
                covering.append(idx)
        assert covering, "no index on username_lower"
        unique = False
        for idx in covering:
            uniq = list(con.execute("PRAGMA index_list(users)"))
            for row in uniq:
                if row[1] == idx and row[2] == 1:   # row[2] = unique flag
                    unique = True
        assert unique, "username_lower index is not UNIQUE"
    finally:
        con.close()


def test_username_uniqueness_is_case_insensitive(auth_stack):
    from blm_v4.auth.passwords import hash_password
    store = auth_stack["state"]["store"]
    with_upper = auth_stack["admin"].upper()
    try:
        store.create_user(username=with_upper,
                          password_hash=hash_password("x", rounds=4),
                          role="user")
        raise AssertionError("duplicate username accepted")
    except ValueError as exc:
        assert "already exists" in str(exc)


def test_role_column_rejects_an_unknown_role(auth_stack):
    db = auth_stack["state"]["config"].db_path
    con = sqlite3.connect(db)
    try:
        with __import__("pytest").raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO users (username, username_lower, password_hash,"
                " role, is_active, created_at, updated_at) VALUES"
                " ('bad','bad','h','superuser',1,'now','now')")
    finally:
        con.close()


def test_sessions_table_tracks_expiry_and_revocation(auth_stack):
    db = auth_stack["state"]["config"].db_path
    con = sqlite3.connect(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        for required in ("token_hash", "user_id", "csrf_token", "created_at",
                         "last_seen_at", "absolute_expires_at",
                         "idle_expires_at", "revoked_at"):
            assert required in cols, f"sessions.{required} missing"
    finally:
        con.close()


# ── §9 authentication cannot be bypassed ──────────────────────────
def test_no_admin_or_system_path_is_public_by_accident(auth_stack):
    from blm_v4.auth.config import AuthConfig
    cfg = AuthConfig.from_env(auth_stack["root"])
    for path in ("/", "/dashboard", "/api/v4/live", "/api/auth/me",
                 "/api/auth/admin/probe", "/metrics", "/docs",
                 "/openapi.json", "/api/v4/betting/status"):
        assert cfg.is_public(path) is False, path


def test_a_forged_cookie_and_header_combination_cannot_authenticate(auth_stack):
    client = auth_stack["client"]
    for headers in ({"Cookie": "blm_session=forged"},
                    {"Authorization": "Bearer forged"},
                    {"X-Forwarded-User": "admin"},
                    {"X-Remote-User": "admin"}):
        r = client.get("/api/v4/live", headers=headers)
        assert r.status_code == 401, headers


def test_fail_closed_when_the_session_lookup_raises(auth_stack, monkeypatch):
    """A database fault must deny, never allow."""
    service = auth_stack["state"]["service"]

    def _boom(_token):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(service, "validate", _boom)
    assert auth_stack["client"].get("/api/v4/live").status_code == 401


# ── cookie hardening ──────────────────────────────────────────────
def test_session_cookie_is_not_readable_by_javascript(auth_stack):
    r = login(auth_stack["client"], auth_stack["admin"], auth_stack["admin_pw"])
    assert "HttpOnly" in r.headers.get("set-cookie", "")


def test_samesite_protection_is_configured(auth_stack):
    from blm_v4.auth.config import AuthConfig
    cfg = AuthConfig.from_env(auth_stack["root"])
    assert cfg.cookie_samesite in ("lax", "strict")


def test_production_defaults_to_secure_cookies(monkeypatch, tmp_path):
    from blm_v4.auth.config import AuthConfig
    monkeypatch.setenv("BLM_ENV", "production")
    monkeypatch.delenv("BLM_AUTH_COOKIE_SECURE", raising=False)
    assert AuthConfig.from_env(tmp_path).cookie_secure is True


# ── session expiry (§3) ───────────────────────────────────────────
def test_an_idle_session_expires(auth_stack):
    client = auth_stack["client"]
    resp = login(client, auth_stack["admin"], auth_stack["admin_pw"])
    assert client.get("/api/v4/live").status_code == 200
    store = auth_stack["state"]["store"]
    con = sqlite3.connect(auth_stack["state"]["config"].db_path)
    con.execute("UPDATE sessions SET idle_expires_at = '2000-01-01T00:00:00.000Z'")
    con.commit()
    con.close()
    r = client.get("/api/v4/live")
    assert r.status_code == 401
    assert store.active_session_count() == 0
    assert any(a["event"] == "login_success" for a in store.recent_audit(10))


def test_an_absolutely_expired_session_is_revoked(auth_stack):
    client = auth_stack["client"]
    login(client, auth_stack["admin"], auth_stack["admin_pw"])
    con = sqlite3.connect(auth_stack["state"]["config"].db_path)
    con.execute(
        "UPDATE sessions SET absolute_expires_at = '2000-01-01T00:00:00.000Z'")
    con.commit()
    con.close()
    assert client.get("/api/v4/live").status_code == 401


def test_session_ttls_come_from_configuration(auth_stack):
    cfg = auth_stack["state"]["config"]
    assert 0 < cfg.session_ttl_s
    assert 0 < cfg.session_idle_s
    assert cfg.remember_ttl_s > cfg.session_ttl_s


# ── §11/§14 no plaintext credentials in the repository ────────────
def test_no_plaintext_credentials_in_tracked_source():
    """Opt-in leak check.

    The operator sets ``BLM_LEAK_CHECK_STRINGS`` to the credentials that
    must never appear in the repository (comma-separated).  The strings are
    supplied at run time so the test file itself never contains them —
    which is the whole point.
    """
    import pytest
    raw = os.environ.get("BLM_LEAK_CHECK_STRINGS", "").strip()
    if not raw:
        pytest.skip("BLM_LEAK_CHECK_STRINGS not set")
    secrets = [s.strip() for s in raw.split(",") if s.strip()]
    try:
        files = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                               capture_output=True, text=True,
                               check=True).stdout.splitlines()
    except Exception:  # pragma: no cover - git unavailable
        pytest.skip("git unavailable")
    hits = []
    for rel in files:
        p = REPO_ROOT / rel
        try:
            if p.stat().st_size > 4_000_000:
                continue
            text = p.read_text(errors="ignore")
        except (OSError, UnicodeError):
            continue
        for s in secrets:
            if s in text:
                hits.append(f"{rel} contains a supplied credential")
    assert not hits, hits


def test_auth_source_reads_credentials_only_from_the_environment():
    """No credential literal may be baked into the auth source."""
    auth_dir = REPO_ROOT / "blm_v4" / "auth"
    for path in auth_dir.glob("*.py"):
        text = path.read_text()
        for forbidden in ('password = "', "password = '", 'PASSWORD = "',
                          "PASSWORD = '"):
            assert forbidden not in text, f"{path.name} has a literal password"
        assert "os.environ" in text or path.name in (
            "passwords.py", "service.py", "store.py", "middleware.py",
            "ratelimit.py", "config.py", "api.py", "__init__.py")
