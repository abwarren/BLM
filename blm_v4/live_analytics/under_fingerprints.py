"""HISTORICAL UNDER FINGERPRINT LAYER — C1, C3, C5 + R2 (authorization 2026-09-21).

This module is the FINGERPRINT LAYER the 2026-09-21 authorization directed:
for every production UNDER evaluation it labels WHICH historical patterns
are present.  It is an ENRICHMENT on the existing alert — never a second
alert source, never a replacement for the required-pace rule, never a
loosening of it.  The production decision remains
``under_alert_state(...)`` verbatim (see
:mod:`blm_v4.live_analytics.under_alert`); this layer only records and
reports historical context beside it.

THE FINGERPRINTS (directive 2026-09-21 — the approved set, EXACTLY the
definitions the read-only analysis measured; thresholds frozen)::

    C1  1.10 <= req_ratio < 1.20          (lower INCLUSIVE, upper EXCLUSIVE)
    C3  req_ratio > 1.04  AND q3_ratio < 1.00
    C5  req_ratio > 1.10  AND q3_ratio < 1.00
    R2  q3_ratio < 0.90

where (all operands available at the trigger instant — see the leakage
guarantee below)::

    req_ratio          = required_pts_per_min / league_average_pace
    q3_ratio           = q3_ppm / league average Q3 pace (same competition)

R1 IS EXCLUDED — deliberately, by directive.  The widened required-pace
band [1.10, 1.35) measured BELOW the historical UNDER baseline (56.47% vs
59.71%); it is NOT implemented here, NOT registered as a fingerprint, NOT
used in any combination and NOT referenced as an active condition.  The
upper cut of C1 (1.20, exclusive) is part of C1's meaning precisely
because [1.20, 1.35) dragged the widened band below baseline.  A source
scan of this module proves the exclusion (enforced by test).

THREE-STATE SEMANTICS (fail closed — missing data is NEVER TRUE)::

    TRUE         every operand provable AND every comparison passes
    FALSE        every operand provable AND at least one comparison fails
    UNAVAILABLE  any operand missing / non-finite / unprovable

A conjunction (C3..C6) is UNAVAILABLE whenever ANY leg is UNAVAILABLE: a
partially-provable pattern is never a pass.

``fingerprint_count`` counts ONLY TRUE fingerprints; UNAVAILABLE and FALSE
contribute nothing.  ``fingerprints_fired`` lists the exact fired keys in
C1..C6, R2 order (e.g. ``["C2", "C3", "C5", "R2"]``).  Per the directive,
fingerprint_count is RECORDED FOR ANALYSIS — it is not a threshold, gates
nothing, and creates no alert.

POINT-IN-TIME GUARANTEE (directive — no data leakage).  Every operand is
available at the exact trigger timestamp:

  * ``required_pts_per_min`` / ``league_average_pace`` / ``actual_pts_per_min``
    / ``recent_pace_3m`` are the SAME values the production alert already
    consumed (the projector row and :mod:`blm_v4.live_analytics.competition_pace`).
  * ``q3_ppm`` uses only the game's Q1-Q3 cumulative scores (the Q3 end is
    at-or-before the 75% boundary by construction) and the competition's
    Q3 archive via :func:`fingerprint_c5.q3_pace_reference` — the SAME
    point-in-time league-reference authority the C5 authorization shipped
    (per-competition population from authoritative OK finals; the live
    reference at the trigger instant contains only games settled before
    it, never future games).
  * NEVER read: the final total, the closing line, any Q4/future-quarter
    data, post-trigger pace or momentum, or a settlement result.  This
    module is pure arithmetic over its operands — it opens no database
    and reads no game state (enforced by test).

HISTORICAL OBSERVATIONS (read-only analysis 2026-09-21, reported as
context — NOT established production accuracy; C1 N=17 and C4 N=7 are
small samples)::

    C1  N= 17   UNDER%=88.24%      C5  N=103   UNDER%=76.70%
    C3  N=147   UNDER%=72.79%      R2  N= 87   UNDER%=78.16%
"""
from __future__ import annotations

from typing import Any

from blm_v4.live_analytics.fingerprint_c5 import (
    C5_NAME,
    C5_Q3_RATIO_MAX,
    C5_REQ_RATIO_MIN,
    FP_FALSE,
    FP_TRUE,
    FP_UNAVAILABLE,
    finite,
)
from blm_v4.live_analytics.under_alert import REQUIRED_MARGIN

