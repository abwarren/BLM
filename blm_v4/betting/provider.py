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
               stake_amount: float, market: str = "TOTAL",
               line: Optional[float] = None) -> dict:
        # Credential presence check (values never leave this method).
        self._credentials()
        # ── STUB: the real transport is intentionally not implemented.
        # Any attempt to run live mode today fails SAFE: the executor
        # records FAILED (definitive — no bet exists) and the audit log
        # explains why.  No partial HTTP call, no ambiguous state.
        raise ProviderUnavailable(
            "live submission not implemented (provider stub; DRY_RUN is "
            "the supported mode)")


def browser_submit_authorized() -> bool:
    """THE SECOND SERVER-SIDE INTERLOCK, read AT THE SUBMISSION BOUNDARY.

    A real browser submission requires BOTH the frontend's AUTO-BET switch
    (the store's kill switch) AND this server-side flag.  Reading the
    environment here — at call time, immediately before the click — means
    the authority can never be baked in when a provider is constructed, so
    a test (or any accidental live-provider invocation) cannot reach a real
    click without an explicit operator opt-in.  Absent/false ⇒ NO BET.

    Only the values ``true``/``1``/``yes``/``on`` (case-insensitive) count.
    """
    return os.environ.get("BETTING_BROWSER_SUBMIT", "").strip().lower() \
        in ("1", "true", "yes", "on")


