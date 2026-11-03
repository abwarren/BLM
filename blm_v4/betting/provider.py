"""Betting execution providers.

A provider submits ONE bet and reports an HONEST outcome (section 5):

    SUBMITTED   the request reached the provider; outcome not yet known
    ACCEPTED    the provider confirmed the bet
    REJECTED    the provider definitively refused (insufficient funds,
                market closed, ...) - no money moved
    FAILED      the request definitively failed before/at the provider - no
                bet exists
    UNKNOWN     the outcome could not be determined (timeout, ambiguous
                response, connection dropped mid-flight) - money MAY be in
                play; the ledger keeps UNKNOWN and the risk limits keep
                counting it

DryRunProvider (the DEFAULT and current mode): performs everything
except the real submission - market checks, stake calculation, decision
and ledger writes - then reports ACCEPTED with a dry-run reference.
Nothing reaches the sportsbook; no credentials are read.

PokerBetProvider: the real-money provider. Two surfaces act as the oracle
for live submission:

1. Account identity (GATE 8). The authenticated side is not hard-coded:
   it comes from the live browser session - the Opera GX tab the operator
   already uses to sign into PokerBet. PokerBetProvider can be told (or,
   in production, read) the account the bookmaker authenticated, and the
   executor verify_account compares it against BLM_EXPECTED_ACCOUNT_ID.
   Different -> BLOCKED at the submission boundary, before any network
   call. Match -> the candidate proceeds.

2. The submission transport. Until the live transport is implemented the
   provider is an inert fail-closed stub that returns ProviderUnavailable.
   No credentials or tokens leave this module; they live only in the
   calling (server/worker) process memory.

Credential policy (unchanged): the username and password are read from
the environment at call time, held ONLY in local variables inside the
request - never logged, never returned, never persisted. This module
contains no credential values.
"""

from __future__ import annotations

import os
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional

from blm_v4.betting.config import BettingConfig

#: the configured account id the operator authorized to bet as (from env
#: BLM_EXPECTED_ACCOUNT_ID). A provider exposes this so the executor
#: account-identity guard can compare it against the account the bookmaker
#: authenticated. Never a credential: an integer label.
_CONFIGURED_ACCOUNT_ID = os.environ.get("BLM_EXPECTED_ACCOUNT_ID", "").strip()


class ProviderUnavailable(Exception):
    """The provider cannot be reached / authenticated - the executor maps
    this to FAILED (definitive local failure, no bet placed)."""


class ProviderAmbiguous(Exception):
    """The provider response could not be interpreted - the executor maps
    this to UNKNOWN (money may be in play)."""


def _new_execution_id() -> str:
    return "bet-" + uuid.uuid4().hex[:20]


class BetProvider(ABC):
    """One bet, one honest answer."""
    name: str = "abstract"

    @abstractmethod
    def submit(self, *, execution_id: str, game_id: str, alert_id: str,
               selection: str, price: Optional[float],
               stake_amount: float, market: str = "TOTAL",
               line: Optional[float] = None) -> dict:
        """Submit one bet. Returns the honest outcome dict with status in
        SUBMITTED / ACCEPTED / REJECTED / FAILED / UNKNOWN."""


class DryRunProvider(BetProvider):
    """DRY_RUN=true (the shipped default): the full decision and stake
    calculation run, the ledger records the would-be bet, and NOTHING is
    submitted. No credentials are read, no network call is made."""
    name = "dry_run"

    def submit(self, *, execution_id: str, game_id: str, alert_id: str,
               selection: str, price: Optional[float],
               stake_amount: float, market: str = "TOTAL",
               line: Optional[float] = None) -> dict:
        return {"status": "ACCEPTED",
                "provider_ref": "dryrun-" + execution_id,
                "error_code": None,
                "error_message": "DRY_RUN - no real bet submitted"}


