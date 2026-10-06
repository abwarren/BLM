"""Auto-Bet BROWSER/EXTENSION EXECUTION BRIDGE — the interface + one reference
implementation + an auditable bridge ledger.

WHAT THIS IS
------------
The last hop of the Auto-Bet pipeline is *execution*: a fully validated
command (``blm_v4/betting/command.py`` → ``rung.validate_execution`` +
``stake.resolve_stake``) must be handed to the browser/extension surface that
actually drives PokerBet, and the *honest* outcome must be written to the
ledger.

That hop is the one thing this environment cannot exercise for real:
there is **no loadable browser extension/userscript in-repo** and there is
**no authenticated headless PokerBet window** (see
``docs/autobet/RUNG_PRODUCTION_WIRING_GATE.md`` PW-02 / PW-03 / PW-14).  This
module therefore delivers the bridge **INTERFACE** — the contract the real
browser hop must satisfy — and proves it against the deterministic fake browser
adapter that already exists (``tests/fake_browser_adapter.py``, which implements
the ``SelectionResolver`` browser contract in ``blm_v4/execution/adapter.py``).
No new DOM abstraction is invented: the browser side stays ``SelectionResolver``.

THE SAFETY ENVELOPE (the eight non-negotiable properties)
---------------------------------------------------------
The bridge refuses to place anything unless every one of these holds; each is
pinned by its own NAMED test in
``tests/test_autobet_browser_bridge_2026_10_04.py``:

  1. ZERO-STAKESUBMITS NOTHING — a ``ZERO_STAKE`` command resolves to a NO_OP;
     the browser is never clicked and never asked to place.
  2. FAIL-CLOSED — a command that is not ``decision == ALLOW`` /
     ``state == VALIDATED``, or whose identity/market/selection/stake is
     missing or ambiguous, is REJECTED before the browser is touched.
  3. IDEMPOTENCY — a command id is claimed exactly once; a second delivery of
     the same command never reaches the browser.
  4. TIMEOUT != SUCCESS — a submission the browser cannot confirm, or a bridge
     call that times out, is reported UNKNOWN, never ACCEPTED/EXECUTED.
  5. EXACT R2.00 — in ``REAL_MONEY_TEST`` the stake must equal exactly 2.00
     ZAR (no rounding drift); anything else is REJECTED before submission.
  6. DUPLICATE-COMMAND REJECTION — a replayed command is REJECTED, not
     executed twice.
  7. LEDGER STATE TRANSITIONS — every command walks an auditable state machine
     (claimed → SENT → terminal); there is no silent loss.
  8. EXECUTION FAILURE HANDLING — a browser/bridge error leaves the ledger in a
     consistent TERMINAL state and surfaces the failure.

FAIL-CLOSED BY CONSTRUCTION
---------------------------
No branch defaults to "allow".  When in doubt the bridge refuses and returns a
terminal REJECTED/FAILED/UNKNOWN state with a stable reason string.  This module
never constructs a real provider, never reads credentials and never performs
I/O — it only drives an injected browser adapter.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

from blm_v4.betting import command as _cmd
from blm_v4.betting import stake as _stake
from blm_v4.execution.adapter import (
    AdapterUnavailable,
    MarketObservation,
    SelectionResolver,
)
from blm_v4.execution.betslip_verifier import VERIFIED, verify_leg_in_betslip
from blm_v4.execution.selection_model import Selection
from blm_v4.execution.selection_resolver import resolve_selection

# ── outcome vocabulary ──────────────────────────────────────────────────────
#: honest submission outcomes (mirrors the provider/SelectionResolver contract)
SUBMITTED = "SUBMITTED"
ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"

#: the bridge command states (the ONE shared vocabulary — command.py)
CREATED = _cmd.CREATED
VALIDATED = _cmd.VALIDATED
SENT = _cmd.SENT
ACKNOWLEDGED = _cmd.ACKNOWLEDGED
EXECUTED = _cmd.EXECUTED
BRIDGE_REJECTED = _cmd.REJECTED
BRIDGE_FAILED = _cmd.FAILED
BRIDGE_UNKNOWN = _cmd.UNKNOWN

#: bridge-local terminal state: a ZERO_STAKE command resolved to "submit nothing".
NO_OP = "NO_OP"

#: terminal states — a command that reaches one of these will never move again.
TERMINAL_STATES = frozenset(
    {EXECUTED, BRIDGE_REJECTED, BRIDGE_FAILED, BRIDGE_UNKNOWN, NO_OP})

# ── stable refusal reasons (auditable strings) ──────────────────────────────
R_COMMAND_MISSING = "bridge_command_missing"
R_NOT_VALIDATED = "bridge_command_not_validated"
R_IDENTITY = "bridge_command_identity_missing"
R_STAKE_INVALID = "bridge_stake_invalid"
R_TEST_STAKE = "bridge_test_stake_not_exact_2_00"
R_UNIT_DRIFT = "bridge_unit_size_drift"
R_DUPLICATE = "duplicate_command"
R_ZERO_STAKE = "zero_stake_no_submission"
R_TIMEOUT = "bridge_timeout_unconfirmed"
R_ADAPTER_DOWN = "browser_disconnected"
R_ADAPTER_ERROR = "bridge_adapter_error"
# live-market-observation + authenticated-session gates (2026-10-05 directive)
R_SESSION_EXPIRED = "session_not_authenticated"
R_OBSERVE_FAILED = "market_not_resolved"
R_LINE_MISSING = "executable_line_missing"
R_ODDS_MISSING = "executable_odds_missing"
R_LINE_MOVED = "line_moved_vs_position"


# ── exceptions ──────────────────────────────────────────────────────────────
class BridgeError(Exception):
    """Base class for a structured bridge failure."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


