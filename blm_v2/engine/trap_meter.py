"""
BLM V2 — Trap Meter Engine

Detects bookmaker "trap" market patterns — situations where the live market
exhibits characteristics consistent with engineered price movements designed
to mislead bettors.

The trap meter models 7 distinct trap types:

    1. BULL_TRAP        — Line moves toward public betting, then reverses
    2. BEAR_TRAP        — Line moves against sharp money
    3. REVERSE_BULL     — Public gets the line they want but sharps win
    4. DEAD_MARKET      — No line movement despite significant action
    5. FALSE_MOMENTUM   — Momentum that appears directional but isn't sustainable
    6. LATE_TRAP        — Movement just before line lock
    7. SHARP_TRAP       — Sharp money triggers market reversal

Trap Meter Composite::

    T = Σ(s_i × c_i) + bonus(aligned_count)

    where:
        s_i  = sensitivity weight for trap type i
        c_i  = confidence of trap type i  ∈ [0.0, 1.0]
        bonus(n) = (n - 1) × alignment_bonus  if n ≥ 2, else 0

        T is clamped to [0, 100]

The bonus term creates a superlinear spike when multiple traps fire
simultaneously, modelling the intuition that aligned trap signals are
stronger evidence than any single trap type alone.

Each trap type produces:
    - detected:   bool — whether the trap pattern is currently active
    - confidence: float 0.0–1.0 — how strongly the pattern matches
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


# ── Trap Type Enum ────────────────────────────────────────────────


class TrapType(str, Enum):
    """Enumerated trap types with descriptive names."""

    BULL_TRAP = "bull_trap"
    BEAR_TRAP = "bear_trap"
    REVERSE_BULL_TRAP = "reverse_bull_trap"
    DEAD_MARKET = "dead_market"
    FALSE_MOMENTUM = "false_momentum"
    LATE_TRAP = "late_trap"
    SHARP_TRAP = "sharp_trap"


# ── Individual Trap Result ────────────────────────────────────────


@dataclass(frozen=True)
class TrapSignal:
    """Detection result for a single trap type.

    Attributes:
        trap_type:      Which trap was evaluated.
        detected:       Whether the pattern is currently active.
        confidence:     Strength of the detection signal ∈ [0.0, 1.0].
        description:    Human-readable explanation of why.
    """

    trap_type: TrapType
    detected: bool
    confidence: float
    description: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"Trap confidence must be in [0.0, 1.0], got {self.confidence}"
            )


# ── Trap Meter Input ──────────────────────────────────────────────


@dataclass(frozen=True)
class TrapMeterInput:
    """Market and momentum signals needed for trap detection.

    Attributes:
        line_movement_history: Sequence of (line_value, public_bias_direction)
            tuples, newest last. public_bias_direction is -1 (under),
            +1 (over), 0 (unknown).
        public_betting_bias: Estimated direction of public money flow.
            -1.0 = heavy public on UNDER, +1.0 = heavy on OVER, 0 = neutral.
        sharp_money_indicator: Estimated direction of sharp money flow.
            -1.0 = sharps on UNDER, +1.0 = sharps on OVER, 0 = no signal.
        action_volume: Normalised betting action intensity ∈ [0.0, 1.0].
        momentum_velocity: Current momentum velocity (rate of change).
        momentum_acceleration: Current momentum acceleration (2nd derivative).
        score_change_rate: Points per minute on the court.
        time_to_lock: Seconds until the line locks (pre-match only).
            Use 0 for in-play markets (late trap not applicable).
        line_change_magnitude: Absolute total line change over the last N
            snapshots.
    """

    line_movement_history: Tuple[Tuple[float, int], ...] = ()  # ((line, dir), ...)
    public_betting_bias: float = 0.0
    sharp_money_indicator: float = 0.0
    action_volume: float = 0.0
    momentum_velocity: float = 0.0
    momentum_acceleration: float = 0.0
    score_change_rate: float = 0.0
    time_to_lock: float = float("inf")
    line_change_magnitude: float = 0.0

    def __post_init__(self) -> None:
        for name in [
            "public_betting_bias",
            "sharp_money_indicator",
            "action_volume",
        ]:
            val = getattr(self, name)
            if not -1.0 <= val <= 1.0:
                raise ValueError(
                    f"{name} must be in [-1.0, 1.0], got {val}"
                )
        if not 0.0 <= self.action_volume <= 1.0:
            raise ValueError(
                f"action_volume must be in [0.0, 1.0], got {self.action_volume}"
            )


# ── Trap Meter Output ─────────────────────────────────────────────


@dataclass(frozen=True)
class TrapOutput:
    """Complete trap detection result for one snapshot.

    Attributes:
        trap_meter:      Composite trap meter score ∈ [0, 100].
        signals:         Detection result for each of the 7 trap types.
        aligned_signals: Number of traps simultaneously detected (confidence
                         > 0.5).
        trap_meter_level: Qualitative label: "low" | "elevated" | "high" |
                          "extreme".
    """

    trap_meter: float
    signals: Dict[str, TrapSignal]  # keyed by TrapType.value
    aligned_signals: int
    trap_meter_level: str


# ── Thresholds & Weights ──────────────────────────────────────────

# Sensitivity weights for each trap type in composite calculation.
DEFAULT_TRAP_WEIGHTS: Dict[TrapType, float] = {
    TrapType.BULL_TRAP: 1.0,
    TrapType.BEAR_TRAP: 1.0,
    TrapType.REVERSE_BULL_TRAP: 0.9,
    TrapType.DEAD_MARKET: 0.7,
    TrapType.FALSE_MOMENTUM: 0.8,
    TrapType.LATE_TRAP: 0.6,
    TrapType.SHARP_TRAP: 1.2,  # highest weight — sharp money is most reliable signal
}

# Additional confidence multiplier per aligned trap beyond the first.
ALIGNMENT_BONUS = 5.0  # points added per extra aligned trap

# Minimum lookback for movement analysis.
MIN_MOVEMENT_SAMPLES = 3


# ── Engine ────────────────────────────────────────────────────────


class TrapMeterEngine:
    """Detect bookmaker trap patterns from market and momentum signals.

    Usage::

        engine = TrapMeterEngine()
        inp = TrapMeterInput(
            line_movement_history=((220.5, 1), (221.0, 1), (220.0, -1)),
            public_betting_bias=0.7,
            ...
        )
        out = engine.analyze(inp)
        # out.trap_meter        → 34.2
        # out.signals["bull_trap"].detected → True
    """

    def __init__(
        self,
        trap_weights: Optional[Dict[TrapType, float]] = None,
        alignment_bonus: float = ALIGNMENT_BONUS,
    ) -> None:
        """Initialise trap meter engine.

        Args:
            trap_weights: Per-trap sensitivity weights. Falls back to
                DEFAULT_TRAP_WEIGHTS.
            alignment_bonus: Points added per aligned trap beyond the first
                in the composite calculation.
        """
        self._weights = dict(DEFAULT_TRAP_WEIGHTS)
        if trap_weights is not None:
            self._weights.update(trap_weights)
        self._bonus = max(alignment_bonus, 0.0)

    # ── Public API ────────────────────────────────────────────

    def analyze(self, inp: TrapMeterInput) -> TrapOutput:
        """Run all 7 trap detectors and compute the composite trap meter.

        Args:
            inp: Market and momentum signals.

        Returns:
            TrapOutput with composite score and per-trap breakdown.
        """
        signals: Dict[str, TrapSignal] = {}

        # Run each detector.
        signals[TrapType.BULL_TRAP.value] = self._detect_bull_trap(inp)
        signals[TrapType.BEAR_TRAP.value] = self._detect_bear_trap(inp)
        signals[TrapType.REVERSE_BULL_TRAP.value] = self._detect_reverse_bull(inp)
        signals[TrapType.DEAD_MARKET.value] = self._detect_dead_market(inp)
        signals[TrapType.FALSE_MOMENTUM.value] = self._detect_false_momentum(inp)
        signals[TrapType.LATE_TRAP.value] = self._detect_late_trap(inp)
        signals[TrapType.SHARP_TRAP.value] = self._detect_sharp_trap(inp)

        # Count how many signals have confidence > 0.5 (strongly aligned).
        aligned = sum(
            1 for s in signals.values() if s.detected and s.confidence > 0.5
        )

        # Compute composite trap meter.
        meter = self._compute_composite(signals, aligned)
        level = self._classify_level(meter)

        return TrapOutput(
            trap_meter=round(meter, 2),
            signals=signals,
            aligned_signals=aligned,
            trap_meter_level=level,
        )

    # ── Trap Detectors ────────────────────────────────────────

    @staticmethod
    def _detect_bull_trap(inp: TrapMeterInput) -> TrapSignal:
        """Bull trap detection.

        A bull trap occurs when the line moves toward the public betting
        direction for several consecutive snapshots, then sharply reverses.

        Detection logic:
            1. Look at the last N movement entries in line_movement_history.
            2. Check if the earlier entries move in the public_betting_bias
               direction (line_dir * public_bias > 0).
            3. Check if the most recent entry reverses (line_dir * public_bias < 0).
            4. Confidence increases with:
               - More consecutive public-aligned movements before reversal
               - Larger public_betting_bias magnitude
               - Larger reversal magnitude
        """
        history = inp.line_movement_history
        if len(history) < MIN_MOVEMENT_SAMPLES:
            return TrapSignal(
                trap_type=TrapType.BULL_TRAP,
                detected=False,
                confidence=0.0,
                description="Insufficient movement history",
            )

        # Use the last 5 entries max.
        recent = history[-5:]
        bias = inp.public_betting_bias

        if abs(bias) < 0.1:
            return TrapSignal(
                trap_type=TrapType.BULL_TRAP,
                detected=False,
                confidence=0.0,
                description="No clear public bias",
            )

        # Count consecutive moves aligned with public before last entry.
        aligned_count = 0
        for line_val, line_dir in recent[:-1]:
            if line_dir * bias > 0:  # line moves WITH public
                aligned_count += 1

        # Check if most recent move reverses (against public).
        last_dir = recent[-1][1]
        is_reversal = last_dir * bias < 0

        if aligned_count >= 2 and is_reversal:
            # Confidence: how many aligned steps × bias magnitude.
            conf = min(1.0, (aligned_count / 4.0) + abs(bias) * 0.3)
            return TrapSignal(
                trap_type=TrapType.BULL_TRAP,
                detected=True,
                confidence=round(conf, 4),
                description=(
                    f"Line moved with public for {aligned_count} steps, "
                    f"then reversed. Public bias: {bias:+.2f}"
                ),
            )

        return TrapSignal(
            trap_type=TrapType.BULL_TRAP,
            detected=False,
            confidence=0.0,
            description="No reversal pattern detected",
        )

    @staticmethod
    def _detect_bear_trap(inp: TrapMeterInput) -> TrapSignal:
        """Bear trap detection.

        A bear trap occurs when the line moves against identified sharp
        money flow. Sharps are typically on the correct side, so line
        movement opposite to sharp money suggests the bookmaker may be
        engineering the price away from sharp capital.

        Detection logic:
            1. Compare line_movement_history direction vs sharp_money_indicator.
            2. If the line persistently moves opposite to sharp money, alarm.
            3. Confidence increases with sharp_money_indicator magnitude and
               persistence of the counter-move.
        """
        history = inp.line_movement_history
        sharp = inp.sharp_money_indicator

        if abs(sharp) < 0.2 or len(history) < 2:
            return TrapSignal(
                trap_type=TrapType.BEAR_TRAP,
                detected=False,
                confidence=0.0,
                description="Insufficient sharp signal or history",
            )

        # Count how many recent moves go AGAINST sharp direction.
        recent = history[-4:]
        counter_count = sum(
            1 for _, line_dir in recent if line_dir * sharp < 0
        )

        if counter_count >= 2:
            conf_ratio = counter_count / max(len(recent), 1)
            conf = min(1.0, conf_ratio * 0.6 + abs(sharp) * 0.4)
            return TrapSignal(
                trap_type=TrapType.BEAR_TRAP,
                detected=True,
                confidence=round(conf, 4),
                description=(
                    f"Line moved against sharp money in {counter_count}/"
                    f"{len(recent)} steps. Sharp indicator: {sharp:+.2f}"
                ),
            )

        return TrapSignal(
            trap_type=TrapType.BEAR_TRAP,
            detected=False,
            confidence=0.0,
            description="No sharp counter-movement detected",
        )

    @staticmethod
    def _detect_reverse_bull(inp: TrapMeterInput) -> TrapSignal:
        """Reverse bull trap detection.

        A reverse bull trap occurs when public money pushes the line to a
        level that looks favourable for the public, but sharp money is on
        the opposite side — the public "gets what they want" but sharps
        have the better position.

        Detection logic:
            1. Public_betting_bias and line movement agree (public is getting
               its way).
            2. Sharp_money_indicator disagrees (sharps are on the other side).
            3. The line has moved significantly in the public direction.
        """
        bias = inp.public_betting_bias
        sharp = inp.sharp_money_indicator
        history = inp.line_movement_history

        if abs(bias) < 0.15 or abs(sharp) < 0.2:
            return TrapSignal(
                trap_type=TrapType.REVERSE_BULL_TRAP,
                detected=False,
                confidence=0.0,
                description="Insufficient bias or sharp signal",
            )

        # Public and sharps disagree: bias * sharp < 0
        if bias * sharp >= 0:
            return TrapSignal(
                trap_type=TrapType.REVERSE_BULL_TRAP,
                detected=False,
                confidence=0.0,
                description="Public and sharps agree — no reverse bull",
            )

        # Check if line moved in public's direction.
        if len(history) >= 2:
            recent = history[-3:]
            moves_with_public = sum(
                1 for _, d in recent if d * bias > 0
            )
            if moves_with_public >= 2:
                conf = min(1.0, abs(bias) * 0.4 + abs(sharp) * 0.4 + 0.2)
                return TrapSignal(
                    trap_type=TrapType.REVERSE_BULL_TRAP,
                    detected=True,
                    confidence=round(conf, 4),
                    description=(
                        f"Line moved toward public ({bias:+.2f}) but sharps "
                        f"oppose ({sharp:+.2f}). Public gets line, sharps get edge."
                    ),
                )

        return TrapSignal(
            trap_type=TrapType.REVERSE_BULL_TRAP,
            detected=False,
            confidence=0.0,
            description="No reverse bull pattern",
        )

    @staticmethod
    def _detect_dead_market(inp: TrapMeterInput) -> TrapSignal:
        """Dead market detection.

        A dead market occurs when there is significant betting action
        (action_volume) but the line does not move. In efficient markets,
        action should move the line. Absence of movement despite action
        suggests the bookmaker may be absorbing unbalanced action without
        adjusting price — a potential trap.

        Detection logic:
            1. action_volume > threshold (0.4).
            2. line_change_magnitude < threshold (0.5 points).
            3. Confidence scales with action_volume and inversely with
               line change.
        """
        if inp.action_volume < 0.4:
            return TrapSignal(
                trap_type=TrapType.DEAD_MARKET,
                detected=False,
                confidence=0.0,
                description="Action volume too low",
            )

        if inp.line_change_magnitude > 0.5:
            return TrapSignal(
                trap_type=TrapType.DEAD_MARKET,
                detected=False,
                confidence=0.0,
                description="Line is moving — market not dead",
            )

        # Higher action with less line movement = stronger dead market signal.
        line_ratio = max(0.0, 1.0 - inp.line_change_magnitude / 0.5)
        conf = min(1.0, inp.action_volume * 0.6 + line_ratio * 0.4)
        return TrapSignal(
            trap_type=TrapType.DEAD_MARKET,
            detected=True,
            confidence=round(conf, 4),
            description=(
                f"Action volume {inp.action_volume:.2f} with minimal line "
                f"change ({inp.line_change_magnitude:.2f})"
            ),
        )

    @staticmethod
    def _detect_false_momentum(inp: TrapMeterInput) -> TrapSignal:
        """False momentum detection.

        False momentum looks directional initially but lacks sustainability.
        The momentum velocity is high but acceleration is negative or the
        velocity spike is an isolated event rather than part of a trend.

        Detection logic:
            1. momentum_velocity is positive (score rising).
            2. momentum_acceleration is negative (decelerating).
            3. Or: velocity is very high but had near-zero acceleration on
               the prior step (spike without follow-through).
            4. Confidence based on how sharp the deceleration is.
        """
        vel = inp.momentum_velocity
        acc = inp.momentum_acceleration

        # Need observable velocity
        if abs(vel) < 0.5:
            return TrapSignal(
                trap_type=TrapType.FALSE_MOMENTUM,
                detected=False,
                confidence=0.0,
                description="Momentum velocity too low",
            )

        # False momentum: velocity is positive but decelerating.
        if vel > 0 and acc < -0.3:
            conf = min(1.0, abs(acc) / 2.0 + 0.3)
            return TrapSignal(
                trap_type=TrapType.FALSE_MOMENTUM,
                detected=True,
                confidence=round(conf, 4),
                description=(
                    f"Velocity {vel:.1f} but decelerating ({acc:.1f}) — "
                    f"momentum not sustainable"
                ),
            )

        # Also detect a velocity spike that came from nowhere (high vel,
        # near-zero prior acceleration isn't available here without state).
        # Fallback: if acc is near zero despite high velocity (plateau).
        if abs(acc) < 0.1 and abs(vel) > 3.0:
            return TrapSignal(
                trap_type=TrapType.FALSE_MOMENTUM,
                detected=True,
                confidence=0.5,
                description=(
                    f"Velocity {vel:.1f} with no acceleration — momentum "
                    f"may be artificial"
                ),
            )

        return TrapSignal(
            trap_type=TrapType.FALSE_MOMENTUM,
            detected=False,
            confidence=0.0,
            description="No false momentum pattern",
        )

    @staticmethod
    def _detect_late_trap(inp: TrapMeterInput) -> TrapSignal:
        """Late trap detection.

        A late trap is significant line movement just before the line locks
        (pre-match). This is suspicious because late movement may target
        bettors who are rushing to get bets in before lock.

        Detection logic:
            1. time_to_lock is small (< 60 seconds) and finite.
            2. line_change_magnitude is significant (> 0.3).
            3. Confidence increases with proximity to lock and movement size.
        """
        ttl = inp.time_to_lock
        lcm = inp.line_change_magnitude

        if math.isinf(ttl) or ttl <= 0:
            return TrapSignal(
                trap_type=TrapType.LATE_TRAP,
                detected=False,
                confidence=0.0,
                description="In-play market — no line lock",
            )

        if ttl > 60.0 or lcm < 0.3:
            return TrapSignal(
                trap_type=TrapType.LATE_TRAP,
                detected=False,
                confidence=0.0,
                description="Not close enough to lock or movement too small",
            )

        # Closer to lock + bigger movement = stronger signal.
        time_factor = max(0.0, 1.0 - ttl / 60.0)
        move_factor = min(1.0, lcm / 2.0)
        conf = min(1.0, time_factor * 0.5 + move_factor * 0.5)
        return TrapSignal(
            trap_type=TrapType.LATE_TRAP,
            detected=True,
            confidence=round(conf, 4),
            description=(
                f"Line moved {lcm:.2f} points {ttl:.0f}s before lock"
            ),
        )

    @staticmethod
    def _detect_sharp_trap(inp: TrapMeterInput) -> TrapSignal:
        """Sharp trap detection.

        A sharp trap occurs when sharp money enters the market and the line
        subsequently reverses. This models the "smart money" triggering a
        market correction that the bookmaker may have engineered.

        Detection logic:
            1. sharp_money_indicator is non-zero and significant.
            2. The most recent line movement opposes the prior sharp direction.
            3. Confidence scales with sharp magnitude and reversal decisiveness.
        """
        sharp = inp.sharp_money_indicator
        history = inp.line_movement_history

        if abs(sharp) < 0.3 or len(history) < MIN_MOVEMENT_SAMPLES:
            return TrapSignal(
                trap_type=TrapType.SHARP_TRAP,
                detected=False,
                confidence=0.0,
                description="Insufficient sharp money signal or history",
            )

        # Check if recent move(s) reversed relative to sharp direction.
        recent = history[-3:]
        moves_after_sharp = sum(
            1 for _, d in recent if d * sharp < 0
        )

        if moves_after_sharp >= 2:
            conf = min(1.0, abs(sharp) * 0.5 + (moves_after_sharp / 3.0) * 0.5)
            return TrapSignal(
                trap_type=TrapType.SHARP_TRAP,
                detected=True,
                confidence=round(conf, 4),
                description=(
                    f"Sharp money ({sharp:+.2f}) followed by "
                    f"{moves_after_sharp} counter-moves"
                ),
            )

        return TrapSignal(
            trap_type=TrapType.SHARP_TRAP,
            detected=False,
            confidence=0.0,
            description="No sharp reversal pattern",
        )

    # ── Composite Calculation ─────────────────────────────────

    def _compute_composite(
        self,
        signals: Dict[str, TrapSignal],
        aligned: int,
    ) -> float:
        """Compute the weighted composite trap meter score.

        .. math::

            T = \\sum_{i} w_i \\times c_i + bonus(aligned - 1)

        Where:
            w_i = weight for trap type i
            c_i = confidence for trap type i
            bonus(x) = x × alignment_bonus  if x > 0, else 0

        The unweighted base is the sum of all signal confidences weighted
        by their sensitivity, then normalised to a 0–100 scale by dividing
        by the sum of weights.

        Alignment bonus: if multiple traps fire simultaneously (aligned ≥ 2),
        additional points are added superlinearly to model the insight that
        aligned trap signals are stronger than their individual components.

        Returns:
            Composite trap meter clamped to [0, 100].
        """
        numerator = 0.0
        total_weight = 0.0

        for trap_type, weight in self._weights.items():
            signal = signals.get(trap_type.value)
            if signal is not None:
                numerator += weight * signal.confidence
            total_weight += weight

        if total_weight == 0.0:
            return 0.0

        base = (numerator / total_weight) * 100.0

        # Alignment bonus: if 2+ traps are aligned, add bonus per extra trap.
        bonus = max(0, aligned - 1) * self._bonus

        raw = base + bonus
        return max(0.0, min(100.0, raw))

    @staticmethod
    def _classify_level(meter: float) -> str:
        """Classify trap meter level.

        Ranges:
            [0, 25)    → "low"
            [25, 50)   → "elevated"
            [50, 75)   → "high"
            [75, 100]  → "extreme"
        """
        if meter < 25:
            return "low"
        if meter < 50:
            return "elevated"
        if meter < 75:
            return "high"
        return "extreme"
