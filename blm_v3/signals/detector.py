"""
BLM V3 — Threshold-Based Signal Detection.

Pure functions that detect 15 distinct market signal types from a single
snapshot and its context.  Each detector returns a list of signal dicts
ready for insertion into the ``signals`` table.

Each signal dict has the shape::

    {
        "signal_type": str,       # one of SignalType.value
        "severity": str,          # "low" | "mid" | "high" | "critical"
        "value": float,           # the metric value that triggered
        "threshold": float,       # the threshold that was crossed
        "description": str,       # human-readable explanation
        "snapshot_id": str,       # triggering observation ID
        "game_id": str,
        "timestamp": str,
    }
"""

from __future__ import annotations

import math
from typing import Any, Optional

from blm_v3.historical.config import (
    LINE_JUMP_THRESHOLD,
    LINE_FREEZE_MIN_TICKS,
    COMPRESSION_HIGH,
    COMPRESSION_LOW,
    SHARP_MOVEMENT_THRESHOLD,
    MOMENTUM_SWING_THRESHOLD,
    INFLATION_MID,
    INFLATION_HIGH,
    TRAP_METER_FORMATION,
    TRAP_METER_ACTIVE,
    CONFIDENCE_LOW,
    PACE_COLLAPSE_THRESHOLD,
)

# ── Constants ────────────────────────────────────────────────────────

FAKE_MOVEMENT_ODDS_THRESHOLD: float = 0.02
"""Max odds delta that still qualifies as 'no odds movement'."""

OVERREACTION_FACTOR: float = 2.0
"""Line delta must exceed this factor × rolling_avg to be an overreaction."""

MARKET_CORRECTION_REVERSAL_FACTOR: float = 0.3
"""Line must reverse at least this fraction of the prior jump."""


