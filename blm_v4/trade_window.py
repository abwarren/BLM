"""The UNDER trade EXECUTION WINDOW — the ONE definition.

The autonomous UNDER trade is placed only while the game's progress sits
inside this band.  Live probing (2026-10-07) shows PokerBet stops quoting
the game total near the end: at 95-97.5% progress the event view renders
with no market grid or redirects to another live game, so an execution
arriving there can never be filled.  The lower edge is the operator's Q4
four-minute mark, where the measured ROI of the production signal inside
the band is +16.9% over 661 settled entries with the market still open
throughout.

One definition, TWO consumers — the executor gate that decides what is
TRADED and the live payload that marks an alert BETTABLE or
NON-ACTIONABLE.  A second copy of these numbers is the failure this module
exists to prevent.
"""
from __future__ import annotations

import math
from typing import Optional

#: No trade before this progress (the Q4 four-minute mark).
EXEC_MIN_PROGRESS_PCT = 85.0
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
