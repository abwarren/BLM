"""Tests for BLM V2 Engine — BLMEngine, ConfidenceEngine, MomentumEngine, TrapMeterEngine, MarketAnalyzer.

Uses real engine instances with known inputs for deterministic assertions.
"""

from __future__ import annotations

import pytest

from blm_v2.engine.blm_engine import BLMConfig, BLMEngine, BLMResult
from blm_v2.engine.confidence import ConfidenceEngine, ConfidenceInput, ConfidenceOutput
from blm_v2.engine.market import MarketAnalyzer, MarketInput, MarketOutput
from blm_v2.engine.momentum import MomentumEngine, MomentumInput, MomentumOutput
from blm_v2.engine.trap_meter import (
    TrapMeterEngine,
    TrapMeterInput,
    TrapOutput,
    TrapType,
)


# ═════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════


@pytest.fixture
def confidence_engine():
    return ConfidenceEngine()


@pytest.fixture
def momentum_engine():
    return MomentumEngine(alpha=0.35, direction_threshold=1.5)


@pytest.fixture
def trap_engine():
    return TrapMeterEngine()


@pytest.fixture
def market_analyzer():
    return MarketAnalyzer(min_window=3)


@pytest.fixture
def blm_engine():
    return BLMEngine()


# ═════════════════════════════════════════════════════════════════════
# Confidence Engine
# ═════════════════════════════════════════════════════════════════════


def test_confidence_calculation(confidence_engine):
    """ConfidenceEngine computes weighted composite correctly."""
    inp = ConfidenceInput(pace=0.9, line=0.8, injury=0.7, blowout=0.6, team_total=0.5)
    out = confidence_engine.calculate(inp)

    # Expected: (0.25*0.9 + 0.25*0.8 + 0.15*0.7 + 0.15*0.6 + 0.20*0.5) / 1.0
    # = (0.225 + 0.2 + 0.105 + 0.09 + 0.1) = 0.72
    assert isinstance(out, ConfidenceOutput)
    assert out.composite_confidence == pytest.approx(0.72, abs=0.01)
    assert out.raw_input == inp


def test_confidence_first_call_no_drift(confidence_engine):
    """First calculate() call has zero drift."""
    inp = ConfidenceInput()
    out = confidence_engine.calculate(inp)
    assert out.drift.current_drift == 0.0
    assert out.drift.samples == 0


def test_confidence_drift_on_second_call(confidence_engine):
    """Second calculate() call captures drift from the first."""
    c1 = confidence_engine.calculate(ConfidenceInput(pace=0.5))
    c2 = confidence_engine.calculate(ConfidenceInput(pace=0.9))

    assert c2.drift.current_drift > 0.0
    assert c2.drift.samples >= 1


def test_confidence_reset(confidence_engine):
    """reset() clears internal state."""
    confidence_engine.calculate(ConfidenceInput(pace=0.8))
    confidence_engine.reset()
    out = confidence_engine.calculate(ConfidenceInput(pace=0.5))
    assert out.drift.samples == 0


def test_confidence_invalid_input():
    """ConfidenceInput validates bounds."""
    with pytest.raises(ValueError):
        ConfidenceInput(pace=1.5)  # > 1.0


# ═════════════════════════════════════════════════════════════════════
# Momentum Engine
# ═════════════════════════════════════════════════════════════════════


def test_momentum_calculation(momentum_engine):
    """MomentumEngine returns first value directly (no EMA on first call)."""
    inp = MomentumInput(raw_momentum=72.0)
    out = momentum_engine.calculate(inp)

    assert out.momentum_score == 72.0
    assert out.momentum_velocity == 0.0
    assert out.momentum_acceleration == 0.0
    assert out.momentum_direction == "flat"


def test_momentum_ema_second_call(momentum_engine):
    """Second call applies EMA smoothing."""
    momentum_engine.calculate(MomentumInput(raw_momentum=50.0))
    out = momentum_engine.calculate(MomentumInput(raw_momentum=80.0))

    # S2 = 0.35 * 80 + 0.65 * 50 = 28 + 32.5 = 60.5
    assert out.momentum_score == pytest.approx(60.5, abs=0.01)
    assert out.momentum_velocity == pytest.approx(10.5, abs=0.01)


def test_momentum_direction_up(momentum_engine):
    """Direction is 'up' when score rises above threshold."""
    momentum_engine.calculate(MomentumInput(raw_momentum=50.0))
    out = momentum_engine.calculate(MomentumInput(raw_momentum=90.0))
    assert out.momentum_direction == "up"


