"""The UNDER trade EXECUTION WINDOW, PRICE FLOOR and SCORE FLOOR — ONE definition.

LIVE TRADE COMBO (operator directive 2026-10-12, third revision —
docs/rag/15_EDGE_FINDINGS.md §15.7).  The autonomous UNDER trade is placed
only when ALL hold:

  * progress sits inside the LIVE TRADE band [75%, 92%]
    (TRADE_MIN/MAX_PROGRESS_PCT) — the ALERT's own floor and the top of the
    last fully-quoted band,
  * at least TRADE_MIN_SCORED_POINTS (70) are already on the board,
  * required_pts_per_min / league_avg_pace is STRICTLY above 1.04
    (TRADE_MIN_PACE_RATIO),
  * required_pts_per_min - actual_pts_per_min is at least 0.0
    (SHORTFALL_MARGIN) — the game is at or below its required rate, and
  * the UNDER price is at least TRADE_MIN_UNDER_PRICE (1.60) — a FLAT,
    trade-only floor (see :func:`trade_price_reason`), NOT the per-band
    break-even.

Measured on the corrected cohort (40 days, in-sample): 72.9% win, n=2,476,
61.9 bets/day.  The band, score floor and shortfall margin now coincide with
the alert-era class; the pace bar (1.04, strict) and the flat 1.60 price floor
are what pinch the volume.  The TRADE_* constants below are TRADE-ONLY.

THE ALERT AND THE FROZEN ANALYSIS ARE SEPARATE AND UNCHANGED.  The alert's own
class bar is ``under_alert.REQUIRED_MARGIN = 1.04`` and its floor
``under_alert.ALERT_PROGRESS_PCT = 75.0`` — the signal, the dashboard's
on-screen alert, and what the frozen historical analysis replays.  Neither
moves.  The EXEC_* constants below are retained VERBATIM as the alert-era /
frozen-analysis class (``live_analytics.fingerprint_stats`` reads them); the
LIVE trade gate no longer reads them — it reads the TRADE_* block instead.

WHY THE (HISTORICAL) WINDOW WAS [75%, 92%].  PokerBet stops quoting the game total near the end — live
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
reproduces the platform's own served cohort — and a trade needs a price above
that band's floor.  Bands above the ceiling are absent because the window
refuses them first.  These are measurements, not round numbers, and they carry
the usual model-drift risk: re-measure before trusting them again.

On the THIRD revision (2026-10-12) the TRADE path moved to a FLAT floor,
``TRADE_MIN_UNDER_PRICE = 1.60`` (:func:`trade_price_reason`), because the
re-measured cohort is dominated by the pace bar and the flat price bound; the
per-band break-evens (:func:`min_price_for`) are retained VERBATIM as the
frozen-analysis / published floor and are no longer the trade's bar.

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
#: The SHORTFALL reason: the game is NOT at or below its required rate.
SHORTFALL = "not_below_required_pace"

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

#: The PRIMARY UNDER price floor (operator decision 2026-10-12,
#: docs/rag/15_EDGE_FINDINGS.md): re-derived for the SHORTFALL population.
#: Applied as a floor ON TOP of the per-band break-evens via ``max()`` in
#: :func:`min_price_for`, so it can only ever make the gate STRICTER, never
#: looser.  Measured with the shortfall leg, ``under_price >= 1.85`` ran 70.6%
#: out-of-time against the cohort's 65.2%.
EXEC_MIN_UNDER_PRICE = 1.85

# ── THE LIVE TRADE COMBO (operator directive 2026-10-12, third revision) ─
# docs/rag/15_EDGE_FINDINGS.md §15.7.  Measured on the corrected cohort (40
# days, in-sample): progress band 75-92, pace ratio > 1.04, scored points >= 70,
# shortfall margin >= 0.0 and under price >= 1.60 -> 72.9% win (n=2,476, 61.9
# bets/day).  The band, score floor and shortfall now COINCIDE with the
# alert-era class; the pace bar (STRICT > 1.04) and the flat 1.60 price floor
# are the active constraints.  These are TRADE-ONLY: the alert
# (under_alert.REQUIRED_MARGIN / ALERT_PROGRESS_PCT) and the frozen historical
# analysis (live_analytics.fingerprint_stats, which reads the EXEC_* constants
# above) do not read them — see the module docstring.
#: TRADE-ONLY progress floor — the ALERT's own floor (75).
TRADE_MIN_PROGRESS_PCT = 75.0
#: TRADE-ONLY progress ceiling — the top of the last fully-quoted band (92).
TRADE_MAX_PROGRESS_PCT = 92.0
#: TRADE-ONLY scored-points floor — the alert-era class floor (70).
TRADE_MIN_SCORED_POINTS = 70.0
#: TRADE-ONLY pace ratio (required_pts_per_min / competition avg_pace), STRICT:
#: a ratio exactly at 1.04 is refused.
TRADE_MIN_PACE_RATIO = 1.04
#: TRADE-ONLY shortfall margin: the required rate must be AT LEAST this far
#: above the actual rate (``required_pts_per_min - actual_pts_per_min >= 0.0``),
#: i.e. the game is at or below its required rate.
SHORTFALL_MARGIN = 0.0
#: TRADE-ONLY flat UNDER-price floor (operator directive 2026-10-12, third
#: revision): ``under_price >= 1.60``.  A FLAT floor for the TRADE path ONLY —
#: NOT the per-band break-even :func:`min_price_for` (retained VERBATIM for the
#: frozen analysis and the published floor).  Consumed ONLY by the trade path
#: (:func:`trade_price_reason`, the executor and the served BETTABLE verdict),
#: never by the alert or the frozen ``EXEC_*`` class.
TRADE_MIN_UNDER_PRICE = 1.60


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
    if pct < TRADE_MIN_PROGRESS_PCT:
        return BEFORE
    if pct > TRADE_MAX_PROGRESS_PCT:
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
            band = price
            break
    else:
        band = MIN_PRICE_BANDS[-1][1]
    # The PRIMARY floor is the HIGHER of the band break-even and the flat
    # operator minimum (2026-10-12) — never the looser of the two.
    return max(band, EXEC_MIN_UNDER_PRICE)


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


def trade_price_reason(price) -> Optional[str]:
    """``None`` when the price clears the FLAT trade floor — else PRICE_FLOOR.

    The TRADE-ONLY price check (operator directive 2026-10-12, third revision):
    ``under_price >= TRADE_MIN_UNDER_PRICE`` (1.60), a FLAT floor — NOT the
    per-band break-even :func:`min_price_for`, which is retained VERBATIM for the
    frozen analysis and the published floor.  This is the bar the executor and
    the served BETTABLE verdict read.

    Same absent-price contract as :func:`price_reason`: an ABSENT price (None)
    is deliberately NOT gated — the payload's line and price come from the SAME
    market row, so a present line with an absent price is already the executor's
    ``market_missing`` concern, and gating absence here would refuse every caller
    that omits the field without making any trade safer.  A present-but-
    unprovable price (non-finite, unparsable) IS refused.
    """
    if price is None:
        return None
    try:
        p = float(price)
    except (TypeError, ValueError):
        return PRICE_FLOOR
    if not math.isfinite(p):
        return PRICE_FLOOR
    return None if p >= TRADE_MIN_UNDER_PRICE else PRICE_FLOOR


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
    return None if pts >= TRADE_MIN_SCORED_POINTS else SCORE_FLOOR


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
    if pct < TRADE_MIN_PROGRESS_PCT:
        return PACE_BELOW
    return None if (req / avg) > TRADE_MIN_PACE_RATIO else PACE_BELOW


def exec_shortfall_reason(required_pace, actual_pace) -> Optional[str]:
    """``None`` when the game is at least SHORTFALL_MARGIN below its required rate.

    The SECOND execution leg (measured 2026-10-12, docs/rag/15_EDGE_FINDINGS.md).
    The UNDER wins when the game is UNDER-shooting the required rate, because the
    total is then falling short of the line's implied requirement.  Measured on the
    corrected cohort: pacegap (actual - required) is monotone across five buckets
    (85 / 67 / 66 / 60 / 57% from the most negative to the most positive), and
    keeping ``actual < required`` lifts the archive UNDER rate 68.0% -> 73.0%
    (n=2355) and holds out-of-time; the LIVE combo tightens this to a MARGIN —
    ``required - actual >= SHORTFALL_MARGIN`` (0.25), see the live trade combo
    block above.  A SEPARATE execution bar — never a change to the signal's own
    constant.

    It is NOT the requirement-pace ratio (that is exec_alert_reason's leg): this
    one reads only the two PACES, so it is orthogonal to the pace bar and to the
    alert.

    Fail closed: an unprovable required or actual pace is REFUSED (SHORTFALL), so a
    payload that cannot show the shortfall can only ever be stricter, never looser.
    """
    try:
        req = float(required_pace)
        act = float(actual_pace)
    except (TypeError, ValueError):
        return SHORTFALL
    if not (math.isfinite(req) and math.isfinite(act)):
        return SHORTFALL
    return None if (req - act) >= SHORTFALL_MARGIN else SHORTFALL


# ═══════════════════════════════════════════════════════════════════════
# LINE-MOVEMENT VOLATILITY FILTER — a NEW trade leg (operator-approved).
# docs/rag/15_EDGE_FINDINGS.md §15.8.  APPENDED to the module: every line
# ABOVE this block is the frozen trade/alert definition and is
# byte-unchanged (tests/test_trade_move_filter_2026_10_12.py asserts this
# file still BEGINS with ``git show HEAD:blm_v4/trade_window.py``).  This
# block adds a gate the trade ANDs into the SERVED
# ``under_alert_exec_armed`` the executor consumes; it touches neither the
# alert nor the FROZEN ``EXEC_*`` class.
# ═══════════════════════════════════════════════════════════════════════
#
# A game's TOTAL line drifting away from where it was FIRST CAPTURED is a
# market-disagreement signal that materially lowers the UNDER win rate.
# Out-of-sample walk-forward over 38 clean days (entry-date attribution,
# corrected finals):
#
#   flat   |entry_line - open_line| / league_avg_line <  2%  -> 78.8% UNDER
#          [76.5, 80.9]  n=1363   EV +0.672
#   moved  |entry_line - open_line| / league_avg_line >= 2%  -> 64.7% UNDER
#          [61.6, 67.7]  n=949    EV +0.228
#
# gap +14.1 pp, p ~ 6e-14; the threshold PLATEAUS across 1-4% and holds in
# all six leagues and all price bands.  The rule TRADES ONLY the flat
# cohort: the leg is satisfied (``None``) iff the normalised move is
# STRICTLY below TRADE_MOVE_MAX.
#
# DEFINITIONS (must match the study):
#   * open_line        the FIRST non-null live_total_line CAPTURED for the
#                      game (from clean_observations) — the first CAPTURED
#                      line, NOT the true market open;
#   * league_avg_line  the mean of the per-game first-captured lines across
#                      the game's competition_slug (study population: NBA
#                      226.6, EuroLeague 172.7, TBSL 167.7, CBA 178.2, KBL
#                      156.9, cyber 218.5);
#   * move_norm        |entry_line - open_line| / league_avg_line.
#
# FAIL CLOSED: if the open line OR the league average is unknown /
# unprovable the trade is REFUSED — for THIS leg an unprovable move must
# not license a trade.  MOVE_UNPROVABLE is distinct from MOVE_VOLATILE so
# the two refusals are never conflated in the served diagnostics.
import os  # placed here so the frozen block above stays byte-stable

#: TRADE-ONLY maximum NORMALISED line move; a game trades only when its
#: move is STRICTLY below this.  Fixed by the study (the 1-4% plateau).
TRADE_MOVE_MAX = 0.02

#: The trade refuses when the line moved by at least TRADE_MOVE_MAX of the
#: league average (the "moved" cohort).
MOVE_VOLATILE = "line_move_too_volatile"
#: The trade refuses when the move cannot be PROVEN (absent/unprovable open
#: line or league average) — fail closed.
MOVE_UNPROVABLE = "line_move_unprovable"


def _env_flag(name, default):
    """A boolean env var; any of 0/false/no/off/'' disables; unset → default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_float(name, default):
    """A float env var; unset OR unparsable → default (never raises)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


#: The line-movement leg's TOGGLE — DEFAULT ON (operator-approved).  Set
#: ``TRADE_MOVE_FILTER_ENABLED=0`` (or false/no/off) to disable the leg
#: entirely; when disabled :func:`trade_move_reason` imposes NO requirement.
TRADE_MOVE_FILTER_ENABLED = _env_flag("TRADE_MOVE_FILTER_ENABLED", True)

#: The STRICTER history variant's minimum captured history, in seconds.
#: DEFAULT 0.0 = the exact configuration the study validated (the variant
#: is EXCLUDED).  Setting it (e.g. 900 for >= 15 minutes) enables the
#: stricter variant WITHOUT a code change — the study's flat bucket at
#: >= 15 min of history is ~38% the size of the 0-second bucket but carries
#: a +12.3 pp gap (smaller sample, same direction).
TRADE_MOVE_MIN_HISTORY_S = _env_float("TRADE_MOVE_MIN_HISTORY_S", 0.0)


def trade_move_reason(entry_line, open_line, league_avg_line,
                      history_s=None):
    """``None`` when the line is provably FLAT — else the refusal reason.

    The LINE-MOVEMENT VOLATILITY leg (docs/rag/15_EDGE_FINDINGS.md §15.8):
    the trade proceeds only when
    ``|entry_line - open_line| / league_avg_line < TRADE_MOVE_MAX`` (2%).

    FAILS CLOSED.  ``open_line`` is the FIRST CAPTURED live line for the
    game (from ``clean_observations``) — the first CAPTURED line, NOT the
    true market open (``market.opening_line``).  ``league_avg_line`` is the
    precomputed per-competition mean of those first-captured lines.  If
    EITHER is absent or unprovable, OR the average is non-positive, the
    trade is REFUSED (MOVE_UNPROVABLE): an unprovable move must not license
    a trade.  A present-but-unparsable line is refused the same way.

    The TOGGLE ``TRADE_MOVE_FILTER_ENABLED`` (env, DEFAULT ON) short-
    circuits FIRST: when it is false the leg imposes no requirement and
    ``None`` is returned for every input.

    ``history_s`` is the game's captured history (seconds between the first
    captured observation and the entry observation).  It is read ONLY when
    ``TRADE_MOVE_MIN_HISTORY_S`` (env, default 0.0) is positive — the
    stricter, future variant; at the default 0 the argument is ignored (the
    exact configuration the study validated).
    """
    if not TRADE_MOVE_FILTER_ENABLED:
        return None
    try:
        e = None if entry_line is None else float(entry_line)
        o = None if open_line is None else float(open_line)
        avg = None if league_avg_line is None else float(league_avg_line)
    except (TypeError, ValueError):
        return MOVE_UNPROVABLE
    if e is None or o is None or avg is None:
        return MOVE_UNPROVABLE
    if not (math.isfinite(e) and math.isfinite(o) and math.isfinite(avg)):
        return MOVE_UNPROVABLE
    if avg <= 0:
        return MOVE_UNPROVABLE
    if TRADE_MOVE_MIN_HISTORY_S > 0:
        try:
            h = None if history_s is None else float(history_s)
        except (TypeError, ValueError):
            h = None
        if h is None or not math.isfinite(h) or h < TRADE_MOVE_MIN_HISTORY_S:
            return MOVE_UNPROVABLE
    return None if abs(e - o) / avg < TRADE_MOVE_MAX else MOVE_VOLATILE
