"""Betting execution providers.

A provider submits ONE bet and reports an HONEST outcome (§5):

    SUBMITTED   the request reached the provider; outcome not yet known
    ACCEPTED    the provider confirmed the bet
    REJECTED    the provider definitively refused (insufficient funds,
                market closed, ...) — no money moved
    FAILED      the request definitively failed before/at the provider —
                no bet exists
    UNKNOWN     the outcome could not be determined (timeout, ambiguous
                response, connection dropped mid-flight) — money MAY be
                in play; the ledger keeps UNKNOWN and the risk limits
                keep counting it

``DryRunProvider`` (the DEFAULT and current mode): performs everything
except the real submission — market checks, stake calculation, decision
and ledger writes — then reports ACCEPTED with a dry-run reference.
Nothing reaches the sportsbook; no credentials are read.

``PokerBetProvider``: the real-money provider.  The submission transport
(login/session/placement HTTP to the bookmaker) is NOT implemented:
the provider is an inert fail-closed stub that returns
``ProviderUnavailable`` until the transport exists.  Credential presence
is checked at call time; the account-identity guard then compares the
account the bookmaker authenticated against the account the operator
authorized, and blocks on mismatch before any submission.  Nothing is
submitted unless the transport is live.
"""

from __future__ import annotations

import os
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional

from blm_v4.betting.config import BettingConfig

#: the configured account id the operator authorized to bet as (from
#: env ``BLM_EXPECTED_ACCOUNT_ID``).  A provider exposes this so the
#: executor's account-identity guard can compare it against the account
#: the bookmaker authenticated.  Never a credential: an integer label.
_CONFIGURED_ACCOUNT_ID = os.environ.get("BLM_EXPECTED_ACCOUNT_ID", "").strip()


class ProviderUnavailable(Exception):
    """The provider cannot be reached / authenticated — the executor
    maps this to FAILED (definitive local failure, no bet placed)."""


class ProviderAmbiguous(Exception):
    """The provider's response could not be interpreted — the executor
    maps this to UNKNOWN (money may be in play)."""


def _new_execution_id() -> str:
    return f"bet-{uuid.uuid4().hex[:20]}"


class BetProvider(ABC):
    """One bet, one honest answer."""

    name: str = "abstract"

    @abstractmethod
    def submit(self, *, execution_id: str, game_id: str, alert_id: str,
               selection: str, price: Optional[float],
               stake_amount: float, market: str = "TOTAL",
               line: Optional[float] = None) -> dict:
        """Submit one bet.  Returns ``{"status": ..., "provider_ref":
        ..., "error_code": ..., "error_message": ...}`` with status in
        SUBMITTED / ACCEPTED / REJECTED / FAILED / UNKNOWN."""


class DryRunProvider(BetProvider):
    """DRY_RUN=true (the shipped default): the full decision and stake
    calculation run, the ledger records the would-be bet, and NOTHING is
    submitted.  No credentials are read, no network call is made."""

    name = "dry_run"

    def submit(self, *, execution_id: str, game_id: str, alert_id: str,
               selection: str, price: Optional[float],
               stake_amount: float, market: str = "TOTAL",
               line: Optional[float] = None) -> dict:
        return {"status": "ACCEPTED",
                "provider_ref": f"dryrun-{execution_id}",
                "error_code": None,
                "error_message": "DRY_RUN — no real bet submitted"}


class PokerBetProvider(BetProvider):
    """The real-money provider — INTENTIONALLY INERT (stub).

    The submission transport (login/session/placement) is not implemented:
    ``submit()`` raises ``ProviderUnavailable`` and the executor records
    FAILED.  The account-identity guard runs at the submission boundary:
    it compares the account the bookmaker authenticated (which the CDP
    bridge / live session supplies as ``authenticated_account_id``) against
    the account the operator configured to bet as
    (``configured_account_id`` = ``BLM_EXPECTED_ACCOUNT_ID``).  A mismatch
    blocks the bet; a matching id passes the guard.

    Credential policy: the username and password are read from the
    environment at call time and held ONLY in local variables inside the
    request — never logged, never returned, never persisted.  This module
    contains no credential values.

    Account identity: identity is an integer account id.  ``requires_
    account_identity`` is True so the executor's guard actually runs.
    """

    name = "pokerbet"
    requires_account_identity = True

    def __init__(self, base_url: str, timeout_s: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._authenticated_account_id: Optional[int] = None
        self._configured_account_id: Optional[int] = None

    @property
    def authenticated_account_id(self) -> Optional[int]:
        """The account the bookmaker authenticated (set by the live
        session layer / CDP bridge).  Read from the live session state
        at call time — never a persisted value, never a credential."""
        return self._authenticated_account_id

    @authenticated_account_id.setter
    def authenticated_account_id(self, value: Optional[int]) -> None:
        self._authenticated_account_id = value

    @property
    def configured_account_id(self) -> Optional[int]:
        """The account the operator authorized to bet as, from
        ``BLM_EXPECTED_ACCOUNT_ID`` (env, read at call time)."""
        if self._configured_account_id is None:
            raw = os.environ.get("BLM_EXPECTED_ACCOUNT_ID", "").strip()
            try:
                self._configured_account_id = int(raw) if raw else None
            except ValueError:
                self._configured_account_id = None
        return self._configured_account_id

    def _credentials(self) -> tuple[str, str]:
        """Read credentials from the environment at call time.  The
        values stay in these locals only — never logged, never stored."""
        user = os.environ.get("POKERBET_USERNAME", "").strip()
        pw = os.environ.get("POKERBET_PASSWORD", "")
        if not user or not pw:
            raise ProviderUnavailable(
                "provider credentials not configured (env)")
        return user, pw

    def submit(self, *, execution_id: str, game_id: str, alert_id: str,
               selection: str, price: Optional[float],
               stake_amount: float, market: str = "TOTAL",
               line: Optional[float] = None) -> dict:
        # Credential presence check (values never leave this method).
        self._credentials()
        # ── STUB: the real transport is intentionally not implemented.
        # The account-identity guard has already run (executor, after
        # the kill switch) and will return BLOCKED if the authenticated
        # account does not equal the configured account.  Any attempt to
        # run live mode today fails SAFE: the executor records FAILED
        # (definitive — no bet exists) and the audit log explains why.
        # No partial HTTP call, no ambiguous state.
        raise ProviderUnavailable(
            "live submission not implemented (provider stub; DRY_RUN is "
            "the supported mode)")


def provider_from_config(cfg) -> BetProvider:
    """The configured provider.  DRY_RUN=true (default) → DryRunProvider;
    DRY_RUN=false → PokerBetProvider (currently a fail-safe stub)."""
    if cfg.dry_run:
        return DryRunProvider()
    return PokerBetProvider(base_url=cfg.provider_base_url,
                            timeout_s=cfg.provider_timeout_s)


__all__ = [
    "BetProvider", "DryRunProvider", "PokerBetProvider",
    "ProviderUnavailable", "ProviderAmbiguous", "provider_from_config",
]

