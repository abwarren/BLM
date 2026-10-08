"""The UNDER trade EXECUTION WINDOW — the ONE definition.

The autonomous UNDER trade is placed only while the game's progress sits
inside this band.  Live probing (2026-10-07) shows PokerBet stops quoting
the game total near the end: at 95-97.5% progress the event view renders
with no market grid or redirects to another live game, so an execution
arriving there can never be filled.  The band runs from the operator's 75%
floor (2026-10-08, revised down from 78% and 85% before it: 75% is the ALERT's
own floor, so the window opens the moment an alert can exist, and the 75-78
slice measured 65.44% UNDER = break-even 1.528, +EV above ~1.53) up
to the operator's HARD game-clock stop (2026-10-08): no placement inside
the FINAL FOUR MINUTES of the match — 90.00% for a 40-minute BETUAL_NBA
game, 91.67% for a 48-minute CYBER_2K26 one.  The ceiling is therefore
derived per classification; a flat percent would mean a different clock
minute in each league, which is the trap the alert window documents.

One definition, TWO consumers — the executor gate that decides what is
TRADED and the live payload that marks an alert BETTABLE or
NON-ACTIONABLE.  A second copy of these numbers is the failure this module
exists to prevent.
"""
from __future__ import annotations

import math
from typing import Optional

#: No trade before this progress (operator directive 2026-10-08: 75%).
#: 75 is the ALERT's own floor (progress_pct >= 75), so the execution window
#: now opens at the earliest moment the alert can exist — the "too early"
#: refusal category disappears.  Measured on the rebuilt cohort: the 75-78
#: slice runs 65.44% UNDER (n=2,208) = break-even price 1.528, i.e. +EV above
#: ~1.53; it was 30.7% of alerted games, all of them previously discarded.
EXEC_MIN_PROGRESS_PCT = 75.0
#: No trade inside the FINAL this-many minutes of the match (operator
#: directive 2026-10-08: bets must not be placed in the last four minutes).
#: A GAME-CLOCK rule — deliberately not a flat percentage, because the same
#: final minutes are a different percent in each classification.
EXEC_LAST_GAME_MINUTES = 4.0
#: Ceiling used only when a classification's length cannot be established —
#: the TIGHTER of the known leagues, never the more permissive one.
EXEC_MAX_PROGRESS_PCT_FALLBACK = 90.0

BEFORE = "before_execution_window"
AFTER = "after_execution_window"


def exec_max_progress_pct(classification=None) -> float:
    """The band's ceiling for one classification, derived from game clock.

    ``(full - EXEC_LAST_GAME_MINUTES) / full`` as a percent:
    BETUAL_NBA (40 min) -> 90.00%, CYBER_2K26 (48 min) -> 91.67%.
    An unknown length takes ``duration_for``'s own BETUAL_NBA default, so
    the ceiling is never the more permissive one.
    """
    try:
        from blm_v4.projection import duration_for
        full = float(duration_for(classification)[1])
    except Exception:                      # fail closed: tighter, not looser
        return EXEC_MAX_PROGRESS_PCT_FALLBACK
    if not math.isfinite(full) or full <= EXEC_LAST_GAME_MINUTES:
        return EXEC_MAX_PROGRESS_PCT_FALLBACK
    return (full - EXEC_LAST_GAME_MINUTES) / full * 100.0


def execution_window_reason(progress_pct,
                            classification=None) -> Optional[str]:
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
    if pct > exec_max_progress_pct(classification):
        return AFTER
    return None


def in_execution_window(progress_pct, classification=None) -> bool:
    """True only when the progress provably sits inside the band."""
    return execution_window_reason(progress_pct, classification) is None
