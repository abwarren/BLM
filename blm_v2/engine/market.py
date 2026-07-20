"""
BLM V2 — Market State Analyzer

Analyzes the current state of the betting market and computes derived
metrics that describe market behaviour:

    - steam_movement:        Rapid, sustained line movement in one direction,
                             indicating heavy, coordinated betting.
    - reverse_line_movement: Line movement opposite to the majority of money,
                             often signalling sharp money or bookmaker protection.
    - market_efficiency:     How efficiently the market is pricing relative to
                             historical expectations.
    - fouls_line_correlation: Correlation between foul calls and line movement
                             during the current snapshot window.
    - market_momentum:       Overall directional bias and strength of the market.

Steam Movement::

    steam = moving_avg(|line_delta|) × frequency_multiplier

    Where moving_avg(|line_delta|) is the mean absolute line change over the
    last N snapshots, and frequency_multiplier is the ratio of changes to
    total observations in the window.

    High steam = fast, sustained line movement.
    Value range: [0.0, ∞), typically 0.0–10.0 points per window.

Reverse Line Movement::

    rlm = |line_delta|  if sign(line_delta) ≠ sign(public_bias), else 0

    Accumulated over the window. High RLM = line persistently moving against
    the public betting direction.

Market Efficiency::

    efficiency = 1.0 - |actual_pace - expected_pace| / expected_pace

    Or, when historical baselines are unavailable, proxied by the variance
    between line movement and scoring rate. Efficiency ∈ [0.0, 1.0].

Fouls/Line Correlation::

    corr(foul_rate, line_delta)  over the observation window.
    Uses Pearson correlation coefficient ∈ [-1.0, 1.0].

Market Momentum::

    Composite of steam_movement magnitude and direction consistency.
    Higher when the market moves decisively in one direction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ── Typed Input / Output ──────────────────────────────────────────


@dataclass(frozen=True)
class MarketInput:
    """All signals needed for market state analysis.

    Attributes:
        line_deltas:          Sequence of absolute line changes (newest last)
                              for the observation window.
        line_change_signs:    Sequence of line change signs: +1 (up/over),
                              -1 (down/under), 0 (no change). Paired by index
                              with line_deltas.
        public_bias:          Estimated public betting direction.
                              -1.0 = heavy on under, +1.0 = heavy on over.
        foul_counts:          Number of fouls per snapshot interval in the
                              observation window (newest last).
        snapshots_in_window:  Total number of observations in the current window.
        expected_pace:        Expected pace (possessions per game) from the
                              league registry. Use 0 to skip efficiency calc.
        actual_pace:          Actual pace computed from current game state.
        historical_spread:    Historical spread standard deviation for market
                              efficiency normalization.
        total_line:           Current total line value.
        previous_total_line:  Total line from previous snapshot.
    """

    line_deltas: Tuple[float, ...] = ()
    line_change_signs: Tuple[int, ...] = ()
    public_bias: float = 0.0
    foul_counts: Tuple[int, ...] = ()
    snapshots_in_window: int = 0
    expected_pace: float = 0.0
    actual_pace: float = 0.0
    historical_spread: float = 1.0
    total_line: float = 0.0
    previous_total_line: float = 0.0

    def __post_init__(self) -> None:
        if not -1.0 <= self.public_bias <= 1.0:
            raise ValueError(
                f"public_bias must be in [-1.0, 1.0], got {self.public_bias}"
            )


@dataclass(frozen=True)
class MarketOutput:
    """Full market state analysis for one snapshot.

    Attributes:
        steam_movement:        Steam movement indicator. Higher = faster,
                               more sustained line movement. Typically 0–10.
        reverse_line_movement: Accumulated reverse line movement over the
                               window. Higher = more persistent movement
                               against public betting direction.
        market_efficiency:     How well the market is pricing relative to
                               expectations. ∈ [0.0, 1.0].
        fouls_line_correlation: Pearson r between foul rate and line change.
                               ∈ [-1.0, 1.0].
        market_momentum:       Composite directional momentum of the market.
                               ∈ [-100, 100]. Positive = upward (over) bias,
                               negative = downward (under) bias.
        market_momentum_strength: Absolute magnitude of market momentum.
                               ∈ [0, 100].
        analysis_window:       Number of snapshots used in the analysis.
    """

    steam_movement: float
    reverse_line_movement: float
    market_efficiency: float
    fouls_line_correlation: float
    market_momentum: float
    market_momentum_strength: float
    analysis_window: int


# ── Engine ────────────────────────────────────────────────────────


class MarketAnalyzer:
    """Analyze market state and compute derived market behaviour metrics.

    Usage::

        analyzer = MarketAnalyzer()
        inp = MarketInput(
            line_deltas=(0.5, 0.0, 0.3, 0.7),
            line_change_signs=(1, 0, 1, 1),
            public_bias=0.6,
            foul_counts=(2, 1, 3, 2),
            snapshots_in_window=4,
            expected_pace=72.0,
            actual_pace=68.5,
        )
        out = analyzer.analyze(inp)
        # out.steam_movement         → 2.1
        # out.market_efficiency       → 0.95
    """

    def __init__(
        self,
        min_window: int = 3,
    ) -> None:
        """Initialise the market analyzer.

        Args:
            min_window: Minimum number of snapshots required in the
                observation window for reliable correlation stats.
        """
        self._min_window = max(min_window, 2)

    # ── Public API ────────────────────────────────────────────

    def analyze(self, inp: MarketInput) -> MarketOutput:
        """Compute all market state metrics from the input signals.

        Args:
            inp: Market signals for the current observation window.

        Returns:
            MarketOutput with all derived metrics.
        """
        window = max(inp.snapshots_in_window, len(inp.line_deltas))

        steam = self._compute_steam(inp, window)
        rlm = self._compute_reverse_line_movement(inp)
        efficiency = self._compute_market_efficiency(inp)
        fouls_corr = self._compute_fouls_correlation(inp)
        momentum, strength = self._compute_market_momentum(inp, steam, rlm)

        return MarketOutput(
            steam_movement=round(steam, 4),
            reverse_line_movement=round(rlm, 4),
            market_efficiency=round(efficiency, 4),
            fouls_line_correlation=round(fouls_corr, 4),
            market_momentum=round(momentum, 4),
            market_momentum_strength=round(strength, 4),
            analysis_window=window,
        )

    # ── Internal Maths ────────────────────────────────────────

    @staticmethod
    def _compute_steam(inp: MarketInput, window: int) -> float:
        """Compute steam movement indicator.

        .. math::

            steam = \\frac{\\sum |\\Delta_i|}{N} \\times \\frac{N_{changed}}{N}

        Where:
            Δ_i   = line change at snapshot i
            N     = total snapshots in window
            N_changed = snapshots where line actually moved (|Δ| > 0)

        The first term is the mean absolute line change. The second term
        (frequency multiplier) penalises windows where movement is sparse.

        A window where the line moves 1.0 point every snapshot:
            steam = 1.0 × 1.0 = 1.0

        A window where the line moves 2.0 points but only 25% of the time:
            steam = 2.0 × 0.25 = 0.5

        Returns:
            Steam movement value (typically 0.0–10.0).
        """
        deltas = inp.line_deltas
        if not deltas or window == 0:
            return 0.0

        mean_abs_change = sum(abs(d) for d in deltas) / len(deltas)
        changed_count = sum(1 for d in deltas if abs(d) > 0.001)
        frequency = changed_count / max(len(deltas), 1)

        return mean_abs_change * frequency

    @staticmethod
    def _compute_reverse_line_movement(inp: MarketInput) -> float:
        """Compute accumulated reverse line movement (RLM).

        .. math::

            RLM = \\sum_{i} |\\Delta_i| \\quad \\text{if } sign(\\Delta_i) \\neq sign(bias)

        Only movement that opposes the public betting direction is counted.
        Movement in the public direction contributes zero.

        Returns:
            Accumulated reverse line movement value.
        """
        bias = inp.public_bias
        if abs(bias) < 0.05:
            # No clear public bias — cannot determine reverse movement.
            return 0.0

        signs = inp.line_change_signs
        deltas = inp.line_deltas
        if not signs or not deltas:
            return 0.0

        rlm = 0.0
        for sign, delta in zip(signs, deltas):
            if sign != 0 and sign * bias < 0:
                rlm += abs(delta)

        return rlm

    @staticmethod
    def _compute_market_efficiency(inp: MarketInput) -> float:
        """Compute market efficiency score.

        When expected_pace is available::

            efficiency = 1.0 - |actual_pace - expected_pace| / expected_pace

        When pace data is unavailable, use line stability as a proxy::

            efficiency = 1.0 - mean_line_variance / max_variance

        Efficiency is clamped to [0.0, 1.0].

        A perfectly efficient market scores 1.0 (line perfectly reflects
        game state). Lower values indicate the market may be mispricing.
        """
        if inp.expected_pace > 0 and inp.actual_pace > 0:
            # Pace-based efficiency.
            ratio = abs(inp.actual_pace - inp.expected_pace) / inp.expected_pace
            eff = max(0.0, 1.0 - ratio)
        else:
            # Line-stability proxy for efficiency.
            deltas = inp.line_deltas
            if not deltas:
                return 0.5  # neutral when no data

            # Variance of line changes as a proxy.
            mean_delta = sum(abs(d) for d in deltas) / len(deltas)
            variance = sum((abs(d) - mean_delta) ** 2 for d in deltas) / len(deltas)

            # Normalise by historical spread. Higher variance = less efficient.
            hs = inp.historical_spread if inp.historical_spread > 0 else 1.0
            eff = max(0.0, 1.0 - math.sqrt(variance) / (hs + 1.0))

        return max(0.0, min(1.0, eff))

    @staticmethod
    def _compute_fouls_correlation(inp: MarketInput) -> float:
        """Compute Pearson correlation between foul count and line change.

        .. math::

            r = \\frac{n\\sum xy - \\sum x \\sum y}
                     {\\sqrt{(n\\sum x^2 - (\\sum x)^2)(n\\sum y^2 - (\\sum y)^2)}}

        Where:
            x = foul_count per interval
            y = |line_delta| per interval

        A positive correlation means fouls and line movement move together
        (more fouls → bigger line moves). A negative correlation means fouls
        and line movement are inversely related.

        Returns:
            Pearson r ∈ [-1.0, 1.0]. Returns 0.0 if fewer than 3 data points
            or if all values are identical (division by zero).
        """
        fouls = list(inp.foul_counts)
        deltas = [abs(d) for d in inp.line_deltas]

        n = min(len(fouls), len(deltas))
        if n < 3:
            return 0.0

        fouls = fouls[:n]
        deltas = deltas[:n]

        # Pearson correlation.
        sum_x = sum(fouls)
        sum_y = sum(deltas)
        sum_xy = sum(x * y for x, y in zip(fouls, deltas))
        sum_x2 = sum(x * x for x in fouls)
        sum_y2 = sum(y * y for y in deltas)

        numerator = n * sum_xy - sum_x * sum_y
        denom_x = n * sum_x2 - sum_x * sum_x
        denom_y = n * sum_y2 - sum_y * sum_y

        if denom_x <= 0 or denom_y <= 0:
            return 0.0

        denom = math.sqrt(denom_x * denom_y)
        if denom == 0.0:
            return 0.0

        r = numerator / denom
        return max(-1.0, min(1.0, r))

    def _compute_market_momentum(
        self,
        inp: MarketInput,
        steam: float,
        rlm: float,
    ) -> Tuple[float, float]:
        """Compute composite market momentum and its strength.

        Market momentum combines steam movement with directional bias::

            direction_sign = +1 if most recent moves are upward
                             -1 if most recent moves are downward
                              0 if no clear direction

            momentum = direction_sign × steam × (1 + rlm_factor)

            strength = |momentum|

        Where rlm_factor amplifies momentum when reverse movement is detected
        (because reverse movement often signals stronger directional conviction).

        Returns:
            Tuple of (market_momentum ∈ [-100, 100],
                      market_momentum_strength ∈ [0, 100]).
        """
        signs = inp.line_change_signs
        if not signs:
            return 0.0, 0.0

        # Determine directional bias from recent signs.
        recent_signs = signs[-5:] if len(signs) > 5 else signs
        up = sum(1 for s in recent_signs if s > 0)
        down = sum(1 for s in recent_signs if s < 0)

        if up > down:
            dir_sign = 1.0
        elif down > up:
            dir_sign = -1.0
        else:
            dir_sign = 0.0

        # RLM amplifies momentum when the market moves against public.
        rlm_factor = min(1.0, rlm / 3.0) if rlm > 0 else 0.0

        raw = dir_sign * steam * (1.0 + rlm_factor * 0.5)
        momentum = max(-100.0, min(100.0, raw * 10.0))
        strength = abs(momentum)

        return momentum, strength
