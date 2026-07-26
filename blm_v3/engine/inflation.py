"""
BLM V3 — Inflation Index.

Measures how much the total line has moved relative to the actual score.
A positive inflation index means the line is inflating faster than scoring
warrants (potential overreaction).

Formula::

    inflation_index = score_movement - line_sensitivity
    where:
      score_movement = (curr_total - start_total) / max(start_total, 1)
      line_movement   = (curr_line - start_line) / max(abs(start_line), 1)
      line_sensitivity = line_movement * sensitivity_factor
      inflation_index  = score_movement - line_sensitivity

Values:
  - 0.0 = line is tracking score perfectly
  - > 3.0 = notable inflation
  - > 5.0 = significant inflation
  - < -3.0 = notable deflation
"""

from __future__ import annotations

from typing import Optional


DEFAULT_SENSITIVITY: float = 1.0
"""Sensitivity factor for line movement contribution."""

DEFAULT_START_LINE: float = 205.0
"""Default starting total line when no previous data."""


def compute_inflation_index(
    current_total_score: int,
    current_total_line: Optional[float],
    start_total_score: int = 0,
    start_total_line: Optional[float] = DEFAULT_START_LINE,
    sensitivity: float = DEFAULT_SENSITIVITY,
) -> Optional[float]:
    """Compute the market inflation index for the current observation.

    Args:
        current_total_score: Actual combined score at this observation.
        current_total_line: Live total line at this observation.
        start_total_score: Score at the start of tracking (game start or
            a reference point). Default 0.
        start_total_line: Total line at the start. Default 205.0.
        sensitivity: How much line movement contributes. Default 1.0.

    Returns:
        Inflation index as a float, or ``None`` if insufficient data.
    """
    if current_total_line is None or start_total_line is None:
        return None

    score_delta = current_total_score - start_total_score
    line_delta = current_total_line - start_total_line

    # Normalise to avoid division by zero
    score_base = max(start_total_score, 1)
    line_base = max(abs(start_total_line), 1)

    score_movement = score_delta / score_base
    line_movement = line_delta / line_base
    line_sensitivity = line_movement * sensitivity

    return round(score_movement - line_sensitivity, 4)


def classify_inflation(inflation_index: Optional[float]) -> str:
    """Classify the inflation level.

    Returns one of: ``extreme_deflation``, ``deflation``, ``normal``,
    ``inflation``, ``extreme_inflation``, or ``unknown``.
    """
    if inflation_index is None:
        return "unknown"
    if inflation_index > 5.0:
        return "extreme_inflation"
    if inflation_index > 3.0:
        return "inflation"
    if inflation_index < -5.0:
        return "extreme_deflation"
    if inflation_index < -3.0:
        return "deflation"
    return "normal"
