"""
BLM V2 — Confidence Engine

Computes a composite confidence score from 5 component inputs:

    PACE, LINE, INJURY, BLOWOUT, TEAM_TOTAL

Each input is a 0.0–1.0 scalar representing the reliability of that particular
data stream at the current snapshot.

Composite confidence is a weighted arithmetic mean:

    C = Σ(w_i × c_i) / Σ(w_i)

    where:
        c_i = confidence input for component i   ∈ [0.0, 1.0]
        w_i = weight assigned to component i      ∈ [0.0, ∞)
        C   = composite confidence                ∈ [0.0, 1.0]

Confidence drift tracks how the composite changes over consecutive snapshots.
Drift is computed as the absolute difference between the current and previous
composite value. Sustained drift (cumulative) can signal model uncertainty or
market instability.

Default weights (configurable):
    PACE:        0.25  — pace-based projections are most stable
    LINE:        0.25  — line data is usually high-quality
    INJURY:      0.15  — injury reports are sporadic but impactful
    BLOWOUT:     0.15  — blowout detection is reliable once triggered
    TEAM_TOTAL:  0.20  — team total data quality varies by league
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ── Typed Input / Output ──────────────────────────────────────────


@dataclass(frozen=True)
class ConfidenceInput:
    """Five component confidence scores, each 0.0–1.0.

    Attributes:
        pace:       Confidence in pace-based calculations (possession rate,
                    expected pace vs actual pace).
        line:       Confidence in line data quality (total movement,
                    spread movement, odds integrity).
        injury:     Confidence in injury-status data (player availability,
                    rotation changes).
        blowout:    Confidence in blowout detection (score differential
                    exceeding historical thresholds).
        team_total: Confidence in team-total data (home/away scoring rates,
                    quarter-by-quarter splits).
    """

    pace: float = 0.5
    line: float = 0.5
    injury: float = 0.5
    blowout: float = 0.5
    team_total: float = 0.5

    def __post_init__(self) -> None:
        """Validate all scores are in [0.0, 1.0]."""
        for name, val in [
            ("pace", self.pace),
            ("line", self.line),
            ("injury", self.injury),
            ("blowout", self.blowout),
            ("team_total", self.team_total),
        ]:
            if not 0.0 <= val <= 1.0:
                raise ValueError(
                    f"{name} confidence must be in [0.0, 1.0], got {val}"
                )

    def as_dict(self) -> Dict[str, float]:
        """Return component scores as a flat dict."""
        return {
            "pace": self.pace,
            "line": self.line,
            "injury": self.injury,
            "blowout": self.blowout,
            "team_total": self.team_total,
        }


@dataclass(frozen=True)
class ConfidenceDrift:
    """Statistics describing how confidence has changed over recent snapshots.

    Attributes:
        current_drift:   |C_curr - C_prev|, the one-step absolute difference.
        mean_drift:      Mean of the last N absolute drifts (exponential
                         moving average if N is large).
        max_drift:       Maximum absolute drift in the tracking window.
        drift_trend:     "increasing" if the last 3 drifts are monotonically
                         increasing, "decreasing" if monotonically decreasing,
                         "stable" otherwise.
        samples:         Number of drift observations in the window.
    """

    current_drift: float = 0.0
    mean_drift: float = 0.0
    max_drift: float = 0.0
    drift_trend: str = "stable"  # "increasing" | "decreasing" | "stable"
    samples: int = 0


@dataclass(frozen=True)
class ConfidenceOutput:
    """Output of the confidence engine for one snapshot.

    Attributes:
        composite_confidence: Weighted composite ∈ [0.0, 1.0].
        raw_input:            The five component scores as supplied.
        weights:              The weights used in the calculation.
        drift:                Drift statistics relative to previous snapshot.
    """

    composite_confidence: float
    raw_input: ConfidenceInput
    weights: Dict[str, float]
    drift: ConfidenceDrift


# ── Default Weights ───────────────────────────────────────────────

DEFAULT_CONFIDENCE_WEIGHTS: Dict[str, float] = {
    "pace": 0.25,
    "line": 0.25,
    "injury": 0.15,
    "blowout": 0.15,
    "team_total": 0.20,
}


# ── Engine ────────────────────────────────────────────────────────


class ConfidenceEngine:
    """Computes composite confidence and tracks drift over time.

    Usage::

        engine = ConfidenceEngine()
        inp = ConfidenceInput(pace=0.9, line=0.7, ...)
        out = engine.calculate(inp)
        # out.composite_confidence  → 0.71
        # out.drift.current_drift   → 0.0 (first snapshot)

    The engine maintains internal state (previous composite plus a drift
    history buffer) so consecutive calls build up drift statistics.
    Call ``reset()`` to clear the history for a new game.
    """

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        drift_window: int = 20,
    ) -> None:
        """Initialise the confidence engine.

        Args:
            weights: Per-component weights. Must include keys ``pace``,
                ``line``, ``injury``, ``blowout``, ``team_total`` if provided.
                Falls back to DEFAULT_CONFIDENCE_WEIGHTS.
            drift_window: Max number of historical composites retained for
                drift calculation.
        """
        self._weights = dict(DEFAULT_CONFIDENCE_WEIGHTS)
        if weights is not None:
            for key in DEFAULT_CONFIDENCE_WEIGHTS:
                if key in weights:
                    w = weights[key]
                    if w < 0.0:
                        raise ValueError(f"Weight '{key}' must be >= 0, got {w}")
                    self._weights[key] = w

        self._drift_window = max(drift_window, 2)
        self._history: List[float] = []  # composite values, newest last
        self._previous_composite: Optional[float] = None

    # ── Public API ────────────────────────────────────────────

    def calculate(self, inp: ConfidenceInput) -> ConfidenceOutput:
        """Compute composite confidence and drift for one snapshot.

        The calculation is deterministic given the same input and state.
        State is updated after each call so the next call can compute drift.

        Args:
            inp: The five component confidence scores.

        Returns:
            ConfidenceOutput with composite, raw input, weights, and drift.
        """
        composite = self._compute_composite(inp, self._weights)
        drift = self._compute_drift(composite)

        # Update internal state for the next call.
        self._history.append(composite)
        if len(self._history) > self._drift_window:
            self._history.pop(0)
        self._previous_composite = composite

        return ConfidenceOutput(
            composite_confidence=composite,
            raw_input=inp,
            weights=dict(self._weights),
            drift=drift,
        )

    def reset(self) -> None:
        """Clear internal history. Call when starting a new game."""
        self._history.clear()
        self._previous_composite = None

    @property
    def history(self) -> List[float]:
        """Read-only view of composite history (newest last)."""
        return list(self._history)

    # ── Internal Maths ────────────────────────────────────────

    @staticmethod
    def _compute_composite(
        inp: ConfidenceInput,
        weights: Dict[str, float],
    ) -> float:
        """Weighted arithmetic mean of the five component scores.

        .. math::

            C = \\frac{\\sum_{i} w_i \\cdot c_i}{\\sum_{i} w_i}

        Returns:
            Composite confidence clamped to [0.0, 1.0].
        """
        components = inp.as_dict()
        numerator = 0.0
        denominator = 0.0
        for key, w in weights.items():
            c = components[key]
            numerator += w * c
            denominator += w

        if denominator == 0.0:
            return 0.0

        raw = numerator / denominator
        return max(0.0, min(1.0, raw))

    def _compute_drift(self, current: float) -> ConfidenceDrift:
        """Compute drift statistics relative to the previous composite.

        Current drift is the absolute one-step difference.

        .. math::

            D_{current} = |C_t - C_{t-1}|

        Mean drift is the arithmetic mean of all absolute drifts in the
        history buffer.

        Max drift is the maximum absolute drift observed in the buffer.

        Drift trend:
            - "increasing" if the last 3 drifts are monotonically increasing
            - "decreasing" if the last 3 drifts are monotonically decreasing
            - "stable" otherwise

        Returns:
            ConfidenceDrift (all values 0.0 for the very first snapshot).
        """
        if self._previous_composite is None:
            return ConfidenceDrift(samples=0)

        current_drift = abs(current - self._previous_composite)

        if len(self._history) < 2:
            return ConfidenceDrift(
                current_drift=current_drift,
                mean_drift=current_drift,
                max_drift=current_drift,
                drift_trend="stable",
                samples=1,
            )

        # Compute drifts from the history buffer.
        drifts: List[float] = []
        for i in range(1, len(self._history)):
            drifts.append(abs(self._history[i] - self._history[i - 1]))

        # Also include the current drift.
        all_drifts = drifts + [current_drift]

        mean_drift = sum(all_drifts) / len(all_drifts)
        max_drift = max(all_drifts)

        # Trend: look at the last 3 drifts (use all available if < 3).
        recent = all_drifts[-3:]
        if len(recent) >= 2 and all(
            recent[i] > recent[i - 1] for i in range(1, len(recent))
        ):
            trend = "increasing"
        elif len(recent) >= 2 and all(
            recent[i] < recent[i - 1] for i in range(1, len(recent))
        ):
            trend = "decreasing"
        else:
            trend = "stable"

        return ConfidenceDrift(
            current_drift=current_drift,
            mean_drift=round(mean_drift, 6),
            max_drift=round(max_drift, 6),
            drift_trend=trend,
            samples=len(all_drifts),
        )