class BridgeTimeout(BridgeError):
    """The bridge call timed out — the outcome is NOT known.  Never success."""

    def __init__(self, message: str = "bridge call timed out"):
        super().__init__("BRIDGE_TIMEOUT", message)


class BridgeUnavailable(BridgeError):
    """The browser/extension cannot be reached at all (tab closed, CDP down)."""

    def __init__(self, message: str = "browser unavailable"):
        super().__init__("BRIDGE_UNAVAILABLE", message)


# ── the receipt every execution returns ─────────────────────────────────────
@dataclass
class BridgeReceipt:
    """The auditable outcome of one bridge execution."""

    command_id: Optional[str]
    idempotency_key: Optional[str]
    state: str
    status: str
    reason: Optional[str] = None
    provider_ref: Optional[str] = None
    stake_amount: Optional[float] = None
    # the LIVE market observation captured at execution time (the ACTUAL
    # executable line + decimal odds) — the caller persists these onto the
    # POSITION.  ``observed_odds`` is DECIMAL ODDS, never the line.
    observed_line: Optional[float] = None
    observed_odds: Optional[float] = None
    submitted: bool = False
    history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "idempotency_key": self.idempotency_key,
            "state": self.state,
            "status": self.status,
            "reason": self.reason,
            "provider_ref": self.provider_ref,
            "stake_amount": self.stake_amount,
            "observed_line": self.observed_line,
            "observed_odds": self.observed_odds,
            "submitted": self.submitted,
            "history": list(self.history),
        }


