"""The actionable UNDER condition — evaluated ONCE, authoritatively.

THE production UNDER trigger (directive 2026-09-14)::

    progress_pct >= 75
    AND
    required_pts_per_min > league_average_pace * 1.04     (strict, RELATIVE)

That is the WHOLE statistical rule — the exact parameter set that produced
the ~69.89% UNDER rate in the historical database analysis.  There is no
50% tier and no ``actual < league_average`` leg: the mid tier was a coin
flip historically (48.51% UNDER) and is not part of that cohort.

The 4% margin is RELATIVE to the league average, never an absolute pts/min
offset, and the comparison is STRICT — a required pace exactly at
``avg * 1.04`` does NOT qualify.

``league_average_pace`` is this game's OWN competition reference (see
:mod:`blm_v4.live_analytics.competition_pace`) — never a global rate.

The ONLY gates besides the statistical rule are technical
data-integrity / live-market gates, supplied by the caller as ``eligible``
from :func:`under_alert_eligibility`: the game must be genuinely live AND
its market line LIVE.  No other statistical threshold, pace condition,
margin or minimum-remaining rule participates.  Any missing / non-finite
input, or a missing ``eligible``, yields active=False (fail closed).

This module is the ONLY definition of the condition.  The dashboard renders
the boolean and the numbers it is built from; it never re-derives them, so
no two surfaces can disagree about the same opportunity.

The served block ALSO carries the FROZEN ``trigger_line`` (plus
``trigger_progress`` / ``trigger_captured_at``) — the market total in force
when the alert's checkpoint was reached — supplied by the caller from the
same ``trigger_observation`` authority settlement reads.  It is what the
frontend shows and what the eventual verdict is measured against; it never
moves when the market does.

Q3 BREAK (directive 2026-09-18) — an ADDITIONAL checkpoint, additive to and
never replacing the 75% production trigger above.  The Q3/Q4 break sits at
exactly 75.0% progress (3 of 4 regulation quarters); the boundary is the
FIRST observation at/after that progress, so it fires at the Q3-end sentinel
or the first instants of Q4, never mid-Q3.  The condition is THE SAME:
``required_pts_per_min > league_average_pace * REQUIRED_MARGIN`` (strict).
See :func:`q3_break_snapshot` below.
"""
from __future__ import annotations

import math
from typing import Any, Optional

# Progress checkpoints (%).  Each is its OWN identity downstream, so a 25%
# record can never suppress a later 50% or 75% one.
CHECKPOINTS = (25, 50, 75)

# ── THE production UNDER trigger (directive 2026-09-14) ────────────────
#: The ONE progress threshold.  Below it there is NO trading alert of any
#: kind — the 50% tier was removed (it was a coin flip historically, 48.51%
#: UNDER, and is not part of the ~69.89% cohort).
ALERT_PROGRESS_PCT = 75.0
#: REQUIRED must exceed the league average by this RELATIVE margin,
#: STRICTLY.  Never an absolute pts/min offset.
REQUIRED_MARGIN = 1.04

# ── Q3 BREAK (directive 2026-09-18) — an ADDITIONAL checkpoint ─────────
#: The progress the Q3/Q4 break sits at: 3 of 4 regulation quarters.  The
#: boundary is the FIRST observation at/after this progress — the Q3-end
#: sentinel or the first instants of Q4, never mid-Q3.
Q3_BREAK_PROGRESS = 75.0
#: The Q3_BREAK checkpoint identity — a STRING, deliberately NOT part of
#: the numeric ``CHECKPOINTS`` tuple (25/50/75 keep their own semantics).
Q3_BREAK_CHECKPOINT = "Q3_BREAK"

# ── Eligibility vocabulary (LIVE MARKETS ONLY directive, 2026-09-12) ──
ELIGIBLE_MARKET_LIVE = "market_live"
INELIGIBLE_MARKET_STALE = "market_stale"
INELIGIBLE_MARKET_MISSING = "market_missing"
#: No genuine-live reason supplied by the caller (fail closed).
INELIGIBLE_NOT_LIVE = "not_live"
#: Accepted game state older than the documented freshness bound
#: (api.ALERT_MAX_STATE_AGE_S, audit 2026-09-16 §6).  Passed through from
#: the backend alert gate's reason verbatim — a stale state is a live-
#: gate failure: nothing about the game is provably current.
INELIGIBLE_STALE_STATE = "stale_state"


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


