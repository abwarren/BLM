"""
BLM V3 — Variance & Volatility.

Tracks the variance and volatility of the total line over a rolling window.
High variance/volatility indicates an unstable market.

Formula::

    mean       = AVG(total_line_values[-N:])
    variance   = SUM((x - mean)^2 / N)
    volatility = SQRT(variance)
"""

from __future__ import annotations

import math
from typing import Optional

# ── Constants ────────────────────────────────────────────────────────

DEFAULT_WINDOW: int = 10
"""Number of recent observations for rolling calculations."""


def compute_variance(
    values: list[Optional[float]],
    window: int = DEFAULT_WINDOW,
) -> Optional[float]:
    """Compute the sample variance of the most recent ``window`` values.

    Args:
        values: List of values (may contain ``None``, which are filtered out).
        window: Maximum number of recent values to consider.

    Returns:
        Variance as a float, or ``None`` if fewer than 2 valid values.
    """
    clean = [v for v in values if v is not None][-window:]
    if len(clean) < 2:
        return None

    n = len(clean)
    mean = sum(clean) / n
    variance = sum((x - mean) ** 2 for x in clean) / (n - 1)  # sample variance
    return round(variance, 4)


def compute_volatility(
    values: list[Optional[float]],
    window: int = DEFAULT_WINDOW,
) -> Optional[float]:
    """Compute the volatility (standard deviation) of recent values.

    Args:
        values: List of values (may contain ``None``).
        window: Maximum number of recent values to consider.

    Returns:
        Volatility (standard deviation), or ``None`` if insufficient data.
    """
    variance = compute_variance(values, window)
    if variance is None:
        return None
    return round(math.sqrt(variance), 4)


def classify_volatility(
    volatility: Optional[float],
    baseline: float = 2.0,
) -> str:
    """Classify volatility relative to a baseline.

    Returns one of: ``very_low``, ``low``, ``moderate``, ``high``,
    ``extreme``, or ``unknown``.
    """
    if volatility is None:
        return "unknown"
    ratio = volatility / baseline
    if ratio < 0.3:
        return "very_low"
    if ratio < 0.7:
        return "low"
    if ratio < 1.3:
        return "moderate"
    if ratio < 2.0:
        return "high"
    return "extreme"
