"""
Adapter that wraps BLMEngine to match the scheduler's BlmEngine protocol.
"""

from __future__ import annotations

from typing import Any, Dict

from blm_v2.collector.snapshot import RawSnapshot
from blm_v2.engine.blm_engine import BLMEngine as CoreEngine
from datetime import datetime, timezone


def _clock_to_seconds(clock: str | None) -> int:
    """Convert MM:SS clock string to total seconds."""
    if not clock or ":" not in clock:
        return 0
    parts = clock.split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 0


class BlmEngineAdapter:
    """Adapts BLMEngine.process_snapshot() to the scheduler's BlmEngine protocol.

    The scheduler calls ``enrich(raw_snapshot) -> dict``.
    This adapter translates ``RawSnapshot`` into the flat dict that
    ``BLMEngine.process_snapshot()`` expects, then extracts ``enriched_fields``.
    """

    def __init__(self, engine: CoreEngine):
        self._engine = engine
        self._tick_counter = 0

    async def enrich(self, snapshot: RawSnapshot) -> Dict[str, Any]:
        """Run the BLM pipeline on a raw snapshot. Returns enriched dict."""
        self._tick_counter += 1
        snapshot_id = f"{snapshot.game_id}-{self._tick_counter:05d}"

        clock_sec = _clock_to_seconds(snapshot.clock)
        total = (snapshot.home_score or 0) + (snapshot.away_score or 0)

        inp: Dict[str, Any] = {
            # Market
            "total_line": snapshot.total_line or 180.0,
            "previous_total_line": snapshot.total_line or 180.0,
            "score_change_rate": 0.0,
            "foul_count_this_interval": 0,
            "public_betting_bias": 0.0,
            "sharp_money_indicator": 0.0,
            "action_volume": 0.0,
            "time_to_lock": float("inf"),
            "expected_pace": 108.0,
            "actual_pace": 100.0,
            "historical_spread": 0.0,
            "line_movement_history": (),
            # Scoreboard
            "home_score": snapshot.home_score or 0,
            "away_score": snapshot.away_score or 0,
            "quarter": snapshot.quarter,
            "clock_seconds": clock_sec,
            "total": total,
            # Confidence (conservative defaults)
            "confidence_pace": 0.7,
            "confidence_line": 0.7,
            "confidence_injury": 1.0,
            "confidence_blowout": 0.9,
            "confidence_team_total": 0.7,
        }

        result = self._engine.process_snapshot(snapshot_id, inp)

        # Build enriched output dict merging raw + BLM fields
        enriched = {
            # Raw fields
            "game_id": snapshot.game_id,
            "timestamp": snapshot.timestamp,
            "home_team": snapshot.home_team,
            "away_team": snapshot.away_team,
            "home_score": snapshot.home_score,
            "away_score": snapshot.away_score,
            "quarter": snapshot.quarter,
            "clock": snapshot.clock,
            "total": total,
            # BLM fields
            "blm": {
                "expected_winner": result.enriched_fields.get("expected_winner", ""),
                "win_probability": result.enriched_fields.get("win_probability", 0.5),
                "confidence": result.confidence.composite_confidence,
                "expected_margin": result.enriched_fields.get("expected_margin", 0),
                "expected_total": result.enriched_fields.get("expected_total", total),
            },
            "pace": {
                "real_pace": inp["actual_pace"],
                "expected_pace": inp["expected_pace"],
                "possessions": 0,
                "remaining_possessions": 0,
            },
            "betting_market": {
                "spread": snapshot.spread or 0,
                "live_spread": snapshot.spread or 0,
                "total": snapshot.total_line or 0,
                "live_total": snapshot.total_line or 0,
                "steam_movement": result.market.steam_movement,
                "reverse_line_movement": result.market.reverse_line_movement,
            },
            "trap_detection": {
                "trap_meter": result.trap_meter.trap_meter,
            } | {
                k: (s.detected if s is not None else False)
                for k, s in (result.trap_meter.signals or {}).items()
            } | {
                # Also add signal confidence values
                f"{k}_confidence": (s.confidence if s is not None else 0.0)
                for k, s in (result.trap_meter.signals or {}).items()
            },
            "momentum": {
                "score": result.momentum.momentum_score,
                "direction": result.momentum.momentum_direction,
                "velocity": result.momentum.momentum_velocity,
                "acceleration": result.momentum.momentum_acceleration,
                "strength": result.momentum.momentum_strength_label,
            },
            "team_totals": {
                "home_projection": total / 2,
                "away_projection": total / 2,
            },
            "confidence_inputs": {
                "PACE": inp["confidence_pace"],
                "LINE": inp["confidence_line"],
                "INJURY": inp["confidence_injury"],
                "BLOWOUT": inp["confidence_blowout"],
                "TEAM_TOTAL": inp["confidence_team_total"],
                "composite_confidence": result.confidence.composite_confidence,
            },
        }

        enriched.update(result.enriched_fields)
        return enriched
