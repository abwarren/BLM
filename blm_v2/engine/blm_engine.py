"""
BLM V2 — Core BLM Engine Orchestrator

The BLMEngine is the top-level orchestrator that ties all sub-engines
together into a single calculation pipeline.

Pipeline::

    Snapshot ──► MarketAnalyzer ──► steam, RLM, efficiency, fouls corr
              │
              ├──► ConfidenceEngine ──► composite_confidence, drift
              │
              ├──► MomentumEngine ────► score, direction, velocity, accel
              │
              └──► TrapMeterEngine ───► trap_meter, per-trap signals

                ▼
         BLMResult (aggregated)

Each sub-engine is injected via constructor (dependency injection), making
the BLMEngine testable — you can substitute mock engines, reconfigure
weights, or bypass components.

The engine is stateful: it holds references to sub-engines that themselves
track internal state (MomentumEngine history, ConfidenceEngine drift).
Call ``reset()`` between games to clear all internal state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from blm_v2.engine.confidence import (
    ConfidenceEngine,
    ConfidenceInput,
    ConfidenceOutput,
)
from blm_v2.engine.market import (
    MarketAnalyzer,
    MarketInput,
    MarketOutput,
)
from blm_v2.engine.momentum import (
    MomentumEngine,
    MomentumInput,
    MomentumOutput,
)
from blm_v2.engine.trap_meter import (
    TrapMeterEngine,
    TrapMeterInput,
    TrapOutput,
)


# ── Configuration ─────────────────────────────────────────────────


@dataclass(frozen=True)
class BLMConfig:
    """Configuration for the BLM Engine.

    Attributes:
        confidence_weights: Per-component weights for the confidence engine.
            Dict with keys ``pace``, ``line``, ``injury``, ``blowout``,
            ``team_total``. Uses defaults if None.
        momentum_alpha: EMA smoothing factor for momentum engine ∈ (0, 1].
            Default 0.35.
        momentum_direction_threshold: Min score diff to classify direction.
            Default 1.5.
        trap_weights: Per-trap sensitivity weights. Uses defaults if None.
        trap_alignment_bonus: Extra points per aligned trap beyond the first.
            Default 5.0.
        min_market_window: Minimum snapshot window for market analysis.
            Default 3.
    """

    confidence_weights: Optional[Dict[str, float]] = None
    momentum_alpha: float = 0.35
    momentum_direction_threshold: float = 1.5
    trap_weights: Optional[Dict[str, float]] = None
    trap_alignment_bonus: float = 5.0
    min_market_window: int = 3


# ── Engine Result ─────────────────────────────────────────────────


@dataclass(frozen=True)
class BLMResult:
    """Complete BLM analysis result for one snapshot.

    Aggregates outputs from all sub-engines into a single result object
    that can be serialised or merged into a BlmSnapshot model.

    Attributes:
        snapshot_id:        Unique identifier for the source snapshot.
        confidence:         Confidence engine output.
        momentum:           Momentum engine output.
        trap_meter:         Trap meter engine output.
        market:             Market analyzer output.
        enriched_fields:    Flat dict of key BLM fields suitable for
                            merging into a snapshot or API response.
    """

    snapshot_id: str
    confidence: ConfidenceOutput
    momentum: MomentumOutput
    trap_meter: TrapOutput
    market: MarketOutput
    enriched_fields: Dict[str, Any] = field(default_factory=dict)


# ── Orchestrator ──────────────────────────────────────────────────


class BLMEngine:
    """Top-level BLM engine that orchestrates all sub-engine calculations.

    Usage::

        config = BLMConfig(momentum_alpha=0.4)
        engine = BLMEngine(config=config)
        result = engine.process_snapshot(
            snapshot_id="game-123-001",
            snapshot_input={
                "total_line": 220.5,
                "previous_total_line": 219.5,
                "home_score": 55,
                "away_score": 48,
                "quarter": 2,
                "clock_seconds": 480,
                "score_change_rate": 2.1,
                "foul_count_this_interval": 3,
                "public_betting_bias": 0.6,
                "sharp_money_indicator": -0.3,
                "action_volume": 0.7,
                "time_to_lock": float("inf"),
                "expected_pace": 72.0,
                "actual_pace": 70.2,
                "historical_spread": 1.5,
                "line_movement_history": ((220.0, 1), (220.5, 1), (219.5, -1)),
                # Confidence inputs (0.0–1.0)
                "confidence_pace": 0.85,
                "confidence_line": 0.72,
                "confidence_injury": 0.5,
                "confidence_blowout": 0.9,
                "confidence_team_total": 0.78,
            }
        )
        # result.confidence.composite_confidence  → 0.76
        # result.enriched_fields["trap_meter"]    → 34.2

    Call ``reset()`` between games to clear state from all sub-engines.
    """

    def __init__(
        self,
        config: Optional[BLMConfig] = None,
        confidence_engine: Optional[ConfidenceEngine] = None,
        momentum_engine: Optional[MomentumEngine] = None,
        trap_meter_engine: Optional[TrapMeterEngine] = None,
        market_analyzer: Optional[MarketAnalyzer] = None,
    ) -> None:
        """Initialise the BLM engine with optional custom sub-engines.

        Args:
            config: Global configuration. Overridden by explicit engine args.
            confidence_engine: Injected confidence engine (creates default if None).
            momentum_engine: Injected momentum engine (creates default if None).
            trap_meter_engine: Injected trap meter engine (creates default if None).
            market_analyzer: Injected market analyzer (creates default if None).
        """
        self._config = config or BLMConfig()

        # Dependency injection — create defaults if not provided.
        self._confidence = confidence_engine or ConfidenceEngine(
            weights=self._config.confidence_weights,
        )
        self._momentum = momentum_engine or MomentumEngine(
            alpha=self._config.momentum_alpha,
            direction_threshold=self._config.momentum_direction_threshold,
        )
        self._trap_meter = trap_meter_engine or TrapMeterEngine(
            trap_weights=self._config.trap_weights,  # type: ignore[arg-type]
            alignment_bonus=self._config.trap_alignment_bonus,
        )
        self._market = market_analyzer or MarketAnalyzer(
            min_window=self._config.min_market_window,
        )

        # Internal buffer for line movement tracking.
        self._line_movement_buffer: List[float] = []
        self._line_sign_buffer: List[int] = []
        self._foul_buffer: List[int] = []

    # ── Public API ────────────────────────────────────────────

    def process_snapshot(
        self,
        snapshot_id: str,
        snapshot_input: Dict[str, Any],
    ) -> BLMResult:
        """Run the full BLM analysis pipeline on one snapshot.

        Pipeline order:
            1. MarketAnalyzer — line movement, steam, efficiency
            2. ConfidenceEngine — composite confidence
            3. MomentumEngine — EMA score, velocity, acceleration
            4. TrapMeterEngine — trap detection

        Args:
            snapshot_id: Unique ID for this snapshot (e.g., ``game_id-001``).
            snapshot_input: Flat dict containing all raw snapshot fields
                needed by the sub-engines. See the method docstring for
                expected keys.

        Expected keys in snapshot_input:
            Market:
                - total_line (float)
                - previous_total_line (float)
                - score_change_rate (float)
                - foul_count_this_interval (int)
                - public_betting_bias (float)
                - sharp_money_indicator (float)
                - action_volume (float)
                - time_to_lock (float)
                - expected_pace (float)
                - actual_pace (float)
                - historical_spread (float)
                - line_movement_history (tuple of (float, int))
            Confidence:
                - confidence_pace (float, 0.0–1.0)
                - confidence_line (float, 0.0–1.0)
                - confidence_injury (float, 0.0–1.0)
                - confidence_blowout (float, 0.0–1.0)
                - confidence_team_total (float, 0.0–1.0)

        Returns:
            BLMResult with all sub-engine outputs.
        """
        # ── Step 0: Extract & update buffers ─────────────────
        inp = snapshot_input
        self._update_buffers(inp)

        # ── Step 1: Market Analysis ──────────────────────────
        line_deltas = tuple(self._line_movement_buffer)
        line_signs = tuple(self._line_sign_buffer)
        fouls = tuple(self._foul_buffer)

        market_in = MarketInput(
            line_deltas=line_deltas,
            line_change_signs=line_signs,
            public_bias=inp.get("public_betting_bias", 0.0),
            foul_counts=fouls,
            snapshots_in_window=len(self._line_movement_buffer),
            expected_pace=inp.get("expected_pace", 0.0),
            actual_pace=inp.get("actual_pace", 0.0),
            historical_spread=inp.get("historical_spread", 1.0),
            total_line=inp.get("total_line", 0.0),
            previous_total_line=inp.get("previous_total_line", 0.0),
        )
        market_out = self._market.analyze(market_in)

        # ── Step 2: Confidence ───────────────────────────────
        conf_in = ConfidenceInput(
            pace=inp.get("confidence_pace", 0.5),
            line=inp.get("confidence_line", 0.5),
            injury=inp.get("confidence_injury", 0.5),
            blowout=inp.get("confidence_blowout", 0.5),
            team_total=inp.get("confidence_team_total", 0.5),
        )
        conf_out = self._confidence.calculate(conf_in)

        # ── Step 3: Momentum ─────────────────────────────────
        # Derive raw momentum from score_change_rate and steam.
        raw_momentum = self._derive_raw_momentum(inp, market_out)
        momentum_in = MomentumInput(
            raw_momentum=raw_momentum,
            timestamp=inp.get("timestamp", 0.0),
        )
        momentum_out = self._momentum.calculate(momentum_in)

        # ── Step 4: Trap Meter ───────────────────────────────
        trap_in = self._build_trap_input(inp, market_out, momentum_out)
        trap_out = self._trap_meter.analyze(trap_in)

        # ── Step 5: Build enriched fields ────────────────────
        enriched = self._build_enriched_fields(
            conf_out, momentum_out, trap_out, market_out,
        )

        return BLMResult(
            snapshot_id=snapshot_id,
            confidence=conf_out,
            momentum=momentum_out,
            trap_meter=trap_out,
            market=market_out,
            enriched_fields=enriched,
        )

    def reset(self) -> None:
        """Reset all sub-engines and internal buffers.

        Call this when starting analysis of a new game to ensure no
        state leaks between games.
        """
        self._confidence.reset()
        self._momentum.reset()
        self._line_movement_buffer.clear()
        self._line_sign_buffer.clear()
        self._foul_buffer.clear()

    @property
    def config(self) -> BLMConfig:
        """Read-only access to current config."""
        return self._config

    # ── Internal Helpers ─────────────────────────────────────

    def _update_buffers(self, inp: Dict[str, Any]) -> None:
        """Update internal movement buffers from the current snapshot."""
        total_line = inp.get("total_line")
        prev_line = inp.get("previous_total_line")

        if total_line is not None and prev_line is not None and prev_line != 0:
            delta = total_line - prev_line
            self._line_movement_buffer.append(delta)
            if abs(delta) < 0.001:
                self._line_sign_buffer.append(0)
            else:
                self._line_sign_buffer.append(1 if delta > 0 else -1)

            # Keep a rolling window of 20 entries.
            if len(self._line_movement_buffer) > 20:
                self._line_movement_buffer.pop(0)
                self._line_sign_buffer.pop(0)

        fouls = inp.get("foul_count_this_interval", 0)
        self._foul_buffer.append(fouls)
        if len(self._foul_buffer) > 20:
            self._foul_buffer.pop(0)

    @staticmethod
    def _derive_raw_momentum(
        inp: Dict[str, Any],
        market_out: MarketOutput,
    ) -> float:
        """Derive a raw momentum signal in [0, 100] from available data.

        Combines three signals:
            1. score_change_rate (scaled to 0–100, weight: 0.4)
            2. market_out.steam_movement (scaled to 0–100, weight: 0.3)
            3. market_out.market_momentum_strength (weight: 0.3)

        The combined signal is clamped to [0, 100].
        """
        score_rate = inp.get("score_change_rate", 0.0)
        # Assume max realistic score rate is ~6 points/min.
        score_signal = min(100.0, (score_rate / 6.0) * 100.0)

        steam_signal = min(100.0, market_out.steam_movement * 10.0)
        mkt_str = market_out.market_momentum_strength

        combined = (
            0.4 * score_signal
            + 0.3 * steam_signal
            + 0.3 * mkt_str
        )
        return max(0.0, min(100.0, combined))

    @staticmethod
    def _build_trap_input(
        inp: Dict[str, Any],
        market_out: MarketOutput,
        momentum_out: MomentumOutput,
    ) -> TrapMeterInput:
        """Build TrapMeterInput from engine outputs and raw snapshot data."""
        hist_raw = inp.get("line_movement_history", ())
        # Ensure it's a tuple of (float, int).
        if isinstance(hist_raw, list):
            hist = tuple(
                (float(v[0]), int(v[1])) for v in hist_raw
            )
        elif isinstance(hist_raw, tuple):
            hist = hist_raw
        else:
            hist = ()

        return TrapMeterInput(
            line_movement_history=hist,
            public_betting_bias=inp.get("public_betting_bias", 0.0),
            sharp_money_indicator=inp.get("sharp_money_indicator", 0.0),
            action_volume=inp.get("action_volume", 0.0),
            momentum_velocity=momentum_out.momentum_velocity,
            momentum_acceleration=momentum_out.momentum_acceleration,
            score_change_rate=inp.get("score_change_rate", 0.0),
            time_to_lock=inp.get("time_to_lock", float("inf")),
            line_change_magnitude=market_out.steam_movement,
        )

    @staticmethod
    def _build_enriched_fields(
        conf_out: ConfidenceOutput,
        momentum_out: MomentumOutput,
        trap_out: TrapOutput,
        market_out: MarketOutput,
    ) -> Dict[str, Any]:
        """Flatten all engine outputs into a single enriched-field dict.

        This dict is designed to be merged into a BlmSnapshot or API
        response so consumers don't need to understand every sub-engine.
        """
        return {
            # Confidence
            "composite_confidence": conf_out.composite_confidence,
            "confidence_drift": conf_out.drift.current_drift,
            "confidence_drift_trend": conf_out.drift.drift_trend,
            # Momentum
            "momentum_score": momentum_out.momentum_score,
            "momentum_direction": momentum_out.momentum_direction,
            "momentum_velocity": momentum_out.momentum_velocity,
            "momentum_acceleration": momentum_out.momentum_acceleration,
            "momentum_strength": momentum_out.momentum_strength,
            "momentum_strength_label": momentum_out.momentum_strength_label,
            # Trap Meter
            "trap_meter": trap_out.trap_meter,
            "trap_meter_level": trap_out.trap_meter_level,
            "aligned_traps": trap_out.aligned_signals,
            # Trap signals (flattened)
            "bull_trap_detected": trap_out.signals.get("bull_trap", None) is not None
            and trap_out.signals["bull_trap"].detected or False,
            "bear_trap_detected": trap_out.signals.get("bear_trap", None) is not None
            and trap_out.signals["bear_trap"].detected or False,
            "reverse_bull_trap_detected": (
                trap_out.signals.get("reverse_bull_trap", None) is not None
                and trap_out.signals["reverse_bull_trap"].detected or False
            ),
            "dead_market_detected": trap_out.signals.get("dead_market", None) is not None
            and trap_out.signals["dead_market"].detected or False,
            "false_momentum_detected": trap_out.signals.get("false_momentum", None) is not None
            and trap_out.signals["false_momentum"].detected or False,
            "late_trap_detected": trap_out.signals.get("late_trap", None) is not None
            and trap_out.signals["late_trap"].detected or False,
            "sharp_trap_detected": trap_out.signals.get("sharp_trap", None) is not None
            and trap_out.signals["sharp_trap"].detected or False,
            # Market
            "steam_movement": market_out.steam_movement,
            "reverse_line_movement": market_out.reverse_line_movement,
            "market_efficiency": market_out.market_efficiency,
            "fouls_line_correlation": market_out.fouls_line_correlation,
            "market_momentum": market_out.market_momentum,
        }
