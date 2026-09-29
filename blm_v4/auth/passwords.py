"""Password hashing — bcrypt with a pre-hash, never a plaintext password.

Design notes
------------
* bcrypt is used directly (available in the runtime), with the standard
  ``bcrypt`` library — no extra service, no plaintext anywhere.
* Passwords are first hashed with SHA-256 and base64-encoded before bcrypt
  (the well-known Django ``BCryptSHA256PasswordHasher`` construction).  This
  removes bcrypt's 72-byte input limit without silently truncating long
  passphrases.
* The stored value carries an explicit scheme prefix so the algorithm can be
  swapped (e.g. to Argon2id) later without ambiguity:

      bcrypt_sha256$<standard bcrypt hash>

* ``verify_password`` is constant-time with respect to an unknown/absent
  user: a dummy verification is always performed so a missing account and a
  wrong password take the same shape of time.

Nothing in this module ever logs, returns or stores a plaintext password.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets

try:  # pragma: no cover - import guard
    import bcrypt as _bcrypt
except ImportError:  # pragma: no cover
    _bcrypt = None

_log = logging.getLogger("blm_v4.auth")

SCHEME_BCRYPT_SHA256 = "bcrypt_sha256"
DEFAULT_ROUNDS = 12
#: bcrypt's own hard limit — the pre-hash removes it, asserted here so a
#: future refactor cannot reintroduce truncation silently.
_MAX_INPUT_BYTES = 72

_dummy_cache: dict[int, str] = {}


class PasswordHashingUnavailable(RuntimeError):
    """Raised when no supported password hashing backend is importable."""


def _require_backend() -> None:
    if _bcrypt is None:  # pragma: no cover - environment guard
        raise PasswordHashingUnavailable(
            "bcrypt is not installed; cannot hash or verify passwords")


def _prehash(password: str) -> bytes:
    """SHA-256 → base64.  Deterministic, 44 ASCII bytes (under bcrypt's cap)."""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str, *, rounds: int = DEFAULT_ROUNDS) -> str:
    """Return the storable, one-way hash for ``password``.

    Raises ``ValueError`` for an empty password — an empty password is never
    a valid credential and is never hashed.
    """
    if not isinstance(password, str) or password == "":
        raise ValueError("password must be a non-empty string")
    _require_backend()
    salt = _bcrypt.gensalt(rounds=max(4, min(16, int(rounds))),
                           prefix=b"2b")
    hashed = _bcrypt.hashpw(_prehash(password), salt)
    return f"{SCHEME_BCRYPT_SHA256}${hashed.decode('ascii')}"


def _dummy_hash(rounds: int = DEFAULT_ROUNDS) -> str:
    """A per-process throwaway hash used to equalise timing for a missing
    user.  It is NOT a credential: it hashes a random string generated at
    runtime and is never stored or returned."""
    key = int(rounds)
    if key not in _dummy_cache:
        _dummy_cache[key] = hash_password(
            secrets.token_urlsafe(32), rounds=rounds)
    return _dummy_cache[key]


def verify_password(password: str, stored: str,
                    *, rounds: int = DEFAULT_ROUNDS) -> bool:
    """Constant-shape verification.

    Returns True only for a well-formed stored hash that matches.  A missing
    or malformed ``stored`` value still performs a dummy verification so the
    caller leaks no timing signal about account existence.
    """
    if _bcrypt is None:  # pragma: no cover - environment guard
        return False
    if not isinstance(password, str) or password == "":
        # still burn a comparison, then fail
        try:
            _bcrypt.checkpw(b"x", _dummy_hash(rounds).split("$", 1)[1]
                            .encode("ascii"))
        except Exception:  # pragma: no cover
            pass
        return False
    if not isinstance(stored, str) or "$" not in stored:
        try:
            _bcrypt.checkpw(_prehash(password),
                            _dummy_hash(rounds).split("$", 1)[1].encode("ascii"))
        except Exception:  # pragma: no cover
            pass
        return False

    scheme, _, digest = stored.partition("$")
    if scheme != SCHEME_BCRYPT_SHA256 or not digest:
        try:
            _bcrypt.checkpw(_prehash(password),
                            _dummy_hash(rounds).split("$", 1)[1].encode("ascii"))
        except Exception:  # pragma: no cover
            pass
        return False
    try:
        return bool(_bcrypt.checkpw(_prehash(password), digest.encode("ascii")))
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str, *, rounds: int = DEFAULT_ROUNDS) -> bool:
    """True when a stored hash should be upgraded (unknown scheme or a cost
    below the configured target).  Used opportunistically at login."""
    if not isinstance(stored, str):
        return True
    scheme, _, digest = stored.partition("$")
    if scheme != SCHEME_BCRYPT_SHA256 or not digest:
        return True
    parts = digest.split("$")
    # bcrypt format: $<version>$<cost>$<salt+hash> → ['', version, cost, ...]
    if len(parts) < 3:
        return True
    try:
        return int(parts[2]) < int(rounds)
    except (IndexError, ValueError):
        return True
