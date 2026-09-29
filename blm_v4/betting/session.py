"""BOOKMAKER SESSION (directive §2) — backend/session-layer auth.

The bookmaker credentials NEVER reach frontend JavaScript and are NEVER
persisted: they are handed to :meth:`BookmakerSession.login` by the
operator/backend at call time, held only in the authenticated session
object, and only NON-SECRET session state is ever published:

    CONNECTED · DISCONNECTED · SESSION_EXPIRED · AUTHENTICATION_ERROR

Every publication path (``describe``/``status``/``snapshot``) scrubs to
those four states plus booleans — never a password, token, cookie or
auth header value.  Expiry is DETECTED (``touch`` revalidates against
the adapter), never silently ridden through: an expired session fails
closed and the engine refuses to submit.
"""
from __future__ import annotations

import threading
from enum import Enum
from typing import Optional


class SessionState(str, Enum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"


#: the only session fields ever safe to publish to the frontend
_PUBLIC_FIELDS = ("state", "account_id", "logged_in", "age_s")


class SessionError(Exception):
    """Raised for state-illegal session operations (e.g. logout while
    disconnected, resume of a foreign/expired session)."""


class BookmakerSession:
    """One authenticated bookmaker session.  Thread-safe.

    Construct with ``credentials=None`` for a DISCONNECTED session; the
    credentials live here in memory only and die with the object.
    """

    def __init__(self, *, credentials: Optional[tuple] = None,
                 account_id: str = "", session_id: str = "",
                 adapter=None, _resume_state: Optional[SessionState] = None):
        self._lock = threading.Lock()
        self._adapter = adapter
        if _resume_state is not None:
            # internal rehydration from a persisted non-secret snapshot
            self._state = _resume_state
            self._account_id = account_id
            self._session_id = session_id
            self._credentials = None
            return
        if credentials is not None:
            self._state = SessionState.CONNECTED
            self._account_id = account_id or (credentials[0] if credentials else "")
            self._session_id = session_id or f"sess-{id(self):x}"
            self._credentials = credentials
        else:
            self._state = SessionState.DISCONNECTED
            self._account_id = ""
            self._session_id = ""
            self._credentials = None

    # ── lifecycle ──────────────────────────────────────────────────────
    def login(self, credentials: tuple) -> dict:
        """Authenticate.  Credentials are consumed, never stored in any
        log or persisted store; only the authenticated account identity
        is retained."""
        user, pw = credentials
        if not user or not pw:
            with self._lock:
                self._state = SessionState.AUTHENTICATION_ERROR
            raise SessionError("login refused: empty credentials")
        with self._lock:
            self._credentials = (user, pw)
            self._state = SessionState.CONNECTED
            self._account_id = user
            self._session_id = self._session_id or f"sess-{id(self):x}"
        if self._adapter is not None \
                and getattr(self._adapter, "authenticate", None) is not None:
            self._adapter.authenticate(user, pw)
        return self.status()

    def logout(self) -> dict:
        """Explicit logout → DISCONNECTED.  Credentials are dropped."""
        with self._lock:
            if self._state is SessionState.DISCONNECTED:
                raise SessionError("logout while disconnected")
            self._credentials = None
            self._account_id = ""
            self._session_id = ""
            self._state = SessionState.DISCONNECTED
        return self.status()

    def touch(self) -> SessionState:
        """Revalidate the session against the adapter (or the presence
        of credentials when no adapter is wired).  Expiry is detected
        here — the engine calls this before every submission."""
        with self._lock:
            if self._state is SessionState.DISCONNECTED:
                return self._state
            if self._adapter is None:
                if self._credentials is None:
                    self._state = SessionState.SESSION_EXPIRED
                return self._state
            authz = getattr(self._adapter, "is_authenticated", None)
            if authz is None:
                return self._state
            try:
                ok = bool(authz())
            except Exception:
                ok = False
            if not ok:
                self._state = SessionState.SESSION_EXPIRED
                self._credentials = None
            return self._state

    def expire(self) -> None:
        """Force the session into SESSION_EXPIRED (tests / adapter
        callbacks).  Drops the held credentials."""
        with self._lock:
            self._state = SessionState.SESSION_EXPIRED
            self._credentials = None

    def fail_authentication(self) -> None:
        with self._lock:
            self._state = SessionState.AUTHENTICATION_ERROR

    # ── submission gate ───────────────────────────────────────────────
    def assert_submittable(self) -> None:
        """Fail closed unless the session is CURRENTLY connected and
        freshly validated.  The engine calls this in PRECHECK; an
        expired/erroring session must never silently attempt a bet."""
        state = self.touch()
        if state is not SessionState.CONNECTED:
            raise SessionError(
                f"submission refused: session state {state.value}")

    # ── publications (non-secret only) ────────────────────────────────
    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    @property
    def account_id(self) -> str:
        with self._lock:
            return self._account_id

    @property
    def logged_in(self) -> bool:
        with self._lock:
            return self._state is SessionState.CONNECTED

    def status(self) -> dict:
        with self._lock:
            return {"state": self._state.value,
                    "account_id": self._account_id,
                    "logged_in": self._state is SessionState.CONNECTED}

    def snapshot(self) -> dict:
        """A persistence-safe, credential-free snapshot (session id is
        an opaque handle; no password/token/cookie ever appears)."""
        with self._lock:
            return {"state": self._state.value,
                    "account_id": self._account_id,
                    "session_id": self._session_id}

    @classmethod
    def resume(cls, snap: dict, adapter=None) -> "BookmakerSession":
        """Rehydrate from a snapshot taken with :meth:`snapshot`.  The
        resumed session is NEVER connected — credentials were never in
        the snapshot — so the operator must log in again."""
        state = SessionState(snap.get("state", "DISCONNECTED"))
        if state is SessionState.CONNECTED:
            # a CONNECTED snapshot cannot be trusted without credentials
            state = SessionState.SESSION_EXPIRED
        return cls(account_id=snap.get("account_id", ""),
                   session_id=snap.get("session_id", ""),
                   adapter=adapter, _resume_state=state)

    def describe(self) -> str:
        """One-line, credential-free description for logs."""
        with self._lock:
            acct = self._account_id
        return f"BookmakerSession(state={self.state.value}, account={acct!r})"


def public_view(session: BookmakerSession) -> dict:
    """The exact dict shape the frontend may receive (§2: only
    authenticated session state, nothing more)."""
    return session.status()


def redact(mapping: dict) -> dict:
    """Defence-in-depth scrub for any dict about to be logged/served:
    drops every key that could carry authentication material."""
    banned = ("password", "passwd", "token", "cookie", "authorization",
              "auth", "secret", "credential", "session_key")
    out = {}
    for k, v in (mapping or {}).items():
        kl = str(k).lower()
        if any(b in kl for b in banned):
            continue
        out[k] = v
    return out
