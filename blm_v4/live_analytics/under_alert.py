"""The actionable UNDER condition — evaluated ONCE, authoritatively.

    active = actual_pace < required_pace
             AND required_pace > league_average_pace

where ``league_average_pace`` is this game's OWN competition reference
(see :mod:`blm_v4.live_analytics.competition_pace`) — never a global rate.

This module is the ONLY definition of that condition.  The dashboard
renders the boolean and the numbers it is built from; it never re-derives
them, so no two surfaces can disagree about the same opportunity.

Distinct from the historical/context indicator: the in-card HISTORICAL
context badge compares each pace against the mean of the game's
(competition, period, state) bucket on a mature benchmark.  That answers
"is this game-state historically an UNDER state"; this answers "is this a
live UNDER opportunity right now".  They are deliberately different
questions and are labelled differently in the UI.
"""
from __future__ import annotations

import math
from typing import Any, Optional

# Progress checkpoints (%).  Each is its OWN identity downstream, so a 25%
# record can never suppress a later 50% or 75% one.
CHECKPOINTS = (25, 50, 75)


def _finite(value: Any) -> Optional[float]:
    """The value as a float, or None when it is not a finite number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def checkpoint_for(progress_pct: Any) -> Optional[int]:
    """The highest checkpoint the game's progress has reached, else None."""
    p = _finite(progress_pct)
    if p is None:
        return None
    cp = None
    for c in CHECKPOINTS:
        if p >= c:
            cp = c
    return cp


def under_alert_state(actual_pace: Any, required_pace: Any,
                      league_average_pace: Any,
                      progress_pct: Any = None,
                      reference_games: Any = None) -> dict:
    """The actionable UNDER state for one game.

    ``active`` is TRUE only when all three numbers are finite and both
    comparisons hold.  A missing league reference yields ``active=False``
    — no comparison is available, so nothing is claimed and no other
    competition's rate is borrowed.
    """
    actual = _finite(actual_pace)
    required = _finite(required_pace)
    league = _finite(league_average_pace)
    games = _finite(reference_games)
    active = bool(actual is not None and required is not None
                  and league is not None
                  and actual < required and required > league)
    return {
        "active": active,
        "checkpoint": checkpoint_for(progress_pct),
        "actual_pace": actual,
        "required_pace": required,
        "league_average_pace": league,
        "league_reference_games": (int(games) if games is not None else None),
        # Derived here, by the same authority that decides `active`, so the
        # displayed gap and the displayed verdict can never disagree.
        # Undefined whenever either operand is — a gap needs both numbers.
        "pace_gap": (actual - required if actual is not None
                     and required is not None else None),
    }