class PokerBetProvider(BetProvider):
    """The real-money provider - INTENTIONALLY FAIL-CLOSED until the live
    transport is implemented.

    Account-identity oracle (GATE 8): two surfaces, both read from the
    environment (or injected), compared as integers by
    blm_v4.betting.account_guard.verify_account at the submission boundary.
    A mismatch blocks the bet; a matching id lets the candidate reach the
    transport.

    Authenticated side: in production this is READ LIVE from the CDP
    browser-bridge session (the Opera GX tab the operator is signed into).
    When the live reader is not installed this is None and the guard
    refuses to bet (fail-closed).

    Credential policy: the username and password are read from the
    environment at call time inside _credentials and held ONLY in those
    locals - never logged, never returned, never persisted.
    """
    name = "pokerbet"
    requires_account_identity = True

    def __init__(
        self,
        base_url: str = "https://pokerbet.co.za",
        timeout_s: float = 20.0,
        *,
        authenticated_account_id: Optional[int] = None,
        configured_account_id: Optional[int] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        # Oracle side 1 - the account the bookmaker authenticated.
        # Production: read from the live browser session at call time.
        # Test/stub: pass in directly.
        self._authenticated_account_id = authenticated_account_id
        # Oracle side 2 - the account the operator authorized, from
        # BLM_EXPECTED_ACCOUNT_ID (read at call time).
        self._configured_account_id = configured_account_id

    @property
    def authenticated_account_id(self) -> Optional[int]:
        """The account the bookmaker authenticated.
        Override this (or install the live-CDP reader) to make the guard
        meaningful in production. None -> identity unverifiable -> the
        guard blocks the bet."""
        return self._authenticated_account_id

    @authenticated_account_id.setter
    def authenticated_account_id(self, value: Optional[int]) -> None:
        self._authenticated_account_id = value

    @property
    def configured_account_id(self) -> Optional[int]:
        """The account the operator authorized to bet as, from
        BLM_EXPECTED_ACCOUNT_ID (env, read at call time)."""
        if self._configured_account_id is None:
            raw = os.environ.get("BLM_EXPECTED_ACCOUNT_ID", "").strip()
            try:
                self._configured_account_id = int(raw) if raw else None
            except ValueError:
                self._configured_account_id = None
        return self._configured_account_id

    @configured_account_id.setter
    def configured_account_id(self, value: Optional[int]) -> None:
        self._configured_account_id = value

    def _credentials(self) -> tuple[str, str]:
        """Read credentials from the environment at call time. The values
        stay in these locals only - never logged, never stored."""
        user = os.environ.get("POKERBET_USERNAME", "").strip()
        pw = os.environ.get("POKERBET_PASSWORD", "")
        if not user or not pw:
            raise ProviderUnavailable("provider credentials not configured (env)")
        return user, pw

    def submit(self, *, execution_id: str, game_id: str, alert_id: str,
               selection: str, price: Optional[float],
               stake_amount: float, market: str = "TOTAL",
               line: Optional[float] = None) -> dict:
        """Submit one bet.
        Fail-closed stub: the live transport is not implemented, so every
        submission fails SAFE with FAILED (no bet exists), per the
        fail-closed rule. The account-identity guard above runs before
        this method is ever reached by the executor."""
        # Credential presence check (values never leave this method).
        self._credentials()
        # -> THE LIVE TRANSPORT IS NOT IMPLEMENTED <-
        # Until the transport is coded, every candidate fails safe: FAILED,
        # no bet, audit records the reason.
        raise ProviderUnavailable(
            "live submission not implemented (PokerBetProvider stub; DRY_RUN is the supported mode)")

    # -- oracle / live-read surface (production override points) ------

    def set_authorized_account(self, account_id: int) -> None:
        """Set the account the operator authorizes to bet as.
        Alternative to BLM_EXPECTED_ACCOUNT_ID. Only the identity label is
        held here - never a credential."""
        self._configured_account_id = account_id

    def set_authenticated_account(self, account_id: int) -> None:
        """Set the account the bookmaker authenticated.
        NOT a credential. Production: the CDP/browser session layer re-reads
        this from the signed-in Opera GX tab on every call. Test/stub: pass
        in directly."""
        self._authenticated_account_id = account_id


# == LIVE SESSION ORACLE READER ==
# The account-identity oracle reads the account the bookmaker authenticated
# from the operator live CDP browser session (the Opera GX tab the user is
# signed into). Production: the worker installs this reader into the
# provider before any candidate is executed, and calls it again immediately
# before submit so a session swap or expiry is seen at the boundary, not a
# second earlier.
#
# This lives in the provider module so the guard, the worker and the server
import threading
from typing import Callable, Optional

# authenticated_account_id is set POST-import by the worker/server when it
# wires the live browser session; default None = unverified.
authenticated_account_id: Optional[int] = None
_authenticated_id_lock = threading.Lock()
# Set from the worker / server to read the live account instead of None.
_live_session_reader: Optional[Callable[[], Optional[int]]] = None


def set_live_session_reader(reader: Optional[Callable[[], Optional[int]]]) -> None:
    """Install a zero-argument callable that returns the authenticated
    account id from the live browser session, or None if no session.
    Replaces any previously installed reader."""
    global _live_session_reader
    _live_session_reader = reader


def get_authenticated_account_id() -> Optional[int]:
    """Read the account the bookmaker authenticated at call time.
    Priority:
      1. a live session reader (CDP bridge -> Opera GX tab) that returns
         the account id the session reports as signed in;
      2. the provider authenticated_account_id attribute (test / stub)
      3. None (identity unverifiable -> guard blocks, fail-closed).
    Read at call time so a session swap / expiry is caught at the
    submission boundary, not silently assumed from an earlier read."""
    # 1. live session reader first (production)
    reader = _live_session_reader
    if reader is not None:
        val = reader()
        if val is not None:
            return int(val)
    # 2. provider attribute (injected / test)
    with _authenticated_id_lock:
        val = authenticated_account_id
    if val is not None:
        return int(val)
    return None


# The transport is NOT implemented. Until _submit_live replaces this stub,
# every candidate fails SAFE with FAILED; the account guard runs before
# this method is ever reached, so no partial state or money moves from a
# failed transport attempt.
def _submit_live(
    provider: "PokerBetProvider",
    *,
    execution_id: str,
    game_id: str,
    alert_id: str,
    selection: str,
    price: Optional[float],
    stake_amount: float,
    market: str = "TOTAL",
    line: Optional[float] = None,
) -> dict:
    """Fail-closed live submission. Replaced by the real transport when
    it is implemented."""
    # The account-identity guard already ran in the executor and
    # returned BLOCKED on any mismatch, so it is safe to re-read here
    # (idempotent) and fail closed on any uncertainty.
    auth = get_authenticated_account_id()
    configured = provider.configured_account_id
    if auth is None or configured is None:
        return {
            "status": "FAILED",
            "error_code": "ACCOUNT_MISMATCH",
            "error_message": (
                "account identity unverifiable: authenticated=%r configured=%r - blocked before submission" % (auth, configured),
            ),
        }
    if int(auth) != int(configured):
        return {
            "status": "FAILED",
            "error_code": "ACCOUNT_MISMATCH",
            "error_message": (
                "authenticated account %r != configured account %r - blocked before submission" % (auth, configured)
            ),
        }
    raise ProviderUnavailable(
        "live submission not implemented (PokerBetProvider transport stub; DRY_RUN is the supported mode)")


def provider_from_config(cfg) -> BetProvider:
    """The configured provider. DRY_RUN=true (default) -> DryRunProvider;
    DRY_RUN=false -> PokerBetProvider. In production the oracle side is
    wired from the live browser session; here we pass the configured
    account for the guard to read at call time."""
    if cfg.dry_run:
        return DryRunProvider()
    return PokerBetProvider(
        base_url=cfg.provider_base_url,
        timeout_s=cfg.provider_timeout_s,
        configured_account_id=cfg.expected_account_id,
    )


__all__ = [
    "BetProvider", "DryRunProvider", "PokerBetProvider",
    "ProviderUnavailable", "ProviderAmbiguous", "provider_from_config",
    "get_authenticated_account_id", "set_live_session_reader",
]
