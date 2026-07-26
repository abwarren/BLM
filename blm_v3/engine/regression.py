"""
BLM V3 — Regression Probability.

Estimates the likelihood that the total will regress toward the line.
Regression is likely when the actual total is far from the line and the
game time is advanced enough for meaningful reversion.

Formula::

    distance_ratio = abs(total_line - fair_total) / max(total_line, fair_total, 1)
    time_ratio     = min(game_minutes / 48.0, 1.0)
    regression     = distance_ratio * time_ratio

Values:
  - 0.0 = no regression expected (tight to line or early game)
  - > 0.5 = notable regression probability
  - > 0.7 = high regression probability
"""

from __future__ import annotations

from typing import Optional


DISTANCE_THRESHOLD: float = 5.0
"""Distance from line to fair total above which regression is considered."""

RETURN_THRESHOLD: float = 1.0
"""Distance considered 'regression complete'."""


def compute_regression_probability(
    total_line: Optional[float],
    fair_total: Optional[float],
    game_minutes: float = 24.0,
) -> Optional[float]:
    """Compute regression probability based on line vs fair value divergence.

    Args:
        total_line: The live market total line.
        fair_total: The model's estimated fair value total.
        game_minutes: Minutes of game time elapsed (0-48+).

    Returns:
        Regression probability ∈ [0.0, 1.0], or ``None`` if data insufficient.
    """
    if total_line is None or fair_total is None or fair_total <= 0:
        return None

    distance = abs(total_line - fair_total)
    max_val = max(total_line, fair_total, 1)
    distance_ratio = min(distance / max_val * 10, 1.0)  # scale to 0-1

    time_ratio = min(game_minutes / 48.0, 1.0)

    probability = distance_ratio * time_ratio
    return round(min(probability, 1.0), 4)


def is_regression_candidate(
    total_line: Optional[float],
    fair_total: Optional[float],
    distance_threshold: float = DISTANCE_THRESHOLD,
) -> bool:
    """Check whether the market is a regression candidate.

    True when the line-fair total distance exceeds the threshold.
    """
    if total_line is None or fair_total is None:
        return False
    return abs(total_line - fair_total) >= distance_threshold


def has_regression_completed(
    total_line: Optional[float],
    fair_total: Optional[float],
    return_threshold: float = RETURN_THRESHOLD,
) -> bool:
    """Check whether regression has completed.

    True when the line and fair total have converged within threshold.
    """
    if total_line is None or fair_total is None:
        return False
    return abs(total_line - fair_total) <= return_threshold
