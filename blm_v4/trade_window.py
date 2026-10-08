"""The UNDER trade EXECUTION WINDOW and PRICE FLOOR — the ONE definition.

The autonomous UNDER trade is placed only when BOTH hold:

  * the game's progress sits inside the execution window, and
  * the UNDER price clears the break-even for the progress band.

WHY THE WINDOW.  PokerBet stops quoting the game total near the end — live
probing (2026-10-07) shows the event view renders with no market grid at
95-97.5% — and every observed unfillable attempt (`EVENT_NOT_FOUND`) landed at
94-96%.  So the band is [75%, 92%]:

  floor 75%   the ALERT's own floor (progress_pct >= 75), so the window opens
              the moment an alert can exist — there is no "too early"
              refusal left.  Measured: the 75-78 slice runs 65.44% UNDER
              (n=2,208, break-even 1.528) and was 30.7% of alerted games,
              every one of them previously discarded.
  ceiling 92% below where the market actually dies.  The 90-92 slice measures
              78.48% UNDER (n=316, break-even 1.274) — +EV, so it is traded
              again (operator directive 2026-10-08 revised the earlier
              "final four minutes" game-clock stop, which refused it).

WHY THE PRICE FLOOR.  The operator's criterion is positive EV, never volume:
a hit rate alone decides nothing without the price.  MIN_PRICE_BANDS holds the
break-even measured for each band on the rebuilt cohort — the construction that
reproduces the platform's own served cohort — and a trade needs a price ABOVE
its band's break-even.  Bands above the ceiling are absent because the window
refuses them first.  These are measurements, not round numbers, and they carry
the usual model-drift risk: re-measure before trusting them again.

One definition, TWO consumers — the executor gate that decides what is TRADED
and the live payload that marks an alert BETTABLE or NON-ACTIONABLE.  A second
copy of these numbers is the failure this module exists to prevent.
"""
from __future__ import annotations

import math
from typing import Optional

#: No trade before this progress (operator directive 2026-10-08: 75%).
EXEC_MIN_PROGRESS_PCT = 75.0
#: No trade after this progress (operator directive 2026-10-08: 92%).
#: 92 is the top of the last fully-quoted band; the observed unfillable
#: attempts were at 94-96%, and the 90-92 slice is +EV (78.48% UNDER).
EXEC_MAX_PROGRESS_PCT = 92.0

BEFORE = "before_execution_window"
AFTER = "after_execution_window"
PRICE_FLOOR = "price_below_break_even"

#: (exclusive upper progress bound, break-even price) — first match wins.
#: Measured on the rebuilt cohort (per game, first firing, settled finals):
#:   75-80   66.39% UNDER  n=2,386  -> 1.506
#:   80-85   73.17% UNDER  n=641    -> 1.367
#:   85-92   78.43% UNDER  n=890    -> 1.275
#: Rounded UP to the nearest cent so the floor is never the looser number.
MIN_PRICE_BANDS = (
    (80.0, 1.51),
    (85.0, 1.37),
    (None, 1.28),
)


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


def min_price_for(progress_pct) -> Optional[float]:
    """The break-even price floor for this progress band.

    ``None`` when the progress is unprovable OR outside the execution window —
    the window refuses those anyway, so no price claim is made.
    """
    try:
        pct = float(progress_pct)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(pct):
        return None
    if not EXEC_MIN_PROGRESS_PCT <= pct <= EXEC_MAX_PROGRESS_PCT:
        return None
    for hi, price in MIN_PRICE_BANDS:
        if hi is None or pct < hi:
            return price
    return MIN_PRICE_BANDS[-1][1]


def price_reason(price, progress_pct) -> Optional[str]:
    """``None`` when the price is acceptable — else PRICE_FLOOR.

    An ABSENT price (``None``, or a progress outside the window) is
    deliberately NOT gated.  The payload's line and price come from the SAME
    market row, so a present line with an absent price is a shape the
    executor's own ``market_missing`` gate already covers; gating absence here
    would refuse every caller that simply omits the field while making no
    trade safer.  What this gate exists for is the case the operator's
    criterion names: a price that is KNOWN to be below its band's break-even
    (the live 1.50 fill against a 1.51 floor).

    A present-but-unprovable price (a non-finite number, an unparsable string)
    IS refused — it cannot be shown to clear the floor.
    """
    if price is None:
        return None
    floor = min_price_for(progress_pct)
    if floor is None:
        return None                 # the window refuses this progress anyway
    try:
        p = float(price)
    except (TypeError, ValueError):
        return PRICE_FLOOR
    if not math.isfinite(p):
        return PRICE_FLOOR
    return None if p >= floor else PRICE_FLOOR
