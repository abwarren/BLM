"""BLM authentication — configuration (environment-sourced, fail-closed).

Every value is read from the environment so that no secret, credential or
deployment-specific constant ever lives in source.  The defaults are the
SAFE defaults: authentication is ON, cookies are SameSite, sessions
expire, login attempts are throttled.

Environment variables (all optional):

    BLM_AUTH_ENABLED            "1" to guard the app (default 1)
    BLM_AUTH_DB                 SQLite path (default <root>/blm_auth.db)
    BLM_AUTH_COOKIE_NAME        session cookie name (default blm_session)
    BLM_AUTH_COOKIE_SECURE      "1" force the Secure flag (default: auto —
                                Secure whenever the request is https or the
                                environment is production)
    BLM_AUTH_COOKIE_SAMESITE    lax | strict | none (default lax)
    BLM_AUTH_SESSION_TTL_S      absolute session lifetime (default 43200 = 12h)
    BLM_AUTH_SESSION_IDLE_S     idle timeout (default 7200 = 2h)
    BLM_AUTH_REMEMBER_TTL_S     absolute lifetime with "remember me"
                                (default 2592000 = 30d)
    BLM_AUTH_BCRYPT_ROUNDS      bcrypt cost (default 12)
    BLM_AUTH_MAX_ATTEMPTS       failed logins per window (default 8)
    BLM_AUTH_WINDOW_S           rate-limit window (default 300)
    BLM_AUTH_LOCKOUT_S          lockout after the window is exhausted (default 300)
    BLM_AUTH_PUBLIC_PATHS       extra comma-separated public path prefixes
    BLM_AUTH_LOGIN_PATH         login page path (default /login)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_COOKIE_NAME = "blm_session"
DEFAULT_LOGIN_PATH = "/login"

#: paths that are ALWAYS reachable without a session.  Everything else is
#: guarded.  Kept here (not in the middleware) so the guard and its tests
#: share one source of truth.
PUBLIC_EXACT = frozenset({
    "/login",
    "/api/auth/login",
    "/healthz",
    "/health",
    "/favicon.ico",
    "/robots.txt",
    "/apple-touch-icon.png",
    "/manifest.json",
})
PUBLIC_PREFIX = (
    "/static/",
    "/dashboard/static/",
    "/assets/",
    "/blm-assets/",
)

VALID_ROLES = ("admin", "user")


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return v if v != "" else default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in (
        "1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class AuthConfig:
    """Immutable, environment-derived authentication configuration."""

    enabled: bool = True
    db_path: str = "blm_auth.db"
    cookie_name: str = DEFAULT_COOKIE_NAME
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    session_ttl_s: int = 43_200
    session_idle_s: int = 7_200
    remember_ttl_s: int = 2_592_000
    bcrypt_rounds: int = 12
    max_attempts: int = 8
    window_s: int = 300
    lockout_s: int = 300
    login_path: str = DEFAULT_LOGIN_PATH
    public_paths: tuple[str, ...] = field(default_factory=tuple)
    #: optional operator banner injected into the login page (used by the
    #: TEST/STAGING stack so a staging login screen is never mistaken for
    #: production).  Empty in production.
    login_banner: str = ""

    @classmethod
    def from_env(cls, root: Path | str | None = None) -> "AuthConfig":
        root = Path(root) if root is not None else Path(".")
        db = _env("BLM_AUTH_DB", str(root / "blm_auth.db"))
        env_name = _env("BLM_ENV", "development").lower()
        # Secure flag: explicit override wins, else production is Secure.
        explicit = os.environ.get("BLM_AUTH_COOKIE_SECURE")
        if explicit is None or explicit.strip() == "":
            secure = env_name == "production"
        else:
            secure = _env_bool("BLM_AUTH_COOKIE_SECURE", False)
        samesite = _env("BLM_AUTH_COOKIE_SAMESITE", "lax").lower()
        if samesite not in ("lax", "strict", "none"):
            samesite = "lax"
        extra = tuple(
            p.strip() for p in
            _env("BLM_AUTH_PUBLIC_PATHS", "").split(",") if p.strip())
        return cls(
            enabled=_env_bool("BLM_AUTH_ENABLED", True),
            db_path=db,
            cookie_name=_env("BLM_AUTH_COOKIE_NAME", DEFAULT_COOKIE_NAME),
            cookie_secure=secure,
            cookie_samesite=samesite,
            session_ttl_s=max(60, _env_int("BLM_AUTH_SESSION_TTL_S", 43_200)),
            session_idle_s=max(60, _env_int("BLM_AUTH_SESSION_IDLE_S", 7_200)),
            remember_ttl_s=max(
                60, _env_int("BLM_AUTH_REMEMBER_TTL_S", 2_592_000)),
            bcrypt_rounds=max(4, min(16, _env_int("BLM_AUTH_BCRYPT_ROUNDS", 12))),
            max_attempts=max(1, _env_int("BLM_AUTH_MAX_ATTEMPTS", 8)),
            window_s=max(1, _env_int("BLM_AUTH_WINDOW_S", 300)),
            lockout_s=max(1, _env_int("BLM_AUTH_LOCKOUT_S", 300)),
            login_path=_env("BLM_AUTH_LOGIN_PATH", DEFAULT_LOGIN_PATH),
            public_paths=extra,
            login_banner=_env("BLM_LOGIN_BANNER", ""),
        )

    def is_public(self, path: str) -> bool:
        """True when ``path`` may be served without a session."""
        if path in PUBLIC_EXACT:
            return True
        if path in self.public_paths:
            return True
        for prefix in PUBLIC_PREFIX:
            if path.startswith(prefix):
                return True
        for prefix in self.public_paths:
            if prefix.endswith("/") and path.startswith(prefix):
                return True
        return False