def test_momentum_direction_down(momentum_engine):
    """Direction is 'down' when score drops below threshold."""
    momentum_engine.calculate(MomentumInput(raw_momentum=50.0))
    out = momentum_engine.calculate(MomentumInput(raw_momentum=10.0))
    assert out.momentum_direction == "down"


def test_momentum_strength(momentum_engine):
    """Strength converts score deviation to 0-100 scale."""
    out = momentum_engine.calculate(MomentumInput(raw_momentum=50.0))
    assert out.momentum_strength == 0.0  # neutral
    assert out.momentum_strength_label == "weak"

    out2 = momentum_engine.calculate(MomentumInput(raw_momentum=100.0))
    assert out2.momentum_strength > 0.0


def test_momentum_reset(momentum_engine):
    """reset() clears state so next call starts fresh."""
    momentum_engine.calculate(MomentumInput(raw_momentum=80.0))
    momentum_engine.reset()
    out = momentum_engine.calculate(MomentumInput(raw_momentum=10.0))
    assert out.momentum_score == 10.0  # no EMA, just raw


def test_momentum_invalid_input():
    """MomentumInput validates bounds."""
    with pytest.raises(ValueError):
        MomentumInput(raw_momentum=150.0)


# ═════════════════════════════════════════════════════════════════════
# Trap Meter Engine
# ═════════════════════════════════════════════════════════════════════


def test_trap_meter_detection(trap_engine):
    """TrapMeterEngine returns a TrapOutput with valid structure."""
    inp = TrapMeterInput(
        line_movement_history=((220.0, 1), (221.0, 1), (222.0, 1), (220.5, -1)),
        public_betting_bias=0.7,
        sharp_money_indicator=-0.5,
        action_volume=0.6,
        momentum_velocity=3.0,
        momentum_acceleration=-1.0,
        score_change_rate=1.5,
        time_to_lock=float("inf"),
        line_change_magnitude=0.0,
    )
    out = trap_engine.analyze(inp)
    assert isinstance(out, TrapOutput)
    assert 0.0 <= out.trap_meter <= 100.0
    assert len(out.signals) == 7
    assert TrapType.BULL_TRAP.value in out.signals


def test_trap_meter_bull_detected(trap_engine):
    """Bull trap is detected when line moves with public then reverses."""
    inp = TrapMeterInput(
        line_movement_history=((220.0, 1), (221.0, 1), (222.0, 1), (219.5, -1)),
        public_betting_bias=0.8,
    )
    out = trap_engine.analyze(inp)
    bull = out.signals[TrapType.BULL_TRAP.value]
    assert bull.detected
    assert bull.confidence > 0.0


def test_trap_meter_bear_detected(trap_engine):
    """Bear trap is detected when line moves against sharp money."""
    inp = TrapMeterInput(
        line_movement_history=((220.0, 1), (221.0, 1), (222.0, 1)),
        sharp_money_indicator=-0.8,
    )
    out = trap_engine.analyze(inp)
    bear = out.signals[TrapType.BEAR_TRAP.value]
    assert bear.detected
    assert bear.confidence > 0.0


def test_trap_meter_no_input(trap_engine):
    """Empty input returns all-clear with trap_meter=0."""
    inp = TrapMeterInput()
    out = trap_engine.analyze(inp)
    assert out.trap_meter == 0.0
    assert not any(s.detected for s in out.signals.values())


# ═════════════════════════════════════════════════════════════════════
# Market Analyzer
# ═════════════════════════════════════════════════════════════════════


def test_market_analysis(market_analyzer):
    """MarketAnalyzer returns structured output with valid metrics."""
    inp = MarketInput(
        line_deltas=(0.5, 0.0, 0.3, 0.7),
        line_change_signs=(1, 0, 1, 1),
        public_bias=0.6,
        foul_counts=(2, 1, 3, 2),
        snapshots_in_window=4,
        expected_pace=72.0,
        actual_pace=68.5,
    )
    out = market_analyzer.analyze(inp)
    assert isinstance(out, MarketOutput)
    assert out.analysis_window == 4
    assert out.market_efficiency >= 0.0
    assert out.market_efficiency <= 1.0
    assert -100.0 <= out.market_momentum <= 100.0


def test_market_steam_movement(market_analyzer):
    """Steam movement is non-zero when line moves consistently."""
    inp = MarketInput(
        line_deltas=(1.0, 1.0, 1.0),
        line_change_signs=(1, 1, 1),
        snapshots_in_window=3,
    )
    out = market_analyzer.analyze(inp)
    assert out.steam_movement > 0.0


