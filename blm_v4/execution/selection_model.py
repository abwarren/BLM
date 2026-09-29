"""BLM EXECUTION — SELECTION MODEL (phase ①, directive 2026-09-22).

The permanent selection identity is EVENT + MARKET + POSITION —
NEVER a line or price.  The line and price are temporary bookmaker
state, resolved fresh from the bookmaker DOM at execution time.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

# ── MVP scope lock (directive §scope): the ONLY supported market ────
MARKET_TOTAL = "TOTAL"
POSITION_OVER = "OVER"
POSITION_UNDER = "UNDER"
POSITIONS = (POSITION_OVER, POSITION_UNDER)


def validate_selection(market: str, position: str) -> None:
    """Fail fast on anything outside the MVP scope.

    The directive is explicit: TOTAL → OVER | UNDER only.  Spreads,
    handicaps, moneylines, team totals, props and player markets must
    NOT reach the engine — an unknown market/position is a construction
    error, not a runtime recoverable condition.
    """
    if market != MARKET_TOTAL:
        raise ValueError(f"unsupported market {market!r}: MVP is TOTAL only")
    if position not in POSITIONS:
        raise ValueError(
            f"unsupported position {position!r}: MVP is OVER/UNDER only")


# ── Execution modes (hard ladder — never a silent transition) ───────
# DRY_RUN      resolve everything, click nothing, place nothing.
# RESOLVE_ONLY legacy alias of DRY_RUN (directive §17 wording).
# TEST_VERIFY  exercise the betslip VERIFIER against the live slip,
#              still never clicking a selection and never placing.
# LIVE         real selection, real placement.  Requires the env var
#              EXECUTION_DRY_RUN=false; the UI must show 🔴 LIVE.
MODE_DRY_RUN = "DRY_RUN"
MODE_RESOLVE_ONLY = "RESOLVE_ONLY"
MODE_TEST_VERIFY = "TEST_VERIFY"
MODE_LIVE = "LIVE"
MODES = (MODE_DRY_RUN, MODE_RESOLVE_ONLY, MODE_TEST_VERIFY, MODE_LIVE)


@dataclass(frozen=True)
class Selection:
    """One leg's PERMANENT identity.

    ``snapshot_line`` / ``snapshot_price`` are informational market
    state captured at creation time ONLY — they never take part in
    equality, hashing or execution matching.

    ``game_id`` is the CANONICAL BLM game binding (the directive's
    "verify: game ID").  It is deliberately OUTSIDE equality/hash (the
    matrix treats same-name events as the same event) — its role is
    VERIFICATION: the betslip verifier rejects a slip entry that names
    the right teams but carries a different game id, the pre-placement
    guard re-checks it, and the ledger stores it so every recorded
    execution joins back to the game's history.
    """

    event: str
    market: str = MARKET_TOTAL
    position: str = POSITION_UNDER
    snapshot_line: Optional[float] = None
    snapshot_price: Optional[float] = None
    game_id: Optional[str] = None
    selection_id: str = ""

    def __post_init__(self) -> None:
        validate_selection(self.market, self.position)
        if not self.event or not str(self.event).strip():
            raise ValueError("selection event is required")
        if not self.selection_id:
            object.__setattr__(
                self, "selection_id",
                f"{self.event}|{self.market}|{self.position}")

    # Line/price are deliberately EXCLUDED from identity comparisons.
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Selection):
            return NotImplemented
        return (self.event == other.event
                and self.market == other.market
                and self.position == other.position)

    def __hash__(self) -> int:
        return hash((self.event, self.market, self.position))

    def identity(self) -> dict:
        return {"event": self.event, "market": self.market,
                "position": self.position}

    def to_dict(self) -> dict:
        return {
            "selection_id": self.selection_id,
            "event": self.event,
            "market": self.market,
            "position": self.position,
            "snapshot_line": self.snapshot_line,
            "snapshot_price": self.snapshot_price,
            "game_id": self.game_id,
        }

    @staticmethod
    def from_dict(d: dict) -> "Selection":
        return Selection(
            event=str(d.get("event") or ""),
            market=str(d.get("market") or MARKET_TOTAL),
            position=str(d.get("position") or POSITION_UNDER),
            snapshot_line=d.get("snapshot_line"),
            snapshot_price=d.get("snapshot_price"),
            game_id=d.get("game_id"),
        )


# ── Parlay job states (the persistent execution state machine) ──────
# Recoverable states return to RESOLVING_LEG (a FRESH resolution —
# never a repeat click on stale DOM).  Terminal states end the job.
STATE_CREATED = "CREATED"
STATE_EXECUTION_STARTED = "EXECUTION_STARTED"
STATE_RESOLVING_LEG = "RESOLVING_LEG"
STATE_CURRENT_SELECTION_FOUND = "CURRENT_SELECTION_FOUND"
STATE_SELECTING_LEG = "SELECTING_LEG"
STATE_VERIFYING_LEG = "VERIFYING_LEG"
STATE_LEG_CONFIRMED = "LEG_CONFIRMED"
STATE_BETSLIP_READY = "BETSLIP_READY"
STATE_PLACING_PARLAY = "PLACING_PARLAY"
STATE_VERIFYING_ORDER = "VERIFYING_ORDER"
STATE_ORDER_PLACED = "ORDER_PLACED"
STATE_COMPLETE = "COMPLETE"
# Non-LIVE modes end here: everything ran (resolution, selection,
# verification) except the real submission — a stop BY CONSTRUCTION.
STATE_DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"
STATE_USER_ABORT = "USER_ABORT"
STATE_NON_RECOVERABLE = "NON_RECOVERABLE_ERROR"

STATES = (
    STATE_CREATED, STATE_EXECUTION_STARTED, STATE_RESOLVING_LEG,
    STATE_CURRENT_SELECTION_FOUND, STATE_SELECTING_LEG,
    STATE_VERIFYING_LEG, STATE_LEG_CONFIRMED, STATE_BETSLIP_READY,
    STATE_PLACING_PARLAY, STATE_VERIFYING_ORDER, STATE_ORDER_PLACED,
    STATE_COMPLETE, STATE_DRY_RUN_COMPLETE, STATE_USER_ABORT,
    STATE_NON_RECOVERABLE,
)

RECOVERABLE_STATES = (
    "SELECTION_CHANGED", "LINE_CHANGED", "PRICE_CHANGED",
    "POSITION_NOT_FOUND", "SELECTION_REJECTED", "CLICK_NOT_REGISTERED",
    "BETSLIP_NOT_UPDATED", "DOM_CHANGED", "STALE_ELEMENT",
    "TEMPORARY_TIMEOUT",
)
TERMINAL_FAILURE_REASONS = (
    "EVENT_NOT_FOUND", "MARKET_NOT_FOUND", "MAX_RETRIES_EXCEEDED",
    "WATCHDOG_TIMEOUT", "BROWSER_DISCONNECTED", "BETSLIP_VERIFICATION_FAILED",
    "MARKET_NOT_ACTIVE",
)

TERMINAL_STATES = (STATE_ORDER_PLACED, STATE_COMPLETE,
                   STATE_DRY_RUN_COMPLETE, STATE_USER_ABORT,
                   STATE_NON_RECOVERABLE)


class AbortRequested(Exception):
    """Raised internally when the abort event is observed — never
    escapes ``run()``; it becomes the USER_ABORT terminal state."""


@dataclass
class ParlayJob:
    """One generated combination — an independent execution job."""

    legs: list[Selection]
    fold_size: int
    parlay_id: str = ""
    combination_id: str = ""
    stake_amount: float = 0.0
    status: str = STATE_CREATED
    current_leg: int = 0            # 1-based index into legs
    retry_count: int = 0
    attempts_per_leg: list = field(default_factory=list)
    resolved_line: Optional[float] = None
    resolved_price: Optional[float] = None
    order_reference: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_error: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.parlay_id:
            object.__setattr__(
                self, "parlay_id",
                "P" + "-".join(l.selection_id[:24] for l in self.legs))
        if not self.combination_id:
            object.__setattr__(
                self, "combination_id",
                f"combo-{self.fold_size}-" + "+".join(
                    l.selection_id[:24] for l in self.legs))
        self.attempts_per_leg = [[] for _ in self.legs]

    @property
    def game_ids(self) -> list:
        """The canonical BLM game ids of this job's legs (deduplicated,
        order-preserving; legs without a binding contribute nothing).
        The directive's execution record requires the game id on every
        recorded execution — this is its single source."""
        seen: list = []
        for leg in self.legs:
            gid = getattr(leg, "game_id", None)
            if gid and gid not in seen:
                seen.append(gid)
        return seen

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def to_dict(self) -> dict:
        return {
            "parlay_id": self.parlay_id,
            "combination_id": self.combination_id,
            "fold_size": self.fold_size,
            "game_ids": self.game_ids,
            "stake_amount": self.stake_amount,
            "status": self.status,
            "current_leg": self.current_leg,
            "retry_count": self.retry_count,
            "legs": [l.to_dict() for l in self.legs],
            "attempts_per_leg": self.attempts_per_leg,
            "resolved_line": self.resolved_line,
            "resolved_price": self.resolved_price,
            "order_reference": self.order_reference,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "last_error": self.last_error,
        }


class AbortEvent:
    """The cooperative abort signal — checked BETWEEN EVERY STEP."""

    def __init__(self) -> None:
        self._ev = threading.Event()

    def request(self) -> None:
        self._ev.set()

    def is_requested(self) -> bool:
        return self._ev.is_set()

    def check(self, state: str = STATE_RESOLVING_LEG) -> None:
        """Raise AbortRequested if an abort is pending (state names the
        step the engine was about to take — recorded for the audit)."""
        if self._ev.is_set():
            raise AbortRequested(f"abort requested during {state}")

    def reset(self) -> None:
        self._ev.clear()
