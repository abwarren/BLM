"""Account-identity guard — account mismatch → NO BET (directive GATE 8).

Before ANY provider submission, the executor verifies that the account
the bookmaker AUTHENTICATED is the account the operator CONFIGURED to
bet as.

Contract (fail-closed):

* An identity-requiring provider MUST present BOTH sides:
  ``authenticated_account_id`` (the account the bookmaker authenticated)
  and ``configured_account_id`` (the account the operator authorized).
  They MUST match exactly, else the bet is BLOCKED before submission
  (no partial state, no money moved).
* A provider with no account identity (DryRunProvider — no real
  submission exists) is exempt: there is nothing to mis-bind.

The guard NEVER handles credential values — only the non-secret account
identity label.  It never logs, stores or returns more than the two
identifiers compared.  Identity is an integer account id, never a
username/password/token.  The value is compared as an integer; the env
supplier and the browser oracle must agree on that label.

This module never reads ``BLM_EXPECTED_ACCOUNT_ID`` itself: the provider
supplies both sides, so the guard stays a pure comparator and the
provider owns where its identity comes from.
"""

from __future__ import annotations

from typing import Optional

BLOCKED_REASON = "ACCOUNT_MISMATCH"


def verify_account(provider) -> Optional[str]:
    """``None`` when the bet may proceed; a reason string when blocked.

    Applies to any provider object.  Identity-requiring providers must
    expose ``requires_account_identity`` (True), ``authenticated_account_id``
    and ``configured_account_id`` (both non-empty and equal) or the bet
    is refused.  Values are compared as integers — an id is an id, never
    a string-vs-int ambiguity.
    """
    if not getattr(provider, "requires_account_identity", False):
        return None
    auth = getattr(provider, "authenticated_account_id", None)
    configured = getattr(provider, "configured_account_id", None)
    if auth is None or configured is None:
        return ("account identity unverifiable: "
                "authenticated or configured account missing")
    try:
        auth_i = int(auth)
    except (TypeError, ValueError):
        auth_i = None
    try:
        configured_i = int(configured)
    except (TypeError, ValueError):
        configured_i = None
    if auth_i is None or configured_i is None:
        return ("account identity unverifiable: a side of the identity "
                "comparison is not an integer account id")
    if auth_i != configured_i:
        return (f"authenticated account {auth_i!r} != configured account "
                f"{configured_i!r}")
    return None
