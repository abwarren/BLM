"""THE BET STATE MACHINE (directive §3) — explicit states, explicit
transitions, fail closed on anything else.

    SIGNAL → ELIGIBLE → PRECHECK → SUBMITTING → ACCEPTED → CONFIRMED → SETTLED

Failure states:
    PRECHECK     → REJECTED
    SUBMITTING   → SUBMISSION_FAILED
    SUBMITTING   → UNKNOWN
    UNKNOWN      → RECONCILING
    RECONCILING  → CONFIRMED
    RECONCILING  → REJECTED

The matrix below is the SINGLE authority for what may follow what.  The
engine and the store both validate through :func:`validate_transition`;
an invalid transition raises :class:`InvalidTransition` — callers must
NEVER catch-and-continue: an out-of-matrix move is a defect, not a
recoverable condition.  In particular no path exists from SIGNAL to
CONFIRMED (or from SIGNAL to any post-submission state): a bet can only
reach CONFIRMED via ACCEPTED or RECONCILING.

The states are a vocabulary SHARED with the pre-existing execution
ledger (``BettingStore`` §5 vocabulary): PENDING/SUBMITTED/BLOCKED/...  stay
valid there; this module adds the directive's finer states (ELIGIBLE,
PRECHECK, SUBMISSION_FAILED, CONFIRMED, SETTLED) on top without
renaming anything in the production ledger.
"""
from __future__ import annotations

from typing import FrozenSet, Optional, Tuple

# ── states ─────────────────────────────────────────────────────────────
SIGNAL = "SIGNAL"
ELIGIBLE = "ELIGIBLE"
PRECHECK = "PRECHECK"
SUBMITTING = "SUBMITTING"
ACCEPTED = "ACCEPTED"
CONFIRMED = "CONFIRMED"
SETTLED = "SETTLED"
REJECTED = "REJECTED"
SUBMISSION_FAILED = "SUBMISSION_FAILED"
UNKNOWN = "UNKNOWN"
RECONCILING = "RECONCILING"

#: every state the machine knows (one flat vocabulary)
STATES: FrozenSet[str] = frozenset({
    SIGNAL, ELIGIBLE, PRECHECK, SUBMITTING, ACCEPTED, CONFIRMED,
    SETTLED, REJECTED, SUBMISSION_FAILED, UNKNOWN, RECONCILING,
})

#: happy path + every legal failure path (directive §3, verbatim)
_TRANSITIONS: Tuple[Tuple[str, str], ...] = (
    (SIGNAL, ELIGIBLE),
    (ELIGIBLE, PRECHECK),
    (PRECHECK, SUBMITTING),
    (PRECHECK, REJECTED),
    (SUBMITTING, ACCEPTED),
    (SUBMITTING, SUBMISSION_FAILED),
    (SUBMITTING, UNKNOWN),
    (UNKNOWN, RECONCILING),
    (RECONCILING, CONFIRMED),
    (RECONCILING, REJECTED),
    (ACCEPTED, CONFIRMED),
    (CONFIRMED, SETTLED),
)

#: (from, to) → True.  THE transition matrix.
TRANSITIONS: dict[tuple[str, str], bool] = {t: True for t in _TRANSITIONS}

#: states that end a bet's life
TERMINAL_STATES: FrozenSet[str] = frozenset({
    SETTLED, REJECTED, SUBMISSION_FAILED,
})


class InvalidTransition(Exception):
    """An out-of-matrix transition was attempted.  Fail closed."""


def validate_transition(current: str, new: str) -> None:
    """Raise :class:`InvalidTransition` unless ``current → new`` is in
    the matrix.  Unknown states are invalid by construction."""
    if current not in STATES or new not in STATES \
            or (current, new) not in TRANSITIONS:
        raise InvalidTransition(
            f"illegal bet-state transition {current!r} → {new!r}")


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def can_transition(current: str, new: str) -> bool:
    """Non-raising form of :func:`validate_transition`."""
    try:
        validate_transition(current, new)
        return True
    except InvalidTransition:
        return False


def next_states(state: str) -> FrozenSet[str]:
    """The legal successors of ``state`` (empty for illegal/terminal)."""
    return frozenset(dst for (src, dst) in TRANSITIONS if src == state)


def path_exists(state: str, target: str, _seen: Optional[set] = None) -> bool:
    """Whether a legal transition path leads from ``state`` to ``target``
    (the §3 reachability assertions are computed, not hand-asserted)."""
    if state == target:
        return True
    seen = _seen if _seen is not None else set()
    if state in seen:
        return False
    seen.add(state)
    return any(path_exists(dst, target, seen)
               for dst in next_states(state))


def summary() -> dict:
    """A compact machine description for logs/tests/dashboards."""
    return {
        "states": sorted(STATES),
        "transitions": sorted(f"{a}->{b}" for (a, b) in TRANSITIONS),
        "terminal": sorted(TERMINAL_STATES),
    }
