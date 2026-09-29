"""Session authentication service — the server-side authority.

The frontend is NEVER the authority here.  A request is authenticated only
when the server finds a live session row for the presented cookie token and
re-reads the user's current role and active flag from the database.

Guarantees
----------
* Opaque 256-bit session tokens; only their SHA-256 is persisted.
* Absolute expiry + idle timeout, both enforced on every validation.
* A user deactivated (or deleted) after login is rejected on the next
  request — role and active state are never trusted from the cookie.
* Logout revokes the row, so the token is dead immediately.
* Every failure path is generic and identical: bad username, bad password,
  inactive account and unknown scheme all take the same time and return the
  same reason code.
* Nothing here logs or returns a password, a hash or a raw token.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from blm_v4.auth.config import AuthConfig
from blm_v4.auth.passwords import (hash_password, needs_rehash,
                                   verify_password)
from blm_v4.auth.ratelimit import LoginRateLimiter
from blm_v4.auth.store import AuthStore, hash_token, iso, parse_iso, utcnow

_log = logging.getLogger("blm_v4.auth")

#: the ONE message every credential failure returns.  Deliberately identical
#: for unknown user / wrong password / disabled account — no enumeration.
GENERIC_LOGIN_ERROR = "Invalid username or password"
#: returned when throttled (not an enumeration signal — it is IP/identity
#: based and is also what a legitimate user with a typo would eventually hit)
RATE_LIMIT_ERROR = "Too many login attempts. Please try again later."

#: don't rewrite last_seen_at on every poll — 60s granularity is plenty for
#: an idle timeout measured in hours, and keeps the write path cold.
TOUCH_GRANULARITY_S = 60.0

SESSION_COOKIE_FALLBACK = "blm_session"


@dataclass
class LoginResult:
    ok: bool
    reason: str = ""
    message: str = ""
    token: Optional[str] = None
    user: Optional[dict] = None
    csrf_token: Optional[str] = None
    expires_in: int = 0
    retry_after_s: int = 0


@dataclass
class SessionView:
    """What the middleware/dependencies hand to the rest of the app."""
    session_id: int
    user_id: int
    username: str
    role: str
    csrf_token: str
    expires_at: str
    remember: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def as_public(self) -> dict:
        return {
            "authenticated": True,
            "username": self.username,
            "role": self.role,
            "csrf_token": self.csrf_token,
        }


class AuthService:
    def __init__(self, store: AuthStore, config: AuthConfig):
        self.store = store
        self.config = config
        self.limiter = LoginRateLimiter(
            max_attempts=config.max_attempts,
            window_s=config.window_s,
            lockout_s=config.lockout_s)

    # ── login ─────────────────────────────────────────────────────
    def login(self, username: str, password: str, *, remember: bool = False,
              ip: Optional[str] = None, user_agent: Optional[str] = None
              ) -> LoginResult:
        """Authenticate and mint a session.  Never raises on bad input.

        Every credential failure returns the SAME generic message; an
        exhausted rate limit returns the throttle message.
        """
        uname = (username or "").strip()
        ip = ip or "-"

        decision = self.limiter.check(uname, ip)
        if not decision.allowed:
            self.store.audit("login_throttled", username=uname, ip=ip,
                             user_agent=user_agent,
                             detail=f"retry_after={decision.retry_after_s}")
            return LoginResult(False, "rate_limited", RATE_LIMIT_ERROR,
                               retry_after_s=decision.retry_after_s)

        # the login field accepts either a username or an email address
        row = self.store.get_user_by_username(uname)
        if row is None and "@" in uname:
            row = self.store.get_user_by_email(uname)
        stored_hash = row["password_hash"] if row is not None else ""
        # verify_password always does real work, so a missing user costs the
        # same as a wrong password (no timing oracle).
        password_ok = verify_password(password, stored_hash,
                                      rounds=self.config.bcrypt_rounds)
        account_ok = bool(row is not None and row["is_active"])
        if not (password_ok and account_ok):
            self.limiter.record_failure(uname, ip)
            self.store.audit(
                "login_failed", username=uname, ip=ip, user_agent=user_agent,
                detail="bad_credentials")
            _log.warning("auth_login_failed username=%r ip=%s", uname, ip)
            return LoginResult(False, "invalid", GENERIC_LOGIN_ERROR)

        # success — opportunistic rehash if the cost has been raised
        if needs_rehash(stored_hash, rounds=self.config.bcrypt_rounds):
            try:
                self.store.set_password(
                    row["id"],
                    hash_password(password, rounds=self.config.bcrypt_rounds))
                self.store.audit("password_rehashed", username=row["username"],
                                 ip=ip)
            except Exception:  # pragma: no cover - never fail a login for this
                _log.exception("auth_rehash_failed")

        self.limiter.record_success(uname, ip)
        token, csrf, ttl = self._mint_session(
            row, remember=remember, ip=ip, user_agent=user_agent)
        self.store.touch_login(row["id"])
        self.store.audit("login_success", username=row["username"], ip=ip,
                         user_agent=user_agent)
        _log.info("auth_login_success username=%r role=%s ip=%s",
                  row["username"], row["role"], ip)
        return LoginResult(
            True, "ok", "", token=token,
            user={"username": row["username"], "role": row["role"]},
            csrf_token=csrf, expires_in=ttl)

    # ── session minting ───────────────────────────────────────────
    def _mint_session(self, user_row, *, remember: bool, ip, user_agent
                      ) -> tuple[str, str, int]:
        """Returns ``(token, csrf_token, ttl_seconds)``.

        Deliberately returns the values instead of stashing them on the
        instance: the API runs on a thread pool, so per-request state must
        never live on the shared service object.
        """
        cfg = self.config
        now = utcnow()
        ttl = cfg.remember_ttl_s if remember else cfg.session_ttl_s
        absolute = now + timedelta(seconds=ttl)
        idle = now + timedelta(seconds=min(cfg.session_idle_s, ttl))
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self.store.create_session(
            token_hash=hash_token(token), user_id=user_row["id"],
            username=user_row["username"], role=user_row["role"],
            csrf_token=csrf, absolute_expires_at=iso(absolute),
            idle_expires_at=iso(idle), ip=ip, user_agent=user_agent)
        return token, csrf, int(ttl)

    # ── validation ────────────────────────────────────────────────
    def validate(self, token: Optional[str]) -> Optional[SessionView]:
        """Resolve a cookie token to a live session, or None.

        Enforces revocation, absolute expiry, idle expiry, and the user's
        CURRENT active flag — on every call.
        """
        if not token or not isinstance(token, str):
            return None
        row = self.store.get_session(hash_token(token))
        if row is None:
            return None
        if row["revoked_at"]:
            return None
        now = utcnow()
        absolute = parse_iso(row["absolute_expires_at"])
        idle = parse_iso(row["idle_expires_at"])
        if absolute is None or absolute <= now:
            self.store.revoke_session(row["token_hash"], "expired")
            return None
        if idle is None or idle <= now:
            self.store.revoke_session(row["token_hash"], "idle_timeout")
            return None

        # the user is the authority for role + active state, not the session
        user = self.store.get_user_by_id(row["user_id"])
        if user is None or not user["is_active"]:
            self.store.revoke_session(row["token_hash"], "user_inactive")
            return None

        last_seen = parse_iso(row["last_seen_at"])
        if last_seen is None or (now - last_seen).total_seconds() > TOUCH_GRANULARITY_S:
            new_idle = min(idle, absolute)
            try:
                self.store.touch_session(int(row["id"]), iso(new_idle))
            except Exception:  # pragma: no cover
                _log.exception("auth_touch_failed")

        return SessionView(
            session_id=int(row["id"]), user_id=int(user["id"]),
            username=user["username"], role=user["role"],
            csrf_token=row["csrf_token"],
            expires_at=row["absolute_expires_at"])

    def logout(self, token: Optional[str], *, reason: str = "logout") -> bool:
        if not token:
            return False
        row = self.store.get_session(hash_token(token))
        revoked = self.store.revoke_session(hash_token(token), reason)
        if row is not None:
            self.store.audit("logout", username=row["username"])
        return revoked

    def revoke_all(self, user_id: int, *, reason: str = "revoked") -> int:
        return self.store.revoke_all_for_user(user_id, reason)

    # ── csrf ──────────────────────────────────────────────────────
    @staticmethod
    def verify_csrf(session: SessionView, provided: Optional[str]) -> bool:
        """Constant-time comparison of the per-session CSRF token."""
        if not provided or not session:
            return False
        return secrets.compare_digest(str(provided), str(session.csrf_token))

    # ── cookie helpers ────────────────────────────────────────────
    def cookie_max_age(self, *, remember: bool) -> int:
        return int(self.config.remember_ttl_s if remember
                   else self.config.session_ttl_s)


__all__ = ["AuthService", "LoginResult", "SessionView",
           "GENERIC_LOGIN_ERROR", "RATE_LIMIT_ERROR"]