# ── the auditable bridge ledger (claimed → terminal; no silent loss) ────────
class BridgeLedger:
    """A thread-safe, append-only audit of bridge executions.

    ``claim`` is the unique command-id slot (idempotency); ``transition`` moves
    the slot and appends to the audit; ``audit`` records an event that never
    claimed a slot (e.g. a fail-closed rejection).  ``history`` is the full,
    ordered chain — a completed execution ALWAYS ends in a terminal state, so
    nothing can be silently lost.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: dict[str, str] = {}            # command_id → state
        self._audit: list[dict] = []

    def claim(self, command_id: str) -> tuple[bool, Optional[str]]:
        """Atomically take the unique command-id slot.  Returns
        ``(claimed, existing_state)``."""
        with self._lock:
            if command_id in self._claims:
                return False, self._claims[command_id]
            self._claims[command_id] = CREATED
            self._audit.append({"command_id": command_id, "state": CREATED,
                                "reason": "claimed", "provider_ref": None,
                                "status": None, "at": _now()})
            return True, CREATED

    def transition(self, command_id: str, state: str, *,
                   reason: Optional[str] = None,
                   provider_ref: Optional[str] = None,
                   status: Optional[str] = None) -> None:
        with self._lock:
            self._claims[command_id] = state
            self._audit.append({"command_id": command_id, "state": state,
                                "reason": reason, "provider_ref": provider_ref,
                                "status": status, "at": _now()})

    def audit(self, command_id: Optional[str], state: str, *,
              reason: Optional[str] = None,
              provider_ref: Optional[str] = None,
              status: Optional[str] = None) -> None:
        with self._lock:
            self._audit.append({"command_id": command_id, "state": state,
                                "reason": reason, "provider_ref": provider_ref,
                                "status": status, "at": _now()})

    def state(self, command_id: str) -> Optional[str]:
        with self._lock:
            return self._claims.get(command_id)

    def history(self, command_id: str) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._audit
                    if e.get("command_id") == command_id]

    def is_terminal(self, command_id: str) -> bool:
        with self._lock:
            return self._claims.get(command_id) in TERMINAL_STATES


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ══════════════════════════════════════════════════════════════════════════
# THE BRIDGE INTERFACE
# ══════════════════════════════════════════════════════════════════════════
class BrowserBridge(ABC):
    """The contract the browser/extension execution surface must satisfy.

    A real implementation (the PokerBet Playwright/DOM layer, or a future
    extension) implements :meth:`place` and :meth:`confirm`; the Auto-Bet
    safety envelope in :class:`ExecutionBridge` sits on top and never has to
    know how the browser works.  ``place`` MUST return an HONEST outcome —
    never an assumption that a click succeeded.
    """

    name: str = "abstract"

    @abstractmethod
    def place(self, *, command: dict, stake_amount: float) -> dict:
        """Place ONE bet for a validated ``command`` at ``stake_amount``.

        Returns ``{"status": SUBMITTED|ACCEPTED|REJECTED|FAILED|UNKNOWN,
        "provider_ref": ..., "error_code": ..., "error_message": ...}``.
        Raises :class:`BridgeTimeout` when the call times out and
        :class:`BridgeUnavailable` when the browser cannot be reached.
        """

    @abstractmethod
    def confirm(self, provider_ref: Optional[str]) -> Optional[dict]:
        """The browser's OWN confirmation receipt for a submission, or None."""

    def is_available(self) -> bool:  # optional capability probe
        return True

    def is_authenticated(self) -> bool:  # optional capability probe
        """True when the attached bookmaker session is SIGNED IN.  Defaults to
        True for a transport with no session concept; the DOM/CDP bridge
        overrides it.  An unauthenticated / expired / logged-out session must
        return False → NO BET."""
        return True

    def observe(self, command: dict) -> Optional[MarketObservation]:
        """Resolve the CURRENT market offer for the command's selection and
        return a ``MarketObservation`` (live line + decimal odds), or None.

        Optional; a transport that cannot observe returns None, which the
        safety envelope treats as a refusal (fail closed)."""
        return None