def test_market_no_data(market_analyzer):
    """Analyzer handles empty inputs gracefully."""
    inp = MarketInput()
    out = market_analyzer.analyze(inp)
    assert out.steam_movement == 0.0
    assert out.market_momentum == 0.0
    assert out.market_efficiency == 0.5  # neutral default


# ═════════════════════════════════════════════════════════════════════
# BLM Engine Integration
# ═════════════════════════════════════════════════════════════════════


def test_blm_engine_integration(blm_engine):
    """Full BLM engine pipeline returns all sub-engine outputs."""
    result = blm_engine.process_snapshot(
        snapshot_id="test-001",
        snapshot_input={
            "total_line": 220.5,
            "previous_total_line": 220.0,
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
            "confidence_pace": 0.85,
            "confidence_line": 0.72,
            "confidence_injury": 0.5,
            "confidence_blowout": 0.9,
            "confidence_team_total": 0.78,
        },
    )

    assert isinstance(result, BLMResult)
    assert result.snapshot_id == "test-001"
    assert result.confidence.composite_confidence > 0.0
    assert result.momentum.momentum_score >= 0.0
    assert result.trap_meter.trap_meter >= 0.0
    assert result.market.analysis_window > 0
    assert "composite_confidence" in result.enriched_fields


def test_blm_engine_multi_snapshot(blm_engine):
    """Processing multiple snapshots produces drift and velocity changes."""
    base_input = {
        "total_line": 220.0,
        "previous_total_line": 220.0,
        "home_score": 50,
        "away_score": 50,
        "quarter": 2,
        "clock_seconds": 600,
        "score_change_rate": 1.0,
        "foul_count_this_interval": 0,
        "public_betting_bias": 0.0,
        "sharp_money_indicator": 0.0,
        "action_volume": 0.5,
        "expected_pace": 72.0,
        "actual_pace": 72.0,
        "historical_spread": 1.0,
        "line_movement_history": ((220.0, 0),),
        "confidence_pace": 0.5,
        "confidence_line": 0.5,
        "confidence_injury": 0.5,
        "confidence_blowout": 0.5,
        "confidence_team_total": 0.5,
    }

    r1 = blm_engine.process_snapshot("test-001", {**base_input, "time_to_lock": 100.0})
    r2 = blm_engine.process_snapshot(
        "test-002",
        {
            **base_input,
            "time_to_lock": 50.0,
            "score_change_rate": 4.0,
            "total_line": 221.0,
            "previous_total_line": 220.0,
            "line_movement_history": ((220.0, 0), (221.0, 1)),
        },
    )

    # Momentum should have changed
    assert r2.momentum.momentum_score != r1.momentum.momentum_score
    assert r2.momentum.momentum_velocity != 0.0 or r2.momentum.momentum_direction != "flat"


def test_blm_engine_reset(blm_engine):
    """reset() clears all internal state."""
    blm_engine.process_snapshot("test-001", {
        "total_line": 220.0,
        "previous_total_line": 219.5,
        "home_score": 50,
        "away_score": 50,
        "score_change_rate": 1.0,
        "foul_count_this_interval": 0,
        "public_betting_bias": 0.0,
        "sharp_money_indicator": 0.0,
        "action_volume": 0.5,
        "expected_pace": 72.0,
        "actual_pace": 72.0,
        "line_movement_history": ((220.0, 0),),
        "confidence_pace": 0.5,
        "confidence_line": 0.5,
        "confidence_injury": 0.5,
        "confidence_blowout": 0.5,
        "confidence_team_total": 0.5,
    })
    blm_engine.reset()

    # After reset, a fresh snapshot should have zero confidence drift
    r = blm_engine.process_snapshot("test-002", {
        "total_line": 220.0,
        "previous_total_line": 219.5,
        "home_score": 50,
        "away_score": 50,
        "score_change_rate": 1.0,
        "foul_count_this_interval": 0,
        "public_betting_bias": 0.0,
        "sharp_money_indicator": 0.0,
        "action_volume": 0.5,
        "expected_pace": 72.0,
        "actual_pace": 72.0,
        "line_movement_history": ((220.0, 0),),
        "confidence_pace": 0.5,
        "confidence_line": 0.5,
        "confidence_injury": 0.5,
        "confidence_blowout": 0.5,
        "confidence_team_total": 0.5,
    })
    assert r.confidence.drift.samples == 0