def under_alert_eligibility(market_status: Any, live: Any,
                            live_reason: Any = None) -> dict:
    """Whether an active Under Alert is PERMITTED for this game right now.

    Returns ``{"eligible": bool, "reason": str}``.  Reasons distinguish the
    live-market case, each market failure, and — reused verbatim, never
    reinvented — the existing genuine-live exclusion vocabulary supplied
    by the caller: game_finished, unsupported_status, no_live_observation,
    stale_observation, stale_state (state older than the documented
    freshness bound — audit 2026-09-16 §6), terminal_*.

    The genuine-live test comes FIRST: when the game is not live at all,
    its own reason is the informative one, and the market state is moot.
    An unrecognised or absent ``market_status`` fails CLOSED as
    ``market_missing`` — a market we cannot prove is live is never treated
    as live.  The OPENING line is never consulted here: a stale or missing
    live line is never substituted, it is excluded.
    """
    if not live:
        # stale_state is a live-gate failure carrying its own reason — pass
        # it through verbatim so surfaces can distinguish "old state" from
        # "no state" (audit §6); anything else keeps the caller's reason or
        # the fail-closed default.
        reason = live_reason or INELIGIBLE_NOT_LIVE
        return {"eligible": False, "reason": reason}
    if market_status == "LIVE":
        return {"eligible": True, "reason": ELIGIBLE_MARKET_LIVE}
    if market_status == "STALE":
        return {"eligible": False, "reason": INELIGIBLE_MARKET_STALE}
    return {"eligible": False, "reason": INELIGIBLE_MARKET_MISSING}


def under_alert_state(actual_pace: Any, required_pace: Any,
                      league_average_pace: Any,
                      progress_pct: Any = None,
                      reference_games: Any = None,
                      eligible: Any = True,
                      trigger_line: Any = None,
                      trigger_progress: Any = None,
                      trigger_captured_at: Any = None) -> dict:
    """The actionable UNDER state for one game (progress-tiered).

    ``active`` is TRUE only when ALL of the following hold:

      * ``eligible`` is True (genuinely live AND a LIVE market line);
      * ``required_pace``, ``league_average_pace`` and ``progress_pct`` are
        all finite;
      * ``progress_pct >= ALERT_PROGRESS_PCT`` (75%) — below 75% there is no
        trading alert of any kind;
      * ``required_pace > league_average_pace * REQUIRED_MARGIN`` (STRICT —
        a required pace exactly at the 4%-above-average threshold does not
        qualify).

    That is the entire decision.  ``actual_pace`` is still REPORTED (the
    active row shows it beside the required pace) but it takes no part in
    ``active`` — the old ``actual < league_average`` leg is gone.

    ``eligible`` is the live/market gate evaluated by the API: it can only
    suppress, never create.  The quantitative block itself is served
    whether or not the alert is active — the reason lives in the sibling
    ``under_alert_eligibility`` field, never in a silently blank verdict.

    A missing league reference yields ``active=False`` — no comparison is
    available, so nothing is claimed and no other competition's rate is
    borrowed.

    ``trigger_line`` / ``trigger_progress`` / ``trigger_captured_at`` are the
    FROZEN trigger provenance and are NOT part of the condition: the caller
    supplies them from :func:`blm_v4.live_analytics.under_outcome.trigger_observation`
    — the same authority settlement reads — so the live alert carries the
    exact market total in force when its checkpoint was reached, and that
    value never moves when the market does later.
    """
    actual = _finite(actual_pace)
    required = _finite(required_pace)
    league = _finite(league_average_pace)
    games = _finite(reference_games)
    prog = _finite(progress_pct)
    # THE production trigger, and nothing else: progress >= 75 AND
    # required > league * 1.04, on a genuinely-live game with a LIVE market.
    # actual_pace is reported but takes no part in the decision, and no
    # minimum-remaining rule applies here.
    active = bool(
        eligible is True
        and required is not None and league is not None and prog is not None
        and prog >= ALERT_PROGRESS_PCT
        and required > league * REQUIRED_MARGIN
    )
    return {
        "active": active,
        "checkpoint": checkpoint_for(prog),
        "actual_pace": actual,
        "required_pace": required,
        "league_average_pace": league,
        "league_reference_games": (int(games) if games is not None else None),
        # Derived here, by the same authority that decides `active`, so the
        # displayed gap and the displayed verdict can never disagree.
        # Undefined whenever either operand is — a gap needs both numbers.
        "pace_gap": (actual - required if actual is not None
                     and required is not None else None),
        # ── the FROZEN trigger line + provenance (see docstring).  Passed
        #    through from the caller; never the current/opening line and
        #    never rewritten once an alert has activated.
        "trigger_line": _finite(trigger_line),
        "trigger_progress": _finite(trigger_progress),
        "trigger_captured_at": (trigger_captured_at
                                if isinstance(trigger_captured_at, str)
                                else None),
    }


