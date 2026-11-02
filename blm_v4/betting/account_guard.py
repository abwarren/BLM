"""ACCOUNT IDENTITY GUARD — account mismatch → NO BET (directive GATE 8).

Before ANY provider submission, the executor verifies that the account
the bookmaker AUTHENTICATED is the account the operator CONFIGURED.

Contract (fail-closed):

* A provider that carries account identity (``requires_account_identity
  is True``) MUST present BOTH sides — the authenticated account and the
  configured account — and they MUST match exactly, else the bet is
  BLOCKED before submission (no partial state, no money moved).
* A provider with no account identity (``DryRunProvider`` — no real
  submission exists) is exempt: there is nothing to mis-bind.

The guard NEVER handles credential values — only the non-secret account
identity label.  It never logs, stores or returns more than the two
identifiers compared.
"""
from __future__ import annotations

from typing import Optional

BLOCKED_REASON = "ACCOUNT_MISMATCH"


def _configured_ids(provider) -> set:
    """Every account id the operator permits (never the values of a secret)."""
    ids = getattr(provider, "configured_account_ids", None)
    if ids:
        out = {str(v).strip() for v in ids if str(v).strip()}
        if out:
            return out
    single = getattr(provider, "configured_account_id", None)
    single = str(single).strip() if single is not None else ""
    return {single} if single else set()


def verify_account(provider) -> Optional[str]:
    """``None`` when the bet may proceed; a reason string when blocked.

    The operator may permit SEVERAL accounts (``BLM_EXPECTED_ACCOUNT_ID``
    accepts a comma/space separated list): whichever account the browser is
    signed into must be one of them.  Everything else about the guard is
    unchanged and fail-closed — an unreadable authenticated id, or an empty
    permitted set on an identity-requiring provider, blocks the bet.
    """
    if not getattr(provider, "requires_account_identity", False):
        return None
    auth = (getattr(provider, "authenticated_account_id", None) or "")
    auth = str(auth).strip()
    permitted = _configured_ids(provider)
    if not auth or not permitted:
        return ("account identity unverifiable: "
                "authenticated or configured account missing")
    if auth not in permitted:
        shown = ", ".join(sorted(permitted)) if len(permitted) <= 3 else \
            f"{len(permitted)} permitted accounts"
        return (f"authenticated account {auth!r} is not a configured "
                f"account ({shown})")
    return None