class ResolverBrowserBridge(BrowserBridge):
    """Reference bridge over the existing ``SelectionResolver`` browser contract.

    Drives ONE Auto-Bet bet through the browser: resolve the CURRENT selection
    (fresh read), click it, verify the betslip actually updated, place the
    parlay at the validated stake, then read back the bookmaker's confirmation.
    A click is never proof — the betslip is re-read from the adapter before
    placement, exactly as the directive requires.

    The ``SelectionResolver`` fakes in the test suite (success / reject / error
    / unconfirmed) drive every branch; no real browser is touched.
    """

    name = "resolver_browser"

    def __init__(self, adapter: SelectionResolver, *,
                 event_resolver: Optional[Callable[[dict], str]] = None,
                 session_probe: Optional[Callable[[], bool]] = None,
                 verify_timeout_s: float = 20.0,
                 verify_poll_s: float = 0.5):
        self.adapter = adapter
        self._event = event_resolver or _default_event
        self._session_probe = session_probe
        # Production rule (2026-10-06): PERSIST the betslip verification —
        # poll until the slip shows the leg (identity) instead of failing on
        # the first read of an SPA that has not re-rendered yet.
        self._verify_timeout_s = float(verify_timeout_s)
        self._verify_poll_s = float(verify_poll_s)

    def is_available(self) -> bool:
        """Optional capability probe.  A ``SelectionResolver`` has no cheap
        side-effect-free probe, so the reference bridge reports available; a
        real transport may override this (e.g. an attached CDP target)."""
        return True

    def is_authenticated(self) -> bool:
        """The attached browser's session state.  With no injected probe the
        bridge keeps its pre-existing behaviour (True); a real run MUST inject
        a probe (``pokerbet.session.detect_authenticated``).  A probe returning
        False OR raising ⇒ NOT authenticated ⇒ NO BET (fail closed)."""
        if self._session_probe is None:
            return True
        try:
            return bool(self._session_probe())
        except Exception:  # noqa: BLE001 — an unprovable session is not a session
            return False

    def observe(self, command: dict):
        """Resolve the CURRENT market offer for the command's selection.

        Returns the live ``MarketObservation`` (``line`` = the current betting
        LINE, ``price`` = the current DECIMAL ODDS) or None when it cannot be
        resolved (fail closed).  The two are distinct: the line is NEVER odds."""
        event = self._event(command)
        sel = Selection(
            event=event, market=str(command.get("market") or "TOTAL"),
            position=str(command.get("selection") or "UNDER"),
            game_id=str(command.get("game_id") or "") or None)
        try:
            res = resolve_selection(self.adapter, sel)
        except AdapterUnavailable as e:
            raise BridgeUnavailable(str(e))
        return res.observation if res.ok else None

    def place(self, *, command: dict, stake_amount: float) -> dict:
        import time as _time
        event = self._event(command)
        market = str(command.get("market") or "TOTAL")
        position = str(command.get("selection") or "UNDER")
        sel = Selection(event=event, market=market, position=position,
                        game_id=str(command.get("game_id") or "") or None)

        # 0. CLEAR stale UNSUBMITTED legs (production rule 2026-10-06: stale
        #    tickets are ALWAYS cleared before an attempt — failed attempts
        #    otherwise accumulate legs and break verification).
        clear = getattr(self.adapter, "clear_betslip", None)
        if callable(clear):
            try:
                clear()
            except Exception:
                pass

        # 1. RESOLVE the CURRENT selection (fresh read; never a stored line)
        try:
            res = resolve_selection(self.adapter, sel)
        except AdapterUnavailable as e:
            raise BridgeUnavailable(str(e))
        if not res.ok:
            # a resolution failure is DEFINITIVE — no bet was placed
            return {"status": FAILED, "provider_ref": None,
                    "error_code": res.reason, "error_message": res.reason}
        obs = res.observation
        if obs is None:  # defensive: ok implies an observation, but fail closed
            return {"status": FAILED, "provider_ref": None,
                    "error_code": "DOM_CHANGED",
                    "error_message": "resolution returned no observation"}

        # 2. CLICK the current selection (a failed click registered nothing)
        try:
            clicked = self.adapter.click_selection(obs)
        except AdapterUnavailable as e:
            raise BridgeUnavailable(str(e))
        except Exception as e:  # noqa: BLE001 — click failure is definitive
            return {"status": FAILED, "provider_ref": None,
                    "error_code": "CLICK_ERROR", "error_message": str(e)}
        if not clicked:
            return {"status": FAILED, "provider_ref": None,
                    "error_code": "CLICK_NOT_REGISTERED",
                    "error_message": "click not registered"}

        # 3. VERIFY — PERSIST (production rule): poll until the slip shows the
        #    leg (identity, line-movement tolerant).  A single read must never
        #    fail while the SPA is still re-rendering.
        check = None
        deadline = _time.monotonic() + self._verify_timeout_s
        while True:
            try:
                check = verify_leg_in_betslip(self.adapter, sel, obs.line,
                                              obs.price)
            except AdapterUnavailable as e:
                raise BridgeUnavailable(str(e))
            except Exception:  # noqa: BLE001
                check = None
            if check is not None and check.outcome == VERIFIED:
                break
            if _time.monotonic() >= deadline:
                break
            _time.sleep(self._verify_poll_s)
        if check is None or check.outcome != VERIFIED:
            oc = check.outcome if check is not None else "BETSLIP_UNREADABLE"
            return {"status": FAILED, "provider_ref": None,
                    "error_code": oc,
                    "error_message": f"betslip not verified: {oc}"}

        # 4. PLACE — the outcome is whatever the browser honestly reports
        try:
            out = self.adapter.place_parlay(stake_amount)
        except AdapterUnavailable as e:
            raise BridgeUnavailable(str(e))
        out = dict(out or {})
        out.setdefault("status", UNKNOWN)
        out.setdefault("provider_ref", None)
        # the LIVE observation actually resolved+clicked — the current betting
        # LINE and the DECIMAL ODDS (distinct; never the line as odds).
        out["observed_line"] = obs.line
        out["observed_odds"] = obs.price
        return out

    def confirm(self, provider_ref: Optional[str]) -> Optional[dict]:
        try:
            return self.adapter.read_order_confirmation()
        except Exception:  # noqa: BLE001 — no receipt is None, never a guess
            return None