LAYER_NAME = "HISTORICAL_UNDER_FINGERPRINTS"

# ── thresholds — EXACTLY the historical definitions, never re-tuned ────
#: C1 lower edge — INCLUSIVE (1.10 exactly qualifies).
C1_REQ_RATIO_MIN = 1.10
#: C1 upper edge — EXCLUSIVE (1.20 exactly does NOT qualify).  The band is
#: deliberately NOT widened to 1.35: the analysis's R1 measurement showed
#: [1.10, 1.35) runs BELOW the historical baseline.
C1_REQ_RATIO_MAX = 1.20
#: C3's required-pace leg — STRICTLY above the production margin itself
#: (1.04, the SAME constant the alert uses; a ratio exactly at 1.04 does
#: NOT qualify).
C3_REQ_RATIO_MIN = REQUIRED_MARGIN
#: The Q3-below-average leg (C3/C5/C6) — STRICTLY below 1.00 (reusing the
#: C5 constant: one authority for the Q3 comparison).
Q3_BELOW_AVG_MAX = C5_Q3_RATIO_MAX
#: R2 — the Q3 slump must be MATERIAL: strictly below 0.90x the league's
#: average Q3 pace (a ratio exactly at 0.90 does NOT qualify).
R2_Q3_RATIO_MAX = 0.90

# ── identities ─────────────────────────────────────────────────────────
C1_NAME = "HISTORICAL_UNDER_FINGERPRINT_C1"
C3_NAME = "HISTORICAL_UNDER_FINGERPRINT_C3"
R2_NAME = "HISTORICAL_UNDER_FINGERPRINT_R2"

#: The approved fingerprint keys, in canonical order.  Exactly seven.
#: R1 is deliberately absent (see module docstring).
FINGERPRINT_KEYS = ("C1", "C3", "C5", "R2")

#: Operator-facing labels — the same wording the read-only analysis used.
FINGERPRINT_LABELS = {
    "C1": "Required pace 1.10-1.20x league avg",
    "C3": "Required pace >1.04x + Q3 below average",
    "C5": "Required pace >1.10x + Q3 below average",
    "R2": "Q3 <0.90x league Q3 average",
}

_FINGERPRINT_NAMES = {
    "C1": C1_NAME, "C3": C3_NAME, "C5": C5_NAME, "R2": R2_NAME,
}


def _and(a: str, b: str) -> str:
    """Three-state conjunction: UNAVAILABLE propagates; otherwise TRUE
    only when BOTH legs are TRUE (fail closed — never silently TRUE)."""
    if a == FP_UNAVAILABLE or b == FP_UNAVAILABLE:
        return FP_UNAVAILABLE
    return FP_TRUE if (a == FP_TRUE and b == FP_TRUE) else FP_FALSE


