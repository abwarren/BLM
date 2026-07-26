"""
BLM V3 — Market Movement Tracker.

Tracks changes to market lines, odds, and spreads between consecutive
observations.  Pure functions — no state, no I/O.

Each delta is computed as ``current - previous`` so:
  - Positive line_delta = line moved UP (over more attractive)
  - Negative line_delta = line moved DOWN (under more attractive)
  - Zero = no change
"""

from __future__ import annotations

from typing import Optional


def compute_movement_deltas(
    prev: dict,
    curr: dict,
) -> dict:
    """Compute line, odds, and spread deltas between two snapshots.

    Args:
        prev: Previous snapshot dict.
        curr: Current snapshot dict.

    Returns:
        Dict with keys:
        - ``line_delta`` (float | None): total line change
        - ``odds_delta`` (float | None): over odds change
        - ``spread_delta`` (float | None): spread change
    """
    result: dict = {
        "line_delta": None,
        "odds_delta": None,
        "spread_delta": None,
    }

    prev_line = prev.get("total_line") or prev.get("total_line_raw")
    curr_line = curr.get("total_line") or curr.get("total_line_raw")
    if prev_line is not None and curr_line is not None:
        result["line_delta"] = round(curr_line - prev_line, 2)

    prev_odds = prev.get("over_odds")
    curr_odds = curr.get("over_odds")
    if prev_odds is not None and curr_odds is not None:
        result["odds_delta"] = round(curr_odds - prev_odds, 4)

    prev_spread = prev.get("spread") or prev.get("spread_raw")
    curr_spread = curr.get("spread") or curr.get("spread_raw")
    if prev_spread is not None and curr_spread is not None:
        result["spread_delta"] = round(curr_spread - prev_spread, 2)

    return result


def is_market_frozen(
    deltas: dict,
    consecutive_zero_deltas: int,
    min_ticks: int = 10,
) -> bool:
    """Check whether the market appears frozen.

    A market is "frozen" when the line has not moved for ``min_ticks``
    consecutive observations while the score has changed.

    Args:
        deltas: Output of ``compute_movement_deltas()``.
        consecutive_zero_deltas: How many consecutive snapshots had zero
            line delta.
        min_ticks: Minimum consecutive zeros to declare a freeze.

    Returns:
        True if the market is frozen.
    """
    line_delta = deltas.get("line_delta")
    if line_delta is not None and abs(line_delta) < 0.01:
        return consecutive_zero_deltas >= min_ticks
    return False


class MovementTracker:
    """Stateless tracker for market movement detection.

    Usage::

        tracker = MovementTracker()
        deltas = tracker.compute(prev_snapshot, curr_snapshot)
        if deltas["line_delta"] and abs(deltas["line_delta"]) > 2.0:
            print("Line jump detected!")
    """

    @staticmethod
    def compute(prev: dict, curr: dict) -> dict:
        """Alias for :func:`compute_movement_deltas`."""
        return compute_movement_deltas(prev, curr)

    @staticmethod
    def is_frozen(
        deltas: dict,
        consecutive_zero_deltas: int,
        min_ticks: int = 10,
    ) -> bool:
        """Alias for :func:`is_market_frozen`."""
        return is_market_frozen(deltas, consecutive_zero_deltas, min_ticks)
