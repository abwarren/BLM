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

``PokerBetProvider``: the real-money implementation STUB.  It is
deliberately not operational: the transport (login/session) is a
placeholder that raises ``ProviderUnavailable``, so even with
``BETTING_DRY_RUN=false`` the fail-safe path yields FAILED/UNKNOWN —
never a silent half-integration.  Credential policy: the username and
password are read from the environment at call time and held ONLY in
local variables inside the request — never logged, never returned, never
persisted.  This module contains no credential values.
"""
from __future__ import annotations

import os
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional

from blm_v4.betting.config import credentials_present


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
               stake_amount: float) -> dict:
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
               stake_amount: float) -> dict:
        return {"status": "ACCEPTED",
                "provider_ref": f"dryrun-{execution_id}",
                "error_code": None,
                "error_message": "DRY_RUN — no real bet submitted"}


class PokerBetProvider(BetProvider):
    """The real-money provider — INTENTIONALLY INERT (stub).

    The submission path is a placeholder: it performs the credential
    presence check and then raises ``ProviderUnavailable``.  Wiring the
    real transport later (login/session/placement endpoint) requires
    code, not configuration alone — so live betting cannot be enabled by
    accident through an env var.
    """

    name = "pokerbet"

    def __init__(self, base_url: str, timeout_s: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

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
               stake_amount: float) -> dict:
        # Credential presence check (values never leave this method).
        self._credentials()
        # ── STUB: the real transport is intentionally not implemented.
        # Any attempt to run live mode today fails SAFE: the executor
        # records FAILED (definitive — no bet exists) and the audit log
        # explains why.  No partial HTTP call, no ambiguous state.
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
