"""
BLM V3 — Compression Index.

Measures how tight the over/under odds are around the line.
Tight odds (both close to 1.91 / -110) indicate a high-confidence market.
Wide odds indicate uncertainty.

Formula::

    compression = 1.0 - (over_spread + under_spread) / max_spread
    where:
      over_spread  = abs(over_odds - fair_odds)
      under_spread = abs(under_odds - fair_odds)
      max_spread   = 0.50 (wide limit)

Values:
  - 1.0 = perfectly tight (both odds at fair value)
  - 0.8+ = tight odds (high confidence market)
  - 0.4-0.8 = normal
  - < 0.4 = wide odds (low confidence market)
"""

from __future__ import annotations

from typing import Optional

# ── Constants ────────────────────────────────────────────────────────

FAIR_ODDS: float = 1.9091
"""Fair over/under odds at -110 vig (decimal)."""

MAX_SPREAD: float = 0.50
"""Maximum spread used for normalisation — wider than this is extreme."""


def compute_compression_index(
    over_odds: Optional[float],
    under_odds: Optional[float],
    fair_odds: float = FAIR_ODDS,
    max_spread: float = MAX_SPREAD,
) -> Optional[float]:
    """Compute the odds compression index.

    Args:
        over_odds: Decimal odds for OVER.
        under_odds: Decimal odds for UNDER.
        fair_odds: Fair odds reference (default 1.9091 ≈ -110).
        max_spread: Maximum spread for normalisation.

    Returns:
        Compression index ∈ [0.0, 1.0], or ``None`` if odds unavailable.
    """
    if over_odds is None or under_odds is None:
        return None

    over_spread = abs(over_odds - fair_odds)
    under_spread = abs(under_odds - fair_odds)
    total_spread = over_spread + under_spread

    if max_spread <= 0:
        return 1.0

    compression = 1.0 - min(total_spread / max_spread, 1.0)
    return round(compression, 4)


def classify_compression(compression_index: Optional[float]) -> str:
    """Classify the compression level.

    Returns one of: ``tight``, ``normal``, ``wide``, or ``unknown``.
    """
    if compression_index is None:
        return "unknown"
    if compression_index >= 0.85:
        return "tight"
    if compression_index >= 0.40:
        return "normal"
    return "wide"
