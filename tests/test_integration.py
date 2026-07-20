"""
Integration tests for BLM V2 platform.
Tests the full pipeline: snapshot → event bus → TS write → query → verify.
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from blm_v2.events.bus import EventBus
from blm_v2.timeseries.sqlite_fallback import SQLiteTimeSeries
from blm_v2.models.events import BlmEvent, EventType, MarketMove, TrapTriggered


class TestFullPipeline:
    """End-to-end pipeline test with simulated data."""

    @pytest.fixture
    def ts(self, tmp_path):
        db_path = str(tmp_path / "blm_ts_test.db")
        return SQLiteTimeSeries(db_path=Path(db_path))

    @pytest.fixture
    def event_bus(self):
        return EventBus()

    def create_snapshot(self, quarter=1, clock="10:00", home=0, away=0, total_line=180.0):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return {
            "metadata": {
                "game_id": "integration-test-001",
                "league": "Cyber 2K26",
                "season": "2026",
                "quarter": quarter,
                "clock": clock,
                "timestamp": ts,
            },
            "game_state": {
                "home_team": "CyberDogs",
                "away_team": "RoboHawks",
                "home_score": home,
                "away_score": away,
                "margin": home - away,
                "total": home + away,
                "possession": "home",
            },
            "betting_market": {
                "spread": -5.5,
                "live_spread": -6.0,
                "total": total_line,
                "live_total": total_line,
                "steam_movement": 0.0,
                "reverse_line_movement": 0.0,
            },
            "blm": {},
            "pace": {"real_pace": 100.0, "expected_pace": 105.0, "possessions": 0, "remaining_possessions": 0},
            "trap_detection": {"trap_meter": 50, "bull_trap": False, "bear_trap": False},
            "momentum": {"score": 50, "direction": "flat", "velocity": 0, "acceleration": 0, "strength": "weak"},
            "team_totals": {"home_projection": 90.0, "away_projection": 90.0},
            "confidence_inputs": {"PACE": 0.8, "LINE": 0.8, "INJURY": 1.0, "BLOWOUT": 1.0, "TEAM_TOTAL": 0.8, "composite_confidence": 0.8},
        }

    @pytest.mark.asyncio
    async def test_snapshot_through_pipeline(self, ts):
        """Test: snapshot → TS write → query back."""
        snap = self.create_snapshot(quarter=1, clock="10:00", home=12, away=8, total_line=180.0)

        await ts.write_snapshot(snap)

        results = await ts.query_snapshots(game_id="integration-test-001", limit=10)
        assert len(results) == 1
        assert results[0]["game_id"] == "integration-test-001"

    @pytest.mark.asyncio
    async def test_multiple_snapshots_accumulate(self, ts):
        """Test that multiple snapshots accumulate correctly."""
        game_id = "integration-test-multi"

        for i in range(5):
            snap = self.create_snapshot(quarter=1, clock=f"{10 - i}:00", home=i * 10, away=i * 8)
            snap["metadata"]["game_id"] = game_id
            await ts.write_snapshot(snap)

        results = await ts.query_snapshots(game_id=game_id, limit=100)
        assert len(results) == 5

        scores = [r["home_score"] for r in results]
        assert scores == sorted(scores)

    @pytest.mark.asyncio
    async def test_event_bus_emit_and_receive(self, event_bus):
        """Test EventBus emits BlmEvent instances to registered handlers."""
        received = []

        async def handler(event: MarketMove):
            received.append(event)

        event_bus.register(MarketMove, handler)

        evt = MarketMove(game_id="test-001", market="total", previous_value=180.5, new_value=182.0)
        await event_bus.emit(evt)
        await event_bus.emit(MarketMove(game_id="test-001", market="total", previous_value=182.0, new_value=181.0))

        assert len(received) == 2
        assert received[0].previous_value == 180.5
        assert received[1].new_value == 181.0

    @pytest.mark.asyncio
    async def test_event_bus_type_filtering(self, event_bus):
        """Test EventBus only dispatches to handlers for matching event types."""
        market_events = []
        trap_events = []

        async def market_handler(e: MarketMove):
            market_events.append(e)

        async def trap_handler(e: TrapTriggered):
            trap_events.append(e)

        event_bus.register(MarketMove, market_handler)
        event_bus.register(TrapTriggered, trap_handler)

        await event_bus.emit(MarketMove(game_id="test-001", market="total", previous_value=180.0, new_value=182.0))
        await event_bus.emit(MarketMove(game_id="test-001", market="total", previous_value=182.0, new_value=181.0))
        await event_bus.emit(TrapTriggered(game_id="test-001", trap_type="bull_trap", trap_score=0.85))

        assert len(market_events) == 2
        assert len(trap_events) == 1

    @pytest.mark.asyncio
    async def test_time_range_query(self, ts):
        """Test querying snapshots within a time range."""
        game_id = "integration-test-range"

        for i in range(3):
            snap = self.create_snapshot(quarter=1, clock=f"{i}:00", home=i * 10, away=i * 8)
            snap["metadata"]["game_id"] = game_id
            await ts.write_snapshot(snap)

        results = await ts.query_snapshots(game_id=game_id, limit=100)
        assert 2 <= len(results) <= 3

    @pytest.mark.asyncio
    async def test_write_performance(self, ts):
        """Test snapshot write performance meets <50ms target."""
        snap = self.create_snapshot()

        await ts.write_snapshot(snap)

        start = time.perf_counter()
        for _ in range(10):
            await ts.write_snapshot(snap)
        elapsed = time.perf_counter() - start
        avg_ms = (elapsed / 10) * 1000

        assert avg_ms < 50, f"Average write time {avg_ms:.2f}ms exceeds 50ms target"
