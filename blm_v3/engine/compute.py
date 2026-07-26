"""
BLM V3 — Derived Metric Compute Pipeline.

Orchestrates all derived metric computations for a single snapshot:
  1. Inflation index
  2. Compression index
  3. Momentum
  4. Regression probability
  5. Variance / Volatility
  6. Fair total / Expected total
  7. Signal detection
  8. Event classification

This module ties together ``blm_v3.engine.*`` and ``blm_v3.signals.*`` into
a single ``compute_all()`` function.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from blm_v3.engine.inflation import (
    compute_inflation_index,
)
from blm_v3.engine.compression import (
    compute_compression_index,
)
from blm_v3.engine.momentum import (
    compute_momentum,
    is_momentum_swing,
)
from blm_v3.engine.regression import (
    compute_regression_probability,
    is_regression_candidate,
)
from blm_v3.engine.variance import (
    compute_variance,
    compute_volatility,
)
from blm_v3.engine.fair_total import (
    compute_fair_total,
    compute_expected_total,
)
from blm_v3.signals.detector import (
    detect_all,
)
from blm_v3.signals.event_classifier import (
    classify_events,
)


def compute_all(
    snapshot: dict[str, Any],
    prev_snapshot: Optional[dict[str, Any]] = None,
    rolling_line_values: Optional[list[float]] = None,
    rolling_total_values: Optional[list[float]] = None,
    prev_momentum: Optional[float] = None,
    prev_signals: Optional[list[dict[str, Any]]] = None,
    consecutive_zero_deltas: int = 0,
    game_start_total: int = 0,
    game_start_line: Optional[float] = None,
    game_minutes: float = 0.0,
) -> dict[str, Any]:
    """Compute ALL derived metrics and signals for a single snapshot.

    This is the main entry point for the historical processing pipeline.

    Args:
        snapshot: The current snapshot dict (may already have pre-computed
            fields like ``line_delta``, ``possessions_per_min``, etc.
            from the collector).
        prev_snapshot: Previous snapshot dict, or None.
        rolling_line_values: Recent total line values (for variance/volatility).
        rolling_total_values: Recent total score values (for variance).
        prev_momentum: Previous momentum value, or None.
        prev_signals: Signals from recent snapshots (for event classification).
        consecutive_zero_deltas: Freeze counter.
        game_start_total: Total score at game start.
        game_start_line: Total line at game start.
        game_minutes: Minutes of game time elapsed.

    Returns:
        A dict with keys:
        - ``snapshot``: The snapshot dict with all derived fields set
        - ``signals``: List of signal dicts
        - ``events``: List of event dicts
    """
    # Work on a copy to avoid mutating the original
    s = dict(snapshot)

    # ── 1. Inflation Index ───────────────────────────────────
    inflation_index = compute_inflation_index(
        current_total_score=s.get("total_score", 0),
        current_total_line=s.get("total_line"),
        start_total_score=game_start_total,
        start_total_line=game_start_line,
    )
    if inflation_index is not None and s.get("inflation_index") is None:
        s["inflation_index"] = inflation_index

    # ── 2. Compression Index ────────────────────────────────
    compression_index = compute_compression_index(
        over_odds=s.get("over_odds"),
        under_odds=s.get("under_odds"),
    )
    if compression_index is not None and s.get("compression_index") is None:
        s["compression_index"] = compression_index

    # ── 3. Momentum ─────────────────────────────────────────
    if prev_snapshot is not None:
        momentum = compute_momentum(
            current_total_score=s.get("total_score", 0),
            previous_total_score=prev_snapshot.get("total_score", 0),
            previous_momentum=prev_momentum,
        )
    else:
        momentum = 0.0
    if s.get("momentum") is None:
        s["momentum"] = momentum

    # ── 4. Regression Probability ───────────────────────────
    regression_prob = compute_regression_probability(
        total_line=s.get("total_line"),
        fair_total=s.get("fair_total"),  # may already be set
        game_minutes=game_minutes,
    )
    if regression_prob is not None and s.get("regression_prob") is None:
        s["regression_prob"] = regression_prob

    # ── 5. Variance / Volatility ────────────────────────────
    line_vals = rolling_line_values or []
    if s.get("total_line") is not None:
        line_vals = list(line_vals) + [s["total_line"]]

    variance = compute_variance(line_vals)  # type: ignore[arg-type]
    volatility = compute_volatility(line_vals)  # type: ignore[arg-type]
    if variance is not None and s.get("variance") is None:
        s["variance"] = variance
    if volatility is not None and s.get("volatility") is None:
        s["volatility"] = volatility

    # ── 6. Fair Total / Expected Total ──────────────────────
    fair_total = compute_fair_total(
        projected_total=s.get("projected_total"),
        game_minutes=game_minutes,
        current_total=s.get("total_score"),
        total_line=s.get("total_line"),
    )
    if fair_total is not None and s.get("fair_total") is None:
        s["fair_total"] = fair_total

    expected_total = compute_expected_total(
        fair_total=fair_total or s.get("fair_total"),
        total_line=s.get("total_line"),
        regression_prob=regression_prob,
    )
    if expected_total is not None and s.get("expected_total") is None:
        s["expected_total"] = expected_total

    # ── 7. Confidence (simple heuristic) ─────────────────────
    confidence = _compute_confidence(
        compression_index=compression_index,
        volatility=volatility,
        regression_prob=regression_prob,
        game_minutes=game_minutes,
    )
    if confidence is not None and s.get("confidence") is None:
        s["confidence"] = confidence

    # ── 8. Signal Detection ─────────────────────────────────
    context = {
        "prev_snapshot": prev_snapshot,
        "rolling_line_values": line_vals,
        "consecutive_zero_deltas": consecutive_zero_deltas,
        "prev_momentum": prev_momentum,
        "current_game_minutes": game_minutes,
        "inflation_index": inflation_index,
        "compression_index": compression_index,
        "trap_meter": s.get("trap_meter", 0),
    }
    signals = detect_all(s, context)

    # ── 9. Event Classification ─────────────────────────────
    events = classify_events(
        current_signals=signals,
        recent_signals=prev_signals or [],
        current_timestamp=s.get("timestamp", ""),
        current_snapshot_id=s.get("id", ""),
        game_id=s.get("game_id", ""),
    )

    return {
        "snapshot": s,
        "signals": signals,
        "events": events,
    }


def _compute_confidence(
    compression_index: Optional[float] = None,
    volatility: Optional[float] = None,
    regression_prob: Optional[float] = None,
    game_minutes: float = 24.0,
) -> float:
    """Compute a naive confidence score from available metrics.

    Factors:
      - Compression (tight odds = higher confidence)
      - Volatility (high volatility = lower confidence)
      - Regression (high regression prob = lower confidence — model is uncertain)
      - Game time (later game = higher confidence — more data available)

    Returns a value in [0.0, 1.0].
    """
    score = 0.5  # default neutral

    if compression_index is not None:
        score += (compression_index - 0.5) * 0.3

    if volatility is not None:
        score -= min(volatility / 10.0, 0.3)

    if regression_prob is not None:
        score -= regression_prob * 0.2

    # Time bonus: later game = more data = higher confidence
    time_bonus = min(game_minutes / 96.0, 0.2)
    score += time_bonus

    return round(max(0.0, min(1.0, score)), 4)
