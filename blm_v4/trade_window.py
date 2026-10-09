"""The UNDER trade EXECUTION WINDOW, PRICE FLOOR and SCORE FLOOR — ONE definition.

The autonomous UNDER trade is placed only when ALL hold:

  * the game's progress sits inside the execution window,
  * enough points have actually been scored, and
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

WHY THE SCORE FLOOR.  A live basketball game cannot be deep into its schedule
with almost nothing on the board, and the provider CAN publish such a state.
Observed live 2026-10-08: PokerBet reported a freshly-listed game as
"4th Quarter, 12:00, 0-0" for about 35 seconds — the raw snapshots show
quarter=4 with game_status live — then corrected itself to "1st Quarter".  That
phantom state computes to EXACTLY 75% progress and, with a real line against a
zero score, a required pace far above the league average, so the alert read it
as a maximal UNDER and the engine placed R200 sixteen seconds after the game
first appeared (execution bet-c4a887dc546863d4331b, game 31156683).  Every
other bet that day came 5-74 minutes after first sighting.  A floor on the
scored total refuses that state AND any variant of it.  Measured over 380,249
real states in the window: only 12 carry a total under 70, ten of them zero —
this binds on nothing legitimate.  Like the price, an ABSENT score is NOT
gated here: a projection with no score has no required_pts_per_min either, so
the executor's own ``market_missing`` gate has already refused it upstream.
What this gate adds is the case that slipped through — a score that is
PRESENT and impossible.

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
SCORE_FLOOR = "score_below_minimum"
PACE_BELOW = "pace_below_execution_threshold"

#: Minimum points that must ALREADY be on the board (operator directive
#: 2026-10-08).  A guard against impossible provider states — see the module
#: docstring.  Measured: binds on 12 of 380,249 real in-window states.
MIN_SCORED_POINTS = 70.0

#: The EXECUTION pace ratio (operator directive 2026-10-08, "take the position
#: earlier before books remove the market").
#:
#: The ALERT's own class threshold is 1.04 and STAYS 1.04 — it is the signal,
#: it is what the dashboard shows, and it is what the frozen historical
#: analysis replays.  This is a SEPARATE, lower bar used only to decide what is
#: TRADED.  The difference matters because the alert often only clears 1.04
#: once the game is nearly over, by which time PokerBet has pulled the total.
#:
#: Swept on the rebuilt cohort (first moment progress >= 75 that the ratio
#: crosses k; hit rate, break-even, price matched to the exact line):
#:
#:   k      games   median fire%   in-window%   hit%     break-even   EV @ ~1.85
#:   1.04   7,707       90.0%          54%     80.27%     1.246        +46.1%
#:   1.02   8,396       85.0%          60%     77.99%     1.282        +44.3%
#:   1.00   9,461       82.5%          65%     75.20%     1.330        +39.1%
#:   0.95  12,594       77.5%          77%     69.68%     1.435        +28.9%
#:   0.90  15,644       75.0%          88%     64.87%     1.542        +20.0%
#:
#: 0.95 was the chosen step for a time: +EV throughout, and it moved the median
#: fire from 90.0% to 77.5% progress.  REVERTED to 1.04 by operator directive
#: 2026-10-09 — auto-betting must fire ONLY on the strict alert cohort.
#:
#: Why: the model put 0.95 at 69.68% hit / +28.9% EV, but realised results over
#: the following day ran ~52% against a 55.8% break-even.  The frozen study
#: prices to the line on screen AT THE DECISION; placements take 8-59s and the
#: live line moves in the meantime, so the relaxed cohort was paying execution
#: slippage it had no margin to absorb.  At 1.04 the traded cohort IS the alert,
#: so the armed flag and the alert agree by construction.
#:
#: Revisit only after placement latency is cut (event-driven dispatch) and the
#: shadow log can measure the relaxed cohort against the line actually taken.
EXEC_MIN_PACE_RATIO = 1.04

#: (exclusive upper progress bound, break-even price) — first match wins.
#: Re-derived for the EXECUTION cohort at k=0.95 (first moment progress >= 75
#: that the ratio crosses 0.95, settled finals, line-matched prices):
#:   75-80   67.91% UNDER  -> 1.473
#:   80-85   72.29% UNDER  -> 1.383
#:   85-92   78.7%  UNDER  -> 1.27  (n is thinner here and noisy upward)
#: Rounded UP to the nearest cent so the floor is never the looser number.
MIN_PRICE_BANDS = (
    (80.0, 1.48),
    (85.0, 1.39),
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


def score_reason(points) -> Optional[str]:
    """``None`` when enough points have been scored — else SCORE_FLOOR.

    An ABSENT score (``None``) is deliberately NOT gated, exactly as for the
    price: a projection with no score has no ``required_pts_per_min`` either,
    so the executor's own ``market_missing`` gate has already refused it
    upstream, and gating absence here would break every caller that omits the
    field without making any trade safer.

    What this gate exists for is a score that is PRESENT and impossible — the
    2026-10-08 phantom: the provider reported a freshly-listed game as
    "4th Quarter, 12:00, 0-0", which computes to exactly 75% progress and,
    with a real line against a zero score, reads as a maximal UNDER.

    A present-but-unprovable score (a non-finite number, an unparsable
    string) IS refused — it cannot be shown to clear the floor.
    """
    if points is None:
        return None
    try:
        pts = float(points)
    except (TypeError, ValueError):
        return SCORE_FLOOR
    if not math.isfinite(pts):
        return SCORE_FLOOR
    return None if pts >= MIN_SCORED_POINTS else SCORE_FLOOR


def exec_alert_reason(required_pace, avg_pace, progress_pct) -> Optional[str]:
    """``None`` when the EXECUTION pace bar is cleared — else PACE_BELOW.

    This is the trade-side threshold, deliberately LOWER than the alert's own
    1.04 class (see EXEC_MIN_PACE_RATIO).  It is not the alert: the alert still
    fires, and the dashboard still shows it, on 1.04.

    Fail closed: an unprovable pace, or an average that is missing or
    non-positive, is refused rather than waved through.
    """
    try:
        req = float(required_pace)
        avg = float(avg_pace)
        pct = float(progress_pct)
    except (TypeError, ValueError):
        return PACE_BELOW
    if not (math.isfinite(req) and math.isfinite(avg) and math.isfinite(pct)):
        return PACE_BELOW
    if avg <= 0:
        return PACE_BELOW
    if pct < EXEC_MIN_PROGRESS_PCT:
        return PACE_BELOW
    return None if (req / avg) > EXEC_MIN_PACE_RATIO else PACE_BELOW
