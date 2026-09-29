"""BLM authentication package.

One call wires the whole layer into an application:

    from blm_v4.auth import install
    state = install(app, root)          # from the composition root

``install`` is deliberately thin — all behaviour lives in the modules:

    config.py      environment-sourced configuration (safe defaults)
    passwords.py   bcrypt (SHA-256 pre-hashed) hashing + constant-shape verify
    store.py       blm_auth.db — users, sessions, audit (never a plaintext)
    service.py     login / validate / logout / CSRF, the server-side authority
    ratelimit.py   per-identity + per-IP sliding-window throttling
    middleware.py  the ASGI guard that protects every non-public route
    api.py         /login page + /api/auth/* endpoints + role dependencies
    seed.py        idempotent, env-driven account provisioning

It is ADDITIVE: existing databases, routers and pipelines are untouched.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from blm_v4.auth.api import (auth_admin_probe, auth_login, auth_logout,
                             auth_me, configure_auth, current_session,
                             get_service, login_page, require_admin,
                             require_role, router as auth_router)
from blm_v4.auth.config import AuthConfig
from blm_v4.auth.middleware import (SESSION_SCOPE_KEY, SessionGuard,
                                    client_ip, safe_next)
from blm_v4.auth.passwords import (hash_password, needs_rehash,
                                   verify_password)
from blm_v4.auth.ratelimit import LoginRateLimiter
from blm_v4.auth.service import (GENERIC_LOGIN_ERROR, RATE_LIMIT_ERROR,
                                 AuthService, LoginResult, SessionView)
from blm_v4.auth.store import AuthStore, hash_token, public_user

__all__ = [
    "install", "AuthConfig", "AuthStore", "AuthService", "SessionGuard",
    "LoginRateLimiter", "LoginResult", "SessionView", "auth_router",
    "configure_auth", "current_session", "require_role", "require_admin",
    "get_service", "login_page", "auth_login", "auth_logout", "auth_me",
    "auth_admin_probe", "hash_password", "verify_password", "needs_rehash",
    "hash_token", "public_user", "safe_next", "client_ip",
    "SESSION_SCOPE_KEY", "GENERIC_LOGIN_ERROR", "RATE_LIMIT_ERROR",
]


def _default_trust_proxy() -> bool:
    """Trust ``X-Forwarded-For`` in production (Caddy is the only ingress and
    binds the app to 127.0.0.1).  Everywhere else the socket peer is used, so
    a client cannot spoof its own throttle bucket."""
    raw = os.environ.get("BLM_AUTH_TRUST_PROXY")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return os.environ.get("BLM_ENV", "development").lower() == "production"


def install(app, root: Path | str, *, logger: Optional[logging.Logger] = None,
            trust_proxy: Optional[bool] = None) -> dict:
    """Install auth on ``app``.  Returns a state dict for logging/tests.

    Idempotent per process and additive: it adds a middleware and a router,
    and opens (creating if needed) ``blm_auth.db``.  It never touches another
    database and never changes an existing route's implementation.
    """
    log = logger or logging.getLogger("blm_v4.auth")
    config = AuthConfig.from_env(root)
    if not config.enabled:
        log.warning("auth_disabled")
        return {"enabled": False, "config": config}

    if trust_proxy is None:
        trust_proxy = _default_trust_proxy()

    store = AuthStore(config.db_path)
    service = AuthService(store, config)
    configure_auth(service, config, trust_proxy=trust_proxy)

    app.add_middleware(SessionGuard, service=service, config=config,
                       trust_proxy=trust_proxy)
    app.include_router(auth_router)

    users = store.count_users()
    log.info("auth_installed db=%s users=%d trust_proxy=%s secure_cookie=%s",
             config.db_path, users, trust_proxy, config.cookie_secure)
    if users == 0:
        log.warning(
            "auth_no_users — run `python3 -m blm_v4.auth.seed` with "
            "BLM_SEED_ADMIN_PASSWORD / BLM_SEED_USER_PASSWORD set, or every "
            "request will be redirected to the login page")
    return {"enabled": True, "config": config, "store": store,
            "service": service, "users": users, "trust_proxy": trust_proxy}
