"""
BLM V3 — Fair Total Estimation.

Estimates the 'fair' value of the total based on current game pace,
historical league pace, and regression toward mean.

Formula::

    fair_total = projected_total * regression_factor
    where:
      regression_factor = 0.5 + (game_minutes / 96.0)
      projected_total   = pace_per_min * 48 (from pace calculator)

The fair total converges toward the projected total early in the game
and toward the actual score pace late in the game.
"""

from __future__ import annotations

from typing import Optional


# ── Constants ────────────────────────────────────────────────────────

DEFAULT_LEAGUE_PACE: float = 108.0
"""Default expected pace for Cyber 2K26 (possessions per 48 min)."""

DEFAULT_POINTS_PER_POSSESSION: float = 1.1
"""Estimated points per possession for Cyber 2K26."""


def compute_fair_total(
    projected_total: Optional[float],
    game_minutes: float = 24.0,
    current_total: Optional[int] = None,
    total_line: Optional[float] = None,
) -> Optional[float]:
    """Estimate the fair value total for the current game state.

    Args:
        projected_total: Projected final total from pace calculator.
        game_minutes: Minutes of game time elapsed.
        current_total: Actual total score so far (for late-game weighting).
        total_line: Current market total line (for anchoring).

    Returns:
        Fair total estimate, or ``None`` if insufficient data.
    """
    if projected_total is None:
        return None

    # Regression factor: early game leans on projection,
    # late game leans on actual score
    time_ratio = min(game_minutes / 48.0, 1.0)
    regression_factor = 0.3 + time_ratio * 0.4  # 0.3 early → 0.7 late

    if current_total is not None and current_total > 0:
        actual_pace_total = (current_total / max(game_minutes, 1)) * 48.0
        fair = projected_total * (1.0 - regression_factor) + actual_pace_total * regression_factor
    else:
        fair = projected_total

    # Anchor to line if line is available (mild convergence)
    if total_line is not None and total_line > 0:
        fair = fair * 0.7 + total_line * 0.3

    return round(fair, 2)


def compute_expected_total(
    fair_total: Optional[float],
    total_line: Optional[float] = None,
    regression_prob: Optional[float] = None,
) -> Optional[float]:
    """Compute the expected final total, blending fair value and market line.

    When regression probability is high, the expected total leans toward
    the market line.  When low, it leans toward the fair value.

    Args:
        fair_total: The fair value total.
        total_line: The live market total line.
        regression_prob: Regression probability [0, 1].

    Returns:
        Expected total, or ``None`` if fair_total unavailable.
    """
    if fair_total is None:
        return None

    if total_line is None:
        return fair_total

    if regression_prob is not None:
        blend = fair_total * (1.0 - regression_prob) + total_line * regression_prob
        return round(blend, 2)

    # Default: 50/50 blend
    return round((fair_total + total_line) / 2.0, 2)
