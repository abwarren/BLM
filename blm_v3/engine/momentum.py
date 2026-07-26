"""
BLM V3 — Momentum Calculation.

Tracks the rate of scoring change weighted by recency using an exponential
moving average (EMA).

Formula::

    momentum_t = EMA(score_delta, alpha)
    where:
      score_delta = total_score(t) - total_score(t-1)
      alpha = DEFAULT_ALPHA (0.3)

The momentum value is positive when scoring is accelerating, negative when
decelerating, and near zero during steady-state play.
"""

from __future__ import annotations

from typing import Optional

# ── Constants ────────────────────────────────────────────────────────

DEFAULT_ALPHA: float = 0.3
"""Exponential moving average alpha for momentum calculation."""

MOMENTUM_SWING_THRESHOLD: float = 3.0
"""Absolute momentum change considered a 'swing'."""


def compute_momentum(
    current_total_score: int,
    previous_total_score: int,
    previous_momentum: Optional[float] = None,
    alpha: float = DEFAULT_ALPHA,
) -> float:
    """Compute momentum as an EMA of score delta.

    Args:
        current_total_score: Total score at this observation.
        previous_total_score: Total score at the previous observation.
        previous_momentum: Previous momentum value (None for first tick).
        alpha: EMA smoothing factor (default 0.3).

    Returns:
        Momentum score (positive = accelerating, negative = decelerating).
    """
    score_delta = float(current_total_score - previous_total_score)

    if previous_momentum is None:
        return round(score_delta, 4)

    momentum = alpha * score_delta + (1 - alpha) * previous_momentum
    return round(momentum, 4)


def compute_momentum_velocity(
    momentum_current: float,
    momentum_previous: float,
    time_elapsed_min: float = 1.0,
) -> Optional[float]:
    """Compute the rate of momentum change (first derivative).

    Args:
        momentum_current: Current momentum value.
        momentum_previous: Previous momentum value.
        time_elapsed_min: Time between observations in minutes.

    Returns:
        Velocity (momentum change per minute), or None if no time elapsed.
    """
    if time_elapsed_min <= 0:
        return None
    return round((momentum_current - momentum_previous) / time_elapsed_min, 4)


def compute_momentum_acceleration(
    velocity_current: float,
    velocity_previous: float,
    time_elapsed_min: float = 1.0,
) -> Optional[float]:
    """Compute the acceleration of momentum (second derivative).

    Args:
        velocity_current: Current momentum velocity.
        velocity_previous: Previous momentum velocity.
        time_elapsed_min: Time between observations in minutes.

    Returns:
        Acceleration, or None if no time elapsed.
    """
    if time_elapsed_min <= 0:
        return None
    return round((velocity_current - velocity_previous) / time_elapsed_min, 4)


def is_momentum_swing(
    current_momentum: float,
    previous_momentum: float,
    threshold: float = MOMENTUM_SWING_THRESHOLD,
) -> bool:
    """Detect whether a momentum swing occurred.

    True when the absolute momentum change exceeds the threshold.
    """
    return abs(current_momentum - previous_momentum) >= threshold