def evaluate_fingerprints(required_pace: Any, league_average_pace: Any,
                          q3_ppm: Any, q3_league_avg: Any,
                          recent3_pace: Any = None,
                          actual_pace: Any = None,
                          recent3_minus_act: Any = None) -> dict:
    """Evaluate the full approved fingerprint set for ONE UNDER evaluation.

    Operands are the point-in-time values the alert itself consumed:

      * ``required_pace`` / ``league_average_pace`` — the projector's
        required pace and the competition's average pace;
      * ``q3_ppm`` / ``q3_league_avg`` — the game's own Q3 pace and its
        competition's average Q3 pace (see
        :func:`fingerprint_c5.q3_pace_reference`);
      * Legacy momentum arguments (``recent3_pace`` / ``actual_pace`` /
        ``recent3_minus_act``) are accepted for API/backward compatibility
        but are deliberately ignored: C2 and its dependent composites were
        removed from the live historical fingerprint layer.

    Returns the flat fingerprint block: one three-state status per
    fingerprint (``fingerprint_c1`` .. ``fingerprint_r2``), a boolean
    ``*_triggered`` per fingerprint (TRUE only for a proven TRUE —
    UNAVAILABLE is never a pass), ``fingerprint_count`` (TRUE only),
    ``fingerprints_fired`` (the exact fired keys), and every supporting
    value.  Any missing operand yields UNAVAILABLE for the fingerprints
    that need it — never a fabricated comparison, never a borrowed
    number.
    """
    required = finite(required_pace)
    league = finite(league_average_pace)
    q3 = finite(q3_ppm)
    q3avg = finite(q3_league_avg)
    # ── supporting ratios (UNAVAILABLE whenever an operand is missing) ──
    req_ratio = ((required / league)
                 if (required is not None and league is not None
                     and league > 0) else None)
    q3_ratio = ((q3 / q3avg)
                if (q3 is not None and q3avg is not None and q3avg > 0)
                else None)
    # ── the primitive three-state legs ─────────────────────────────────
    # C1: req_ratio inside [1.10, 1.20) — lower INCLUSIVE, upper EXCLUSIVE.
    if req_ratio is None:
        c1 = FP_UNAVAILABLE
    elif C1_REQ_RATIO_MIN <= req_ratio < C1_REQ_RATIO_MAX:
        c1 = FP_TRUE
    else:
        c1 = FP_FALSE
    # required-pace legs: C3 uses the production margin (STRICT > 1.04),
    # C5 its own audited edge (STRICT > 1.10).
    if req_ratio is None:
        leg_req_gt_margin = leg_req_gt_c5 = FP_UNAVAILABLE
    else:
        leg_req_gt_margin = (FP_TRUE if req_ratio > C3_REQ_RATIO_MIN
                             else FP_FALSE)
        leg_req_gt_c5 = (FP_TRUE if req_ratio > C5_REQ_RATIO_MIN
                         else FP_FALSE)
    # the Q3-below-league-average leg (STRICT < 1.00)
    if q3_ratio is None:
        leg_q3_below = FP_UNAVAILABLE
    elif q3_ratio < Q3_BELOW_AVG_MAX:
        leg_q3_below = FP_TRUE
    else:
        leg_q3_below = FP_FALSE

    # ── composite fingerprints: C3/C5 pair their required-pace leg with
    #    the same Q3 leg. UNAVAILABLE on any leg propagates.
    c3 = _and(leg_req_gt_margin, leg_q3_below)
    c5 = _and(leg_req_gt_c5, leg_q3_below)
    # R2: a MATERIAL Q3 slump — STRICTLY below 0.90x league Q3 average.
    if q3_ratio is None:
        r2 = FP_UNAVAILABLE
    elif q3_ratio < R2_Q3_RATIO_MAX:
        r2 = FP_TRUE
    else:
        r2 = FP_FALSE

    status = {"C1": c1, "C3": c3, "C5": c5, "R2": r2}
    fired = [k for k in FINGERPRINT_KEYS if status[k] == FP_TRUE]

    block = {
        "name": LAYER_NAME,
        # one three-state verdict per fingerprint (directive names)
        **{f"fingerprint_{k.lower()}": status[k] for k in FINGERPRINT_KEYS},
        # `*_triggered` is TRUE only for a proven TRUE — UNAVAILABLE is
        # never a pass (directive: do not silently treat missing as TRUE)
        **{f"fingerprint_{k.lower()}_triggered": status[k] == FP_TRUE
           for k in FINGERPRINT_KEYS},
        # only TRUE fingerprints count; recorded for analysis, never a gate
        "fingerprint_count": len(fired),
        # the exact fingerprints that fired, canonical order
        "fingerprints_fired": fired,
        # ── supporting values (directive) ──────────────────────────────
        "required_pace": required,
        "league_avg_pace": league,
        "req_ratio": (round(req_ratio, 6) if req_ratio is not None
                      else None),
        "q3_pace": q3,
        "league_q3_avg": q3avg,
        "q3_ratio": (round(q3_ratio, 6) if q3_ratio is not None else None),
        # ── C5-compat aliases — the pre-layer C5 block's field names, so
        #    consumers of the 2026-09-21 C5 authorization (and dashboard
        #    history records that sealed that shape) keep reading one truth
        "fingerprint_c5_req_ratio": (round(req_ratio, 6)
                                     if req_ratio is not None else None),
        "fingerprint_c5_q3_ratio": (round(q3_ratio, 6)
                                    if q3_ratio is not None else None),
        "required_pts_per_min": required,
        "league_average_pace": league,
        "q3_ppm": q3,
        "q3_league_avg": q3avg,
    }
    return block
