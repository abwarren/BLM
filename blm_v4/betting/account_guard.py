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


def verify_account(provider) -> Optional[str]:
    """``None`` when the bet may proceed; a reason string when blocked.

    Applies to any provider object.  Identity-requiring providers must
    expose ``authenticated_account_id`` and ``configured_account_id``
    (both non-empty and equal) or the bet is refused.
    """
    if not getattr(provider, "requires_account_identity", False):
        return None
    auth = (getattr(provider, "authenticated_account_id", None) or "")
    configured = (getattr(provider, "configured_account_id", None) or "")
    auth, configured = str(auth).strip(), str(configured).strip()
    if not auth or not configured:
        return ("account identity unverifiable: "
                "authenticated or configured account missing")
    if auth != configured:
        return (f"authenticated account {auth!r} != "
                f"configured account {configured!r}")
    return None
