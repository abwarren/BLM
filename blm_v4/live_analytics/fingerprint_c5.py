"""HISTORICAL_UNDER_FINGERPRINT_C5 — a confirmation gate on the 75% alert.

Authorization 2026-09-21.  This module adds ONE named historical
fingerprint as an ENRICHMENT/CONFIRMATION on the existing production
UNDER alert (``under_alert_state``).  It is NOT a replacement for the
required-pace rule, never loosens it, and never fires an alert by
itself: the production decision remains
``under_alert_state(...)`` verbatim, and this block only labels a fired
alert as matching the historical fingerprint.

THE FINGERPRINT (directive 2026-09-21 — analysis C5 family)::

    required_pts_per_min / league_average_pace  > 1.10   (STRICT)
    AND
    q3_ratio < 1.00                              (STRICT)

where ``q3_ratio`` = the game's THIRD-QUARTER pace (points scored in Q3
/ the classification's quarter minutes) divided by the game's OWN
competition's average Q3 pace.  Historical cohort (read-only backtest
``analysis_under_vs_over_pattern_discovery_2026-09-21.md`` §12): N=103,
UNDER 76.70% vs the 59.71% trigger baseline.

The band is deliberately NOT widened to 1.35: req/avg in [1.10,1.35)
measured 56.47% — BELOW the baseline — so the upper cut is part of the
fingerprint's meaning (the analysis's R1 result).

THREE-STATE SEMANTICS (fail closed — never silently TRUE)::

    TRUE         both ratios provable AND both comparisons pass
    FALSE        both ratios provable AND at least one comparison fails
    UNAVAILABLE  req_ratio or q3_ratio missing / non-finite / unprovable

POINT-IN-TIME GUARANTEE (directive #4-#8).  Every operand is available
at the trigger instant:

  * ``required_pts_per_min`` and ``league_average_pace`` are the SAME
    values the production alert already consumed (the projector row and
    :mod:`blm_v4.live_analytics.competition_pace`).
  * ``q3_ratio`` uses only the game's Q1-Q3 cumulative scores (the Q3
    end is at-or-before the 75% boundary by construction) and the
    competition's settled Q3 archive (``game_results`` OK finals only).
  * NEVER read: the final score, the closing line, any Q4/future
    quarter data, post-trigger pace, or a settlement result.  A source
    scan of this module proves it (enforced by test).

The Q3 league reference (:func:`q3_pace_reference`) mirrors
``competition_pace.competition_pace_reference`` exactly: per-competition
population from authoritative OK finals, ``duration_for`` quarter
minutes, grouped mean, per-database cache with the same TTL, read-only
and failure-isolated (empty reference -> the game's q3_ratio is
UNAVAILABLE, never a borrowed number).
"""
from __future__ import annotations

import math
import sqlite3
import time
from typing import Any, Optional

# ── the named condition (directive #2) ─────────────────────────────────
C5_NAME = "HISTORICAL_UNDER_FINGERPRINT_C5"

#: required/league-average ratio must exceed this STRICTLY.  The lower
#: edge of the audited C5 band; a required pace exactly at 1.10x does NOT
#: qualify (mirrors the production rule's own strictness at 1.04).
C5_REQ_RATIO_MIN = 1.10

#: q3_ratio must be BELOW this STRICTLY (the previous quarter ran below
#: its league-normal pace).  q3_ratio exactly 1.00 does NOT qualify.
C5_Q3_RATIO_MAX = 1.00

# ── the three evaluation states (never silently TRUE) ──────────────────
FP_TRUE = "TRUE"
FP_FALSE = "FALSE"
FP_UNAVAILABLE = "UNAVAILABLE"

# Same cache discipline as competition_pace: the population only changes
# when a game settles, so a short TTL keeps the payload honest while the
# per-poll cost stays a single grouped scan.
CACHE_TTL_SECONDS = 300.0
_cache: dict[str, Any] = {}


