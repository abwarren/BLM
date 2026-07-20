"""
BLM V2 — Momentum Engine

Computes momentum metrics from a raw input signal using an exponential
moving average (EMA) approach.

Pipeline::

    raw_signal  ──[EMA]──►  momentum_score  ──[Δ]──►  velocity  ──[Δ]──►  acceleration
                            momentum_score  ──[classify]──►  direction
                            momentum_score  ──[magnitude]──►  strength

Key formulas:

    momentum_score(t) = α × raw(t) + (1 - α) × momentum_score(t-1)

    momentum_velocity(t)  = momentum_score(t) - momentum_score(t-1)

    momentum_acceleration(t) = momentum_velocity(t) - momentum_velocity(t-1)

    momentum_strength = |momentum_score - 50| × 2

    momentum_direction = "up"     if score > prev + threshold
                         "down"   if score < prev - threshold
                         "flat"   otherwise

Where:
    α (alpha) is the EMA smoothing factor ∈ (0, 1].
        Higher α = more responsive to recent input, less smoothing.
        Default α = 0.35 (moderate smoothing).

    momentum_score is bounded to [0, 100].

    momentum_strength maps linearly from 0 (at score = 50) to 100
    (at score = 0 or score = 100), representing how far the score has
    deviated from the neutral midpoint.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional


# ── Typed Input / Output ──────────────────────────────────────────


@dataclass(frozen=True)
class MomentumInput:
    """Input signal for a single momentum calculation.

    Attributes:
        raw_momentum: A value in [0, 100] representing the raw momentum
                      signal. This could come from line movement rate,
                      scoring burst intensity, or any other momentum proxy.
        timestamp:    Optional epoch-seconds tiebreaker for ordering.
    """

    raw_momentum: float
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.raw_momentum <= 100.0:
            raise ValueError(
                f"raw_momentum must be in [0, 100], got {self.raw_momentum}"
            )


@dataclass(frozen=True)
class MomentumOutput:
    """Complete momentum analysis for one snapshot.

    Attributes:
        momentum_score:        EMA-smoothed score ∈ [0, 100].
        momentum_direction:    "up" | "down" | "flat".
        momentum_velocity:     Rate of change (current - previous score).
        momentum_acceleration: Second derivative (velocity - previous velocity).
        momentum_strength:     Magnitude ∈ [0, 100]; 0 = neutral, 100 = extreme.
        momentum_strength_label: "weak" | "moderate" | "strong" | "extreme".
        raw_input:             The raw signal that was fed in.
        alpha:                 EMA smoothing factor used.
    """

    momentum_score: float
    momentum_direction: str  # "up" | "down" | "flat"
    momentum_velocity: float
    momentum_acceleration: float
    momentum_strength: float
    momentum_strength_label: str  # "weak" | "moderate" | "strong" | "extreme"
    raw_input: MomentumInput
    alpha: float


# ── Strength Classification Thresholds ────────────────────────────

# strength thresholds (inclusive upper bound)
STRENGTH_RANGES: List[tuple[str, float]] = [
    ("weak", 25.0),
    ("moderate", 50.0),
    ("strong", 75.0),
    ("extreme", 100.0),
]


def classify_strength(strength: float) -> str:
    """Classify a momentum strength value into a human label.

    Thresholds:
        [0, 25]  → "weak"
        (25, 50] → "moderate"
        (50, 75] → "strong"
        (75, 100] → "extreme"
    """
    for label, bound in STRENGTH_RANGES:
        if strength <= bound:
            return label
    return "extreme"


# ── Engine ────────────────────────────────────────────────────────


class MomentumEngine:
    """Computes momentum metrics via EMA with stateful history tracking.

    Usage::

        engine = MomentumEngine(alpha=0.35)
        inp = MomentumInput(raw_momentum=72.0)
        out = engine.calculate(inp)
        # out.momentum_score        → 72.0  (first call, no history)
        # out.momentum_velocity     → 0.0
        # out.momentum_direction    → "flat"

    Second call with a different raw signal produces derivatives::

        inp2 = MomentumInput(raw_momentum=85.0)
        out2 = engine.calculate(inp2)
        # out2.momentum_velocity    → score₂ - score₁  (non-zero)
        # out2.momentum_direction   → "up"

    Call ``reset()`` to clear state between games.
    """

    def __init__(
        self,
        alpha: float = 0.35,
        direction_threshold: float = 1.5,
    ) -> None:
        """Initialise the momentum engine.

        Args:
            alpha: EMA smoothing factor ∈ (0.0, 1.0].
                   Default 0.35 — moderate smoothing.
            direction_threshold: Minimum absolute difference between
                consecutive momentum scores to classify as "up" or "down"
                instead of "flat". Default 1.5.
        """
        if not 0.0 < alpha <= 1.0:
            raise ValueError(f"alpha must be in (0.0, 1.0], got {alpha}")
        self._alpha = alpha
        self._threshold = max(direction_threshold, 0.0)

        # Internal state
        self._previous_score: Optional[float] = None
        self._previous_velocity: Optional[float] = None
        self._score_history: List[float] = []

    # ── Public API ────────────────────────────────────────────

    def calculate(self, inp: MomentumInput) -> MomentumOutput:
        """Process one raw-momentum input and produce the full momentum analysis.

        The calculation is deterministic given the same input + engine state.

        Args:
            inp: Raw momentum signal in [0, 100].

        Returns:
            MomentumOutput with all derived metrics.
        """
        # Step 1: EMA smoothing
        score = self._compute_ema(inp.raw_momentum)

        # Step 2: Direction
        direction = self._compute_direction(score)

        # Step 3: Velocity (first derivative)
        velocity = self._compute_velocity(score)

        # Step 4: Acceleration (second derivative)
        acceleration = self._compute_acceleration(velocity)

        # Step 5: Strength
        strength = self._compute_strength(score)
        strength_label = classify_strength(strength)

        # Update state
        self._previous_score = score
        self._previous_velocity = velocity
        self._score_history.append(score)

        return MomentumOutput(
            momentum_score=round(score, 4),
            momentum_direction=direction,
            momentum_velocity=round(velocity, 4),
            momentum_acceleration=round(acceleration, 4),
            momentum_strength=round(strength, 4),
            momentum_strength_label=strength_label,
            raw_input=inp,
            alpha=self._alpha,
        )

    def reset(self) -> None:
        """Clear internal state. Call when starting a new game."""
        self._previous_score = None
        self._previous_velocity = None
        self._score_history.clear()

    @property
    def score_history(self) -> List[float]:
        """Read-only view of EMA scores (newest last)."""
        return list(self._score_history)

    # ── Internal Maths ────────────────────────────────────────

    def _compute_ema(self, raw: float) -> float:
        """Apply exponential moving average.

        .. math::

            S_t = \\alpha \\cdot r_t + (1 - \\alpha) \\cdot S_{t-1}

        Where:
            r_t = raw momentum at time t
            S_{t-1} = previous smoothed score
            α = smoothing factor

        If no prior score exists (first call), returns the raw value directly.

        Returns:
            EMA-smoothed score clamped to [0, 100].
        """
        if self._previous_score is None:
            return max(0.0, min(100.0, raw))
        smoothed = self._alpha * raw + (1.0 - self._alpha) * self._previous_score
        return max(0.0, min(100.0, smoothed))

    def _compute_direction(self, score: float) -> str:
        """Classify direction based on difference from previous score.

        .. math::

            direction =
                \\begin{cases}
                \\text{up}   & \\text{if } score - prev > threshold \\\\
                \\text{down} & \\text{if } prev - score > threshold \\\\
                \\text{flat} & \\text{otherwise}
                \\end{cases}

        Returns "flat" when there is no prior score.
        """
        if self._previous_score is None:
            return "flat"
        diff = score - self._previous_score
        if diff > self._threshold:
            return "up"
        if diff < -self._threshold:
            return "down"
        return "flat"

    def _compute_velocity(self, score: float) -> float:
        """First derivative (rate of change).

        .. math::

            v_t = S_t - S_{t-1}

        Returns 0.0 when there is no prior score.
        """
        if self._previous_score is None:
            return 0.0
        return score - self._previous_score

    def _compute_acceleration(self, velocity: float) -> float:
        """Second derivative (rate of change of velocity).

        .. math::

            a_t = v_t - v_{t-1}

        Returns 0.0 when there is no prior velocity.
        """
        if self._previous_velocity is None:
            return 0.0
        return velocity - self._previous_velocity

    @staticmethod
    def _compute_strength(score: float) -> float:
        """Compute momentum strength as deviation from neutral (50).

        .. math::

            strength = |score - 50| \\times 2

        This maps:
            score = 50  → strength = 0   (neutral, "weak")
            score = 75  → strength = 50  (moderate)
            score = 100 → strength = 100 (extreme)
            score = 25  → strength = 50  (moderate)
            score = 0   → strength = 100 (extreme)

        Returns:
            Strength value in [0, 100].
        """
        return abs(score - 50.0) * 2.0
