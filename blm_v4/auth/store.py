"""Auth persistence — blm_auth.db, a SEPARATE SQLite database.

Failure domain: authentication gets its own database so a schema change or
corruption there can never touch the analytics or betting stores.

SECURITY
--------
* ``users.password_hash`` is the ONLY password-related column, and it holds a
  one-way bcrypt hash (see ``passwords.py``).  There is no plaintext column,
  no reversible encoding and no way to recover a password from this schema.
* ``sessions.token_hash`` stores the SHA-256 of the opaque session token —
  never the token itself — so a database read cannot be replayed as a live
  session.
* ``auth_audit`` records events and the *username attempted*, never a
  password, hash or token.
* No API surface of this module selects ``password_hash`` for output; the
  public projection (``public_user``) is an explicit allow-list of fields.

SCHEMA
------
users(id, username, username_lower UNIQUE, email, password_hash, role,
      is_active, created_at, updated_at, last_login_at, password_hash_version)
sessions(id, token_hash UNIQUE, user_id, username, role, csrf_token,
         created_at, last_seen_at, absolute_expires_at, idle_expires_at,
         revoked_at, ip, user_agent)
auth_audit(id, at_utc, event, username, ip, user_agent, detail)

The schema is created idempotently (``CREATE TABLE IF NOT EXISTS``) and
extended by ADDITIVE migrations only — never a rebuild, never a drop.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from blm_v4.auth.config import VALID_ROLES

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    username               TEXT NOT NULL,
    username_lower         TEXT NOT NULL UNIQUE,
    email                  TEXT,
    password_hash          TEXT NOT NULL,
    role                   TEXT NOT NULL CHECK (role IN ('admin','user')),
    is_active              INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    last_login_at          TEXT,
    password_changed_at    TEXT,
    password_hash_version  TEXT NOT NULL DEFAULT 'bcrypt_sha256'
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique
    ON users(lower(email)) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS sessions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash           TEXT NOT NULL UNIQUE,
    user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username             TEXT NOT NULL,
    role                 TEXT NOT NULL,
    csrf_token           TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL,
    absolute_expires_at  TEXT NOT NULL,
    idle_expires_at      TEXT NOT NULL,
    revoked_at           TEXT,
    revoked_reason       TEXT,
    ip                   TEXT,
    user_agent           TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(absolute_expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_live
    ON sessions(token_hash, revoked_at);

CREATE TABLE IF NOT EXISTS auth_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at_utc     TEXT NOT NULL,
    event      TEXT NOT NULL,
    username   TEXT,
    ip         TEXT,
    user_agent TEXT,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_audit_event ON auth_audit(event, at_utc);
CREATE INDEX IF NOT EXISTS idx_auth_audit_user ON auth_audit(username, at_utc);
"""

_ISO = "%Y-%m-%dT%H:%M:%S.%f"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_ISO)[:-3] + "Z"


def parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value[:26].replace("Z", ""), _ISO).replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def hash_token(token: str) -> str:
    """SHA-256 of an opaque session token.  The token itself is never stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_user(row: Any) -> dict:
    """The ONLY user projection allowed out of this module.

    An explicit allow-list — ``password_hash`` is deliberately absent, so a
    caller cannot accidentally serialise it.
    """
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "email": row["email"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


class AuthStore:
    """SQLite-backed users + sessions + audit log."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(Path(db_path))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    # ── connection ────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """ADDITIVE migrations only.  Never rebuilds, never drops."""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        additions = {
            "email": "TEXT",
            "last_login_at": "TEXT",
            "password_changed_at": "TEXT",
            "password_hash_version": "TEXT NOT NULL DEFAULT 'bcrypt_sha256'",
        }
        for name, decl in additions.items():
            if name not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")

    # ── users ─────────────────────────────────────────────────────
    def get_user_by_username(self, username: str) -> Optional[sqlite3.Row]:
        """Case-insensitive lookup.  Returns the FULL row (includes the
        hash) — callers must project through ``public_user`` for output."""
        if not username:
            return None
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM users WHERE username_lower = ?",
                (username.strip().lower(),)).fetchone()

    def get_user_by_id(self, user_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM users WHERE id = ?",
                             (user_id,)).fetchone()

    def get_user_by_email(self, email: str) -> Optional[sqlite3.Row]:
        """Case-insensitive email lookup (the login field accepts either)."""
        if not email:
            return None
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM users WHERE lower(email) = ?",
                (email.strip().lower(),)).fetchone()

    def list_users(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM users ORDER BY id").fetchall()
        return [public_user(r) for r in rows]

    def count_users(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) AS n FROM users")
                       .fetchone()["n"])

    def create_user(self, *, username: str, password_hash: str,
                    role: str = "user", email: Optional[str] = None,
                    is_active: bool = True) -> dict:
        """Insert a user.  Raises ``ValueError`` on a duplicate username or an
        invalid role.  Never accepts a plaintext password."""
        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        uname = (username or "").strip()
        if not uname:
            raise ValueError("username must be a non-empty string")
        now = iso(utcnow())
        with self._lock, self._conn() as c:
            existing = c.execute(
                "SELECT id FROM users WHERE username_lower = ?",
                (uname.lower(),)).fetchone()
            if existing is not None:
                raise ValueError(f"user already exists: {uname}")
            cur = c.execute(
                """INSERT INTO users
                   (username, username_lower, email, password_hash, role,
                    is_active, created_at, updated_at, password_changed_at,
                    password_hash_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (uname, uname.lower(), email, password_hash, role,
                 1 if is_active else 0, now, now, now,
                 password_hash.split("$", 1)[0] or "bcrypt_sha256"))
            return public_user(
                c.execute("SELECT * FROM users WHERE id = ?",
                          (cur.lastrowid,)).fetchone())

    def set_password(self, user_id: int, password_hash: str) -> None:
        now = iso(utcnow())
        with self._lock, self._conn() as c:
            c.execute(
                """UPDATE users SET password_hash = ?, updated_at = ?,
                   password_changed_at = ?, password_hash_version = ?
                   WHERE id = ?""",
                (password_hash, now, now,
                 password_hash.split("$", 1)[0] or "bcrypt_sha256", user_id))

    def set_active(self, user_id: int, active: bool) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
                      (1 if active else 0, iso(utcnow()), user_id))

    def set_role(self, user_id: int, role: str) -> None:
        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
                      (role, iso(utcnow()), user_id))

    def touch_login(self, user_id: int) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                      (iso(utcnow()), user_id))

    # ── sessions ──────────────────────────────────────────────────
    def create_session(self, *, token_hash: str, user_id: int, username: str,
                       role: str, csrf_token: str, absolute_expires_at: str,
                       idle_expires_at: str, ip: Optional[str] = None,
                       user_agent: Optional[str] = None) -> int:
        now = iso(utcnow())
        with self._lock, self._conn() as c:
            cur = c.execute(
                """INSERT INTO sessions
                   (token_hash, user_id, username, role, csrf_token,
                    created_at, last_seen_at, absolute_expires_at,
                    idle_expires_at, ip, user_agent)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (token_hash, user_id, username, role, csrf_token, now, now,
                 absolute_expires_at, idle_expires_at,
                 (ip or "")[:64], (user_agent or "")[:256]))
            return int(cur.lastrowid or 0)

    def get_session(self, token_hash: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM sessions WHERE token_hash = ?",
                (token_hash,)).fetchone()

    def touch_session(self, session_id: int, idle_expires_at: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE sessions SET last_seen_at = ?, idle_expires_at = ? "
                "WHERE id = ?",
                (iso(utcnow()), idle_expires_at, session_id))

    def revoke_session(self, token_hash: str, reason: str = "logout") -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute(
                """UPDATE sessions SET revoked_at = ?, revoked_reason = ?
                   WHERE token_hash = ? AND revoked_at IS NULL""",
                (iso(utcnow()), reason[:64], token_hash))
            return cur.rowcount > 0

    def revoke_all_for_user(self, user_id: int, reason: str = "revoked") -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                """UPDATE sessions SET revoked_at = ?, revoked_reason = ?
                   WHERE user_id = ? AND revoked_at IS NULL""",
                (iso(utcnow()), reason[:64], user_id))
            return int(cur.rowcount)

    def purge_expired(self) -> int:
        """Delete long-dead session rows (housekeeping, not a security path)."""
        cutoff = iso(utcnow())
        with self._lock, self._conn() as c:
            cur = c.execute(
                "DELETE FROM sessions WHERE absolute_expires_at < ?",
                (cutoff,))
            return int(cur.rowcount)

    def active_session_count(self) -> int:
        with self._conn() as c:
            return int(c.execute(
                """SELECT COUNT(*) AS n FROM sessions
                   WHERE revoked_at IS NULL AND absolute_expires_at > ?""",
                (iso(utcnow()),)).fetchone()["n"])

    # ── audit ─────────────────────────────────────────────────────
    def audit(self, event: str, *, username: Optional[str] = None,
              ip: Optional[str] = None, user_agent: Optional[str] = None,
              detail: Optional[str] = None) -> None:
        """Record an auth event.  NEVER pass a password, hash or token here."""
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    """INSERT INTO auth_audit
                       (at_utc, event, username, ip, user_agent, detail)
                       VALUES (?,?,?,?,?,?)""",
                    (iso(utcnow()), event[:64], (username or "")[:128] or None,
                     (ip or "")[:64] or None, (user_agent or "")[:256] or None,
                     (detail or "")[:512] or None))
        except sqlite3.Error:
            # Auditing must never break authentication.
            pass

    def recent_audit(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM auth_audit ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]