def finite(value: Any) -> Optional[float]:
    """The value as a float, or None when it is not a finite number
    (the same semantics as under_alert._finite)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def fingerprint_c5(required_pace: Any, league_average_pace: Any,
                   q3_ppm: Any, q3_league_avg: Any) -> dict:
    """Evaluate C5 from its four point-in-time operands.

    ``required_pace`` / ``league_average_pace`` are the production
    alert's own numbers; ``q3_ppm`` is the game's Q3 points-per-minute
    and ``q3_league_avg`` its competition's average Q3 pace.  Any
    missing operand yields UNAVAILABLE (fail closed) — a missing input
    is never a pass.
    """
    required = finite(required_pace)
    league = finite(league_average_pace)
    q3 = finite(q3_ppm)
    q3avg = finite(q3_league_avg)

    req_ratio = (required / league) if (required is not None and league
                                        is not None and league > 0) else None
    q3_ratio = (q3 / q3avg) if (q3 is not None and q3avg is not None
                                and q3avg > 0) else None

    if req_ratio is None or q3_ratio is None:
        status = FP_UNAVAILABLE
    elif req_ratio > C5_REQ_RATIO_MIN and q3_ratio < C5_Q3_RATIO_MAX:
        status = FP_TRUE
    else:
        status = FP_FALSE
    # `triggered` is TRUE only for a proven TRUE — UNAVAILABLE is never a
    # pass (directive: do not silently treat missing data as TRUE).
    triggered = status == FP_TRUE
    return {
        "name": C5_NAME,
        "fingerprint_c5": status,
        "fingerprint_c5_triggered": triggered,
        "fingerprint_c5_req_ratio": (round(req_ratio, 6)
                                     if req_ratio is not None else None),
        "fingerprint_c5_q3_ratio": (round(q3_ratio, 6)
                                    if q3_ratio is not None else None),
        # the display block (directive UI list) — the same numbers the
        # verdict was computed from, so screen and state never disagree
        "required_pts_per_min": required,
        "league_average_pace": league,
        "q3_ppm": q3,
        "q3_league_avg": q3avg,
    }


def q3_pace_reference(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{competition_slug: {"avg_q3_pace": float, "games": int}}``

    The per-competition average THIRD-QUARTER pace over authoritative
    settled finals — the exact construction the read-only analysis used:
    per game, the cumulative score at the Q3 end minus the cumulative at
    the Q2 end (snapshot maxima per period label — the cumulative only
    rises within a segment, so the segment maximum is its end state),
    divided by the classification's quarter minutes from
    ``projection.duration_for`` — the same units the alert's paces use.

    Mirrors ``competition_pace.competition_pace_reference``: OK finals
    only, per-slug groups (never a global rate, never merged
    competitions), per-database cache with the same TTL, read-only,
    failure-isolated (empty on any gap — a missing reference makes the
    game's q3_ratio UNAVAILABLE rather than borrowing a number).
    """
    now = time.monotonic()
    identity = _db_identity(conn)
    cached = _cache.get(identity)
    if cached and now - cached["computed_at"] < CACHE_TTL_SECONDS:
        return cached["reference"]

    # The Q3 segment needs BOTH boundary cumulatives; a game missing
    # either quarter label contributes nothing (fail closed, not guessed).
    try:
        rows = conn.execute(
            "SELECT g.competition_slug AS competition, "
            "       g.classification   AS classification, "
            "       s.q3_end           AS q3_end, "
            "       s.q2_end           AS q2_end "
            "FROM ("
            "  SELECT source_game_id, "
            "  MAX(CASE WHEN period_label='3rd Quarter' "
            "      THEN home_score+away_score END) AS q3_end, "
            "  MAX(CASE WHEN period_label='2nd Quarter' "
            "      THEN home_score+away_score END) AS q2_end "
            "  FROM snapshots GROUP BY source_game_id"
            ") AS s "
            "JOIN games g ON g.source_game_id = s.source_game_id "
            "JOIN game_results gr ON gr.source_game_id = s.source_game_id "
            "WHERE gr.final_result_status = 'OK' "
            "  AND gr.final_total IS NOT NULL AND gr.final_total > 0 "
            "  AND g.competition_slug IS NOT NULL "
            "  AND g.competition_slug <> ''"
        ).fetchall()
    except sqlite3.Error:
        return {}

    grouped: dict[str, list[float]] = {}
    for row in rows:
        # POSITIONAL access on purpose (see competition_pace): this module
        # must not depend on the caller having set row_factory.
        try:
            slug = row[0]
            classification = row[1]
            q3_end = row[2]
            q2_end = row[3]
        except (IndexError, TypeError):
            continue
        quarter_minutes = _quarter_minutes(classification)
        if not slug or not quarter_minutes or quarter_minutes <= 0:
            continue
        if q3_end is None or q2_end is None:
            continue
        segment = float(q3_end) - float(q2_end)
        if segment < 0:
            # a non-monotonic series (replay artefact) is not a pace —
            # exclude rather than poison the mean
            continue
        grouped.setdefault(slug, []).append(segment / quarter_minutes)

    reference: dict[str, dict] = {}
    for slug, values in grouped.items():
        if not values:
            continue
        reference[slug] = {
            "avg_q3_pace": round(sum(values) / len(values), 4),
            "games": len(values),
        }

    if reference:
        _cache[identity] = {"computed_at": now, "reference": reference}
    return reference


def _quarter_minutes(classification: Optional[str]) -> Optional[float]:
    """One regulation quarter's minutes for a classification — the
    projector's own authority, imported lazily to avoid an import cycle
    (same pattern as competition_pace._regulation_minutes)."""
    try:
        from blm_v4.projection import duration_for
        quarter, _full = duration_for(classification)
        return float(quarter) if quarter else None
    except Exception:
        return None


def _db_identity(conn: sqlite3.Connection) -> str:
    """A stable key for the database behind this connection."""
    try:
        for _seq, name, path in conn.execute("PRAGMA database_list"):
            if name == "main":
                return path or ":memory:"
    except sqlite3.Error:
        pass
    return "unknown"
