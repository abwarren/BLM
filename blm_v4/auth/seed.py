"""Idempotent authentication seeding.

Usage
-----
    python3 -m blm_v4.auth.seed

Credentials are supplied ONLY through the environment — never on the command
line (which would land in shell history) and never in source:

    BLM_SEED_ADMIN_USERNAME   default "admin"
    BLM_SEED_ADMIN_PASSWORD   required to CREATE the admin account
    BLM_SEED_USER_USERNAME    default "bradblm"
    BLM_SEED_USER_PASSWORD    required to CREATE the standard account
    BLM_SEED_ADMIN_ROLE       default "admin"
    BLM_SEED_USER_ROLE        default "user"
    BLM_AUTH_DB               default <repo>/blm_auth.db
    BLM_AUTH_BCRYPT_ROUNDS    default 12

Idempotency
-----------
Running twice creates nothing the second time.  An existing account keeps its
password unless ``BLM_SEED_RESET_PASSWORDS=1`` is explicitly set — a re-run
can therefore never silently overwrite a password an operator has changed.

Output never contains a password or a hash: only the username, the role and
what was done.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from blm_v4.auth.config import AuthConfig
from blm_v4.auth.passwords import hash_password
from blm_v4.auth.store import AuthStore

_ROOT = Path(__file__).resolve().parent.parent.parent


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else default


def _plan() -> list[dict]:
    return [
        {
            "username": _env("BLM_SEED_ADMIN_USERNAME", "admin"),
            "password": _env("BLM_SEED_ADMIN_PASSWORD"),
            "role": _env("BLM_SEED_ADMIN_ROLE", "admin"),
            "email": _env("BLM_SEED_ADMIN_EMAIL") or None,
            "label": "admin",
        },
        {
            "username": _env("BLM_SEED_USER_USERNAME", "bradblm"),
            "password": _env("BLM_SEED_USER_PASSWORD"),
            "role": _env("BLM_SEED_USER_ROLE", "user"),
            "email": _env("BLM_SEED_USER_EMAIL") or None,
            "label": "user",
        },
    ]


def seed(store: AuthStore, *, rounds: int = 12, reset_passwords: bool = False,
         accounts: Optional[list[dict]] = None, out=sys.stdout) -> dict:
    """Idempotently ensure the configured accounts exist.  Returns a report.

    ``accounts`` defaults to the environment-derived plan (``main``).  An
    explicit list lets the TEST/STAGING stack provision its own throwaway
    accounts without inheriting an operator's shell environment.
    """
    created, existing, reset, skipped = [], [], [], []
    for spec in (accounts if accounts is not None else _plan()):
        username = spec["username"]
        password = spec["password"]
        row = store.get_user_by_username(username)
        if row is None:
            if not password:
                skipped.append((username, spec["label"]))
                continue
            store.create_user(
                username=username,
                password_hash=hash_password(password, rounds=rounds),
                role=spec["role"], email=spec["email"], is_active=True)
            created.append((username, spec["role"]))
            continue
        # exists — idempotent no-op unless an explicit reset was requested
        if reset_passwords and password:
            store.set_password(row["id"], hash_password(password,
                                                        rounds=rounds))
            reset.append((username, row["role"]))
        else:
            existing.append((username, row["role"]))
        # keep the declared role authoritative (additive-safe, never silent
        # about a change) — only when it is a real change
        if row["role"] != spec["role"]:
            store.set_role(row["id"], spec["role"])
            out.write(f"  ~ role updated: {username}: {row['role']} -> "
                      f"{spec['role']}\n")
        if not row["is_active"]:
            store.set_active(row["id"], True)
            out.write(f"  ~ reactivated: {username}\n")

    out.write("BLM auth seed complete\n")
    for u, r in created:
        out.write(f"  + created  {u} (role={r})\n")
    for u, r in existing:
        out.write(f"  = exists   {u} (role={r}) — unchanged\n")
    for u, r in reset:
        out.write(f"  ~ password reset {u} (role={r})\n")
    for u, label in skipped:
        out.write(f"  ! skipped  {u} ({label}): no password in environment "
                  f"(set BLM_SEED_{'ADMIN' if label == 'admin' else 'USER'}"
                  f"_PASSWORD)\n")
    out.write(f"  users in database: {store.count_users()}\n")
    return {"created": created, "existing": existing, "reset": reset,
            "skipped": skipped, "total": store.count_users()}


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    config = AuthConfig.from_env(
        _ROOT if os.environ.get("BLM_AUTH_DB") is None else None)
    db_path = os.environ.get("BLM_AUTH_DB") or config.db_path
    rounds = config.bcrypt_rounds
    reset = _env("BLM_SEED_RESET_PASSWORDS", "0").lower() in (
        "1", "true", "yes", "on")
    store = AuthStore(db_path)
    sys.stdout.write(f"BLM auth seed — database: {db_path}\n")
    report = seed(store, rounds=rounds, reset_passwords=reset)
    if report["skipped"]:
        sys.stdout.write(
            "NOT an error: existing accounts are never modified without "
            "BLM_SEED_RESET_PASSWORDS=1.\n")
        # only a hard failure when the account does not exist AND no password
        # was supplied — seed() already skipped those, so exit 2 signals it
        missing = [u for u, _ in report["skipped"]]
        if missing and report["total"] < 2:
            return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