def q3_break_snapshot(score_at_trigger: Any, triggered_line: Any,
                      league_average_pace: Any, quarter_minutes: Any,
                      eligible: Any = True,
                      trigger_progress: Any = None,
                      trigger_captured_at: Any = None) -> dict:
    """The Q3_BREAK checkpoint state for one game (directive 2026-09-18).

    The Q3/Q4 break — progress 75.0%, three of four regulation quarters —
    is an ADDITIONAL checkpoint identity, additive to the production 75%
    trigger and never a replacement for it.  The CONDITION is the same
    production rule, applied at the break with the break's own geometry::

        remaining_minutes    = quarter_minutes      (one full quarter left)
        required_pts_per_min = (triggered_line - score_at_trigger)
                               / remaining_minutes
        active = eligible AND required > league_average_pace * 1.04 (STRICT)

    ALL inputs must be finite and ``eligible`` must be True (the caller's
    genuine-live / LIVE-market / state-freshness gate — it can only
    suppress, never create).  A missing league reference, a missing or
    unprovable ``triggered_line``, or any non-finite operand yields
    ``active=False`` — fail closed, never fabricated.

    ``quarter_minutes`` is the game's OWN classification quarter length
    (``projection.duration_for(classification)[0]`` — 10 min BETUAL_NBA,
    12 min CYBER_2K26), never a hardcoded value and never shared across
    classifications.

    ``triggered_line`` is the FROZEN market total in force at the boundary
    — the last line observed at-or-before the boundary observation, from
    the same ``trigger_observation`` authority settlement reads.  The
    returned snapshot is immutable downstream: it is written once and
    never rewritten when the market moves later.

    ``checkpoint`` is the string ``"Q3_BREAK"`` — its own identity, never
    colliding with the numeric checkpoints.
    """
    score = _finite(score_at_trigger)
    line = _finite(triggered_line)
    league = _finite(league_average_pace)
    qmin = _finite(quarter_minutes)
    prog = _finite(trigger_progress)
    required = None
    if (line is not None and score is not None and qmin is not None
            and qmin > 0):
        required = (line - score) / qmin
    active = bool(
        eligible is True
        and required is not None and league is not None
        and required > league * REQUIRED_MARGIN
    )
    return {
        "active": active,
        "checkpoint": Q3_BREAK_CHECKPOINT,
        "score_at_trigger": score,
        "triggered_line": line,
        "remaining_minutes": qmin,
        "required_pts_per_min": required,
        "league_average_pace": league,
        # Derived here, by the same authority that decides ``active``, so
        # the displayed gap and the verdict can never disagree.
        "gap": (league - required if required is not None
                and league is not None else None),
        "trigger_progress": prog,
        "trigger_captured_at": (trigger_captured_at
                                if isinstance(trigger_captured_at, str)
                                else None),
    }