class PokerBetBrowserProvider(BetProvider):
    """The REAL transport — drives the proven PokerBet DOM execution gate.

    It does NOT decide WHAT to bet (that is the BLM signal) and it never
    weakens the gate.  For one bet request it: resolves the exact live
    game/market offer, selects it with the platform strategy (Cyber pointer /
    BETUAL DOM click — inside the adapter), verifies the betslip leg actually
    shows the pinned line, enters the stake, revalidates the odds and the
    BET NOW control, and reaches GATE_READY.  ONLY with ``live=True`` does it
    click BET NOW; any uncertain post-submit outcome raises
    ``ProviderAmbiguous`` (the executor records UNKNOWN) — never a claimed
    success.  The default is gate-only (no submission).
    """

    name = "pokerbet_browser"

    def __init__(self, *, adapter=None, bridge=None,
                 cdp_url: str = "http://127.0.0.1:9222",
                 db_path: Optional[str] = None, live: bool = False,
                 hydrate_timeout_ms: int = 15000, event_resolver=None):
        self._adapter = adapter
        self._bridge = bridge
        self._cdp_url = cdp_url
        self._db_path = db_path
        self._live = bool(live)
        self._hydrate = hydrate_timeout_ms
        self._event_resolver = event_resolver

    # ── game_id → the event label the browser resolves ───────────────
    def _event_label(self, game_id: str) -> str:
        if self._event_resolver is not None:
            try:
                lbl = self._event_resolver(game_id)
                if lbl:
                    return str(lbl)
            except Exception:
                pass
        if self._db_path:
            try:
                import sqlite3
                con = sqlite3.connect(f"file:{self._db_path}?mode=ro",
                                      uri=True)
                row = con.execute(
                    "SELECT home_team, away_team FROM games "
                    "WHERE source_game_id=? ORDER BY last_seen_at DESC "
                    "LIMIT 1", (str(game_id),)).fetchone()
                con.close()
                if row and row[0] and row[1]:
                    return f"{row[0]} vs {row[1]}"
            except Exception:
                pass
        return str(game_id)

    # ── lazy wiring (the real CDP browser + PokerBet adapter) ─────────
    def _wire(self) -> None:
        if self._bridge is not None:
            return
        from blm_v4.execution.pokerbet.browser import CdpBrowser
        from blm_v4.execution.pokerbet.dom import PokerBetDomAdapter
        from blm_v4.execution.browser_bridge import ResolverBrowserBridge
        if self._adapter is None:
            b = CdpBrowser(self._cdp_url).connect()
            self._adapter = PokerBetDomAdapter(b, hydrate_timeout_ms=self._hydrate)
            try:
                self._adapter._page = b.page()
            except Exception:
                pass
        self._bridge = ResolverBrowserBridge(self._adapter)

    def _gate(self, command: dict, stake_amount: float) -> dict:
        """Resolve → select → verify the betslip → PRICE/STAKE gate, then STOP.
        Never submits.  Returns {ready, reason, ...}."""
        from blm_v4.execution.browser_bridge import BrowserBridge  # noqa: F401
        try:
            obs = self._bridge.observe(command)
        except ProviderUnavailable:
            raise
        except Exception as e:
            raise ProviderUnavailable(str(e)[:300])
        if obs is None:
            return {"ready": False, "reason": "MARKET_NOT_RESOLVED"}
        try:
            if not self._adapter.click_selection(obs):
                return {"ready": False, "reason": "SELECTION_NOT_ADDED"}
        except Exception as e:
            return {"ready": False, "reason": "CLICK_ERROR", "detail": str(e)[:200]}
        # betslip verify — a click is never proof
        try:
            from blm_v4.execution.betslip_verifier import (
                VERIFIED, verify_leg_in_betslip)
            from blm_v4.execution.selection_model import Selection
            sel = Selection(event=str(command.get("event")
                                       or command.get("game_id") or ""),
                            market=str(command.get("market") or "TOTAL"),
                            position=str(command.get("selection") or "UNDER"),
                            game_id=str(command.get("game_id") or "") or None)
            check = verify_leg_in_betslip(self._adapter, sel, obs.line, obs.price)
            if check.outcome != VERIFIED:
                return {"ready": False, "reason": check.outcome}
        except Exception as e:
            return {"ready": False, "reason": "BETSLIP_UNREADABLE",
                    "detail": str(e)[:200]}
        # the adapter's own final gate (stake read-back, odds bound, control).
        # A SelectionResolver without a final gate still gets the resolve +
        # click + betslip-verify checks above; the real adapter adds its gate.
        prep = getattr(self._adapter, "prepare_submit", None)
        if prep is None:
            return {"ready": True, "reason": "verified", "line": obs.line,
                    "price": obs.price}
        try:
            gate = prep(float(stake_amount), obs)
        except Exception as e:
            return {"ready": False, "reason": "GATE_ERROR", "detail": str(e)[:200]}
        return gate if isinstance(gate, dict) else {"ready": False,
                                                    "reason": "GATE_NO_RESULT"}

    def submit(self, *, execution_id: str, game_id: str, alert_id: str,
               selection: str, price: Optional[float],
               stake_amount: float, market: str = "TOTAL",
               line: Optional[float] = None) -> dict:
        try:
            self._wire()
        except Exception as e:
            # an unreachable browser is DEFINITIVE — no bet exists
            raise ProviderUnavailable(f"browser transport unavailable: {e}")
        command = {"game_id": str(game_id), "alert_id": alert_id,
                   "event": self._event_label(str(game_id)),
                   "market": market, "selection": selection,
                   "line": line, "price": price, "execution_id": execution_id}

        # ── gate-only: prove the chain reaches GATE_READY, never submit ──
        if not self._live:
            gate = self._gate(command, stake_amount)
            if not gate.get("ready"):
                return {"status": "FAILED", "provider_ref": None,
                        "error_code": gate.get("reason") or "GATE_NOT_READY",
                        "error_message": f"gate not ready: {gate.get('reason')}"}
            return {"status": "ACCEPTED",
                    "provider_ref": f"gate-ready-{execution_id}",
                    "error_code": None,
                    "error_message": "GATE_READY — no submit (gate-only mode)"}

        # ── live: the SECOND INTERLOCK is re-checked HERE, at the final
        # submission boundary — after the gate, immediately before the click.
        # Without the explicit server-side opt-in there is NO BET, even if a
        # caller (or a test) invoked this live provider and GATE_READY held.
        if not browser_submit_authorized():
            return {"status": "FAILED", "provider_ref": None,
                    "error_code": "SUBMIT_NOT_AUTHORIZED",
                    "error_message": ("BETTING_BROWSER_SUBMIT is not enabled "
                                      "— real submission refused at the "
                                      "submission boundary")}

        # ── the gate ran inside place_parlay; the outcome is honest ──
        try:
            out = dict(self._bridge.place(command=command,
                                          stake_amount=float(stake_amount)) or {})
        except ProviderUnavailable:
            raise
        except Exception as e:
            # after the placement boundary an exception is AMBIGUOUS
            raise ProviderAmbiguous(str(e)[:300])
        status = out.get("status")
        if status not in ("SUBMITTED", "ACCEPTED", "REJECTED", "FAILED",
                          "UNKNOWN"):
            raise ProviderAmbiguous(f"unmapped bridge status {status!r}")
        return {"status": status, "provider_ref": out.get("provider_ref"),
                "error_code": out.get("error_code"),
                "error_message": out.get("error_message")}


def provider_from_config(cfg) -> BetProvider:
    """The configured provider.  DRY_RUN=true (default) → DryRunProvider;
    DRY_RUN=false → ``PokerBetBrowserProvider`` — the REAL transport that
    drives the proven PokerBet DOM gate (resolve → select with the platform
    strategy → verify the betslip → enter the configured unit size → revalidate
    → GATE_READY → submit).

    Selecting the live provider is NOT the authority to submit: the executor
    re-reads the store's kill switch at the submission boundary, so the UI's
    AUTO-BET switch must be ON for a live submit to reach this provider at all.
    ``live=True`` here means the provider *may* submit once the gate is ready;
    every other guard (account identity, idempotency, exact unit size,
    game/market/direction/line verification) stays mandatory.
    """
    if cfg.dry_run:
        return DryRunProvider()
    return PokerBetBrowserProvider(
        cdp_url=os.environ.get("BETTING_CDP_URL", "http://127.0.0.1:9222"),
        db_path=os.environ.get("BLM_POKERBET_DB") or None,
        live=True)
