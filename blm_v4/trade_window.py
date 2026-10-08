"""The UNDER trade EXECUTION WINDOW — the ONE definition.

The autonomous UNDER trade is placed only while the game's progress sits
inside this band.  Live probing (2026-10-07) shows PokerBet stops quoting
the game total near the end: at 95-97.5% progress the event view renders
with no market grid or redirects to another live game, so an execution
arriving there can never be filled.  The band runs from the operator's 78%
floor (2026-10-07, revised down from 85% to restore the volume the narrower
floor discarded — the measured edge is positive across the whole of Q4) to
the 92% ceiling, where the market is still open and the production signal's
measured ROI is positive.

One definition, TWO consumers — the executor gate that decides what is
TRADED and the live payload that marks an alert BETTABLE or
NON-ACTIONABLE.  A second copy of these numbers is the failure this module
exists to prevent.
"""
from __future__ import annotations

import math
from typing import Optional

#: No trade before this progress (operator directive 2026-10-07: 78%).
EXEC_MIN_PROGRESS_PCT = 78.0
#: No trade after this progress (the market is gone by then).
EXEC_MAX_PROGRESS_PCT = 92.0

BEFORE = "before_execution_window"
AFTER = "after_execution_window"


def execution_window_reason(progress_pct) -> Optional[str]:
    """``None`` when the progress is inside the band; else the refusal.

    Fail closed: a missing / non-finite progress is treated as BEFORE the
    window, never as a licence to trade.
    """
    try:
        pct = float(progress_pct)
    except (TypeError, ValueError):
        return BEFORE
    if not math.isfinite(pct):
        return BEFORE
    if pct < EXEC_MIN_PROGRESS_PCT:
        return BEFORE
    if pct > EXEC_MAX_PROGRESS_PCT:
        return AFTER
    return None


def in_execution_window(progress_pct) -> bool:
    """True only when the progress provably sits inside the band."""
    return execution_window_reason(progress_pct) is None
