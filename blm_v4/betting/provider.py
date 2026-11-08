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
        self._transport: Optional['PokerBetTransport'] = None

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

        Runs the live transport and records the HONEST outcome
        (SUBMITTED / ACCEPTED / REJECTED / FAILED / UNKNOWN).
        """
        # Credential presence check (values never leave this method).
        # A browser transport rides the operator's already-authenticated
        # PokerBet session and needs no environment credentials.
        if self._transport is None \
                or not getattr(self._transport, "uses_browser_session", False):
            self._credentials()
        # Account-identity guard (GATE 8) runs in the executor BEFORE this
        # method is ever called.  Fail-closed: if the authenticated or
        # configured account cannot be read, this method refuses.  The
        # AUTHENTICATED side prefers the live session reader and falls
        # back to this instance's injected identity; it never silently
        # uses a stale process-global value.
        auth = get_authenticated_account_id()
        if auth is None:
            auth = self._authenticated_account_id
        configured = self.configured_account_id
        if auth is None or configured is None:
            raise ProviderUnavailable(
                "account identity unverifiable: authenticated=%r "
                "configured=%r - blocked before submission"
                % (auth, configured))
        if int(auth) != int(configured):
            raise ProviderUnavailable(
                "authenticated account %r != configured account %r - "
                "blocked before submission" % (auth, configured))
        # FAIL CLOSED: no transport means the live path does not exist.
        # This is a definitive local failure (no bet exists), never an
        # ambiguous outcome — mapping it to UNKNOWN would wrongly tell
        # the operator money may be in play.
        if self._transport is None:
            raise ProviderUnavailable(
                "live submission transport not installed "
                "(PokerBetProvider fail-closed stub)")
        # Hand the identity re-read at THIS boundary to the transport, so
        # a session swap between provider construction and submission is
        # seen rather than assumed away.
        if hasattr(self._transport, "authenticated_account_id"):
            self._transport.authenticated_account_id = auth
        return self._transport.submit(
            execution_id=execution_id, game_id=game_id, alert_id=alert_id,
            selection=selection, price=price,
            stake_amount=stake_amount, market=market, line=line)

    # -- oracle / live-read surface (production override points) ------

    def set_transport(self, transport: 'PokerBetTransport') -> None:
        """Wire the live bookmaker transport into this provider.

        Production: the worker/server installs the configured transport
        before any candidate is executed, so the provider can submit a
        real bet through it.  The transport itself owns the network
        call, the account identity and the idempotency; the provider
        only delegates and then records the HONEST outcome.

        Test/stub: set_transport(None) keeps the provider in fail-closed
        stub mode.
        """
        self._transport = transport
        if transport is not None and hasattr(
                transport, "configured_account_id"):
            if getattr(transport, "authenticated_account_id", None) is None:
                transport.authenticated_account_id = \
                    self._authenticated_account_id
            if getattr(transport, "_configured_account_id", None) is None:
                transport.configured_account_id = self.configured_account_id

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


# == BOOKMAKER TRANSPORT ==
# The live bookmaker submission transport.  This owns:
#
#   * the account identity (reads the signed-in Opera GX session via the
#     CDP bridge, from the environment, or from a configured stub);
#   * the idempotency key (so retries never re-submit a live bet);
#   * the HTTP/SSE submission to PokerBet;
#   * the HONEST outcome reporting (SUBMITTED / ACCEPTED / REJECTED /
#     FAILED / UNKNOWN).
#
# Fail-closed by construction: the provider's account-identity guard and
# this transport's identity reader are the ONLY paths to submission.
# Nothing here logs, stores or returns credential values.

class PokerBetTransport:
    """Live bookmaker submission transport for PokerBet.

    One bet, one honest outcome:

        SUBMITTED   the request reached the bookmaker; outcome not yet known
        ACCEPTED    the bookmaker confirmed the bet
        REJECTED    the bookmaker definitively refused (insufficient funds,
                    market closed, ...) - no money moved
        FAILED      the request definitively failed before/at the bookmaker
                    - no bet exists
        UNKNOWN     the outcome could not be determined (timeout, ambiguous
                    response, connection dropped mid-flight) - money MAY be
                    in play; the ledger keeps UNKNOWN and the risk limits
                    keep counting it

    The transport is constructed with the credentials, the authorised/
    authenticated account ids, and the base URL.  It is the ONLY place in
    the provider that touches the network.
    """

    name = "pokerbet_transport"

    def __init__(
        self,
        base_url: str = "https://pokerbet.co.za",
        *,
        username: str,
        password: str,
        authenticated_account_id: Optional[int] = None,
        configured_account_id: Optional[int] = None,
        timeout_s: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.authenticated_account_id = authenticated_account_id
        self._configured_account_id = configured_account_id
        self.timeout_s = timeout_s

    @property
    def configured_account_id(self) -> Optional[int]:
        """The account the operator authorizes to bet as."""
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

    def submit(
        self,
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
        """Submit one bet to the bookmaker and return the HONEST outcome.

        The transport:
          1. reads the account the bookmaker authenticated at call time
             (CDP bridge / environment / configured stub);
          2. builds an idempotency key from game + checkpoint + alert;
          3. performs the placement HTTP/SSE request;
          4. handles any retry/reconciliation at the transport (so the
             executor and worker never re-submit);
          5. returns the honest outcome.

        Fail-closed: if the authenticated account cannot be read, the
        transport refuses (no network call), and returns FAILED.
        """
        # --- 1. account identity at call time --------------------------
        auth = self.authenticated_account_id
        configured = self.configured_account_id
        if auth is None or configured is None:
            return {
                "status": "FAILED",
                "error_code": "ACCOUNT_MISMATCH",
                "error_message": (
                    "account identity unverifiable: authenticated=%r "
                    "configured=%r - blocked before submission"
                    % (auth, configured)),
            }
        if int(auth) != int(configured):
            return {
                "status": "FAILED",
                "error_code": "ACCOUNT_MISMATCH",
                "error_message": (
                    "authenticated account %r != configured account %r - "
                    "blocked before submission" % (auth, configured)),
            }

        # --- 2. idempotent key (unique per opportunity) ------------------
        # It is the SAME key the execution ledger claimed, so a later
        # reconciliation looks up the exact order this attempt created.
        ik = self._idempotency_key(game_id=game_id, alert_id=alert_id)
        # --- 3. perform the placement ----------------------------------
        try:
            result = self._place(
                execution_id=execution_id, game_id=game_id, alert_id=alert_id,
                selection=selection, price=price,
                stake_amount=stake_amount, market=market, line=line,
                idempotency_key=ik)
        except ProviderUnavailable as e:
            # definitive local failure: no request reached the bookmaker,
            # so no money moved.  Never report this as UNKNOWN.
            return {"status": "FAILED",
                    "error_code": "TRANSPORT_UNAVAILABLE",
                    "error_message": str(e)[:300]}
        except TimeoutError:
            return {"status": "UNKNOWN",
                    "error_code": "TRANSACTION_TIMEOUT",
                    "error_message": "bookmaker request timed out"}
        except Exception as e:
            return {"status": "UNKNOWN",
                    "error_code": "TRANSPORT_EXCEPTION",
                    "error_message": str(e)[:300]}

        status = result.get("status", "UNKNOWN")
        if status == "REJECTED":
            return {"status": "REJECTED",
                    "provider_ref": result.get("provider_ref"),
                    "error_code": result.get("error_code"),
                    "error_message": result.get("error_message")}
        if status == "FAILED":
            return {"status": "FAILED",
                    "provider_ref": result.get("provider_ref"),
                    "error_code": result.get("error_code"),
                    "error_message": result.get("error_message")}
        if status == "SUBMITTED":
            return {"status": "SUBMITTED",
                    "provider_ref": result.get("provider_ref")}
        if status == "ACCEPTED":
            return {"status": "ACCEPTED",
                    "provider_ref": result.get("provider_ref")}
        return {"status": "UNKNOWN",
                "error_code": "UNMAPPED_TRANSPORT_STATUS",
                "error_message": "unmapped transport status %r" % (status,)}

    def _idempotency_key(self, game_id: str, alert_id: str,
                         checkpoint: str = "") -> str:
        """A unique idempotency key per (game, checkpoint, alert).

        Format: ``{game_id}|{checkpoint}|{alert_id}`` — the SAME key the
        executor's ledger claimed.  The autonomous alert id is
        ``{game_id}|{checkpoint}``, so the checkpoint is recovered from
        it here instead of being invented.  A manual key (no such
        prefix) keeps an empty checkpoint, which is still stable and
        unique.  The database's UNIQUE index on
        ``bet_executions.idempotency_key`` enforces ONE execution per
        opportunity across restarts, retries and workers.
        """
        prefix = f"{game_id}|"
        if not checkpoint and str(alert_id).startswith(prefix):
            checkpoint = str(alert_id)[len(prefix):]
        return f"{game_id}|{checkpoint}|{alert_id}"

    def _place(self, *, execution_id: str, game_id: str, alert_id: str,
               selection: str, price: Optional[float],
               stake_amount: float, market: str, line: Optional[float],
               idempotency_key: str) -> dict:
        """The actual HTTP/SSE placement call.

        Override this in production to issue the real bookmaker request.
        The default raises ProviderUnavailable - the transport is NOT
        wired to the bookmaker until the real endpoint exists.
        """
        raise ProviderUnavailable(
            "bookmaker placement not implemented (live transport not wired)")


class PokerBetBrowserTransport(PokerBetTransport):
    """Submission transport over the VERIFIED PokerBet DOM adapter.

    This is the bridge between the Auto-Bet execution authority and the
    existing browser execution subsystem (``blm_v4.execution.pokerbet``).
    It does not open a browser and does not create a second execution
    engine: the operator installs an already-connected
    ``PokerBetDomAdapter`` and registers each canonical game id to its
    rendered event name.

    Sequence (fail closed at every step):
      1. canonical game id → registered event (unregistered = no bet)
      2. event identity + game id verified on the page
      3. game-TOTAL market present and unambiguous
      4. the selection at the ORIGINAL trigger line (never a substitute)
      5. exact odds match
      6. click the uniquely identified outcome
      7. re-read the betslip and verify event/line/odds exactly once
      8. fill + submit the stake through the existing adapter
      9. return the bookmaker's own receipt, or SUBMITTED (unconfirmed)

    ``PokerBetProvider`` is still the only caller, still guarded by the
    account-identity check, and this class is never constructed by
    ``provider_from_config`` — the live path requires explicit code.
    """

    name = "pokerbet_browser"
    uses_browser_session = True

    def __init__(self, adapter, games: dict[str, str], *,
                 authenticated_account_id: Optional[int] = None,
                 configured_account_id: Optional[int] = None,
                 base_url: str = "https://pokerbet.co.za",
                 timeout_s: float = 20.0):
        super().__init__(
            base_url=base_url, username="", password="",
            authenticated_account_id=authenticated_account_id,
            configured_account_id=configured_account_id,
            timeout_s=timeout_s)
        self._adapter = adapter
        self._games = {str(k): str(v) for k, v in (games or {}).items()}

    def register_game(self, game_id: str, event: str) -> None:
        """Bind a canonical BLM game id to the rendered event name.  An
        unregistered game can never be submitted."""
        self._games[str(game_id)] = str(event)

    def _place(self, *, execution_id: str, game_id: str, alert_id: str,
               selection: str, price: Optional[float],
               stake_amount: float, market: str, line: Optional[float],
               idempotency_key: str) -> dict:
        # THE final authorization gate: real in-browser submission stays
        # disabled unless BETTING_BROWSER_SUBMIT is explicitly true.
        if os.environ.get("BETTING_BROWSER_SUBMIT", "").strip().lower() \
                not in ("1", "true", "yes", "on"):
            raise ProviderUnavailable(
                "browser submission not authorized "
                "(BETTING_BROWSER_SUBMIT is not true)")
        adapter = self._adapter
        if adapter is None:
            raise ProviderUnavailable("no browser adapter installed")
        event = self._games.get(str(game_id))
        if not event:
            return {"status": "FAILED", "error_code": "GAME_NOT_REGISTERED",
                    "error_message": ("game %s is not bound to a "
                                      "bookmaker event" % game_id)}
        if line is None or price is None:
            return {"status": "FAILED", "error_code": "MISSING_LINE_OR_ODDS",
                    "error_message": "trigger line and odds are required"}
        try:
            adapter.set_required_line(float(line))
        except AttributeError:
            return {"status": "FAILED", "error_code": "ADAPTER_CONTRACT",
                    "error_message": "adapter cannot bind a trigger line"}
        try:
            found = adapter.find_event(event)
        except Exception as e:  # noqa: BLE001 — browser gone → no bet
            return {"status": "FAILED", "error_code": "BROWSER_UNAVAILABLE",
                    "error_message": str(e)[:300]}
        if not found:
            return {"status": "FAILED", "error_code": "GAME_NOT_FOUND",
                    "error_message": "event/game identity not verified"}
        try:
            if not adapter.find_market(event, market or "TOTAL"):
                return {"status": "FAILED",
                        "error_code": "MARKET_NOT_FOUND",
                        "error_message": "game-total market not offered "
                                        "or ambiguous"}
        except Exception as e:  # noqa: BLE001
            return {"status": "FAILED", "error_code": "BROWSER_UNAVAILABLE",
                    "error_message": str(e)[:300]}
        try:
            obs = adapter.find_position(event, market or "TOTAL", selection)
        except Exception as e:  # noqa: BLE001
            return {"status": "FAILED", "error_code": "BROWSER_UNAVAILABLE",
                    "error_message": str(e)[:300]}
        if obs is None:
            return {"status": "FAILED",
                    "error_code": "SELECTION_NOT_AVAILABLE",
                    "error_message": ("no unique %s offer at line %s"
                                      % (selection, line))}
        if abs(float(obs.line) - float(line)) > 1e-9:
            return {"status": "FAILED", "error_code": "LINE_MISMATCH",
                    "error_message": "resolved line is not the trigger line"}
        if abs(float(obs.price) - float(price)) > 1e-9:
            return {"status": "FAILED", "error_code": "ODDS_MISMATCH",
                    "error_message": "resolved odds differ from the "
                                    "revalidated candidate"}
        if getattr(obs, "suspended", False):
            return {"status": "FAILED", "error_code": "MARKET_SUSPENDED",
                    "error_message": "selection is suspended"}
        try:
            clicked = adapter.click_selection(obs)
        except Exception as e:  # noqa: BLE001
            return {"status": "FAILED", "error_code": "SELECTION_NOT_CLICKED",
                    "error_message": str(e)[:300]}
        if not clicked:
            return {"status": "FAILED", "error_code": "SELECTION_NOT_CLICKED",
                    "error_message": "no unambiguous outcome could be clicked"}
        # INDEPENDENT betslip verification: the click is not the bet.
        try:
            entries = adapter.read_betslip()
        except Exception as e:  # noqa: BLE001
            return {"status": "UNKNOWN",
                    "error_code": "BETSLIP_UNREADABLE",
                    "error_message": str(e)[:300]}
        matches = [e for e in (entries or [])
                   if str(e.get("position") or "").upper()
                   == str(selection).upper()
                   and str(e.get("game_id") or "") in ("", str(game_id))
                   and abs(float(e.get("line") or -1) - float(line)) <= 1e-9]
        if len(matches) != 1:
            return {"status": "FAILED",
                    "error_code": "BETSLIP_VERIFICATION_FAILED",
                    "error_message": ("expected exactly one matching "
                                      "betslip leg, found %d" % len(matches))}
        if abs(float(matches[0].get("price") or -1) - float(price)) > 1e-9:
            return {"status": "FAILED", "error_code": "ODDS_MISMATCH",
                    "error_message": "betslip odds differ from the "
                                    "verified offer"}
        result = adapter.place_parlay(float(stake_amount))
        status = str(result.get("status") or "UNKNOWN").upper()
        if status == "ACCEPTED" and result.get("provider_ref"):
            return result
        if status == "ACCEPTED":         # acceptance without a receipt
            return {"status": "SUBMITTED", "provider_ref": None}
        if status in ("SUBMITTED", "REJECTED", "FAILED", "UNKNOWN"):
            return result
        return {"status": "UNKNOWN", "error_code": "UNMAPPED_ADAPTER_STATUS",
                "error_message": "unmapped adapter status %r" % (status,)}


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
