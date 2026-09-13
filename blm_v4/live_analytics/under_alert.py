"""The actionable UNDER condition — evaluated ONCE, authoritatively.

Progress-tiered policy (directive 2026-09-13)::

    progress < 50%          -> NO trading alert
    50% <= progress < 75%    -> CONFIRMED UNDER ALERT
        required_pace > league_average_pace * 1.04   (strict, RELATIVE margin)
        AND actual_pace < league_average_pace        (strict)
    progress >= 75%         -> LATE UNDER ALERT
        required_pace > league_average_pace * 1.04   (strict)
        (the actual<average leg is DROPPED — the historical sweep showed it
         slightly diluted the late-game signal)

Below 50% the historical alert population was a coin flip (48.51% UNDER),
so no trading alert is raised there.  The 4% margin is RELATIVE to the
league average, never an absolute pts/min offset, and every comparison is
STRICT.

``league_average_pace`` is this game's OWN competition reference (see
:mod:`blm_v4.live_analytics.competition_pace`) — never a global rate.

Non-quantitative gates are unchanged.  The caller passes ``eligible`` from
the live-market eligibility gate (:func:`under_alert_eligibility`) plus the
upstream alert gate (genuinely-live, fresh observation, non-terminal,
>= min remaining minutes, valid quality) — the quantitative condition is
necessary but NOT sufficient.  Any missing / non-finite input, or a missing
``eligible``, yields active=False (fail closed).

This module is the ONLY definition of the condition.  The dashboard renders
the boolean and the numbers it is built from; it never re-derives them, so
no two surfaces can disagree about the same opportunity.

The served block ALSO carries the FROZEN ``trigger_line`` (plus
``trigger_progress`` / ``trigger_captured_at``) — the market total in force
when the alert's checkpoint was reached — supplied by the caller from the
same ``trigger_observation`` authority settlement reads.  It is what the
frontend shows and what the eventual verdict is measured against; it never
moves when the market does.
"""
from __future__ import annotations

import math
from typing import Any, Optional

# Progress checkpoints (%).  Each is its OWN identity downstream, so a 25%
# record can never suppress a later 50% or 75% one.
CHECKPOINTS = (25, 50, 75)

# ── Progress tiers for the live trading UNDER alert (directive 2026-09-13) ──
#: Below this progress NO trading alert is raised (no alert for < 50%).
MID_PROGRESS_PCT = 50.0
#: At / above this progress the actual<average leg is DROPPED (late tier).
LATE_PROGRESS_PCT = 75.0
#: REQUIRED must exceed the league average by this RELATIVE margin, strictly.
REQUIRED_MARGIN = 1.04

# ── Eligibility vocabulary (LIVE MARKETS ONLY directive, 2026-09-12) ──
ELIGIBLE_MARKET_LIVE = "market_live"
INELIGIBLE_MARKET_STALE = "market_stale"
INELIGIBLE_MARKET_MISSING = "market_missing"
#: No genuine-live reason supplied by the caller (fail closed).
INELIGIBLE_NOT_LIVE = "not_live"


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
    reinvented — the existing genuine-live exclusion vocabulary supplied by
    the caller: game_finished, unsupported_status, no_live_observation,
    stale_observation, terminal_*.

    The genuine-live test comes FIRST: when the game is not live at all,
    its own reason is the informative one, and the market state is moot.
    An unrecognised or absent ``market_status`` fails CLOSED as
    ``market_missing`` — a market we cannot prove is live is never treated
    as live.  The OPENING line is never consulted here: a stale or missing
    live line is never substituted, it is excluded.
    """
    if not live:
        return {"eligible": False,
                "reason": (live_reason or INELIGIBLE_NOT_LIVE)}
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

      * ``eligible`` is True (the live / market / quality gate);
      * ``actual_pace``, ``required_pace``, ``league_average_pace`` and
        ``progress_pct`` are all finite;
      * ``progress_pct >= MID_PROGRESS_PCT`` (50%) — below 50% there is no
        trading alert of any kind;
      * ``required_pace > league_average_pace * REQUIRED_MARGIN`` (STRICT —
        a required pace exactly at the 4%-above-average threshold does not
        qualify);
      * AND, in the MID tier only (``progress_pct < LATE_PROGRESS_PCT``),
        ``actual_pace < league_average_pace`` (STRICT).  The LATE tier
        (``progress_pct >= 75%``) DROPS this leg.

    ``eligible`` is the market/live gate evaluated by the API: the condition
    is necessary but not sufficient, so a stale market can never leave an
    active alert standing.  The quantitative block itself is unchanged and
    is still served when suppressed — the reason lives in the sibling
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
    active = bool(
        eligible is True
        and actual is not None and required is not None
        and league is not None and prog is not None
        and prog >= MID_PROGRESS_PCT
        and required > league * REQUIRED_MARGIN
        and (prog >= LATE_PROGRESS_PCT or actual < league)
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
