"""
BLM V3 — Pace Calculator.

Computes pace-related metrics from consecutive snapshots.  Pure functions —
no state, no I/O.

Pace measures the rate of scoring::
    - possessions_per_min:   Points scored per minute of game time
    - projected_possessions: Estimated full-game possessions at current pace
    - projected_total:       Projected final total score at current pace
"""

from __future__ import annotations

import re
from typing import Optional


# ── Clock parsing ────────────────────────────────────────────────────


_MMSS_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_clock_to_seconds(clock: Optional[str]) -> Optional[float]:
    """Parse "MM:SS" to total seconds elapsed in the current quarter.

    Returns ``None`` if the clock string is ``None``, malformed, or
    indicates ``0:00`` (quarter over).

    Example: "7:30" → 270.0 (4 min 30 sec elapsed in quarter).
    """
    if not clock:
        return None
    m = _MMSS_RE.match(clock.strip())
    if not m:
        return None
    mins = int(m.group(1))
    secs = int(m.group(2))
    total = mins * 60 + secs
    if total <= 0:
        return None
    return float(total)


def clock_elapsed_seconds(clock: Optional[str]) -> Optional[float]:
    """Return the number of seconds *elapsed* in the current quarter.

    A 12-minute quarter has 720 seconds.  If clock is "7:30" (450 s remaining),
    the elapsed time is 720 - 450 = 270 s.
    """
    remaining = parse_clock_to_seconds(clock)
    if remaining is None:
        return None
    return 720.0 - remaining  # 12 min quarter


def game_elapsed_minutes(
    quarter: int,
    clock: Optional[str],
) -> Optional[float]:
    """Total game minutes elapsed (including completed quarters).

    Each quarter is 12 minutes.  Returns ``None`` if clock can't be parsed.
    """
    if quarter <= 0:
        return None
    completed_quarters = quarter - 1
    elapsed_in_current = clock_elapsed_seconds(clock)
    if elapsed_in_current is None:
        return completed_quarters * 12.0
    return completed_quarters * 12.0 + elapsed_in_current / 60.0


# ── Pace computations ────────────────────────────────────────────────


def compute_pace_metrics(
    prev_snapshot: dict,
    curr_snapshot: dict,
) -> dict:
    """Compute pace-related metrics between two consecutive snapshots.

    Args:
        prev_snapshot: Previous snapshot dict (must contain ``quarter``,
            ``clock``, ``home_score``, ``away_score``, ``total_score``).
        curr_snapshot: Current snapshot dict (same fields).

    Returns:
        A dict with keys:
        - ``possessions`` (int | None): total points scored so far
        - ``possessions_per_min`` (float | None): points per minute
        - ``projected_possessions`` (float | None): projected full-game
        - ``projected_total`` (float | None): projected final total
    """
    prev_q = prev_snapshot.get("quarter", 1)
    curr_q = curr_snapshot.get("quarter", 1)
    prev_clock = prev_snapshot.get("clock")
    curr_clock = curr_snapshot.get("clock")

    prev_game_min = game_elapsed_minutes(prev_q, prev_clock)
    curr_game_min = game_elapsed_minutes(curr_q, curr_clock)

    prev_total = prev_snapshot.get("total_score", 0) or (
        prev_snapshot.get("home_score", 0) + prev_snapshot.get("away_score", 0)
    )
    curr_total = curr_snapshot.get("total_score", 0) or (
        curr_snapshot.get("home_score", 0) + curr_snapshot.get("away_score", 0)
    )

    result: dict = {
        "possessions": curr_total,
        "possessions_per_min": None,
        "projected_possessions": None,
        "projected_total": None,
    }

    # Can't compute pace without game time
    if curr_game_min is None or curr_game_min <= 0:
        return result

    # Points per minute of game time elapsed
    p_min = curr_total / curr_game_min
    result["possessions_per_min"] = round(p_min, 4)

    # Projected total at current pace over 48 minutes
    result["projected_total"] = round(p_min * 48.0, 2)

    # Projected possessions (estimated from pace: ~1.1 pts per possession)
    # For Cyber 2K26 basketball, ~1.1 points per possession is typical
    ppp = 1.1  # points per possession estimate
    if ppp > 0:
        projected_poss = round(curr_total / ppp + (48.0 - curr_game_min) * p_min / ppp, 0)
        result["projected_possessions"] = projected_poss

    return result


# ── Backward compatibility: also export a stateless class wrapper ────


class PaceCalculator:
    """Stateless calculator for game pace metrics.

    Usage::

        pc = PaceCalculator()
        result = pc.compute(prev_snapshot, curr_snapshot)
        print(result.projected_total)
    """

    @staticmethod
    def compute(prev_snapshot: dict, curr_snapshot: dict) -> dict:
        """Alias for :func:`compute_pace_metrics`."""
        return compute_pace_metrics(prev_snapshot, curr_snapshot)