def _default_event(command: dict) -> str:
    """The event label the browser resolves.  Callers may carry an explicit
    ``event`` on the command; otherwise the canonical ``game_id`` is used."""
    return str(command.get("event") or command.get("game_id") or "")


# ══════════════════════════════════════════════════════════════════════════
# THE SAFETY ENVELOPE
# ══════════════════════════════════════════════════════════════════════════
class ExecutionBridge:
    """Executes a VALIDATED Auto-Bet command on a browser bridge, enforcing the
    eight safety properties and recording an auditable ledger transition chain.
    """

    def __init__(self, bridge: BrowserBridge, *,
                 ledger: Optional[BridgeLedger] = None):
        self.bridge = bridge
        self.ledger = ledger if ledger is not None else BridgeLedger()

    # ── public entry point ──────────────────────────────────────────────
    def execute(self, command: Optional[dict], *,
                expected_unit_size: Optional[float] = None) -> BridgeReceipt:
        """Validate → claim → submit → record.  Never raises for a refusal.

        The browser is touched ONLY when the command is fully validated, the
        stake is exact, and this command id has not already been claimed.
        """
        # ── (2) FAIL-CLOSED: only a fully validated command may execute ──
        if not isinstance(command, dict):
            return self._refuse(None, None, R_COMMAND_MISSING,
                                "no command supplied")
        cid = command.get("command_id")
        ik = command.get("idempotency_key")
        if cid is None or ik is None:
            reason = R_IDENTITY
            return self._refuse(cid, ik, reason, "command identity missing")
        if (command.get("decision") != _cmd.ALLOW
                or command.get("state") != _cmd.VALIDATED):
            return self._refuse(cid, ik, R_NOT_VALIDATED,
                                f"decision={command.get('decision')!r} "
                                f"state={command.get('state')!r}")
        if not str(command.get("game_id") or "").strip():
            return self._refuse(cid, ik, R_IDENTITY, "game_id missing")

        mode = command.get("mode")
        stk = command.get("stake") or {}
        amount = stk.get("stake")

        # ── (1) ZERO-STAKESUBMITS NOTHING ───────────────────────────────
        if mode == _stake.ZERO_STAKE or amount == 0:
            self.ledger.audit(cid, NO_OP, reason=R_ZERO_STAKE,
                              status=NO_OP)
            return BridgeReceipt(cid, ik, NO_OP, NO_OP, reason=R_ZERO_STAKE,
                                 stake_amount=0.0, submitted=False,
                                 history=self.ledger.history(cid))

        # ── (5) EXACT STAKE: R2.00 exact / unit size verbatim ───────────
        exact_error = self._stake_error(command, mode, amount,
                                        expected_unit_size)
        if exact_error is not None:
            return self._refuse(cid, ik, exact_error[0], exact_error[1])

        # ── AUTHENTICATED SESSION is mandatory (unauth/expired/logged-out → NO BET) ──
        try:
            authed = bool(self.bridge.is_authenticated())
        except Exception:  # noqa: BLE001 — an unprovable session is not a session
            authed = False
        if not authed:
            return self._refuse(cid, ik, R_SESSION_EXPIRED,
                                "bookmaker session not authenticated")

        # ── LIVE MARKET OBSERVATION — the ACTUAL executable line + DECIMAL ODDS ──
        obs_line: Optional[float] = None
        obs_odds: Optional[float] = None
        observe = getattr(self.bridge, "observe", None)
        if callable(observe):
            try:
                obs = observe(command)
            except (BridgeTimeout, TimeoutError) as e:
                return self._terminal(cid, ik, BRIDGE_UNKNOWN, UNKNOWN,
                                      reason=R_TIMEOUT, detail=str(e))
            except (BridgeUnavailable, AdapterUnavailable) as e:
                return self._terminal(cid, ik, BRIDGE_FAILED, FAILED,
                                      reason=R_ADAPTER_DOWN, detail=str(e))
            except Exception as e:  # noqa: BLE001
                return self._terminal(cid, ik, BRIDGE_UNKNOWN, UNKNOWN,
                                      reason=R_ADAPTER_ERROR, detail=str(e))
            if obs is None:
                return self._refuse(cid, ik, R_OBSERVE_FAILED,
                                    "current market could not be resolved")
            obs_line = getattr(obs, "line", None)
            obs_odds = getattr(obs, "price", None)
            # FAIL CLOSED and NEVER substitute: the line and the odds are distinct.
            if obs_line is None:
                return self._refuse(cid, ik, R_LINE_MISSING, "no live line")
            if obs_odds is None:
                return self._refuse(cid, ik, R_ODDS_MISSING,
                                    "no live decimal odds")
            # a position line, when supplied, must equal the live line (stale → NO BET)
            exp = command.get("executable_line")
            if exp is not None:
                try:
                    if abs(float(exp) - float(obs_line)) > 1e-9:
                        return self._refuse(
                            cid, ik, R_LINE_MOVED,
                            f"position line {exp} != live line {obs_line}")
                except (TypeError, ValueError):
                    return self._refuse(cid, ik, R_LINE_MISSING,
                                        f"position line not numeric: {exp!r}")

        # ── (3)+(6) IDEMPOTENCY / DUPLICATE: claim the command id once ──
        claimed, existing = self.ledger.claim(cid)
        if not claimed:
            self.ledger.audit(cid, BRIDGE_REJECTED, reason=R_DUPLICATE,
                              status=existing)
            return BridgeReceipt(cid, ik, BRIDGE_REJECTED, BRIDGE_REJECTED,
                                 reason=R_DUPLICATE, stake_amount=amount,
                                 submitted=False,
                                 history=self.ledger.history(cid))

        # ── SENT: the command leaves the validation layer for the browser ─
        self.ledger.transition(cid, SENT, reason="dispatching to browser",
                               status=SENT)
        return self._dispatch(cid, ik, command, float(amount),  # type: ignore[arg-type]
                              obs_line, obs_odds)

    # ── submission + honest-outcome mapping ─────────────────────────────
    def _dispatch(self, cid: str, ik: str, command: dict,
                  amount: float,
                  obs_line: Optional[float] = None,
                  obs_odds: Optional[float] = None) -> BridgeReceipt:
        try:
            out = self.bridge.place(command=command, stake_amount=amount)
        except (BridgeTimeout, TimeoutError) as e:
            return self._terminal(cid, ik, BRIDGE_UNKNOWN, UNKNOWN,
                                  reason=R_TIMEOUT, detail=str(e),
                                  observed_line=obs_line, observed_odds=obs_odds)
        except (BridgeUnavailable, AdapterUnavailable) as e:
            return self._terminal(cid, ik, BRIDGE_FAILED, FAILED,
                                  reason=R_ADAPTER_DOWN, detail=str(e),
                                  observed_line=obs_line, observed_odds=obs_odds)
        except BridgeError as e:
            return self._terminal(cid, ik, BRIDGE_FAILED, FAILED,
                                  reason=e.code, detail=str(e),
                                  observed_line=obs_line, observed_odds=obs_odds)
        except Exception as e:  # noqa: BLE001 — after dispatch: outcome unknown
            return self._terminal(cid, ik, BRIDGE_UNKNOWN, UNKNOWN,
                                  reason=R_ADAPTER_ERROR, detail=str(e),
                                  observed_line=obs_line, observed_odds=obs_odds)

        status = str(out.get("status") or UNKNOWN).upper()
        ref = out.get("provider_ref")
        # the LIVE observation actually resolved+clicked (line + decimal odds)
        ol = out.get("observed_line")
        if ol is None:
            ol = obs_line
        oo = out.get("observed_odds")
        if oo is None:
            oo = obs_odds

        if status == ACCEPTED:
            self.ledger.transition(cid, ACKNOWLEDGED, reason="accepted",
                                   provider_ref=ref, status=status)
            conf = None
            try:
                conf = self.bridge.confirm(ref)
            except Exception:  # noqa: BLE001 — no receipt ⇒ UNKNOWN, not success
                conf = None
            if conf:  # the bookmaker's OWN receipt → authoritative confirmation
                self.ledger.transition(cid, EXECUTED,
                                       reason="confirmed by bookmaker",
                                       provider_ref=ref, status=status)
                return BridgeReceipt(cid, ik, EXECUTED, status,
                                     provider_ref=ref, stake_amount=amount,
                                     observed_line=ol, observed_odds=oo,
                                     submitted=True,
                                     history=self.ledger.history(cid))
            # accepted but never confirmed → UNKNOWN (never report success)
            return self._terminal(cid, ik, BRIDGE_UNKNOWN, UNKNOWN,
                                  reason=R_TIMEOUT, provider_ref=ref,
                                  observed_line=ol, observed_odds=oo)

        if status == REJECTED:
            return self._terminal(cid, ik, BRIDGE_REJECTED, REJECTED,
                                  reason=out.get("error_code") or "rejected",
                                  provider_ref=ref,
                                  observed_line=ol, observed_odds=oo)
        if status == FAILED:
            return self._terminal(cid, ik, BRIDGE_FAILED, FAILED,
                                  reason=out.get("error_code") or "failed",
                                  provider_ref=ref,
                                  observed_line=ol, observed_odds=oo)
        # SUBMITTED / UNKNOWN / anything unmapped → UNKNOWN (timeout ≠ success)
        return self._terminal(cid, ik, BRIDGE_UNKNOWN, UNKNOWN,
                              reason=R_TIMEOUT, provider_ref=ref,
                              observed_line=ol, observed_odds=oo)

    # ── stake authority (reuses blm_v4/betting/stake.py — never re-derived) ─
    @staticmethod
    def _stake_error(command: dict, mode, amount,
                     expected_unit_size) -> Optional[tuple[str, str]]:
        """Return ``(reason, detail)`` when the stake is not exactly what the
        mode permits, else None.  No rounding: the exact float is compared."""
        if mode not in _stake.MODES:
            return (R_STAKE_INVALID, f"unknown mode {mode!r}")
        a = amount
        if a is None or isinstance(a, bool):
            return (R_STAKE_INVALID, "stake missing")
        try:
            a = float(a)
        except (TypeError, ValueError):
            return (R_STAKE_INVALID, f"stake not numeric: {amount!r}")
        import math
        if not math.isfinite(a) or a <= 0:
            return (R_STAKE_INVALID, f"stake non-positive/non-finite: {a}")

        if mode == _stake.REAL_MONEY_TEST:
            # EXACT R2.00 ZAR — above AND below are rejected (not a ceiling)
            if not _stake.is_authorized_test_stake(a, _stake.CURRENCY):
                return (R_TEST_STAKE,
                        f"real-money test stake must be exactly "
                        f"{_stake.REAL_MONEY_TEST_STAKE:.2f} ZAR, got {a}")
            return None
        if mode == _stake.PRODUCTION_AUTO_BET:
            check = _stake.validate_unit_size(a, _stake.CURRENCY)
            if not check["valid"]:
                return (R_STAKE_INVALID, check["reason"] or "unit size invalid")
            if expected_unit_size is not None:
                exp = float(expected_unit_size)
                if a != exp:  # verbatim — no silent rounding
                    return (R_UNIT_DRIFT,
                            f"stake {a} != configured unit size {exp}")
            return None
        return (R_STAKE_INVALID, f"unhandled mode {mode!r}")

    # ── helpers ─────────────────────────────────────────────────────────
    def _refuse(self, cid, ik, reason, detail) -> BridgeReceipt:
        self.ledger.audit(cid, BRIDGE_REJECTED, reason=reason,
                          status=BRIDGE_REJECTED)
        return BridgeReceipt(cid, ik, BRIDGE_REJECTED, BRIDGE_REJECTED,
                             reason=reason, submitted=False,
                             history=self.ledger.history(cid) if cid
                             else [])

    def _terminal(self, cid, ik, state, status, *, reason,
                  detail: Optional[str] = None,
                  provider_ref=None,
                  observed_line: Optional[float] = None,
                  observed_odds: Optional[float] = None) -> BridgeReceipt:
        r = reason if not detail else f"{reason}: {detail}"
        self.ledger.transition(cid, state, reason=r, provider_ref=provider_ref,
                               status=status)
        return BridgeReceipt(cid, ik, state, status, reason=r,
                             provider_ref=provider_ref, submitted=False,
                             observed_line=observed_line,
                             observed_odds=observed_odds,
                             history=self.ledger.history(cid))


def execute_command(command: dict, bridge: BrowserBridge, *,
                    ledger: Optional[BridgeLedger] = None,
                    expected_unit_size: Optional[float] = None) -> BridgeReceipt:
    """Convenience: run ONE command through a fresh (or shared) bridge."""
    return ExecutionBridge(bridge, ledger=ledger).execute(
        command, expected_unit_size=expected_unit_size)


__all__ = [
    "BrowserBridge", "ResolverBrowserBridge", "ExecutionBridge",
    "BridgeLedger", "BridgeReceipt", "BridgeError", "BridgeTimeout",
    "BridgeUnavailable", "execute_command",
    "SUBMITTED", "ACCEPTED", "REJECTED", "FAILED", "UNKNOWN",
    "CREATED", "VALIDATED", "SENT", "ACKNOWLEDGED", "EXECUTED",
    "BRIDGE_REJECTED", "BRIDGE_FAILED", "BRIDGE_UNKNOWN", "NO_OP",
    "TERMINAL_STATES",
]