def detect_all(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run all 15 detectors against a snapshot and its context.

    Args:
        snapshot: The current snapshot dict (must contain all fields).
        context: Pre-computed context dict with keys:
            - ``prev_snapshot``: Previous snapshot or None
            - ``rolling_line_values``: Recent line values list
            - ``consecutive_zero_deltas``: Freeze counter
            - ``prev_momentum``: Previous momentum value
            - ``momentum_velocity`` / ``momentum_acceleration``
            - ``current_game_minutes``
            - ``inflation_index`` (pre-computed)
            - ``compression_index`` (pre-computed)
            - ``trap_meter`` (pre-computed; 0 if not available)

    Returns:
        List of signal dicts.
    """
    signals: list[dict[str, Any]] = []

    detectors = [
        _detect_line_freeze,
        _detect_line_jump,
        _detect_odds_compression,
        _detect_odds_expansion,
        _detect_sharp_movement,
        _detect_fake_movement,
        _detect_trap_formation,
        _detect_bull_trap,
        _detect_bear_trap,
        _detect_market_correction,
        _detect_overreaction,
        _detect_regression,
        _detect_momentum_swing,
        _detect_pace_collapse,
        _detect_inflation_spike,
    ]

    for detector in detectors:
        try:
            sig = detector(snapshot, context)
            if sig:
                signals.append(sig)
        except Exception:
            # Detector failures are non-fatal — log them upstream
            pass

    return signals


# ── Helpers ──────────────────────────────────────────────────────────


def _make_signal(
    signal_type: str,
    severity: str,
    value: float,
    threshold: float,
    description: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Build a standard signal dict."""
    return {
        "signal_type": signal_type,
        "severity": severity,
        "value": round(value, 4),
        "threshold": threshold,
        "description": description,
        "snapshot_id": snapshot.get("id", ""),
        "game_id": snapshot.get("game_id", ""),
        "timestamp": snapshot.get("timestamp", ""),
    }


def _severity_from_ratio(value: float, threshold: float) -> str:
    """Map a value/threshold ratio to a severity level."""
    if threshold <= 0:
        return "mid"
    ratio = value / threshold
    if ratio >= 3.0:
        return "critical"
    if ratio >= 2.0:
        return "high"
    if ratio >= 1.0:
        return "mid"
    return "low"


# ── Detectors ────────────────────────────────────────────────────────


def _detect_line_freeze(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Line has not moved for N consecutive ticks."""
    freeze_count = context.get("consecutive_zero_deltas", 0)
    if freeze_count < LINE_FREEZE_MIN_TICKS:
        return None

    severity = _severity_from_ratio(freeze_count, LINE_FREEZE_MIN_TICKS)
    return _make_signal(
        "line_freeze", severity,
        float(freeze_count), float(LINE_FREEZE_MIN_TICKS),
        f"Line frozen for {freeze_count} consecutive ticks",
        snapshot,
    )


def _detect_line_jump(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Total line jumped more than threshold in one tick."""
    line_delta = abs(snapshot.get("line_delta", 0) or 0)
    if line_delta < LINE_JUMP_THRESHOLD:
        return None

    severity = _severity_from_ratio(line_delta, LINE_JUMP_THRESHOLD)
    direction = "up" if (snapshot.get("line_delta") or 0) > 0 else "down"
    return _make_signal(
        "line_jump", severity, line_delta, LINE_JUMP_THRESHOLD,
        f"Line jumped {line_delta:+.1f} points ({direction})",
        snapshot,
    )


def _detect_odds_compression(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Odds have compressed (tightened) above threshold."""
    comp = context.get("compression_index")
    if comp is None or comp < COMPRESSION_HIGH:
        return None

    return _make_signal(
        "odds_compression", "mid", comp, COMPRESSION_HIGH,
        f"Odds compressed to {comp:.2f} (tight market)",
        snapshot,
    )


def _detect_odds_expansion(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Odds have expanded (widened) below threshold."""
    comp = context.get("compression_index")
    if comp is None or comp > COMPRESSION_LOW:
        return None

    deficit = COMPRESSION_LOW - comp
    severity = _severity_from_ratio(deficit, COMPRESSION_LOW * 0.5)
    return _make_signal(
        "odds_expansion", severity, comp, COMPRESSION_LOW,
        f"Odds expanded to {comp:.2f} (wide market)",
        snapshot,
    )


def _detect_sharp_movement(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Line moved opposite to the direction pace momentum suggests."""
    line_delta = snapshot.get("line_delta", 0) or 0
    momentum = context.get("prev_momentum", 0) or 0

    if abs(line_delta) < 0.5:
        return None
    if abs(momentum) < 1.0:
        return None

    # Sharp = line moves against scoring momentum
    # If scoring is accelerating (momentum > 0) and line moves DOWN
    # OR scoring is decelerating (momentum < 0) and line moves UP
    is_sharp = (momentum > 0 and line_delta < -SHARP_MOVEMENT_THRESHOLD) or \
               (momentum < 0 and line_delta > SHARP_MOVEMENT_THRESHOLD)

    if not is_sharp:
        return None

    severity = _severity_from_ratio(abs(line_delta), SHARP_MOVEMENT_THRESHOLD)
    return _make_signal(
        "sharp_movement", severity, abs(line_delta), SHARP_MOVEMENT_THRESHOLD,
        f"Line moved {line_delta:+.1f} opposite momentum ({momentum:+.1f})",
        snapshot,
    )


def _detect_fake_movement(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Line moved but odds didn't change proportionally."""
    line_delta = abs(snapshot.get("line_delta", 0) or 0)
    odds_delta = abs(snapshot.get("odds_delta", 0) or 0)

    if line_delta < 1.0:
        return None
    if odds_delta >= FAKE_MOVEMENT_ODDS_THRESHOLD:
        return None

    severity = _severity_from_ratio(line_delta, 2.0)
    return _make_signal(
        "fake_movement", severity, line_delta, 2.0,
        f"Line moved {line_delta:+.1f} with no odds change ({odds_delta})",
        snapshot,
    )


def _detect_trap_formation(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Trap meter is elevated but below active threshold — conditions forming."""
    trap = context.get("trap_meter", 0) or 0
    if trap < TRAP_METER_FORMATION or trap >= TRAP_METER_ACTIVE:
        return None

    severity = _severity_from_ratio(trap, TRAP_METER_FORMATION)
    return _make_signal(
        "trap_formation", severity, trap, TRAP_METER_FORMATION,
        f"Trap conditions forming: meter={trap:.0f}",
        snapshot,
    )


def _detect_bull_trap(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Trap meter is high AND inflation is positive (bullish false signal)."""
    trap = context.get("trap_meter", 0) or 0
    inflation = context.get("inflation_index", 0) or 0
    if trap < TRAP_METER_ACTIVE or inflation < INFLATION_MID:
        return None

    severity = _severity_from_ratio(trap, TRAP_METER_ACTIVE)
    return _make_signal(
        "bull_trap", severity, trap, TRAP_METER_ACTIVE,
        f"Bull trap: meter={trap:.0f}, inflation={inflation:.1f}",
        snapshot,
    )


def _detect_bear_trap(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Trap meter is high AND inflation is negative (bearish false signal)."""
    trap = context.get("trap_meter", 0) or 0
    inflation = context.get("inflation_index", 0) or 0
    if trap < TRAP_METER_ACTIVE or inflation > -INFLATION_MID:
        return None

    severity = _severity_from_ratio(trap, TRAP_METER_ACTIVE)
    return _make_signal(
        "bear_trap", severity, trap, TRAP_METER_ACTIVE,
        f"Bear trap: meter={trap:.0f}, deflation={inflation:.1f}",
        snapshot,
    )


def _detect_market_correction(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Market reversed direction after a prior jump."""
    prev = context.get("prev_snapshot")
    if not prev:
        return None

    line_delta = snapshot.get("line_delta", 0) or 0
    prev_line_delta = prev.get("line_delta", 0) or 0

    # Correction = previous jump was significant AND current delta is opposite
    if abs(prev_line_delta) < LINE_JUMP_THRESHOLD:
        return None
    if line_delta * prev_line_delta >= 0:  # same direction
        return None

    reversal_ratio = abs(line_delta / prev_line_delta)
    if reversal_ratio < MARKET_CORRECTION_REVERSAL_FACTOR:
        return None

    severity = _severity_from_ratio(abs(line_delta), LINE_JUMP_THRESHOLD)
    return _make_signal(
        "market_correction", severity, abs(line_delta), LINE_JUMP_THRESHOLD,
        f"Market corrected: reversed {prev_line_delta:+.1f} → {line_delta:+.1f}",
        snapshot,
    )


def _detect_overreaction(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Line moved more than expected given rolling average movement."""
    line_delta = abs(snapshot.get("line_delta", 0) or 0)
    rolling_values = context.get("rolling_line_values", [])
    if not rolling_values or len(rolling_values) < 3:
        return None

    mean_delta = sum(abs(rolling_values[i] - rolling_values[i-1])
                     for i in range(1, len(rolling_values))) / (len(rolling_values) - 1)

    if mean_delta <= 0 or line_delta < mean_delta * OVERREACTION_FACTOR:
        return None

    severity = _severity_from_ratio(line_delta, mean_delta)
    return _make_signal(
        "overreaction", severity, line_delta, mean_delta,
        f"Overreaction: line move {line_delta:.1f} vs avg {mean_delta:.2f}",
        snapshot,
    )


def _detect_regression(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """The line has returned close to fair value after being far from it."""
    total_line = snapshot.get("total_line")
    fair_total = snapshot.get("fair_total")
    if total_line is None or fair_total is None:
        return None

    distance = abs(total_line - fair_total)
    prev = context.get("prev_snapshot")
    if not prev:
        # First observation — check if already close
        if distance <= 1.0:
            return _make_signal(
                "regression", "mid", distance, 1.0,
                f"Total ({total_line}) close to fair ({fair_total})",
                snapshot,
            )
        return None

    prev_distance = abs(
        (prev.get("total_line") or total_line) - (prev.get("fair_total") or fair_total)
    )

    # Regression = we were far and now we're close
    if prev_distance > 5.0 and distance <= 1.5:
        severity = _severity_from_ratio(prev_distance, 5.0)
        return _make_signal(
            "regression", severity, distance, 1.5,
            f"Regressed from {prev_distance:.1f} to {distance:.1f} from fair value",
            snapshot,
        )

    return None


def _detect_momentum_swing(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Momentum changed significantly."""
    prev_momentum = context.get("prev_momentum")
    curr_momentum = snapshot.get("momentum")
    if prev_momentum is None or curr_momentum is None:
        return None

    swing = abs(curr_momentum - prev_momentum)
    if swing < MOMENTUM_SWING_THRESHOLD:
        return None

    severity = _severity_from_ratio(swing, MOMENTUM_SWING_THRESHOLD)
    direction = "up" if curr_momentum > prev_momentum else "down"
    return _make_signal(
        "momentum_swing", severity, swing, MOMENTUM_SWING_THRESHOLD,
        f"Momentum swung {direction}: {prev_momentum:.1f} → {curr_momentum:.1f}",
        snapshot,
    )


def _detect_pace_collapse(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Game pace dropped below collapse threshold after early game."""
    ppm = snapshot.get("possessions_per_min")
    game_min = context.get("current_game_minutes", 0)
    if ppm is None or game_min < 12:
        return None
    if ppm >= PACE_COLLAPSE_THRESHOLD:
        return None

    deficit = PACE_COLLAPSE_THRESHOLD - ppm
    severity = _severity_from_ratio(deficit, PACE_COLLAPSE_THRESHOLD)
    return _make_signal(
        "pace_collapse", severity, ppm, PACE_COLLAPSE_THRESHOLD,
        f"Pace collapsed to {ppm:.2f} ppm at {game_min:.0f} minutes",
        snapshot,
    )


def _detect_inflation_spike(
    snapshot: dict[str, Any],
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Inflation index exceeded high threshold."""
    inflation = context.get("inflation_index")
    if inflation is None or abs(inflation) < INFLATION_MID:
        return None

    severity = "critical" if abs(inflation) >= INFLATION_HIGH else "high"
    direction = "up" if inflation > 0 else "down"
    return _make_signal(
        "inflation_spike", severity, inflation, INFLATION_MID,
        f"Inflation spike {direction}: index={inflation:.2f}",
        snapshot,
    )
